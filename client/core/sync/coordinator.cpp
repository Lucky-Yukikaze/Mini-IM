#include "core/sync/coordinator.h"
#include "core/model/eventmapper.h"

#include <stdexcept>
#include <utility>

namespace
{
constexpr int kRequestRetryMs = 5000;
constexpr quint32 kPageSize = 200;

MiniImStateEvent MapFile(quint64 position, const std::string& eventId, const im::file::FileUpdated& file)
{
    return {position, QString::fromStdString(eventId), QStringLiteral("file"),
        miniim::BuildFileProgressPayload(eventId, file.file_id(), file.conversation_id(),
            file.transferred_bytes(), file.completed(), file.version(), file.updated_at_ms())};
}

MiniImStateEvent MapEvent(const im::sync::SyncEvent& event)
{
    MiniImStateEvent item{event.global_seq(), QString::fromStdString(event.event_id()), {}, {}};
    if (event.has_message())
    {
        item.type = QStringLiteral("message");
        item.data = miniim::BuildMessagePayload(event.message());
    }
    else if (event.has_conversation_updated())
    {
        item.type = QStringLiteral("conversation");
        item.data = miniim::BuildConversationPayload(event.conversation_updated());
    }
    else if (event.has_receipt())
    {
        item.type = QStringLiteral("receipt");
        item.data = miniim::BuildReceiptPayload(event.receipt());
    }
    else if (event.has_recall())
    {
        item.data = miniim::BuildRecallPayload(event.recall());
        item.type = item.data.value(QStringLiteral("type")).toString();
    }
    else if (event.has_file_updated())
    {
        return MapFile(event.global_seq(), event.event_id(), event.file_updated());
    }
    return item;
}
}

MiniImSyncCoordinator::MiniImSyncCoordinator(
    MiniImStateStore& store, RequestFactory factory, Sender sender, QObject* parent)
    : QObject(parent), m_store(store), m_factory(std::move(factory)), m_sender(std::move(sender))
{
    m_retryTimer.setInterval(500);
    QObject::connect(&m_retryTimer, &QTimer::timeout, this, &MiniImSyncCoordinator::retryRequest);
}

void MiniImSyncCoordinator::start()
{
    stop();
    m_running = true;
    m_retryTimer.start();
    requestNext();
}

void MiniImSyncCoordinator::stop()
{
    m_retryTimer.stop();
    m_running = false;
    m_caughtUp = false;
    emit readinessChanged(false);
    clearRequest();
}

bool MiniImSyncCoordinator::isReady() const
{
    return m_running && m_caughtUp && !m_store.hasGap();
}

void MiniImSyncCoordinator::clearRequest()
{
    m_requestId.clear();
    m_requestPayload.clear();
    m_attemptTime.invalidate();
    m_reportedGap = false;
}

void MiniImSyncCoordinator::requestNext()
{
    if (!m_running || !m_requestId.isEmpty())
    {
        return;
    }
    m_caughtUp = false;
    emit readinessChanged(false);
    m_requestCursor = m_store.cursor();
    const auto request = m_factory(m_requestCursor, kPageSize);
    m_requestId = QString::fromStdString(request.request_id());
    m_requestPayload = request.SerializeAsString();
    m_attemptTime.start();
    if (!m_sender(m_requestPayload))
    {
        emit errorRaised(QStringLiteral("failed to send sync request; waiting to retry"));
    }
}

void MiniImSyncCoordinator::retryRequest()
{
    if (m_running && !m_requestId.isEmpty() && m_attemptTime.isValid()
        && m_attemptTime.elapsed() >= kRequestRetryMs)
    {
        m_attemptTime.restart();
        if (!m_sender(m_requestPayload))
        {
            emit errorRaised(QStringLiteral("failed to send sync request; waiting to retry"));
        }
    }
}

void MiniImSyncCoordinator::fail(const QString& error)
{
    stop();
    emit errorRaised(error);
    emit failed();
}

bool MiniImSyncCoordinator::apply(const QVector<MiniImStateEvent>& events)
{
    QVector<MiniImStateEvent> applied;
    if (!m_store.apply(events, &applied))
    {
        fail(m_store.errorString());
        return false;
    }
    for (const auto& event : applied)
    {
        emit eventApplied(event);
    }
    emit stateApplied();
    emit progressChanged({{"globalCursor", QVariant::fromValue(m_store.cursor())}, {"hasGap", m_store.hasGap()}});
    return true;
}

void MiniImSyncCoordinator::handleResponse(const im::envelope::Envelope& envelope)
{
    const bool requested = !m_requestId.isEmpty() && QString::fromStdString(envelope.request_id()) == m_requestId;
    const auto& response = envelope.sync_response();
    QVector<MiniImStateEvent> events;
    for (const auto& event : response.events())
    {
        events.append(MapEvent(event));
    }
    if (!apply(events))
    {
        return;
    }
    for (const auto& event : response.events())
    {
        if (event.has_file_updated())
        {
            emit fileUpdated(event.file_updated());
        }
    }
    if (!requested)
    {
        if (m_store.hasGap())
        {
            requestNext();
        }
        return;
    }
    const bool more = response.has_more() || m_store.hasGap() || response.new_global_cursor() > m_store.cursor();
    if (more && m_store.cursor() <= m_requestCursor)
    {
        // An empty or non-advancing page must neither release pending writes nor create a busy request loop.
        // Retain the original request and its retry clock so temporary server gaps can heal.
        if (!m_reportedGap)
        {
            m_reportedGap = true;
            emit errorRaised(QStringLiteral("server sync stream has an unresolved gap; waiting to retry"));
        }
        return;
    }
    clearRequest();
    if (more)
    {
        requestNext();
    }
    else
    {
        m_caughtUp = true;
        emit readinessChanged(true);
        emit ready();
    }
}

void MiniImSyncCoordinator::handleFile(const im::envelope::Envelope& envelope)
{
    const auto& updated = envelope.file_updated();
    if (!updated.event_id().empty() && !apply({MapFile(envelope.seq(), updated.event_id(), updated)}))
    {
        return;
    }
    // Control metadata must reach pending downloads even if its sync event was already applied.
    emit fileUpdated(updated);
    if (m_store.hasGap())
    {
        requestNext();
    }
}

void MiniImSyncCoordinator::handleMessage(const im::envelope::Envelope& envelope)
{
    const auto& pushed = envelope.message_push();
    if (!pushed.event_id().empty())
    {
        if (pushed.messages_size() != 1)
        {
            fail(QStringLiteral("message event must contain exactly one message"));
            return;
        }
        if (!apply({{envelope.seq(), QString::fromStdString(pushed.event_id()),
                QStringLiteral("message"), miniim::BuildMessagePayload(pushed.messages(0))}}))
        {
            return;
        }
    }
    if (m_store.hasGap())
    {
        requestNext();
    }
}

bool MiniImSyncCoordinator::handleEnvelope(const im::envelope::Envelope& envelope)
{
    if (!envelope.has_sync_response() && !envelope.has_file_updated() && !envelope.has_message_push())
    {
        return false;
    }
    if (!m_running)
    {
        return true;
    }
    try
    {
        if (envelope.has_sync_response())
        {
            handleResponse(envelope);
        }
        else if (envelope.has_file_updated())
        {
            handleFile(envelope);
        }
        else
        {
            handleMessage(envelope);
        }
    }
    catch (const std::exception& error)
    {
        fail(QString::fromUtf8(error.what()));
    }
    return true;
}
