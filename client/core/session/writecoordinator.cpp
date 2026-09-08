#include "core/session/writecoordinator.h"

#include <stdexcept>
#include <utility>

namespace
{
QString Operation(const im::envelope::Envelope& body)
{
    using Envelope = im::envelope::Envelope;
    switch (body.body_case())
    {
    case Envelope::kCreateConversation: return QStringLiteral("create_conversation");
    case Envelope::kAddMembers: return QStringLiteral("add_members");
    case Envelope::kRemoveMembers: return QStringLiteral("remove_members");
    case Envelope::kLeaveConversation: return QStringLiteral("leave_conversation");
    case Envelope::kJoinConversation: return QStringLiteral("join_conversation");
    case Envelope::kRenameConversation: return QStringLiteral("rename_conversation");
    case Envelope::kReceipt: return QStringLiteral("receipt");
    case Envelope::kRecall: return QStringLiteral("recall");
    default: throw std::runtime_error("unsupported queued control write");
    }
}

QString Conversation(const im::envelope::Envelope& body)
{
    using Envelope = im::envelope::Envelope;
    switch (body.body_case())
    {
    case Envelope::kAddMembers: return QString::fromStdString(body.add_members().conversation_id());
    case Envelope::kRemoveMembers: return QString::fromStdString(body.remove_members().conversation_id());
    case Envelope::kLeaveConversation: return QString::fromStdString(body.leave_conversation().conversation_id());
    case Envelope::kJoinConversation: return QString::fromStdString(body.join_conversation().conversation_id());
    case Envelope::kRenameConversation: return QString::fromStdString(body.rename_conversation().conversation_id());
    case Envelope::kReceipt: return QString::fromStdString(body.receipt().conversation_id());
    case Envelope::kRecall: return QString::fromStdString(body.recall().conversation_id());
    default: return QStringLiteral("");
    }
}
}

MiniImControlWriteCoordinator::MiniImControlWriteCoordinator(MiniImControlWriteStore& store,
    Sender sender, RequestIdFactory requestIdFactory, QObject* parent)
    : QObject(parent), m_store(store), m_sender(std::move(sender)), m_requestIdFactory(std::move(requestIdFactory))
{
    m_timer.setInterval(500);
    QObject::connect(&m_timer, &QTimer::timeout, this, &MiniImControlWriteCoordinator::pump);
}

void MiniImControlWriteCoordinator::start()
{
    stop();
    m_running = true;
    m_timer.start();
}

void MiniImControlWriteCoordinator::stop()
{
    m_running = false;
    m_syncReady = false;
    m_timer.stop();
    m_activeRequest.clear();
    m_attemptTime.invalidate();
}

void MiniImControlWriteCoordinator::setSyncReady(bool ready)
{
    m_syncReady = ready;
}

void MiniImControlWriteCoordinator::publish()
{
    emit writesChanged({{"items", m_store.pending()}});
}

void MiniImControlWriteCoordinator::fail(const std::exception& error)
{
    stop();
    emit errorRaised(QString::fromUtf8(error.what()));
    emit failed();
}

bool MiniImControlWriteCoordinator::enqueue(const im::envelope::Envelope& body)
{
    if (!m_running)
    {
        return false;
    }
    QVariantMap item;
    try
    {
        const auto operation = Operation(body);
        const auto clientConvId = body.has_create_conversation()
            ? QString::fromStdString(body.create_conversation().client_conv_id()) : QStringLiteral("");
        const auto payload = QByteArray::fromStdString(body.SerializeAsString());
        item = m_store.enqueue(m_requestIdFactory(), operation, Conversation(body), clientConvId, payload);
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        return false;
    }
    // Once saved, notification or sending failures cannot turn acceptance into a rejected user intent.
    try
    {
        publish();
        pump();
    }
    catch (const std::exception& error)
    {
        fail(error);
    }
    if (item.value("status").toString() == QStringLiteral("failed"))
    {
        emit errorRaised(item.value("error").toString());
        return false;
    }
    return true;
}

void MiniImControlWriteCoordinator::pump()
{
    if (!m_running || !m_syncReady)
    {
        return;
    }
    try
    {
        const auto item = m_store.nextPending();
        if (item.isEmpty())
        {
            m_activeRequest.clear();
            return;
        }
        const auto requestId = item.value("requestId").toString();
        if (requestId == m_activeRequest && m_attemptTime.isValid() && m_attemptTime.elapsed() < 5000)
        {
            return;
        }
        im::envelope::Envelope envelope;
        const auto payload = item.value("payload").toByteArray();
        if (!envelope.ParseFromArray(payload.constData(), payload.size()) || Operation(envelope) != item.value("operation"))
        {
            throw std::runtime_error("invalid persisted control write");
        }
        m_store.markAttempt(requestId);
        m_activeRequest = requestId;
        m_attemptTime.start();
        envelope.set_request_id(requestId.toStdString());
        envelope.set_client_time_ms(item.value("createdAtMs").toLongLong());
        publish();
        if (!m_sender(std::move(envelope)))
        {
            emit errorRaised(QStringLiteral("control write saved locally; waiting to retry"));
        }
    }
    catch (const std::exception& error)
    {
        fail(error);
    }
}

void MiniImControlWriteCoordinator::handleResult(
    const QString& requestId, bool success, int code, const QString& error, const QString& entityId)
{
    if (!m_running)
    {
        return;
    }
    try
    {
        if (m_store.acknowledge(requestId, success, code, error, entityId))
        {
            publish();
            pump();
        }
    }
    catch (const std::exception& exception)
    {
        fail(exception);
    }
}
