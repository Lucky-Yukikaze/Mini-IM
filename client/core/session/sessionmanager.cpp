#include "core/session/sessionmanager.h"
#include "core/logging.h"

#include <QByteArray>
#include <QDateTime>
#include <QDir>
#include <QUuid>
#include <QUrl>
#include <QStringList>
#include <QStandardPaths>
#include <QRegularExpression>

#include <QtEndian>

#include <cstring>
#include <string>
#include <stdexcept>

#ifdef SendMessage
#undef SendMessage
#endif

#include "auth.pb.h"
#include "common.pb.h"
#include "conversation.pb.h"
#include "envelope.pb.h"
#include "file.pb.h"
#include "message.pb.h"
#include "sync.pb.h"

namespace
{
constexpr int kDefaultHeartbeatIntervalSec = 15;
constexpr quint32 kEnvelopeFrameHeaderSize = 4;
constexpr int kRequestRetryMs = 5000;

QByteArray BuildEnvelopeFrame(const std::string& payload)
{
    QByteArray frame;
    const quint32 payload_size = static_cast<quint32>(payload.size());
    const quint32 payload_size_be = qToBigEndian(payload_size);
    frame.resize(static_cast<int>(kEnvelopeFrameHeaderSize + payload_size));
    std::memcpy(frame.data(), &payload_size_be, sizeof(payload_size_be));
    if (payload_size > 0)
    {
        std::memcpy(frame.data() + kEnvelopeFrameHeaderSize, payload.data(), payload_size);
    }
    return frame;
}
}

MiniImSessionManager::MiniImSessionManager(QObject* parent)
    : QObject(parent),
      m_sync(m_stateStore,
          [this](quint64 cursor, quint32 limit) { return makeSyncRequest(cursor, limit); },
          [this](const std::string& payload) { return sendEnvelope(payload); }),
      m_files(m_stateStore.fileTasks(), m_transport,
          [this](const QString& id) { return makeRequestEnvelope(id, im::common::CHANNEL_FILE); },
          [this](const std::string& payload) { return sendEnvelope(payload); },
          [this](const QString& suffix) { return makeRequestId(suffix); }),
      m_connected(false),
      m_connecting(false),
      m_hello_sent(false),
      m_seq(0),
      m_heartbeat_interval_sec(kDefaultHeartbeatIntervalSec)
{
    connectFileSignals();
    QObject::connect(&m_sync, &MiniImSyncCoordinator::eventApplied, this,
        &MiniImSessionManager::onSyncEventApplied);
    QObject::connect(&m_sync, &MiniImSyncCoordinator::stateApplied, this,
        &MiniImSessionManager::publishMessageSends);
    QObject::connect(&m_sync, &MiniImSyncCoordinator::progressChanged, this, &MiniImSessionManager::syncProgress);
    QObject::connect(&m_sync, &MiniImSyncCoordinator::fileUpdated, &m_files, &MiniImFileCoordinator::handleFileUpdated);
    QObject::connect(&m_sync, &MiniImSyncCoordinator::errorRaised, this, &MiniImSessionManager::errorRaised);
    QObject::connect(&m_sync, &MiniImSyncCoordinator::failed, this, &MiniImSessionManager::disconnectFromServer);
    QObject::connect(&m_sync, &MiniImSyncCoordinator::ready, this, &MiniImSessionManager::pumpMessageOutbox);
    QObject::connect(&m_sync, &MiniImSyncCoordinator::readinessChanged, &m_files, &MiniImFileCoordinator::setSyncReady);
    QObject::connect(&m_sync, &MiniImSyncCoordinator::ready, &m_files, &MiniImFileCoordinator::pumpFileTasks);
    QObject::connect(&m_transport, &MiniImQuicConnection::connected, this,
        &MiniImSessionManager::onTransportConnected);
    QObject::connect(&m_transport, &MiniImQuicConnection::disconnected, this,
        &MiniImSessionManager::onTransportDisconnected);
    QObject::connect(&m_transport, &MiniImQuicConnection::errorRaised, this,
        &MiniImSessionManager::errorRaised);
    QObject::connect(&m_transport, &MiniImQuicConnection::controlData, this,
        &MiniImSessionManager::handleIncomingControlStreamData);
    QObject::connect(&m_recovery, &MiniImConnectionRecovery::attemptRequested, this,
        [this]() { startConnectionAttempt(); });
    QObject::connect(&m_recovery, &MiniImConnectionRecovery::loginTimedOut, this,
        [this]() { restartConnection(QStringLiteral("login timed out")); });
    QObject::connect(&m_recovery, &MiniImConnectionRecovery::retryScheduled, this,
        [this](int delayMs)
        {
            AppendClientLog(QStringLiteral("reconnect scheduled delay_ms=%1").arg(delayMs));
            emit connectionChanged(QStringLiteral("reconnecting"), m_session_id);
        });
    m_heartbeat_timer.setTimerType(Qt::PreciseTimer);
    m_messageRetryTimer.setInterval(500);
    QObject::connect(&m_messageRetryTimer, &QTimer::timeout, this, &MiniImSessionManager::pumpMessageOutbox);
    m_heartbeat_timer.setSingleShot(false);
    QObject::connect(&m_heartbeat_timer, &QTimer::timeout, this, &MiniImSessionManager::onHeartbeatTimeout);
}

void MiniImSessionManager::connectFileSignals()
{
    QObject::connect(&m_files, &MiniImFileCoordinator::fileProgress, this, &MiniImSessionManager::fileProgress);
    QObject::connect(&m_files, &MiniImFileCoordinator::fileTasksChanged, this, &MiniImSessionManager::fileTasksChanged);
    QObject::connect(&m_files, &MiniImFileCoordinator::errorRaised, this, &MiniImSessionManager::errorRaised);
    QObject::connect(&m_files, &MiniImFileCoordinator::restartRequested, this,
        [this](const QString& reason) { restartConnection(reason); });
    QObject::connect(&m_files, &MiniImFileCoordinator::failed, this, &MiniImSessionManager::disconnectFromServer);
}

MiniImSessionManager::~MiniImSessionManager()
{
    disconnectFromServer();
    QObject::disconnect(&m_transport, nullptr, this, nullptr);
}

bool MiniImSessionManager::connectToServer(const QString& endpoint, const QString& token, const QString& device_id)
{
    return connectToServerWithResume(
        endpoint,
        token,
        device_id,
        m_resume_session_id,
        m_stateStore.cursor(),
        m_last_acked_request_id);
}

bool MiniImSessionManager::connectToServerWithResume(
    const QString& endpoint,
    const QString& token,
    const QString& device_id,
    const QString& resume_session_id,
    quint64 global_cursor,
    const QString& last_acked_request_id)
{
    AppendClientLog(
        QStringLiteral("connectToServerWithResume endpoint=%1 device=%2 resume=%3 cursor=%4")
            .arg(endpoint)
            .arg(device_id)
            .arg(resume_session_id)
            .arg(global_cursor));
    if (endpoint.trimmed().isEmpty() || token.trimmed().isEmpty() || device_id.trimmed().isEmpty())
    {
        AppendClientLog(QStringLiteral("connect rejected: invalid arguments"));
        emit errorRaised(QStringLiteral("invalid connect arguments"));
        return false;
    }
    if (m_connecting || m_connected || m_transport.isActive())
    {
        AppendClientLog(QStringLiteral("connect rejected: session already running"));
        emit errorRaised(QStringLiteral("session is already running"));
        return false;
    }

    Q_UNUSED(resume_session_id);
    Q_UNUSED(last_acked_request_id);
    Q_UNUSED(global_cursor);
    QString stateHost;
    uint16_t statePort = 0;
    if (!parseEndpoint(endpoint, &stateHost, &statePort))
    {
        emit errorRaised(QStringLiteral("invalid endpoint"));
        return false;
    }
    m_recovery.stop();
    m_endpoint = endpoint;
    m_token = token;
    m_device_id = device_id;
    const QString prefix = QStringLiteral("dev-token:");
    const QString user = token.startsWith(prefix) ? token.mid(prefix.size()).trimmed() : QString();
    static const QRegularExpression userPattern(QStringLiteral("^[A-Za-z0-9_.-]{1,64}$"));
    m_userId = userPattern.match(user).hasMatch() ? user : QStringLiteral("u-demo");
    QString stateRoot = qEnvironmentVariable("MINIIM_STATE_ROOT");
    if (stateRoot.isEmpty())
    {
        stateRoot = QDir(QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation))
            .filePath(QStringLiteral("state"));
    }
    const QString identity = stateHost.toLower() + QStringLiteral(":") + QString::number(statePort);
    if (!m_stateStore.open(stateRoot, identity, m_userId, device_id))
    {
        emit errorRaised(m_stateStore.errorString());
        return false;
    }
    m_recovery.start();
    startConnectionAttempt();
    return m_recovery.enabled();
}

bool MiniImSessionManager::startConnectionAttempt()
{
    if (!m_recovery.enabled() || m_transport.isActive())
    {
        return false;
    }
    m_transportStopping = false;
    m_recovery.beginAttempt();
    m_resume_session_id = m_stateStore.sessionId();
    m_last_acked_request_id = m_stateStore.lastAck();
    m_seq = 0;
    m_heartbeat_interval_sec = kDefaultHeartbeatIntervalSec;
    m_hello_sent = false;
    m_connecting = true;
    m_connected = false;

    emit connectionChanged(QStringLiteral("connecting"), QString());

    QString host;
    uint16_t port = 0;
    if (!parseEndpoint(m_endpoint, &host, &port))
    {
        AppendClientLog(QStringLiteral("parseEndpoint failed: %1").arg(m_endpoint));
        emitConnectionError(QStringLiteral("invalid endpoint"));
        return false;
    }
    AppendClientLog(QStringLiteral("endpoint parsed host=%1 port=%2").arg(host).arg(port));

    if (!m_transport.open(host, port))
    {
        emitConnectionError(QStringLiteral("failed to open native connection"));
        return false;
    }
    return true;
}

void MiniImSessionManager::disconnectFromServer()
{
    m_recovery.stop();
    m_transportStopping = true;
    m_heartbeat_timer.stop();
    resetRuntimeState();
    if (m_transport.isActive())
    {
        m_transport.shutdown();
        return;
    }
    m_connected = false;
    m_connecting = false;
    emit connectionChanged(QStringLiteral("disconnected"), m_session_id);
}

void MiniImSessionManager::restartConnection(const QString& reason, bool clearSession)
{
    if (!m_recovery.enabled() || m_transportStopping)
    {
        return;
    }
    emit errorRaised(reason);
    m_transportStopping = true;
    m_connected = false;
    m_heartbeat_timer.stop();
    resetRuntimeState();
    if (clearSession && !m_stateStore.saveSession(QStringLiteral(""), m_last_acked_request_id))
    {
        emit errorRaised(m_stateStore.errorString());
        disconnectFromServer();
        return;
    }
    if (m_transport.isActive())
    {
        m_transport.shutdown();
    }
    else
    {
        m_connecting = false;
        m_recovery.connectionLost();
    }
}

void MiniImSessionManager::onHeartbeatTimeout()
{
    if (!m_connected)
    {
        return;
    }
    if (m_lastResponseTime.isValid() && m_lastResponseTime.elapsed() >= m_heartbeat_interval_sec * 2000LL)
    {
        restartConnection(QStringLiteral("server response timed out"));
        return;
    }
    if (!sendHeartbeat())
    {
        emit errorRaised(QStringLiteral("failed to send heartbeat"));
    }
}

void MiniImSessionManager::onTransportConnected()
{
    if (m_transportStopping)
    {
        return;
    }
    if (!sendHello())
    {
        restartConnection(QStringLiteral("failed to send hello"));
        return;
    }
    m_hello_sent = true;
}

void MiniImSessionManager::onTransportDisconnected()
{
    m_heartbeat_timer.stop();
    resetRuntimeState();
    m_connected = false;
    m_connecting = false;
    m_hello_sent = false;
    emit connectionChanged(QStringLiteral("disconnected"), m_session_id);
    m_recovery.connectionLost();
}

void MiniImSessionManager::resetRuntimeState()
{
    m_files.stop();
    m_sync.stop();
    m_messageRetryTimer.stop();
    m_activeMessageRequest.clear();
    m_messageAttemptTime.invalidate();
    m_control_stream_buffer.clear();
}

bool MiniImSessionManager::parseEndpoint(const QString& endpoint, QString* host, uint16_t* port) const
{
    const QUrl url(endpoint);
    if (url.isValid() && !url.scheme().isEmpty())
    {
        if (url.host().isEmpty())
        {
            return false;
        }
        *host = url.host();
        *port = static_cast<uint16_t>(url.port(4433));
        return true;
    }

    const int split = endpoint.lastIndexOf(':');
    if (split <= 0 || split >= endpoint.size() - 1)
    {
        return false;
    }
    bool ok = false;
    const auto parsed = endpoint.mid(split + 1).toUShort(&ok);
    if (!ok)
    {
        return false;
    }
    *host = endpoint.left(split);
    *port = parsed;
    return true;
}

bool MiniImSessionManager::sendEnvelope(const std::string& payload)
{
    return !payload.empty() && m_transport.sendControl(BuildEnvelopeFrame(payload));
}

void MiniImSessionManager::handleIncomingControlStreamData(const QByteArray& payload)
{
    m_control_stream_buffer.append(payload);
    AppendClientLog(
        QStringLiteral("handleIncomingControlStreamData append=%1 buffered=%2")
            .arg(payload.size())
            .arg(m_control_stream_buffer.size()));

    while (m_control_stream_buffer.size() >= static_cast<int>(kEnvelopeFrameHeaderSize))
    {
        quint32 payload_size_be = 0;
        std::memcpy(&payload_size_be, m_control_stream_buffer.constData(), sizeof(payload_size_be));
        const quint32 payload_size = qFromBigEndian(payload_size_be);
        const quint32 frame_size = kEnvelopeFrameHeaderSize + payload_size;
        if (payload_size == 0)
        {
            AppendClientLog(QStringLiteral("control frame rejected: empty payload"));
            m_control_stream_buffer.clear();
            emit errorRaised(QStringLiteral("invalid envelope frame"));
            return;
        }
        if (payload_size > 16 * 1024 * 1024)
        {
            AppendClientLog(QStringLiteral("control frame rejected: payload too large size=%1").arg(payload_size));
            m_control_stream_buffer.clear();
            emit errorRaised(QStringLiteral("invalid envelope frame"));
            return;
        }
        if (m_control_stream_buffer.size() < static_cast<int>(frame_size))
        {
            return;
        }

        const QByteArray envelope_payload =
            m_control_stream_buffer.mid(static_cast<int>(kEnvelopeFrameHeaderSize), static_cast<int>(payload_size));
        m_control_stream_buffer.remove(0, static_cast<int>(frame_size));
        try
        {
            handleIncomingEnvelope(envelope_payload);
        }
        catch (const std::exception& error)
        {
            emit errorRaised(QString::fromUtf8(error.what()));
            disconnectFromServer();
        }
        if (m_transportStopping)
        {
            m_control_stream_buffer.clear();
            return;
        }
    }
}

bool MiniImSessionManager::sendHello()
{
    AppendClientLog(QStringLiteral("sendHello begin"));
    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("hello"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_resume_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* hello = envelope.mutable_hello();
    hello->set_token(m_token.toStdString());
    hello->set_device_id(m_device_id.toStdString());
    hello->set_global_cursor(m_stateStore.cursor());
    hello->set_resume_session_id(m_resume_session_id.toStdString());
    hello->set_last_acked_request_id(m_last_acked_request_id.toStdString());

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::sendHeartbeat()
{
    if (m_session_id.isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("hb"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());
    envelope.mutable_heartbeat()->set_ts_ms(now_ms);

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::sendMessage(
    const QString& conversation_id,
    const QString& client_msg_id,
    const QString& text,
    quint32 burn_mode,
    quint32 burn_ttl_sec)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty() || client_msg_id.trimmed().isEmpty())
    {
        return false;
    }
    if (burn_mode > 1)
    {
        return false;
    }
    if (burn_mode == 0)
    {
        burn_ttl_sec = 0;
    }
    else if (burn_ttl_sec < 5 || burn_ttl_sec > 604800)
    {
        return false;
    }

    try
    {
        const QVariantMap intent{{"conversationId", conversation_id}, {"clientMsgId", client_msg_id},
            {"text", text}, {"burnMode", burn_mode}, {"burnTtlSec", burn_ttl_sec}};
        m_stateStore.outbox().enqueue(makeRequestId(QStringLiteral("msg")), intent);
        publishMessageSends();
        pumpMessageOutbox();
        return true;
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        return false;
    }
}

bool MiniImSessionManager::retryMessage(const QString& conversationId, const QString& clientMsgId)
{
    if (!m_connected)
    {
        return false;
    }
    try
    {
        const bool accepted = m_stateStore.outbox().retry(conversationId, clientMsgId);
        publishMessageSends();
        pumpMessageOutbox();
        return accepted;
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        return false;
    }
}

void MiniImSessionManager::publishMessageSends()
{
    emit messageSendsChanged({{"items", m_stateStore.outbox().pending()}});
}

bool MiniImSessionManager::sendQueuedMessage(const QVariantMap& item)
{
    im::envelope::Envelope envelope;
    const QString requestId = item.value(QStringLiteral("requestId")).toString();
    envelope.set_version(1);
    envelope.set_request_id(requestId.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(item.value(QStringLiteral("createdAtMs")).toLongLong());
    envelope.set_trace_id(requestId.toStdString());
    auto* message = envelope.mutable_send_message();
    message->set_conversation_id(item.value(QStringLiteral("conversationId")).toString().toStdString());
    message->set_client_msg_id(item.value(QStringLiteral("clientMsgId")).toString().toStdString());
    message->set_type(im::common::MSG_TEXT);
    message->set_content(item.value(QStringLiteral("text")).toString().toStdString());
    message->set_burn_mode(item.value(QStringLiteral("burnMode")).toUInt());
    message->set_burn_ttl_sec(item.value(QStringLiteral("burnTtlSec")).toUInt());
    return sendEnvelope(envelope.SerializeAsString());
}

void MiniImSessionManager::pumpMessageOutbox()
{
    if (!m_connected)
    {
        return;
    }
    try
    {
        if (!m_sync.isReady())
        {
            return;
        }
        const auto item = m_stateStore.outbox().nextPending();
        if (item.isEmpty())
        {
            m_activeMessageRequest.clear();
            return;
        }
        const QString requestId = item.value(QStringLiteral("requestId")).toString();
        if (requestId == m_activeMessageRequest && m_messageAttemptTime.isValid()
            && m_messageAttemptTime.elapsed() < kRequestRetryMs)
        {
            return;
        }
        m_stateStore.outbox().markAttempt(requestId);
        m_activeMessageRequest = requestId;
        m_messageAttemptTime.start();
        publishMessageSends();
        if (!sendQueuedMessage(item))
        {
            emit errorRaised(QStringLiteral("message saved locally; waiting to retry"));
        }
    }
    catch (const std::exception& error)
    {
        m_messageRetryTimer.stop();
        emit errorRaised(QString::fromUtf8(error.what()));
    }
}

void MiniImSessionManager::handleMessageResult(
    const QString& requestId, bool success, int code, const QString& error, const QString& entityId)
{
    try
    {
        if (m_stateStore.outbox().acknowledge(requestId, success, code, error, entityId))
        {
            publishMessageSends();
            pumpMessageOutbox();
        }
    }
    catch (const std::exception& exception)
    {
        m_messageRetryTimer.stop();
        emit errorRaised(QString::fromUtf8(exception.what()));
    }
}

bool MiniImSessionManager::createConversation(
    const QString& client_conv_id,
    const QString& title,
    const QVariantList& member_ids)
{
    if (!m_connected || m_session_id.isEmpty() || client_conv_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("conv"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* create_conversation = envelope.mutable_create_conversation();
    create_conversation->set_client_conv_id(client_conv_id.toStdString());
    create_conversation->set_type(im::common::CONVERSATION_GROUP);
    create_conversation->set_title(title.toStdString());
    for (const QVariant& item : member_ids)
    {
        const QString member_id = item.toString().trimmed();
        if (!member_id.isEmpty())
        {
            create_conversation->add_member_ids(member_id.toStdString());
        }
    }

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::createDirectConversation(const QString& client_conv_id, const QString& peer_user_id)
{
    if (!m_connected || m_session_id.isEmpty() || client_conv_id.trimmed().isEmpty() || peer_user_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("direct"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* create_conversation = envelope.mutable_create_conversation();
    create_conversation->set_client_conv_id(client_conv_id.toStdString());
    create_conversation->set_type(im::common::CONVERSATION_DIRECT);
    create_conversation->set_title(QString().toStdString());
    create_conversation->add_member_ids(peer_user_id.trimmed().toStdString());

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::addMembers(const QString& conversation_id, const QVariantList& member_ids)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("addmembers"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* add_members = envelope.mutable_add_members();
    add_members->set_conversation_id(conversation_id.toStdString());
    for (const QVariant& item : member_ids)
    {
        const QString member_id = item.toString().trimmed();
        if (!member_id.isEmpty())
        {
            add_members->add_member_ids(member_id.toStdString());
        }
    }

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::removeMembers(const QString& conversation_id, const QVariantList& member_ids)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("removemembers"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* remove_members = envelope.mutable_remove_members();
    remove_members->set_conversation_id(conversation_id.toStdString());
    for (const QVariant& item : member_ids)
    {
        const QString member_id = item.toString().trimmed();
        if (!member_id.isEmpty())
        {
            remove_members->add_member_ids(member_id.toStdString());
        }
    }

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::leaveConversation(const QString& conversation_id)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("leaveconv"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    envelope.mutable_leave_conversation()->set_conversation_id(conversation_id.toStdString());
    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::joinConversation(const QString& conversation_id)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("joinconv"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    envelope.mutable_join_conversation()->set_conversation_id(conversation_id.toStdString());
    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::renameConversation(const QString& conversation_id, const QString& title)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty() || title.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("renameconv"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* rename_conversation = envelope.mutable_rename_conversation();
    rename_conversation->set_conversation_id(conversation_id.toStdString());
    rename_conversation->set_title(title.toStdString());
    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::sendReceipt(const QString& conversation_id, quint64 last_read_seq)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("receipt"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* receipt = envelope.mutable_receipt();
    receipt->set_conversation_id(conversation_id.toStdString());
    receipt->set_last_read_seq(last_read_seq);

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::recallMessage(const QString& conversation_id, const QString& message_id)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty() || message_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("recall"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* recall = envelope.mutable_recall();
    recall->set_conversation_id(conversation_id.toStdString());
    recall->set_message_id(message_id.toStdString());

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::sendFile(const QString& conversation_id, const QString& file_path, quint32 priority)
{
    return m_files.sendFile(conversation_id, file_path, priority);
}

bool MiniImSessionManager::downloadFile(
    const QString& conversation_id,
    const QString& source_file_id,
    const QString& save_path,
    quint32 priority)
{
    return m_files.downloadFile(conversation_id, source_file_id, save_path, priority);
}

bool MiniImSessionManager::retryFile(const QString& clientFileId)
{
    return m_files.retryFile(clientFileId);
}

bool MiniImSessionManager::cancelFile(const QString& clientFileId)
{
    return m_files.cancelFile(clientFileId);
}

im::envelope::Envelope MiniImSessionManager::makeRequestEnvelope(
    const QString& requestId, im::common::Channel channel)
{
    im::envelope::Envelope envelope;
    envelope.set_version(1);
    envelope.set_request_id(requestId.toStdString());
    envelope.set_channel(channel);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(QDateTime::currentMSecsSinceEpoch());
    envelope.set_trace_id(requestId.toStdString());
    return envelope;
}

im::envelope::Envelope MiniImSessionManager::makeSyncRequest(quint64 cursor, quint32 limit)
{
    auto envelope = makeRequestEnvelope(makeRequestId(QStringLiteral("sync")), im::common::CHANNEL_CONTROL);
    auto* request = envelope.mutable_sync_request();
    request->set_global_cursor(cursor);
    request->set_limit(limit);
    return envelope;
}

void MiniImSessionManager::handleIncomingEnvelope(const QByteArray& payload)
{
    AppendClientLog(QStringLiteral("handleIncomingEnvelope begin size=%1").arg(payload.size()));
    im::envelope::Envelope envelope;
    if (!envelope.ParseFromArray(payload.constData(), payload.size()))
    {
        AppendClientLog(QStringLiteral("handleIncomingEnvelope parse failed"));
        emit errorRaised(QStringLiteral("invalid envelope payload"));
        return;
    }

    if (m_transportStopping)
    {
        return;
    }
    m_lastResponseTime.start();
    if (envelope.has_welcome())
    {
        AppendClientLog(QStringLiteral("handleIncomingEnvelope: welcome"));
        const auto& welcome = envelope.welcome();
        if (welcome.need_reauth())
        {
            restartConnection(QStringLiteral("session requires reauthentication"), true);
            return;
        }
        if (QString::fromStdString(welcome.user_id()) != m_userId)
        {
            emit errorRaised(QStringLiteral("welcome user does not match account state"));
            disconnectFromServer();
            return;
        }
        m_connected = true;
        m_connecting = false;
        m_session_id = QString::fromStdString(welcome.session_id());
        m_resume_session_id = m_session_id;
        if (!m_stateStore.saveSession(m_session_id, m_last_acked_request_id))
        {
            emit errorRaised(m_stateStore.errorString());
            disconnectFromServer();
            return;
        }
        m_recovery.authenticated();
        m_heartbeat_interval_sec = welcome.heartbeat_interval_sec() > 0
            ? static_cast<int>(qMin<quint32>(welcome.heartbeat_interval_sec(), 300))
            : kDefaultHeartbeatIntervalSec;

        emit connectionChanged(QStringLiteral("connected"), m_session_id);
        const QVariantMap initial = m_stateStore.snapshot();
        if (initial.isEmpty())
        {
            emit errorRaised(m_stateStore.errorString());
            disconnectFromServer();
            return;
        }
        m_files.start(initial.value(QStringLiteral("files")).toList());
        emit initialStateLoaded(initial);
        m_heartbeat_timer.start(m_heartbeat_interval_sec * 1000);
        m_messageRetryTimer.start();
        m_sync.start();
        return;
    }

    if (envelope.has_ack())
    {
        AppendClientLog(QStringLiteral("handleIncomingEnvelope: ack"));
        const auto& ack = envelope.ack();
        if (!ack.success() && ack.code() == 401)
        {
            restartConnection(QStringLiteral("session rejected; reconnecting"), true);
        }
        const QString request_id = QString::fromStdString(ack.request_id());
        handleMessageResult(request_id, ack.success(), ack.code(),
            QString::fromStdString(ack.message()), QString::fromStdString(ack.entity_id()));
        if (ack.success())
        {
            m_last_acked_request_id = request_id;
            if (!m_stateStore.saveSession(m_session_id, request_id))
            {
                emit errorRaised(m_stateStore.errorString());
            }
        }
        m_files.handleAck(ack);
        if (!ack.success())
        {
            const QString message = ack.message().empty()
                ? QStringLiteral("request rejected by server")
                : QString::fromStdString(ack.message());
            emit errorRaised(message);
        }
        return;
    }

    if (m_sync.handleEnvelope(envelope))
    {
        return;
    }

    if (envelope.has_error())
    {
        AppendClientLog(QStringLiteral("handleIncomingEnvelope: error"));
        const auto& error = envelope.error();
        if (error.code() == 401)
        {
            restartConnection(QStringLiteral("session rejected; reconnecting"), true);
        }
        m_files.handleFileResult(QString::fromStdString(envelope.request_id()), false, error.code(),
            QString::fromStdString(error.message()), QString());
        handleMessageResult(QString::fromStdString(envelope.request_id()), false, error.code(),
            QString::fromStdString(error.message()), QString());
        const QString message = error.message().empty()
            ? QStringLiteral("server error")
            : QString::fromStdString(error.message());
        emit errorRaised(message);
        return;
    }
}

void MiniImSessionManager::onSyncEventApplied(const MiniImStateEvent& event)
{
    if (event.type == QStringLiteral("message"))
    {
        emit messagePushed(event.data);
    }
    else if (event.type == QStringLiteral("conversation"))
    {
        emit conversationUpdated(event.data);
    }
    else if (event.type != QStringLiteral("file"))
    {
        emit messageUpdated(event.data);
    }
}

QString MiniImSessionManager::makeRequestId(const QString& suffix) const
{
    return m_device_id + QStringLiteral("-") + suffix + QStringLiteral("-")
        + QUuid::createUuid().toString(QUuid::WithoutBraces);
}

void MiniImSessionManager::emitConnectionError(const QString& message)
{
    AppendClientLog(QStringLiteral("emitConnectionError: %1").arg(message));
    m_connecting = false;
    m_connected = false;
    emit errorRaised(message);
    emit connectionChanged(QStringLiteral("error"), QString());
    if (!m_transport.isActive())
    {
        m_recovery.connectionLost();
    }
}
