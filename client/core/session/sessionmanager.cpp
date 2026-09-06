#include "core/session/sessionmanager.h"

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

#include <QtEndian>

#include <cstring>
#include <string>

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

struct StreamSendContext
{
    QUIC_BUFFER buffer;
    uint8_t* data = nullptr;
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

QVariantMap BuildMessagePayload(const im::message::Message& message)
{
    const std::string content = message.content();
    QVariantMap item;
    item.insert(QStringLiteral("id"), QString::fromStdString(message.message_id()));
    item.insert(QStringLiteral("conversationId"), QString::fromStdString(message.conversation_id()));
    item.insert(QStringLiteral("senderId"), QString::fromStdString(message.sender_id()));
    item.insert(QStringLiteral("clientMsgId"), QString::fromStdString(message.client_msg_id()));
    item.insert(QStringLiteral("seq"), static_cast<qulonglong>(message.seq()));
    item.insert(QStringLiteral("text"), QString::fromUtf8(content.data(), static_cast<int>(content.size())));
    item.insert(QStringLiteral("createdAtMs"), static_cast<qlonglong>(message.created_at_ms()));
    item.insert(QStringLiteral("recalled"), message.recalled());
    item.insert(QStringLiteral("unreadCount"), static_cast<uint>(message.unread_count()));
    item.insert(QStringLiteral("burnMode"), static_cast<uint>(message.burn_mode()));
    item.insert(QStringLiteral("burnTtlSec"), static_cast<uint>(message.burn_ttl_sec()));
    return item;
}

QVariantMap BuildConversationPayload(const im::conversation::ConversationUpdated& updated)
{
    QVariantMap payload;
    QVariantList member_ids;
    for (const auto& member_id : updated.member_ids())
    {
        member_ids.append(QString::fromStdString(member_id));
    }
    payload.insert(QStringLiteral("eventId"), QString::fromStdString(updated.event_id()));
    payload.insert(QStringLiteral("conversationId"), QString::fromStdString(updated.conversation_id()));
    payload.insert(QStringLiteral("updatedAtMs"), static_cast<qlonglong>(updated.updated_at_ms()));
    payload.insert(QStringLiteral("title"), QString::fromStdString(updated.title()));
    payload.insert(QStringLiteral("type"), updated.type() == im::common::CONVERSATION_GROUP ? QStringLiteral("group") : QStringLiteral("direct"));
    payload.insert(QStringLiteral("ownerId"), QString::fromStdString(updated.owner_id()));
    payload.insert(QStringLiteral("memberIds"), member_ids);
    return payload;
}

QVariantMap BuildReceiptPayload(const im::message::Receipt& receipt)
{
    QVariantMap payload;
    payload.insert(QStringLiteral("type"), QStringLiteral("receipt"));
    payload.insert(QStringLiteral("eventId"), QString::fromStdString(receipt.event_id()));
    payload.insert(QStringLiteral("conversationId"), QString::fromStdString(receipt.conversation_id()));
    payload.insert(QStringLiteral("lastReadSeq"), static_cast<qulonglong>(receipt.last_read_seq()));
    payload.insert(QStringLiteral("readAtMs"), static_cast<qlonglong>(receipt.read_at_ms()));
    payload.insert(QStringLiteral("readerId"), QString::fromStdString(receipt.reader_id()));
    return payload;
}

QVariantMap BuildRecallPayload(const im::message::Recall& recall)
{
    QVariantMap payload;
    const QString operator_id = QString::fromStdString(recall.operator_id());
    payload.insert(
        QStringLiteral("type"),
        operator_id == QStringLiteral("system-burn") ? QStringLiteral("burn") : QStringLiteral("recall"));
    payload.insert(QStringLiteral("eventId"), QString::fromStdString(recall.event_id()));
    payload.insert(QStringLiteral("conversationId"), QString::fromStdString(recall.conversation_id()));
    payload.insert(QStringLiteral("messageId"), QString::fromStdString(recall.message_id()));
    payload.insert(QStringLiteral("tsMs"), static_cast<qlonglong>(recall.ts_ms()));
    payload.insert(QStringLiteral("operatorId"), operator_id);
    return payload;
}

QVariantMap BuildFileProgressPayload(
    const std::string& event_id,
    const std::string& file_id,
    const std::string& conversation_id,
    quint64 transferred_bytes,
    bool completed,
    quint64 version,
    qlonglong updated_at_ms)
{
    QVariantMap payload;
    payload.insert(QStringLiteral("eventId"), QString::fromStdString(event_id));
    payload.insert(QStringLiteral("fileId"), QString::fromStdString(file_id));
    payload.insert(QStringLiteral("conversationId"), QString::fromStdString(conversation_id));
    payload.insert(QStringLiteral("transferredBytes"), static_cast<qulonglong>(transferred_bytes));
    payload.insert(QStringLiteral("completed"), completed);
    payload.insert(QStringLiteral("version"), static_cast<qulonglong>(version));
    payload.insert(QStringLiteral("updatedAtMs"), updated_at_ms);
    return payload;
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

    m_endpoint = endpoint;
    m_token = token;
    m_device_id = device_id;
    m_resume_session_id = resume_session_id;
    m_last_acked_request_id = last_acked_request_id;
    m_global_cursor = global_cursor;
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
    if (!parseEndpoint(endpoint, &host, &port))
    {
        AppendClientLog(QStringLiteral("parseEndpoint failed: %1").arg(endpoint));
        emitConnectionError(QStringLiteral("invalid endpoint"));
        return false;
    }
    AppendClientLog(QStringLiteral("endpoint parsed host=%1 port=%2").arg(host).arg(port));

    const QUIC_STATUS open_status = m_msquic->ConnectionOpen(
        m_registration,
        &MiniImSessionManager::handleConnectionEvent,
        this,
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
        m_msquic->ConnectionClose(m_connection);
        m_connection = nullptr;
        AppendClientLog(QStringLiteral("ConnectionStart failed status=%1").arg(start_status));
        emitConnectionError(QStringLiteral("msquic connection start failed: %1").arg(start_status));
        return false;
    }
    AppendClientLog(QStringLiteral("ConnectionStart ok"));

    return true;
}

void MiniImSessionManager::disconnectFromServer()
{
    m_heartbeat_timer.stop();
    resetRuntimeState();
    if (m_connection != nullptr && m_msquic != nullptr)
    {
        m_msquic->ConnectionShutdown(m_connection, QUIC_CONNECTION_SHUTDOWN_FLAG_NONE, 0);
        return;
    }

    if (m_connected || m_connecting)
    {
        m_connected = false;
        m_connecting = false;
        emit connectionChanged(QStringLiteral("disconnected"), m_session_id);
    }
}

void MiniImSessionManager::onHeartbeatTimeout()
{
    if (!m_connected)
    {
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
    auto* manager = static_cast<MiniImSessionManager*>(context);
    if (manager == nullptr)
    {
        return QUIC_STATUS_INTERNAL_ERROR;
    }

    switch (event->Type)
    {
    case QUIC_CONNECTION_EVENT_CONNECTED:
    {
        AppendClientLog(QStringLiteral("connection event: CONNECTED"));
        QMetaObject::invokeMethod(
            manager,
            [manager]()
            {
                AppendClientLog(QStringLiteral("queued CONNECTED handler begin"));
                if (manager->m_connection == nullptr || manager->m_stream != nullptr)
                {
                    AppendClientLog(QStringLiteral("queued CONNECTED handler skipped"));
                    return;
                }

                const QUIC_STATUS open_status = manager->m_msquic->StreamOpen(
                    manager->m_connection,
                    QUIC_STREAM_OPEN_FLAG_NONE,
                    &MiniImSessionManager::handleStreamEvent,
                    manager,
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
                manager);
        }
        break;
    }
    case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_TRANSPORT:
    {
        const uint64_t error_code = event->SHUTDOWN_INITIATED_BY_TRANSPORT.Status;
        AppendClientLog(QStringLiteral("connection event: TRANSPORT_SHUTDOWN status=%1").arg(error_code));
        QMetaObject::invokeMethod(
            manager,
            [manager, error_code]()
            {
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
            [manager]()
            {
                AppendClientLog(QStringLiteral("queued SHUTDOWN_COMPLETE handler begin"));
                manager->m_heartbeat_timer.stop();
                manager->resetRuntimeState();
                manager->closeConnectionHandle();
                const bool should_emit = manager->m_connected || manager->m_connecting;
                manager->m_connected = false;
                manager->m_connecting = false;
                manager->m_hello_sent = false;
                if (should_emit)
                {
                    emit manager->connectionChanged(QStringLiteral("disconnected"), manager->m_session_id);
                }
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
    auto* manager = static_cast<MiniImSessionManager*>(context);
    if (manager == nullptr)
    {
        return QUIC_STATUS_INTERNAL_ERROR;
    }

    switch (event->Type)
    {
    case QUIC_STREAM_EVENT_RECEIVE:
    {
        AppendClientLog(QStringLiteral("stream event: RECEIVE len=%1").arg(event->RECEIVE.TotalBufferLength));
        const uint32_t total_len = event->RECEIVE.TotalBufferLength;
        if (total_len == 0)
        {
            break;
        }

        QByteArray payload;
        payload.resize(static_cast<int>(total_len));
        uint32_t offset = 0;
        for (uint32_t index = 0; index < event->RECEIVE.BufferCount; ++index)
        {
            const QUIC_BUFFER& buffer = event->RECEIVE.Buffers[index];
            std::memcpy(payload.data() + offset, buffer.Buffer, buffer.Length);
            offset += buffer.Length;
        }

        QMetaObject::invokeMethod(
            manager,
            [manager, payload, stream]()
            {
                AppendClientLog(
                    QStringLiteral("queued stream RECEIVE handler stream=%1 size=%2 control=%3")
                        .arg(reinterpret_cast<quintptr>(stream))
                        .arg(payload.size())
                        .arg(manager->m_stream == stream ? 1 : 0));
                if (manager->m_stream == stream)
                {
                    manager->handleIncomingControlStreamData(payload);
                    return;
                }
                manager->handleIncomingFileStream(stream, payload);
            },
            Qt::QueuedConnection);
        break;
    }
    case QUIC_STREAM_EVENT_SEND_COMPLETE:
    {
        AppendClientLog(QStringLiteral("stream event: SEND_COMPLETE canceled=%1").arg(event->SEND_COMPLETE.Canceled ? 1 : 0));
        auto* send_context = static_cast<StreamSendContext*>(event->SEND_COMPLETE.ClientContext);
        if (send_context != nullptr)
        {
            delete[] send_context->data;
            delete send_context;
        }
        break;
    }
    case QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE:
    {
        AppendClientLog(QStringLiteral("stream event: SHUTDOWN_COMPLETE"));
        QMetaObject::invokeMethod(
            manager,
            [manager, stream]()
            {
                AppendClientLog(QStringLiteral("queued stream SHUTDOWN_COMPLETE handler begin"));
                if (manager->m_stream == stream && manager->m_msquic != nullptr)
                {
                    manager->closeControlStreamHandle();
                    manager->m_stream = nullptr;
                    return;
                }
                if (manager->m_msquic == nullptr)
                {
                    return;
                }
                const quintptr key = reinterpret_cast<quintptr>(stream);
                const auto it = manager->m_file_stream_to_file_id.find(key);
                if (it != manager->m_file_stream_to_file_id.end())
                {
                    const QString file_id = it.value();
                    manager->m_file_stream_to_file_id.erase(it);
                    manager->m_msquic->StreamClose(stream);
                    if (!manager->sendFileFinish(file_id, true))
                    {
                        emit manager->errorRaised(QStringLiteral("failed to send file finish"));
                    }
                    manager->m_pending_file_uploads.remove(file_id);
                }
                else
                {
                    auto state_it = manager->m_download_stream_states.find(key);
                    if (state_it != manager->m_download_stream_states.end() && !state_it->file_id.isEmpty())
                    {
                        manager->flushPendingDownloadBuffers(state_it->file_id);
                    }
                    manager->m_download_stream_states.remove(key);
                    manager->m_msquic->StreamClose(stream);
                }
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
    m_control_stream_buffer.clear();
    m_file_stream_to_file_id.clear();
    m_download_stream_states.clear();
    m_latest_file_versions.clear();
    m_pending_file_init_requests.clear();
    m_pending_file_uploads.clear();
    m_pending_file_download_init_requests.clear();
    m_pending_file_downloads.clear();
    m_failed_file_downloads.clear();
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
    send_context->data = new uint8_t[static_cast<size_t>(frame.size())];
    std::memcpy(send_context->data, frame.constData(), static_cast<size_t>(frame.size()));
    send_context->buffer.Length = static_cast<uint32_t>(frame.size());
    send_context->buffer.Buffer = send_context->data;

    const QUIC_STATUS status = m_msquic->StreamSend(
        m_stream,
        &send_context->buffer,
        1,
        QUIC_SEND_FLAG_NONE,
        send_context);
    if (QUIC_FAILED(status))
    {
        delete[] send_context->data;
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

    im::envelope::Envelope envelope;
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    const QString request_id = makeRequestId(QStringLiteral("msg"));
    const QByteArray utf8_text = text.toUtf8();

    envelope.set_version(1);
    envelope.set_request_id(request_id.toStdString());
    envelope.set_channel(im::common::CHANNEL_CONTROL);
    envelope.set_session_id(m_session_id.toStdString());
    envelope.set_device_id(m_device_id.toStdString());
    envelope.set_seq(++m_seq);
    envelope.set_client_time_ms(now_ms);
    envelope.set_trace_id(request_id.toStdString());

    auto* send_message = envelope.mutable_send_message();
    send_message->set_conversation_id(conversation_id.toStdString());
    send_message->set_client_msg_id(client_msg_id.toStdString());
    send_message->set_type(im::common::MSG_TEXT);
    send_message->set_content(utf8_text.constData(), utf8_text.size());
    send_message->set_burn_mode(static_cast<unsigned int>(burn_mode));
    send_message->set_burn_ttl_sec(static_cast<unsigned int>(burn_ttl_sec));

    return sendEnvelope(envelope.SerializeAsString());
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

    return sendEnvelope(envelope.SerializeAsString());
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

bool MiniImSessionManager::sendFileFinish(const QString& file_id, bool success)
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

    return sendEnvelope(envelope.SerializeAsString());
}

bool MiniImSessionManager::sendFileStreamData(const QString& file_id, const QString& file_path, quint64 offset)
{
    if (m_connection == nullptr || m_msquic == nullptr || file_id.trimmed().isEmpty() || file_path.trimmed().isEmpty())
    {
        return false;
    }

    HQUIC file_stream = nullptr;
    const QUIC_STATUS open_status = m_msquic->StreamOpen(
        m_connection,
        QUIC_STREAM_OPEN_FLAG_UNIDIRECTIONAL,
        &MiniImSessionManager::handleStreamEvent,
        this,
        &file_stream);
    if (QUIC_FAILED(open_status) || file_stream == nullptr)
    {
        return false;
    }

    const QUIC_STATUS start_status = m_msquic->StreamStart(file_stream, QUIC_STREAM_START_FLAG_IMMEDIATE);
    if (QUIC_FAILED(start_status))
    {
        m_msquic->StreamClose(file_stream);
        return false;
    }

    m_file_stream_to_file_id.insert(reinterpret_cast<quintptr>(file_stream), file_id);

    auto send_bytes = [this, file_stream](const QByteArray& bytes) -> bool
    {
        if (bytes.isEmpty())
        {
            return true;
        }
        auto* send_context = new StreamSendContext;
        send_context->data = new uint8_t[static_cast<size_t>(bytes.size())];
        std::memcpy(send_context->data, bytes.constData(), static_cast<size_t>(bytes.size()));
        send_context->buffer.Length = static_cast<uint32_t>(bytes.size());
        send_context->buffer.Buffer = send_context->data;
        const QUIC_STATUS status = m_msquic->StreamSend(
            file_stream,
            &send_context->buffer,
            1,
            QUIC_SEND_FLAG_NONE,
            send_context);
        if (QUIC_FAILED(status))
        {
            delete[] send_context->data;
            delete send_context;
            return false;
        }
        return true;
    };

    const QByteArray header = QByteArrayLiteral("MINIIMFILE1 ") + file_id.toUtf8() + QByteArrayLiteral("\n");
    if (!send_bytes(header))
    {
        m_file_stream_to_file_id.remove(reinterpret_cast<quintptr>(file_stream));
        m_msquic->StreamClose(file_stream);
        return false;
    }

    QFile file(file_path);
    if (!file.open(QIODevice::ReadOnly))
    {
        m_file_stream_to_file_id.remove(reinterpret_cast<quintptr>(file_stream));
        m_msquic->StreamClose(file_stream);
        return false;
    }
    if (offset > 0 && !file.seek(static_cast<qint64>(offset)))
    {
        file.close();
        m_file_stream_to_file_id.remove(reinterpret_cast<quintptr>(file_stream));
        m_msquic->StreamClose(file_stream);
        return false;
    }

    while (!file.atEnd())
    {
        const QByteArray chunk = file.read(64 * 1024);
        if (chunk.isEmpty() && file.error() != QFileDevice::NoError)
        {
            file.close();
            m_file_stream_to_file_id.remove(reinterpret_cast<quintptr>(file_stream));
            m_msquic->StreamClose(file_stream);
            return false;
        }
        if (!chunk.isEmpty() && !send_bytes(chunk))
        {
            file.close();
            m_file_stream_to_file_id.remove(reinterpret_cast<quintptr>(file_stream));
            m_msquic->StreamClose(file_stream);
            return false;
        }
    }
    file.close();

    m_msquic->StreamShutdown(file_stream, QUIC_STREAM_SHUTDOWN_FLAG_GRACEFUL, 0);
    return true;
}

void MiniImSessionManager::handleFileUpdated(
    const std::string& event_id,
    const std::string& file_id,
    const std::string& conversation_id,
    quint64 transferred_bytes,
    bool completed,
    quint64 version,
    qlonglong updated_at_ms,
    bool check_event_id)
{
    const QString qevent_id = QString::fromStdString(event_id);
    if (check_event_id && !qevent_id.isEmpty())
    {
        if (m_seen_event_ids.contains(qevent_id))
        {
            return;
        }
        m_seen_event_ids.insert(qevent_id);
    }
    const QString qfile_id = QString::fromStdString(file_id);
    const quint64 old_version = m_latest_file_versions.value(qfile_id, 0);
    if (version > 0 && version < old_version)
    {
        return;
    }
    if (version > old_version)
    {
        m_latest_file_versions.insert(qfile_id, version);
    }
    emit fileProgress(
        BuildFileProgressPayload(event_id, file_id, conversation_id, transferred_bytes, completed, version, updated_at_ms));

    auto pending_it = m_pending_file_uploads.find(qfile_id);
    if (pending_it == m_pending_file_uploads.end() || pending_it->stream_started)
    {
        if (completed)
        {
            m_pending_file_downloads.remove(qfile_id);
        }
        return;
    }

    if (sendFileStreamData(qfile_id, pending_it->file_path, transferred_bytes))
    {
        pending_it->stream_started = true;
    }
    else
    {
        emit errorRaised(QStringLiteral("failed to send file stream"));
    }
}

void MiniImSessionManager::handleIncomingFileStream(HQUIC stream, const QByteArray& payload)
{
    const quintptr key = reinterpret_cast<quintptr>(stream);
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
            if (state.buffer.size() > 1024)
            {
                AppendClientLog(QStringLiteral("handleIncomingFileStream header too large stream=%1").arg(key));
                m_download_stream_states.remove(key);
            }
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
        return;
    }
    flushPendingDownloadBuffers(state.file_id);
}

void MiniImSessionManager::flushPendingDownloadBuffers(const QString& file_id)
{
    if (file_id.trimmed().isEmpty())
    {
        return;
    }
    auto pending_it = m_pending_file_downloads.find(file_id);
    if (pending_it == m_pending_file_downloads.end())
    {
        AppendClientLog(QStringLiteral("flushPendingDownloadBuffers pending not ready fileId=%1").arg(file_id));
        return;
    }

    for (auto it = m_download_stream_states.begin(); it != m_download_stream_states.end(); ++it)
    {
        DownloadStreamState& state = it.value();
        if (state.file_id != file_id || state.buffer.isEmpty() || m_failed_file_downloads.contains(file_id))
        {
            continue;
        }

        const QFileInfo info(pending_it->save_path);
        const QString parent_dir = info.dir().absolutePath();
        if (!QDir().mkpath(parent_dir))
        {
            m_failed_file_downloads.insert(file_id);
            AppendClientLog(
                QStringLiteral("flushPendingDownloadBuffers mkdir failed fileId=%1 dir=%2 path=%3")
                    .arg(file_id, parent_dir, pending_it->save_path));
            emit errorRaised(QStringLiteral("failed to create download directory"));
            return;
        }

        QFile output(pending_it->save_path);
        if (!output.open(QIODevice::WriteOnly | QIODevice::Append))
        {
            m_failed_file_downloads.insert(file_id);
            AppendClientLog(
                QStringLiteral("flushPendingDownloadBuffers write failed fileId=%1 path=%2 error=%3")
                    .arg(file_id, pending_it->save_path, output.errorString()));
            emit errorRaised(QStringLiteral("failed to write download file: %1").arg(pending_it->save_path));
            return;
        }
        output.write(state.buffer);
        output.close();
        AppendClientLog(
            QStringLiteral("flushPendingDownloadBuffers wrote fileId=%1 bytes=%2 path=%3")
                .arg(file_id)
                .arg(state.buffer.size())
                .arg(pending_it->save_path));
        state.buffer.clear();
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

    if (!envelope.request_id().empty())
    {
        m_last_acked_request_id = QString::fromStdString(envelope.request_id());
    }

    if (envelope.has_welcome())
    {
        AppendClientLog(QStringLiteral("handleIncomingEnvelope: welcome"));
        const auto& welcome = envelope.welcome();
        m_connected = true;
        m_connecting = false;
        m_session_id = QString::fromStdString(welcome.session_id());
        m_resume_session_id = m_session_id;
        m_global_cursor = static_cast<quint64>(welcome.global_cursor());
        m_heartbeat_interval_sec = welcome.heartbeat_interval_sec() > 0
            ? static_cast<int>(welcome.heartbeat_interval_sec())
            : kDefaultHeartbeatIntervalSec;

        emit connectionChanged(QStringLiteral("connected"), m_session_id);
        emit initialStateLoaded(buildInitialStatePayload(QString::fromStdString(welcome.user_id()), m_global_cursor));
        m_heartbeat_timer.start(m_heartbeat_interval_sec * 1000);
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
        const QString request_id = QString::fromStdString(ack.request_id());
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
                QFile output(pending.save_path);
                if (output.exists())
                {
                    output.remove();
                }
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
        AppendClientLog(QStringLiteral("handleIncomingEnvelope: file_updated"));
        const auto& updated = envelope.file_updated();
        handleFileUpdated(
            updated.event_id(),
            updated.file_id(),
            updated.conversation_id(),
            static_cast<quint64>(updated.transferred_bytes()),
            updated.completed(),
            static_cast<quint64>(updated.version()),
            static_cast<qlonglong>(updated.updated_at_ms()));
        m_global_cursor = qMax(m_global_cursor, static_cast<quint64>(envelope.seq()));
        return;
    }

    if (envelope.has_message_push())
    {
        AppendClientLog(QStringLiteral("handleIncomingEnvelope: message_push"));
        const auto& message_push = envelope.message_push();
        const QString event_id = QString::fromStdString(message_push.event_id());
        if (!event_id.isEmpty())
        {
            if (m_seen_event_ids.contains(event_id))
            {
                m_global_cursor = qMax(m_global_cursor, static_cast<quint64>(envelope.seq()));
                return;
            }
            m_seen_event_ids.insert(event_id);
        }

        for (const auto& message : message_push.messages())
        {
            emit messagePushed(BuildMessagePayload(message));
        }

        m_global_cursor = qMax(m_global_cursor, static_cast<quint64>(envelope.seq()));
        return;
    }

    if (envelope.has_sync_response())
    {
        AppendClientLog(QStringLiteral("handleIncomingEnvelope: sync_response"));
        const auto& sync_response = envelope.sync_response();
        for (const auto& event : sync_response.events())
        {
            const QString event_id = QString::fromStdString(event.event_id());
            if (!event_id.isEmpty())
            {
                if (m_seen_event_ids.contains(event_id))
                {
                    continue;
                }
                m_seen_event_ids.insert(event_id);
            }

            if (event.has_message())
            {
                emit messagePushed(BuildMessagePayload(event.message()));
                continue;
            }

            if (event.has_conversation_updated())
            {
                emit conversationUpdated(BuildConversationPayload(event.conversation_updated()));
                continue;
            }

            if (event.has_receipt())
            {
                emit messageUpdated(BuildReceiptPayload(event.receipt()));
                continue;
            }

            if (event.has_recall())
            {
                emit messageUpdated(BuildRecallPayload(event.recall()));
                continue;
            }

            if (event.has_file_updated())
            {
                const auto& updated = event.file_updated();
                handleFileUpdated(
                    updated.event_id(),
                    updated.file_id(),
                    updated.conversation_id(),
                    static_cast<quint64>(updated.transferred_bytes()),
                    updated.completed(),
                    static_cast<quint64>(updated.version()),
                    static_cast<qlonglong>(updated.updated_at_ms()),
                    false);
            }
        }

        m_global_cursor = qMax(m_global_cursor, static_cast<quint64>(sync_response.new_global_cursor()));
        m_global_cursor = qMax(m_global_cursor, static_cast<quint64>(envelope.seq()));
        if (sync_response.has_more() && !sendSyncRequest(m_global_cursor))
        {
            emit errorRaised(QStringLiteral("failed to request next sync page"));
        }
        return;
    }

    if (envelope.has_error())
    {
        AppendClientLog(QStringLiteral("handleIncomingEnvelope: error"));
        const auto& error = envelope.error();
        const QString message = error.message().empty()
            ? QStringLiteral("server error")
            : QString::fromStdString(error.message());
        emit errorRaised(message);
        return;
    }
}

QVariantMap MiniImSessionManager::buildInitialStatePayload(const QString& user_id, quint64 global_cursor) const
{
    QVariantMap payload;
    QVariantMap current_user;
    current_user.insert(QStringLiteral("userId"), user_id);
    payload.insert(QStringLiteral("currentUser"), current_user);
    payload.insert(QStringLiteral("globalCursor"), static_cast<qulonglong>(global_cursor));
    return payload;
}

QString MiniImSessionManager::makeRequestId(const QString& suffix) const
{
    const auto now_ms = QDateTime::currentMSecsSinceEpoch();
    if (suffix.isEmpty())
    {
        return QStringLiteral("%1-%2").arg(m_device_id).arg(now_ms);
    }
    return QStringLiteral("%1-%2-%3").arg(m_device_id).arg(suffix).arg(now_ms);
}

void MiniImSessionManager::emitConnectionError(const QString& message)
{
    AppendClientLog(QStringLiteral("emitConnectionError: %1").arg(message));
    m_connecting = false;
    m_connected = false;
    emit errorRaised(message);
    emit connectionChanged(QStringLiteral("error"), QString());
}
