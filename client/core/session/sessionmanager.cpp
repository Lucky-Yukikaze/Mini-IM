#include "core/session/sessionmanager.h"
#include "core/file/downloadsink.h"
#include "core/file/uploadstream.h"
#include "core/model/eventmapper.h"

#include <QByteArray>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QMetaObject>
#include <QTextStream>
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
constexpr const char* kAlpn = "mini-im";
constexpr int kDefaultHeartbeatIntervalSec = 15;
constexpr quint32 kEnvelopeFrameHeaderSize = 4;
constexpr quint64 kReceiveBatchSize = 64 * 1024;
constexpr int kRequestRetryMs = 5000;

struct StreamSendContext
{
    QUIC_BUFFER buffer;
    QByteArray data;
};

bool IsClientDebugLogEnabled()
{
    static const QString value = qEnvironmentVariable("MINIIM_DEBUG_LOG");
    static const bool enabled = value.isEmpty() || value != QStringLiteral("0");
    return enabled;
}

void AppendClientLog(const QString& message)
{
    if (!IsClientDebugLogEnabled())
    {
        return;
    }
    const QString base_dir = QCoreApplication::applicationDirPath();
    QFile file(QDir(base_dir).filePath(QStringLiteral("mini_im_client.log")));
    if (!file.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text))
    {
        return;
    }

    QTextStream stream(&file);
    stream << QDateTime::currentDateTime().toString(QStringLiteral("yyyy-MM-dd HH:mm:ss.zzz"))
           << QStringLiteral(" | ")
           << message
           << Qt::endl;
}

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
      m_connected(false),
      m_connecting(false),
      m_hello_sent(false),
      m_global_cursor(0),
      m_seq(0),
      m_heartbeat_interval_sec(kDefaultHeartbeatIntervalSec),
      m_msquic(nullptr),
      m_registration(nullptr),
      m_configuration(nullptr),
      m_connection(nullptr),
      m_stream(nullptr)
{
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

MiniImSessionManager::~MiniImSessionManager()
{
    disconnectFromServer();
    releaseMsQuic();
}

bool MiniImSessionManager::connectToServer(const QString& endpoint, const QString& token, const QString& device_id)
{
    return connectToServerWithResume(
        endpoint,
        token,
        device_id,
        m_resume_session_id,
        m_global_cursor,
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
    if (m_connecting || m_connected || m_connection != nullptr)
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
    if (!m_recovery.enabled() || m_connection != nullptr)
    {
        return false;
    }
    m_transportStopping = false;
    m_recovery.beginAttempt();
    m_resume_session_id = m_stateStore.sessionId();
    m_last_acked_request_id = m_stateStore.lastAck();
    m_global_cursor = m_stateStore.cursor();
    m_syncRequestId.clear();
    m_latest_file_versions.clear();
    m_seq = 0;
    m_heartbeat_interval_sec = kDefaultHeartbeatIntervalSec;
    m_hello_sent = false;
    m_connecting = true;
    m_connected = false;

    emit connectionChanged(QStringLiteral("connecting"), QString());

    if (!initializeMsQuic())
    {
        AppendClientLog(QStringLiteral("initializeMsQuic failed"));
        emitConnectionError(QStringLiteral("failed to initialize msquic"));
        return false;
    }

    QString host;
    uint16_t port = 0;
    if (!parseEndpoint(m_endpoint, &host, &port))
    {
        AppendClientLog(QStringLiteral("parseEndpoint failed: %1").arg(m_endpoint));
        emitConnectionError(QStringLiteral("invalid endpoint"));
        return false;
    }
    AppendClientLog(QStringLiteral("endpoint parsed host=%1 port=%2").arg(host).arg(port));

    m_callbackContext = std::make_unique<CallbackContext>(CallbackContext{this, m_connectionGeneration});
    const QUIC_STATUS open_status = m_msquic->ConnectionOpen(
        m_registration,
        &MiniImSessionManager::handleConnectionEvent,
        m_callbackContext.get(),
        &m_connection);
    if (QUIC_FAILED(open_status))
    {
        m_connection = nullptr;
        AppendClientLog(QStringLiteral("ConnectionOpen failed status=%1").arg(open_status));
        emitConnectionError(QStringLiteral("msquic connection open failed: %1").arg(open_status));
        return false;
    }
    AppendClientLog(QStringLiteral("ConnectionOpen ok"));

    const QByteArray host_utf8 = host.toUtf8();
    const QUIC_STATUS start_status = m_msquic->ConnectionStart(
        m_connection,
        m_configuration,
        QUIC_ADDRESS_FAMILY_UNSPEC,
        host_utf8.constData(),
        port);
    if (QUIC_FAILED(start_status))
    {
        closeConnectionHandle();
        AppendClientLog(QStringLiteral("ConnectionStart failed status=%1").arg(start_status));
        emitConnectionError(QStringLiteral("msquic connection start failed: %1").arg(start_status));
        return false;
    }
    AppendClientLog(QStringLiteral("ConnectionStart ok"));

    return true;
}

void MiniImSessionManager::disconnectFromServer()
{
    m_recovery.stop();
    m_transportStopping = true;
    m_heartbeat_timer.stop();
    resetRuntimeState();
    if (m_connection != nullptr && m_msquic != nullptr)
    {
        m_msquic->ConnectionShutdown(m_connection, QUIC_CONNECTION_SHUTDOWN_FLAG_NONE, 0);
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
    if (m_connection != nullptr)
    {
        m_msquic->ConnectionShutdown(m_connection, QUIC_CONNECTION_SHUTDOWN_FLAG_NONE, 0);
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

QUIC_STATUS QUIC_API MiniImSessionManager::handleConnectionEvent(
    HQUIC connection,
    void* context,
    QUIC_CONNECTION_EVENT* event)
{
    const auto* callback = static_cast<CallbackContext*>(context);
    if (callback == nullptr || callback->manager == nullptr)
    {
        return QUIC_STATUS_INTERNAL_ERROR;
    }
    auto* manager = callback->manager;
    const quint64 generation = callback->generation;

    switch (event->Type)
    {
    case QUIC_CONNECTION_EVENT_CONNECTED:
    {
        AppendClientLog(QStringLiteral("connection event: CONNECTED"));
        QMetaObject::invokeMethod(
            manager,
            [manager, generation]()
            {
                if (generation != manager->m_connectionGeneration)
                {
                    return;
                }
                AppendClientLog(QStringLiteral("queued CONNECTED handler begin"));
                if (manager->m_transportStopping || manager->m_connection == nullptr || manager->m_stream != nullptr)
                {
                    AppendClientLog(QStringLiteral("queued CONNECTED handler skipped"));
                    return;
                }

                const QUIC_STATUS open_status = manager->m_msquic->StreamOpen(
                    manager->m_connection,
                    QUIC_STREAM_OPEN_FLAG_NONE,
                    &MiniImSessionManager::handleStreamEvent,
                    manager->m_callbackContext.get(),
                    &manager->m_stream);
                if (QUIC_FAILED(open_status))
                {
                    AppendClientLog(QStringLiteral("StreamOpen failed status=%1").arg(open_status));
                    manager->emitConnectionError(QStringLiteral("stream open failed: %1").arg(open_status));
                    manager->m_msquic->ConnectionShutdown(manager->m_connection, QUIC_CONNECTION_SHUTDOWN_FLAG_NONE, 0);
                    return;
                }
                AppendClientLog(QStringLiteral("StreamOpen ok"));

                const QUIC_STATUS start_status =
                    manager->m_msquic->StreamStart(manager->m_stream, QUIC_STREAM_START_FLAG_IMMEDIATE);
                if (QUIC_FAILED(start_status))
                {
                    AppendClientLog(QStringLiteral("StreamStart failed status=%1").arg(start_status));
                    manager->emitConnectionError(QStringLiteral("stream start failed: %1").arg(start_status));
                    manager->m_msquic->StreamClose(manager->m_stream);
                    manager->m_stream = nullptr;
                    manager->m_msquic->ConnectionShutdown(manager->m_connection, QUIC_CONNECTION_SHUTDOWN_FLAG_NONE, 0);
                    return;
                }
                AppendClientLog(QStringLiteral("StreamStart ok"));

                if (!manager->sendHello())
                {
                    AppendClientLog(QStringLiteral("sendHello failed"));
                    manager->emitConnectionError(QStringLiteral("failed to send hello"));
                    manager->m_msquic->ConnectionShutdown(manager->m_connection, QUIC_CONNECTION_SHUTDOWN_FLAG_NONE, 0);
                }
                else
                {
                    AppendClientLog(QStringLiteral("sendHello ok"));
                    manager->m_hello_sent = true;
                }
            },
            Qt::QueuedConnection);
        break;
    }
    case QUIC_CONNECTION_EVENT_PEER_STREAM_STARTED:
    {
        const HQUIC peer_stream = event->PEER_STREAM_STARTED.Stream;
        AppendClientLog(
            QStringLiteral("connection event: PEER_STREAM_STARTED stream=%1 flags=%2")
                .arg(reinterpret_cast<quintptr>(peer_stream))
                .arg(static_cast<qulonglong>(event->PEER_STREAM_STARTED.Flags)));
        if (manager->m_msquic != nullptr && peer_stream != nullptr)
        {
            manager->m_msquic->SetCallbackHandler(
                peer_stream,
                reinterpret_cast<void*>(MiniImSessionManager::handleStreamEvent),
                context);
        }
        break;
    }
    case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_TRANSPORT:
    {
        const uint64_t error_code = event->SHUTDOWN_INITIATED_BY_TRANSPORT.Status;
        AppendClientLog(QStringLiteral("connection event: TRANSPORT_SHUTDOWN status=%1").arg(error_code));
        QMetaObject::invokeMethod(
            manager,
            [manager, generation, error_code]()
            {
                if (generation != manager->m_connectionGeneration)
                {
                    return;
                }
                emit manager->errorRaised(QStringLiteral("transport shutdown: %1").arg(error_code));
            },
            Qt::QueuedConnection);
        break;
    }
    case QUIC_CONNECTION_EVENT_SHUTDOWN_COMPLETE:
    {
        AppendClientLog(QStringLiteral("connection event: SHUTDOWN_COMPLETE"));
        QMetaObject::invokeMethod(
            manager,
            [manager, generation]()
            {
                if (generation != manager->m_connectionGeneration)
                {
                    return;
                }
                AppendClientLog(QStringLiteral("queued SHUTDOWN_COMPLETE handler begin"));
                manager->m_heartbeat_timer.stop();
                manager->resetRuntimeState();
                manager->closeControlStreamHandle();
                manager->closeConnectionHandle();
                manager->m_connected = false;
                manager->m_connecting = false;
                manager->m_hello_sent = false;
                emit manager->connectionChanged(QStringLiteral("disconnected"), manager->m_session_id);
                manager->m_recovery.connectionLost();
            },
            Qt::QueuedConnection);
        break;
    }
    default:
        break;
    }

    Q_UNUSED(connection);
    return QUIC_STATUS_SUCCESS;
}

QUIC_STATUS QUIC_API MiniImSessionManager::handleStreamEvent(HQUIC stream, void* context, QUIC_STREAM_EVENT* event)
{
    const auto* callback = static_cast<CallbackContext*>(context);
    if (callback == nullptr || callback->manager == nullptr)
    {
        return QUIC_STATUS_INTERNAL_ERROR;
    }
    auto* manager = callback->manager;
    const quint64 generation = callback->generation;

    quint64 streamId = 0;
    if (event->Type == QUIC_STREAM_EVENT_RECEIVE
        || event->Type == QUIC_STREAM_EVENT_PEER_SEND_SHUTDOWN
        || event->Type == QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE)
    {
        uint32_t length = sizeof(streamId);
        const QUIC_STATUS status = manager->m_msquic->GetParam(
            stream, QUIC_PARAM_STREAM_ID, &length, &streamId);
        if (QUIC_FAILED(status))
        {
            return status;
        }
    }

    switch (event->Type)
    {
    case QUIC_STREAM_EVENT_RECEIVE:
    {
        AppendClientLog(QStringLiteral("stream event: RECEIVE len=%1").arg(event->RECEIVE.TotalBufferLength));
        const quint64 totalLength = qMin<quint64>(event->RECEIVE.TotalBufferLength, kReceiveBatchSize);
        if (totalLength == 0)
        {
            break;
        }

        QByteArray payload;
        payload.reserve(static_cast<qsizetype>(totalLength));
        for (uint32_t index = 0; index < event->RECEIVE.BufferCount && payload.size() < totalLength; ++index)
        {
            const QUIC_BUFFER& buffer = event->RECEIVE.Buffers[index];
            const auto count = qMin<quint64>(buffer.Length, totalLength - payload.size());
            payload.append(reinterpret_cast<const char*>(buffer.Buffer), static_cast<qsizetype>(count));
        }

        QMetaObject::invokeMethod(
            manager,
            [manager, generation, payload, stream, streamId, totalLength]()
            {
                if (generation != manager->m_connectionGeneration)
                {
                    return;
                }
                if (manager->m_transportStopping)
                {
                    manager->m_msquic->StreamReceiveComplete(stream, totalLength);
                    return;
                }
                if (manager->m_stream == stream)
                {
                    manager->handleIncomingControlStreamData(payload);
                    manager->m_msquic->StreamReceiveComplete(stream, totalLength);
                    manager->m_msquic->StreamReceiveSetEnabled(stream, TRUE);
                    return;
                }
                auto& state = manager->m_download_stream_states[streamId];
                state.pending_receive = stream;
                state.pending_receive_bytes = totalLength;
                manager->handleIncomingFileStream(streamId, payload);
                auto received = manager->m_download_stream_states.find(streamId);
                if (received == manager->m_download_stream_states.end())
                {
                    manager->m_msquic->StreamReceiveComplete(stream, totalLength);
                    manager->m_msquic->StreamShutdown(stream, QUIC_STREAM_SHUTDOWN_FLAG_ABORT_RECEIVE, 0x1004);
                }
                else if (!received->header_parsed || received->buffer.isEmpty()
                    || manager->m_failed_file_downloads.contains(received->file_id))
                {
                    manager->completeFileReceive(streamId);
                }
                // Keep this receive pending until control metadata permits writing the file.
            },
            Qt::QueuedConnection);
        return QUIC_STATUS_PENDING;
    }
    case QUIC_STREAM_EVENT_SEND_COMPLETE:
    {
        AppendClientLog(QStringLiteral("stream event: SEND_COMPLETE canceled=%1").arg(event->SEND_COMPLETE.Canceled ? 1 : 0));
        auto* send_context = static_cast<StreamSendContext*>(event->SEND_COMPLETE.ClientContext);
        if (send_context != nullptr)
        {
            delete send_context;
        }
        break;
    }
    case QUIC_STREAM_EVENT_PEER_SEND_ABORTED:
    case QUIC_STREAM_EVENT_PEER_RECEIVE_ABORTED:
    {
        QMetaObject::invokeMethod(manager, [manager, generation, stream]()
        {
            if (generation == manager->m_connectionGeneration && manager->m_stream == stream)
            {
                manager->restartConnection(QStringLiteral("control stream aborted"));
            }
        }, Qt::QueuedConnection);
        break;
    }
    case QUIC_STREAM_EVENT_PEER_SEND_SHUTDOWN:
    {
        QMetaObject::invokeMethod(manager, [manager, generation, stream, streamId]()
        {
            if (generation != manager->m_connectionGeneration)
            {
                return;
            }
            if (manager->m_stream == stream)
            {
                manager->restartConnection(QStringLiteral("control stream closed"));
                return;
            }
            if (manager->m_transportStopping)
            {
                return;
            }
            auto& state = manager->m_download_stream_states[streamId];
            state.finished = true;
            manager->flushPendingDownloadBuffers(state.file_id);
        }, Qt::QueuedConnection);
        break;
    }
    case QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE:
    {
        AppendClientLog(QStringLiteral("stream event: SHUTDOWN_COMPLETE"));
        const bool connectionShutdown = event->SHUTDOWN_COMPLETE.ConnectionShutdown;
        QMetaObject::invokeMethod(
            manager,
            [manager, generation, stream, streamId, connectionShutdown]()
            {
                if (generation != manager->m_connectionGeneration)
                {
                    return;
                }
                AppendClientLog(QStringLiteral("queued stream SHUTDOWN_COMPLETE handler begin"));
                if (manager->m_stream == stream && manager->m_msquic != nullptr)
                {
                    manager->closeControlStreamHandle();
                    manager->m_stream = nullptr;
                    if (!connectionShutdown)
                    {
                        manager->restartConnection(QStringLiteral("control stream interrupted"));
                    }
                    return;
                }
                if (manager->m_msquic == nullptr)
                {
                    return;
                }
                auto state_it = manager->m_download_stream_states.find(streamId);
                if (state_it != manager->m_download_stream_states.end())
                {
                    const QString fileId = state_it->file_id;
                    if (!state_it->finished || connectionShutdown)
                    {
                        manager->m_failed_file_downloads.insert(fileId);
                        manager->m_download_stream_states.remove(streamId);
                        emit manager->errorRaised(QStringLiteral("download stream interrupted"));
                    }
                    else
                    {
                        manager->flushPendingDownloadBuffers(fileId);
                        if (manager->m_failed_file_downloads.contains(fileId))
                        {
                            manager->m_download_stream_states.remove(streamId);
                        }
                    }
                }
                manager->m_msquic->StreamClose(stream);
            },
            Qt::QueuedConnection);
        break;
    }
    default:
        break;
    }

    return QUIC_STATUS_SUCCESS;
}

bool MiniImSessionManager::initializeMsQuic()
{
    if (m_msquic != nullptr)
    {
        return true;
    }

    if (QUIC_FAILED(MsQuicOpen2(&m_msquic)))
    {
        m_msquic = nullptr;
        return false;
    }

    const QUIC_REGISTRATION_CONFIG registration_config = { "mini-im-client", QUIC_EXECUTION_PROFILE_LOW_LATENCY };
    if (QUIC_FAILED(m_msquic->RegistrationOpen(&registration_config, &m_registration)))
    {
        releaseMsQuic();
        return false;
    }

    QUIC_BUFFER alpn_buffer;
    alpn_buffer.Length = static_cast<uint32_t>(std::strlen(kAlpn));
    alpn_buffer.Buffer = reinterpret_cast<uint8_t*>(const_cast<char*>(kAlpn));
    QUIC_SETTINGS settings = {};
    settings.IsSet.PeerUnidiStreamCount = TRUE;
    settings.PeerUnidiStreamCount = 16;
    settings.IsSet.SendBufferingEnabled = TRUE;
    settings.SendBufferingEnabled = FALSE;
    settings.IsSet.StreamRecvBufferDefault = TRUE;
    settings.StreamRecvBufferDefault = static_cast<uint32_t>(kReceiveBatchSize);
    settings.IsSet.StreamRecvWindowDefault = TRUE;
    settings.StreamRecvWindowDefault = static_cast<uint32_t>(kReceiveBatchSize);
    if (QUIC_FAILED(
            m_msquic->ConfigurationOpen(
                m_registration,
                &alpn_buffer,
                1,
                &settings,
                sizeof(settings),
                nullptr,
                &m_configuration)))
    {
        releaseMsQuic();
        return false;
    }

    QUIC_CREDENTIAL_CONFIG credential_config = {};
    credential_config.Type = QUIC_CREDENTIAL_TYPE_NONE;
    credential_config.Flags = QUIC_CREDENTIAL_FLAG_CLIENT | QUIC_CREDENTIAL_FLAG_NO_CERTIFICATE_VALIDATION;
    if (QUIC_FAILED(m_msquic->ConfigurationLoadCredential(m_configuration, &credential_config)))
    {
        releaseMsQuic();
        return false;
    }

    return true;
}

void MiniImSessionManager::releaseMsQuic()
{
    closeControlStreamHandle();
    closeConnectionHandle();
    resetRuntimeState();
    if (m_configuration != nullptr && m_msquic != nullptr)
    {
        m_msquic->ConfigurationClose(m_configuration);
        m_configuration = nullptr;
    }
    if (m_registration != nullptr && m_msquic != nullptr)
    {
        m_msquic->RegistrationClose(m_registration);
        m_registration = nullptr;
    }
    if (m_msquic != nullptr)
    {
        MsQuicClose(m_msquic);
        m_msquic = nullptr;
    }
}

void MiniImSessionManager::resetRuntimeState()
{
    m_messageRetryTimer.stop();
    m_messageSyncReady = false;
    m_activeMessageRequest.clear();
    m_syncRequestPayload.clear();
    m_messageAttemptTime.invalidate();
    m_syncAttemptTime.invalidate();
    m_control_stream_buffer.clear();
    m_syncRequestId.clear();
    qDeleteAll(m_upload_streams);
    m_upload_streams.clear();
    for (auto it = m_download_stream_states.begin(); it != m_download_stream_states.end(); ++it)
    {
        if (it->pending_receive != nullptr && m_msquic != nullptr)
        {
            m_msquic->StreamReceiveComplete(it->pending_receive, it->pending_receive_bytes);
            m_msquic->StreamShutdown(it->pending_receive, QUIC_STREAM_SHUTDOWN_FLAG_ABORT_RECEIVE, 0);
        }
    }
    m_download_stream_states.clear();
    m_latest_file_versions.clear();
    m_pending_file_init_requests.clear();
    m_pending_file_uploads.clear();
    m_pending_file_download_init_requests.clear();
    m_pending_file_downloads.clear();
    m_failed_file_downloads.clear();
    m_pending_file_finish_requests.clear();
}

void MiniImSessionManager::closeControlStreamHandle()
{
    if (m_stream == nullptr || m_msquic == nullptr)
    {
        return;
    }
    HQUIC stream = m_stream;
    m_stream = nullptr;
    m_msquic->StreamClose(stream);
}

void MiniImSessionManager::closeConnectionHandle()
{
    if (m_connection == nullptr || m_msquic == nullptr)
    {
        return;
    }
    HQUIC connection = m_connection;
    m_connection = nullptr;
    m_msquic->ConnectionClose(connection);
    ++m_connectionGeneration;
    m_callbackContext.reset();
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
    if (m_msquic == nullptr || m_stream == nullptr || payload.empty())
    {
        AppendClientLog(QStringLiteral("sendEnvelope rejected msquic=%1 stream=%2 size=%3")
                            .arg(m_msquic != nullptr ? 1 : 0)
                            .arg(m_stream != nullptr ? 1 : 0)
                            .arg(static_cast<qulonglong>(payload.size())));
        return false;
    }

    const QByteArray frame = BuildEnvelopeFrame(payload);
    auto* send_context = new StreamSendContext;
    send_context->data = frame;
    send_context->buffer.Length = static_cast<uint32_t>(frame.size());
    send_context->buffer.Buffer = reinterpret_cast<uint8_t*>(send_context->data.data());

    const QUIC_STATUS status = m_msquic->StreamSend(
        m_stream,
        &send_context->buffer,
        1,
        QUIC_SEND_FLAG_NONE,
        send_context);
    if (QUIC_FAILED(status))
    {
        delete send_context;
        AppendClientLog(
            QStringLiteral("StreamSend failed status=%1 frameSize=%2 payloadSize=%3")
                .arg(status)
                .arg(static_cast<qulonglong>(frame.size()))
                .arg(static_cast<qulonglong>(payload.size())));
        return false;
    }
    AppendClientLog(
        QStringLiteral("StreamSend ok frameSize=%1 payloadSize=%2")
            .arg(static_cast<qulonglong>(frame.size()))
            .arg(static_cast<qulonglong>(payload.size())));
    return true;
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
        handleIncomingEnvelope(envelope_payload);
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
    hello->set_global_cursor(m_global_cursor);
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
    if (!m_syncRequestId.isEmpty() && m_syncAttemptTime.isValid()
        && m_syncAttemptTime.elapsed() >= kRequestRetryMs)
    {
        sendEnvelope(m_syncRequestPayload);
        m_syncAttemptTime.restart();
    }
    try
    {
        if (!m_messageSyncReady || m_stateStore.hasGap())
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
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty() || file_path.trimmed().isEmpty())
    {
        return false;
    }

    QFile file(file_path);
    if (!file.exists() || !file.open(QIODevice::ReadOnly))
    {
        emit errorRaised(QStringLiteral("failed to open file"));
        return false;
    }

    QCryptographicHash hash(QCryptographicHash::Sha256);
    while (!file.atEnd())
    {
        const QByteArray chunk = file.read(64 * 1024);
        if (chunk.isEmpty() && file.error() != QFileDevice::NoError)
        {
            emit errorRaised(QStringLiteral("failed to read file"));
            return false;
        }
        if (!chunk.isEmpty())
        {
            hash.addData(chunk);
        }
    }
    const quint64 file_size = static_cast<quint64>(file.size());
    file.close();
    if (file_size == 0)
    {
        emit errorRaised(QStringLiteral("empty file is not supported"));
        return false;
    }

    const QFileInfo info(file_path);
    const QString file_name = info.fileName();
    const QString sha256 = QString::fromLatin1(hash.result().toHex());
    const QString client_file_id = QStringLiteral("intent-%1")
        .arg(QUuid::createUuid().toString(QUuid::WithoutBraces));
    const QString request_id = makeRequestId(QStringLiteral("fileinit"));

    PendingFileUpload pending;
    pending.conversation_id = conversation_id;
    pending.file_path = file_path;
    pending.file_name = file_name;
    pending.client_file_id = client_file_id;
    pending.sha256 = sha256;
    pending.file_size = file_size;
    pending.priority = priority;
    pending.stream_started = false;
    m_pending_file_init_requests.insert(request_id, pending);

    if (!sendFileInitRequest(
            request_id,
            conversation_id,
            client_file_id,
            file_name,
            file_size,
            pending.sha256,
            0,
            priority,
            static_cast<int>(im::common::FILE_DIRECTION_UPLOAD),
            QString()))
    {
        m_pending_file_init_requests.remove(request_id);
        emit errorRaised(QStringLiteral("failed to send file init"));
        return false;
    }
    return true;
}

bool MiniImSessionManager::downloadFile(
    const QString& conversation_id,
    const QString& source_file_id,
    const QString& save_path,
    quint32 priority)
{
    if (!m_connected || m_session_id.isEmpty() || conversation_id.trimmed().isEmpty() || source_file_id.trimmed().isEmpty() || save_path.trimmed().isEmpty())
    {
        return false;
    }

    const QString client_file_id = QStringLiteral("intent-%1")
        .arg(QUuid::createUuid().toString(QUuid::WithoutBraces));
    const QString request_id = makeRequestId(QStringLiteral("filedl"));
    QString normalized_save_path = save_path.trimmed();
    const QFileInfo save_info(normalized_save_path);
    if (save_info.fileName().isEmpty() || save_info.isDir())
    {
        QString safe_name = source_file_id.trimmed();
        safe_name.replace(QChar('/'), QChar('_'));
        safe_name.replace(QChar('\\'), QChar('_'));
        safe_name.replace(QChar(':'), QChar('_'));
        if (safe_name.isEmpty())
        {
            safe_name = QStringLiteral("download");
        }
        normalized_save_path = QDir(normalized_save_path).filePath(safe_name + QStringLiteral(".bin"));
    }
    PendingFileDownload pending;
    pending.conversation_id = conversation_id;
    pending.source_file_id = source_file_id;
    pending.save_path = QDir::toNativeSeparators(normalized_save_path);
    pending.client_file_id = client_file_id;
    m_pending_file_download_init_requests.insert(request_id, pending);

    if (!sendFileInitRequest(
            request_id,
            conversation_id,
            client_file_id,
            QStringLiteral("download.bin"),
            1,
            QStringLiteral("na"),
            0,
            priority,
            static_cast<int>(im::common::FILE_DIRECTION_DOWNLOAD),
            source_file_id))
    {
        m_pending_file_download_init_requests.remove(request_id);
        emit errorRaised(QStringLiteral("failed to send download init"));
        return false;
    }
    return true;
}

bool MiniImSessionManager::sendSyncRequest(quint64 global_cursor, quint32 limit)
{
    if (!m_connected || m_session_id.isEmpty())
    {
        return false;
    }
    if (!m_syncRequestId.isEmpty())
    {
        return true;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("sync"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* sync_request = envelope.mutable_sync_request();
    sync_request->set_global_cursor(global_cursor);
    sync_request->set_limit(limit);

    m_syncRequestId = request_id;
    m_syncRequestPayload = envelope.SerializeAsString();
    m_syncAttemptTime.start();
    if (!sendEnvelope(m_syncRequestPayload))
    {
        return false;
    }
    return true;
}

bool MiniImSessionManager::sendFileInitRequest(
    const QString& request_id,
    const QString& conversation_id,
    const QString& client_file_id,
    const QString& file_name,
    quint64 file_size,
    const QString& sha256,
    quint64 resume_offset,
    quint32 priority,
    int direction,
    const QString& source_file_id)
{
    if (!m_connected || m_session_id.isEmpty() || request_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_FILE);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* file_init = envelope.mutable_file_init();
    file_init->set_conversation_id(conversation_id.toStdString());
    file_init->set_client_file_id(client_file_id.toStdString());
    file_init->set_file_name(file_name.toStdString());
    file_init->set_file_size(file_size);
    file_init->set_sha256(sha256.toStdString());
    file_init->set_direction(static_cast<im::common::FileTransferDirection>(direction));
    file_init->set_resume_offset(resume_offset);
    file_init->set_priority(priority);
    file_init->set_source_file_id(source_file_id.toStdString());

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::sendFileFinish(
    const QString& file_id, bool success, quint64 transferredBytes, const QString& sha256)
{
    if (!m_connected || m_session_id.isEmpty() || file_id.trimmed().isEmpty())
    {
        return false;
    }

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("filefinish"));

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_FILE);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());
    auto* file_finish = envelope.mutable_file_finish();
    file_finish->set_file_id(file_id.toStdString());
    file_finish->set_success(success);
    file_finish->set_transferred_bytes(transferredBytes);
    file_finish->set_sha256(sha256.toStdString());

    m_pending_file_finish_requests.insert(request_id, file_id);
    if (!sendEnvelope(envelope.SerializeAsString()))
    {
        m_pending_file_finish_requests.remove(request_id);
        return false;
    }
    return true;
}

bool MiniImSessionManager::sendFileStreamData(
    const QString& file_id, const QString& file_path, quint64 offset, quint64 fileSize)
{
    if (m_connection == nullptr || m_msquic == nullptr || m_upload_streams.contains(file_id))
    {
        return false;
    }
    auto* sender = new MiniImUploadStream(m_msquic, m_connection, file_path, fileSize, this);
    QObject::connect(sender, &MiniImUploadStream::finished, this,
        [this, sender, file_id](bool success, const QString& error)
        {
            m_upload_streams.remove(file_id);
            sender->deleteLater();
            if (!success)
            {
                emit errorRaised(error);
            }
            else if (!sendFileFinish(file_id, true))
            {
                emit errorRaised(QStringLiteral("failed to send file finish"));
            }
        });
    if (!sender->start(file_id, offset))
    {
        delete sender;
        return false;
    }
    m_upload_streams.insert(file_id, sender);
    return true;
}

void MiniImSessionManager::handleFileUpdated(const im::file::FileUpdated& updated)
{
    const QString fileId = QString::fromStdString(updated.file_id());
    if (updated.version() < m_latest_file_versions.value(fileId, 0))
    {
        return;
    }
    m_latest_file_versions.insert(fileId, updated.version());
    emit fileProgress(miniim::BuildFileProgressPayload(
        updated.event_id(), updated.file_id(), updated.conversation_id(), updated.transferred_bytes(),
        updated.completed(), updated.version(), updated.updated_at_ms()));

    auto download = m_pending_file_downloads.find(fileId);
    if (download != m_pending_file_downloads.end() && !download->sink && updated.file_size() > 0)
    {
        download->sink = std::make_shared<MiniImDownloadSink>(download->save_path, fileId);
        if (!download->sink->open(updated.file_size(), QString::fromStdString(updated.sha256())))
        {
            m_failed_file_downloads.insert(fileId);
            emit errorRaised(download->sink->errorString());
            for (auto it = m_download_stream_states.begin(); it != m_download_stream_states.end(); ++it)
            {
                if (it->file_id == fileId)
                {
                    completeFileReceive(it.key());
                }
            }
            return;
        }
        flushPendingDownloadBuffers(fileId);
    }
    auto upload = m_pending_file_uploads.find(fileId);
    if (upload == m_pending_file_uploads.end() || upload->stream_started || updated.completed())
    {
        return;
    }
    if (sendFileStreamData(fileId, upload->file_path, updated.transferred_bytes(), upload->file_size))
    {
        upload->stream_started = true;
    }
    else
    {
        emit errorRaised(QStringLiteral("failed to send file stream"));
    }
}

void MiniImSessionManager::handleIncomingFileStream(quint64 streamId, const QByteArray& payload)
{
    const quint64 key = streamId;
    AppendClientLog(
        QStringLiteral("handleIncomingFileStream stream=%1 append=%2")
            .arg(key)
            .arg(payload.size()));
    auto state_it = m_download_stream_states.find(key);
    if (state_it == m_download_stream_states.end())
    {
        state_it = m_download_stream_states.insert(key, DownloadStreamState());
    }
    DownloadStreamState& state = state_it.value();
    state.buffer.append(payload);

    if (!state.header_parsed)
    {
        const int split = state.buffer.indexOf('\n');
        if (split < 0)
        {
            if (state.buffer.size() >= 512)
            {
                AppendClientLog(QStringLiteral("handleIncomingFileStream header too large stream=%1").arg(key));
                m_download_stream_states.remove(key);
            }
            return;
        }
        if (split >= 512)
        {
            m_download_stream_states.remove(key);
            return;
        }
        const QByteArray header = state.buffer.left(split);
        state.buffer.remove(0, split + 1);
        const QByteArray prefix("MINIIMFILE1 ");
        if (!header.startsWith(prefix))
        {
            AppendClientLog(QStringLiteral("handleIncomingFileStream invalid header stream=%1").arg(key));
            m_download_stream_states.remove(key);
            return;
        }
        state.file_id = QString::fromUtf8(header.mid(prefix.size())).trimmed();
        AppendClientLog(
            QStringLiteral("handleIncomingFileStream header parsed stream=%1 fileId=%2")
                .arg(key)
                .arg(state.file_id));
        state.header_parsed = true;
    }

    if (state.file_id.isEmpty())
    {
        AppendClientLog(QStringLiteral("handleIncomingFileStream empty file id stream=%1").arg(key));
        m_download_stream_states.remove(key);
        return;
    }
    flushPendingDownloadBuffers(state.file_id);
}

void MiniImSessionManager::flushPendingDownloadBuffers(const QString& file_id)
{
    auto pending = m_pending_file_downloads.find(file_id);
    if (pending == m_pending_file_downloads.end() || !pending->sink || m_failed_file_downloads.contains(file_id))
    {
        return;
    }
    bool finished = false;
    for (auto it = m_download_stream_states.begin(); it != m_download_stream_states.end(); ++it)
    {
        auto& state = it.value();
        if (state.file_id != file_id)
        {
            continue;
        }
        if (!state.buffer.isEmpty() && !pending->sink->append(state.buffer))
        {
            m_failed_file_downloads.insert(file_id);
            emit errorRaised(pending->sink->errorString());
            completeFileReceive(it.key());
            return;
        }
        state.buffer.clear();
        completeFileReceive(it.key());
        finished = finished || state.finished;
    }
    if (finished)
    {
        finishDownload(file_id);
    }
}

void MiniImSessionManager::completeFileReceive(quint64 streamId)
{
    auto state = m_download_stream_states.find(streamId);
    if (state == m_download_stream_states.end() || state->pending_receive == nullptr)
    {
        return;
    }
    HQUIC stream = state->pending_receive;
    const quint64 bytes = state->pending_receive_bytes;
    state->pending_receive = nullptr;
    state->pending_receive_bytes = 0;
    m_msquic->StreamReceiveComplete(stream, bytes);
    if (m_failed_file_downloads.contains(state->file_id))
    {
        state->buffer.clear();
        m_msquic->StreamShutdown(stream, QUIC_STREAM_SHUTDOWN_FLAG_ABORT_RECEIVE, 0x1004);
    }
    else
    {
        m_msquic->StreamReceiveSetEnabled(stream, TRUE);
    }
}

void MiniImSessionManager::finishDownload(const QString& file_id)
{
    auto pending = m_pending_file_downloads.find(file_id);
    if (pending == m_pending_file_downloads.end() || pending->finish_sent || !pending->sink)
    {
        return;
    }
    if (!pending->sink->finish())
    {
        m_failed_file_downloads.insert(file_id);
        emit errorRaised(pending->sink->errorString());
        return;
    }
    pending->finish_sent = sendFileFinish(file_id, true, pending->sink->receivedBytes(), pending->sink->sha256());
    if (!pending->finish_sent)
    {
        emit errorRaised(QStringLiteral("failed to confirm downloaded file"));
    }
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
        m_global_cursor = m_stateStore.cursor();
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
        for (const auto& value : initial.value(QStringLiteral("files")).toList())
        {
            const auto file = value.toMap();
            m_latest_file_versions.insert(file.value(QStringLiteral("fileId")).toString(),
                file.value(QStringLiteral("version")).toULongLong());
        }
        emit initialStateLoaded(initial);
        m_heartbeat_timer.start(m_heartbeat_interval_sec * 1000);
        m_messageSyncReady = false;
        m_messageRetryTimer.start();
        if (!sendSyncRequest(m_global_cursor))
        {
            emit errorRaised(QStringLiteral("failed to request sync after welcome"));
        }
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
        const QString finishedFileId = m_pending_file_finish_requests.take(request_id);
        if (!finishedFileId.isEmpty())
        {
            if (ack.success())
            {
                m_pending_file_uploads.remove(finishedFileId);
                m_pending_file_downloads.remove(finishedFileId);
                for (auto it = m_download_stream_states.begin(); it != m_download_stream_states.end();)
                {
                    it = it->file_id == finishedFileId ? m_download_stream_states.erase(it) : ++it;
                }
            }
            else if (m_pending_file_downloads.contains(finishedFileId))
            {
                m_pending_file_downloads[finishedFileId].finish_sent = false;
            }
        }
        auto pending_init = m_pending_file_init_requests.find(request_id);
        if (pending_init != m_pending_file_init_requests.end())
        {
            if (ack.success())
            {
                PendingFileUpload pending = pending_init.value();
                pending.file_id = QString::fromStdString(ack.entity_id());
                pending.stream_started = false;
                m_pending_file_uploads.insert(pending.file_id, pending);
            }
            m_pending_file_init_requests.erase(pending_init);
        }
        auto pending_download_init = m_pending_file_download_init_requests.find(request_id);
        if (pending_download_init != m_pending_file_download_init_requests.end())
        {
            if (ack.success())
            {
                PendingFileDownload pending = pending_download_init.value();
                pending.file_id = QString::fromStdString(ack.entity_id());
                m_failed_file_downloads.remove(pending.file_id);
                m_pending_file_downloads.insert(pending.file_id, pending);
                AppendClientLog(
                    QStringLiteral("download ack ready fileId=%1 savePath=%2")
                        .arg(pending.file_id, pending.save_path));
                flushPendingDownloadBuffers(pending.file_id);
            }
            m_pending_file_download_init_requests.erase(pending_download_init);
        }
        if (!ack.success())
        {
            const QString message = ack.message().empty()
                ? QStringLiteral("request rejected by server")
                : QString::fromStdString(ack.message());
            emit errorRaised(message);
        }
        return;
    }

    if (envelope.has_file_updated())
    {
        const auto& updated = envelope.file_updated();
        if (!updated.event_id().empty())
        {
            const MiniImStateEvent event{envelope.seq(), QString::fromStdString(updated.event_id()),
                QStringLiteral("file"), miniim::BuildFileProgressPayload(
                    updated.event_id(), updated.file_id(), updated.conversation_id(), updated.transferred_bytes(),
                    updated.completed(), updated.version(), updated.updated_at_ms())};
            if (!applySyncEvents({event}))
            {
                return;
            }
        }
        handleFileUpdated(updated);
        if (m_stateStore.hasGap())
        {
            sendSyncRequest(m_global_cursor);
        }
        return;
    }

    if (envelope.has_message_push())
    {
        const auto& pushed = envelope.message_push();
        if (!pushed.event_id().empty())
        {
            if (pushed.messages_size() != 1)
            {
                emit errorRaised(QStringLiteral("message event must contain exactly one message"));
                return;
            }
            if (!applySyncEvents({{envelope.seq(), QString::fromStdString(pushed.event_id()),
                    QStringLiteral("message"), miniim::BuildMessagePayload(pushed.messages(0))}}))
            {
                return;
            }
        }
        if (m_stateStore.hasGap())
        {
            sendSyncRequest(m_global_cursor);
        }
        return;
    }

    if (envelope.has_sync_response())
    {
        const bool requested = QString::fromStdString(envelope.request_id()) == m_syncRequestId
            && !m_syncRequestId.isEmpty();
        if (requested)
        {
            m_syncRequestId.clear();
            m_syncRequestPayload.clear();
            m_syncAttemptTime.invalidate();
        }
        const auto& response = envelope.sync_response();
        QVector<MiniImStateEvent> events;
        for (const auto& event : response.events())
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
                const auto& file = event.file_updated();
                item.type = QStringLiteral("file");
                item.data = miniim::BuildFileProgressPayload(file.event_id(), file.file_id(), file.conversation_id(),
                    file.transferred_bytes(), file.completed(), file.version(), file.updated_at_ms());
            }
            events.append(item);
        }
        if (!applySyncEvents(events))
        {
            return;
        }
        for (const auto& event : response.events())
        {
            if (event.has_file_updated())
            {
                handleFileUpdated(event.file_updated());
            }
        }
        if (requested && response.events().empty() && m_stateStore.hasGap())
        {
            emit errorRaised(QStringLiteral("server sync stream has an unresolved gap"));
        }
        else if (response.has_more() || m_stateStore.hasGap())
        {
            sendSyncRequest(m_global_cursor);
        }
        else if (requested)
        {
            m_messageSyncReady = true;
            pumpMessageOutbox();
        }
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
        handleMessageResult(QString::fromStdString(envelope.request_id()), false, error.code(),
            QString::fromStdString(error.message()), QString());
        const QString message = error.message().empty()
            ? QStringLiteral("server error")
            : QString::fromStdString(error.message());
        emit errorRaised(message);
        return;
    }
}

bool MiniImSessionManager::applySyncEvents(const QVector<MiniImStateEvent>& events)
{
    QVector<MiniImStateEvent> applied;
    if (!m_stateStore.apply(events, &applied))
    {
        emit errorRaised(m_stateStore.errorString());
        disconnectFromServer();
        return false;
    }
    m_global_cursor = m_stateStore.cursor();
    for (const auto& event : applied)
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
    publishMessageSends();
    emit syncProgress({{"globalCursor", QVariant::fromValue(m_global_cursor)}, {"hasGap", m_stateStore.hasGap()}});
    return true;
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
    if (m_connection == nullptr)
    {
        m_recovery.connectionLost();
    }
}
