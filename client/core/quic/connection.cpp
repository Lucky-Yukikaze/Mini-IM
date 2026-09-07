#include "core/quic/connection.h"
#include "core/file/uploadstream.h"

#include <QMetaObject>
#include <QMutex>
#include <QMutexLocker>
#include <QSet>
#include <cstring>

namespace
{
constexpr const char* kAlpn = "mini-im";
constexpr quint64 kReceiveBatchSize = 64 * 1024;

struct SendContext
{
    QByteArray bytes;
    QUIC_BUFFER buffer;
};
}

struct MiniImQuicConnection::CallbackContext
{
    MiniImQuicConnection* owner;
    const QUIC_API_TABLE* api;
    quint64 generation;
    // Peer handles arrive on the MsQuic worker before the Qt event loop sees them.
    QMutex mutex;
    QSet<HQUIC> peerStreams;
};

MiniImQuicConnection::MiniImQuicConnection(QObject* parent) : QObject(parent)
{
}

MiniImQuicConnection::~MiniImQuicConnection()
{
    shutdown();
    release();
}

bool MiniImQuicConnection::isActive() const
{
    return m_connection != nullptr;
}

bool MiniImQuicConnection::open(const QString& host, quint16 port)
{
    if (isActive() || !initialize())
    {
        return false;
    }
    m_stopping = false;
    m_callbackContext = std::make_unique<CallbackContext>();
    m_callbackContext->owner = this;
    m_callbackContext->api = m_api;
    m_callbackContext->generation = m_generation;
    auto status = m_api->ConnectionOpen(
        m_registration, handleConnectionEvent, m_callbackContext.get(), &m_connection);
    if (QUIC_FAILED(status))
    {
        m_connection = nullptr;
        m_callbackContext.reset();
        emit errorRaised(QStringLiteral("msquic connection open failed: %1").arg(status));
        return false;
    }
    const auto hostname = host.toUtf8();
    status = m_api->ConnectionStart(
        m_connection, m_configuration, QUIC_ADDRESS_FAMILY_UNSPEC, hostname.constData(), port);
    if (QUIC_FAILED(status))
    {
        closeConnection();
        emit errorRaised(QStringLiteral("msquic connection start failed: %1").arg(status));
        return false;
    }
    return true;
}

void MiniImQuicConnection::shutdown()
{
    if (!isActive() || m_stopping)
    {
        return;
    }
    m_stopping = true;
    const auto pendingStreams = m_pendingReceives.keys();
    for (const auto streamId : pendingStreams)
    {
        completeFileReceive(streamId, true);
    }
    m_api->ConnectionShutdown(m_connection, QUIC_CONNECTION_SHUTDOWN_FLAG_NONE, 0);
}

bool MiniImQuicConnection::sendControl(const QByteArray& frame)
{
    if (m_stopping || m_controlStream == nullptr || frame.isEmpty())
    {
        return false;
    }
    auto* context = new SendContext;
    context->bytes = frame;
    context->buffer.Length = static_cast<uint32_t>(frame.size());
    context->buffer.Buffer = reinterpret_cast<uint8_t*>(context->bytes.data());
    const auto status = m_api->StreamSend(m_controlStream, &context->buffer, 1, QUIC_SEND_FLAG_NONE, context);
    if (QUIC_FAILED(status))
    {
        delete context;
        emit errorRaised(QStringLiteral("control send failed: %1").arg(status));
        return false;
    }
    return true;
}

bool MiniImQuicConnection::sendFile(
    const QString& fileId, const QString& path, quint64 offset, quint64 fileSize)
{
    if (!isActive() || m_stopping || m_uploads.contains(fileId))
    {
        return false;
    }
    auto* sender = new MiniImUploadStream(m_api, m_connection, path, fileSize, this);
    const auto generation = m_generation;
    QObject::connect(sender, &MiniImUploadStream::finished, this,
        [this, sender, fileId, generation](bool success, const QString& error)
        {
            m_uploads.remove(fileId);
            sender->deleteLater();
            if (generation == m_generation && !m_stopping)
            {
                emit uploadFinished(fileId, success, error);
            }
        });
    m_uploads.insert(fileId, sender);
    if (!sender->start(fileId, offset))
    {
        // Keep the sender alive until connection shutdown has stopped its callbacks.
        shutdown();
        return false;
    }
    return true;
}

void MiniImQuicConnection::completeFileReceive(quint64 streamId, bool abort)
{
    auto pending = m_pendingReceives.find(streamId);
    if (pending == m_pendingReceives.end())
    {
        return;
    }
    const auto batch = pending.value();
    m_pendingReceives.erase(pending);
    m_api->StreamReceiveComplete(batch.stream, batch.bytes);
    if (abort)
    {
        m_api->StreamShutdown(batch.stream, QUIC_STREAM_SHUTDOWN_FLAG_ABORT_RECEIVE, 0x1004);
    }
    else if (!m_stopping)
    {
        m_api->StreamReceiveSetEnabled(batch.stream, TRUE);
    }
}

void MiniImQuicConnection::receive(HQUIC stream, quint64 streamId, const QByteArray& payload)
{
    if (m_stopping)
    {
        m_api->StreamReceiveComplete(stream, static_cast<quint64>(payload.size()));
        return;
    }
    if (stream == m_controlStream)
    {
        emit controlData(payload);
        m_api->StreamReceiveComplete(stream, static_cast<quint64>(payload.size()));
        if (!m_stopping)
        {
            m_api->StreamReceiveSetEnabled(stream, TRUE);
        }
        return;
    }
    m_pendingReceives.insert(streamId, {stream, static_cast<quint64>(payload.size())});
    emit fileData(streamId, payload);
}

void MiniImQuicConnection::openControlStream()
{
    if (m_stopping || !isActive() || m_controlStream != nullptr)
    {
        return;
    }
    auto status = m_api->StreamOpen(
        m_connection, QUIC_STREAM_OPEN_FLAG_NONE, handleStreamEvent, m_callbackContext.get(), &m_controlStream);
    if (QUIC_FAILED(status))
    {
        m_controlStream = nullptr;
        interruptControl(QStringLiteral("stream open failed: %1").arg(status));
        return;
    }
    status = m_api->StreamStart(m_controlStream, QUIC_STREAM_START_FLAG_IMMEDIATE);
    if (QUIC_FAILED(status))
    {
        m_api->StreamClose(m_controlStream);
        m_controlStream = nullptr;
        interruptControl(QStringLiteral("stream start failed: %1").arg(status));
        return;
    }
    emit connected();
}

void MiniImQuicConnection::interruptControl(const QString& error)
{
    if (!m_stopping)
    {
        emit errorRaised(error);
        shutdown();
    }
}

void MiniImQuicConnection::streamEnded(HQUIC stream, quint64 streamId)
{
    if (stream == m_controlStream)
    {
        interruptControl(QStringLiteral("control stream closed"));
    }
    else if (!m_stopping)
    {
        emit fileEnded(streamId);
    }
}

void MiniImQuicConnection::streamClosed(HQUIC stream, quint64 streamId, bool connectionShutdown)
{
    if (stream == m_controlStream)
    {
        m_controlStream = nullptr;
        m_api->StreamClose(stream);
        if (!connectionShutdown)
        {
            interruptControl(QStringLiteral("control stream interrupted"));
        }
        return;
    }
    m_pendingReceives.remove(streamId);
    {
        QMutexLocker lock(&m_callbackContext->mutex);
        m_callbackContext->peerStreams.remove(stream);
    }
    m_api->StreamClose(stream);
    if (!m_stopping)
    {
        emit fileClosed(streamId, connectionShutdown);
    }
}

QUIC_STATUS QUIC_API MiniImQuicConnection::handleConnectionEvent(
    HQUIC, void* context, QUIC_CONNECTION_EVENT* event)
{
    auto* callback = static_cast<CallbackContext*>(context);
    auto* owner = callback->owner;
    const auto generation = callback->generation;
    switch (event->Type)
    {
    case QUIC_CONNECTION_EVENT_CONNECTED:
        QMetaObject::invokeMethod(owner, [owner, generation]()
        {
            if (generation == owner->m_generation)
            {
                owner->openControlStream();
            }
        }, Qt::QueuedConnection);
        break;
    case QUIC_CONNECTION_EVENT_PEER_STREAM_STARTED:
    {
        const auto stream = event->PEER_STREAM_STARTED.Stream;
        {
            QMutexLocker lock(&callback->mutex);
            callback->peerStreams.insert(stream);
        }
        callback->api->SetCallbackHandler(stream, reinterpret_cast<void*>(handleStreamEvent), context);
        break;
    }
    case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_TRANSPORT:
    {
        const auto status = event->SHUTDOWN_INITIATED_BY_TRANSPORT.Status;
        QMetaObject::invokeMethod(owner, [owner, generation, status]()
        {
            if (generation == owner->m_generation)
            {
                emit owner->errorRaised(QStringLiteral("transport shutdown: %1").arg(status));
            }
        }, Qt::QueuedConnection);
        break;
    }
    case QUIC_CONNECTION_EVENT_SHUTDOWN_COMPLETE:
        QMetaObject::invokeMethod(owner, [owner, generation]()
        {
            if (generation == owner->m_generation)
            {
                owner->closeConnection();
                emit owner->disconnected();
            }
        }, Qt::QueuedConnection);
        break;
    default:
        break;
    }
    return QUIC_STATUS_SUCCESS;
}

QUIC_STATUS QUIC_API MiniImQuicConnection::handleStreamEvent(
    HQUIC stream, void* context, QUIC_STREAM_EVENT* event)
{
    const auto* callback = static_cast<CallbackContext*>(context);
    auto* owner = callback->owner;
    const auto generation = callback->generation;
    if (event->Type == QUIC_STREAM_EVENT_SEND_COMPLETE)
    {
        delete static_cast<SendContext*>(event->SEND_COMPLETE.ClientContext);
        return QUIC_STATUS_SUCCESS;
    }
    if (event->Type == QUIC_STREAM_EVENT_PEER_SEND_ABORTED
        || event->Type == QUIC_STREAM_EVENT_PEER_RECEIVE_ABORTED)
    {
        QMetaObject::invokeMethod(owner, [owner, generation, stream]()
        {
            if (generation == owner->m_generation && stream == owner->m_controlStream)
            {
                owner->interruptControl(QStringLiteral("control stream aborted"));
            }
        }, Qt::QueuedConnection);
        return QUIC_STATUS_SUCCESS;
    }
    if (event->Type != QUIC_STREAM_EVENT_RECEIVE && event->Type != QUIC_STREAM_EVENT_PEER_SEND_SHUTDOWN
        && event->Type != QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE)
    {
        return QUIC_STATUS_SUCCESS;
    }
    quint64 streamId = 0;
    uint32_t length = sizeof(streamId);
    const auto status = callback->api->GetParam(stream, QUIC_PARAM_STREAM_ID, &length, &streamId);
    if (QUIC_FAILED(status))
    {
        return status;
    }
    if (event->Type == QUIC_STREAM_EVENT_RECEIVE)
    {
        return queueReceive(stream, *callback, streamId, *event);
    }
    const bool ended = event->Type == QUIC_STREAM_EVENT_PEER_SEND_SHUTDOWN;
    const bool connectionShutdown = !ended && event->SHUTDOWN_COMPLETE.ConnectionShutdown;
    QMetaObject::invokeMethod(owner, [owner, generation, stream, streamId, ended, connectionShutdown]()
    {
        if (generation == owner->m_generation)
        {
            if (ended)
            {
                owner->streamEnded(stream, streamId);
            }
            else
            {
                owner->streamClosed(stream, streamId, connectionShutdown);
            }
        }
    }, Qt::QueuedConnection);
    return QUIC_STATUS_SUCCESS;
}

QUIC_STATUS MiniImQuicConnection::queueReceive(
    HQUIC stream, const CallbackContext& callback, quint64 streamId, const QUIC_STREAM_EVENT& event)
{
    auto* owner = callback.owner;
    const auto generation = callback.generation;
    const auto total = qMin<quint64>(event.RECEIVE.TotalBufferLength, kReceiveBatchSize);
    if (total == 0)
    {
        return QUIC_STATUS_SUCCESS;
    }
    QByteArray payload;
    payload.reserve(static_cast<qsizetype>(total));
    for (uint32_t index = 0; index < event.RECEIVE.BufferCount && payload.size() < total; ++index)
    {
        const auto& buffer = event.RECEIVE.Buffers[index];
        const auto count = qMin<quint64>(buffer.Length, total - payload.size());
        payload.append(reinterpret_cast<const char*>(buffer.Buffer), static_cast<qsizetype>(count));
    }
    QMetaObject::invokeMethod(owner, [owner, generation, stream, streamId, payload]()
    {
        if (generation == owner->m_generation)
        {
            owner->receive(stream, streamId, payload);
        }
    }, Qt::QueuedConnection);
    return QUIC_STATUS_PENDING;
}

bool MiniImQuicConnection::initialize()
{
    if (m_api != nullptr)
    {
        return true;
    }
    if (QUIC_FAILED(MsQuicOpen2(&m_api)))
    {
        m_api = nullptr;
        return false;
    }
    const QUIC_REGISTRATION_CONFIG registration = {"mini-im-client", QUIC_EXECUTION_PROFILE_LOW_LATENCY};
    if (QUIC_FAILED(m_api->RegistrationOpen(&registration, &m_registration)))
    {
        release();
        return false;
    }
    QUIC_BUFFER alpn;
    alpn.Length = static_cast<uint32_t>(std::strlen(kAlpn));
    alpn.Buffer = reinterpret_cast<uint8_t*>(const_cast<char*>(kAlpn));
    QUIC_SETTINGS settings = {};
    settings.IsSet.PeerUnidiStreamCount = TRUE;
    settings.PeerUnidiStreamCount = 16;
    settings.IsSet.SendBufferingEnabled = TRUE;
    settings.SendBufferingEnabled = FALSE;
    settings.IsSet.StreamRecvBufferDefault = TRUE;
    settings.StreamRecvBufferDefault = static_cast<uint32_t>(kReceiveBatchSize);
    settings.IsSet.StreamRecvWindowDefault = TRUE;
    settings.StreamRecvWindowDefault = static_cast<uint32_t>(kReceiveBatchSize);
    if (QUIC_FAILED(m_api->ConfigurationOpen(
            m_registration, &alpn, 1, &settings, sizeof(settings), nullptr, &m_configuration)))
    {
        release();
        return false;
    }
    QUIC_CREDENTIAL_CONFIG credentials = {};
    credentials.Type = QUIC_CREDENTIAL_TYPE_NONE;
    credentials.Flags = QUIC_CREDENTIAL_FLAG_CLIENT | QUIC_CREDENTIAL_FLAG_NO_CERTIFICATE_VALIDATION;
    if (QUIC_FAILED(m_api->ConfigurationLoadCredential(m_configuration, &credentials)))
    {
        release();
        return false;
    }
    return true;
}

void MiniImQuicConnection::closeConnection()
{
    m_stopping = true;
    ++m_generation;
    if (m_connection != nullptr)
    {
        // This synchronous close stops worker callbacks and shuts down associated streams.
        // Keep contexts and stream owners alive until it returns, including during destruction.
        m_api->ConnectionClose(m_connection);
        m_connection = nullptr;
    }
    qDeleteAll(m_uploads);
    m_uploads.clear();
    m_pendingReceives.clear();
    if (m_controlStream != nullptr)
    {
        m_api->StreamClose(m_controlStream);
        m_controlStream = nullptr;
    }
    if (m_callbackContext)
    {
        for (const auto stream : m_callbackContext->peerStreams)
        {
            m_api->StreamClose(stream);
        }
        m_callbackContext.reset();
    }
}

void MiniImQuicConnection::release()
{
    closeConnection();
    if (m_configuration != nullptr)
    {
        m_api->ConfigurationClose(m_configuration);
        m_configuration = nullptr;
    }
    if (m_registration != nullptr)
    {
        m_api->RegistrationClose(m_registration);
        m_registration = nullptr;
    }
    if (m_api != nullptr)
    {
        MsQuicClose(m_api);
        m_api = nullptr;
    }
}
