#include "core/file/uploadstream.h"

#include <QByteArray>
#include <QMetaObject>

namespace
{
constexpr qint64 kChunkSize = 64 * 1024;
constexpr int kMaxInFlight = 2;

struct SendContext
{
    QByteArray bytes;
    QUIC_BUFFER buffer;
};
}

MiniImUploadStream::MiniImUploadStream(
    const QUIC_API_TABLE* api, HQUIC connection, const QString& path, quint64 expectedSize, QObject* parent)
    : QObject(parent), m_api(api), m_connection(connection), m_file(path), m_expectedSize(expectedSize)
{
}

MiniImUploadStream::~MiniImUploadStream()
{
    if (m_stream != nullptr)
    {
        m_api->StreamClose(m_stream);
    }
}

bool MiniImUploadStream::start(const QString& fileId, quint64 offset)
{
    if (m_stream != nullptr || fileId.isEmpty() || !m_file.open(QIODevice::ReadOnly))
    {
        return false;
    }
    if (static_cast<quint64>(m_file.size()) != m_expectedSize
        || offset > m_expectedSize || !m_file.seek(static_cast<qint64>(offset)))
    {
        return false;
    }
    if (QUIC_FAILED(m_api->StreamOpen(
            m_connection, QUIC_STREAM_OPEN_FLAG_UNIDIRECTIONAL, handleEvent, this, &m_stream)))
    {
        return false;
    }
    if (QUIC_FAILED(m_api->StreamStart(m_stream, QUIC_STREAM_START_FLAG_IMMEDIATE)))
    {
        return false;
    }
    if (!send(QByteArrayLiteral("MINIIMFILE1 ") + fileId.toUtf8() + '\n'))
    {
        abort(QStringLiteral("failed to send upload header"));
        return false;
    }
    pump();
    return m_error.isEmpty();
}

bool MiniImUploadStream::send(const QByteArray& bytes)
{
    auto* context = new SendContext;
    context->bytes = bytes;
    context->buffer.Length = static_cast<uint32_t>(bytes.size());
    context->buffer.Buffer = reinterpret_cast<uint8_t*>(context->bytes.data());
    const QUIC_STATUS status = m_api->StreamSend(m_stream, &context->buffer, 1, QUIC_SEND_FLAG_NONE, context);
    if (QUIC_FAILED(status))
    {
        delete context;
        return false;
    }
    ++m_inFlight;
    return true;
}

void MiniImUploadStream::pump()
{
    if (m_stream == nullptr || m_ending)
    {
        return;
    }
    while (m_inFlight < kMaxInFlight)
    {
        if (static_cast<quint64>(m_file.size()) != m_expectedSize)
        {
            abort(QStringLiteral("upload file size changed"));
            return;
        }
        if (m_file.atEnd())
        {
            m_ending = true;
            if (QUIC_FAILED(m_api->StreamShutdown(m_stream, QUIC_STREAM_SHUTDOWN_FLAG_GRACEFUL, 0)))
            {
                abort(QStringLiteral("failed to finish upload stream"));
            }
            return;
        }
        const QByteArray chunk = m_file.read(kChunkSize);
        if (chunk.isEmpty() || !send(chunk))
        {
            abort(QStringLiteral("failed to read or send upload chunk"));
            return;
        }
    }
}

void MiniImUploadStream::abort(const QString& error)
{
    m_error = error;
    m_ending = true;
    m_api->StreamShutdown(m_stream, QUIC_STREAM_SHUTDOWN_FLAG_ABORT_SEND, 0);
}

QUIC_STATUS QUIC_API MiniImUploadStream::handleEvent(HQUIC, void* context, QUIC_STREAM_EVENT* event)
{
    auto* sender = static_cast<MiniImUploadStream*>(context);
    if (event->Type == QUIC_STREAM_EVENT_SEND_COMPLETE)
    {
        delete static_cast<SendContext*>(event->SEND_COMPLETE.ClientContext);
        const bool canceled = event->SEND_COMPLETE.Canceled;
        QMetaObject::invokeMethod(sender, [sender, canceled]()
        {
            --sender->m_inFlight;
            if (canceled)
            {
                sender->m_ending = true;
                sender->m_error = QStringLiteral("upload stream interrupted");
            }
            sender->pump();
        }, Qt::QueuedConnection);
    }
    else if (event->Type == QUIC_STREAM_EVENT_SEND_SHUTDOWN_COMPLETE)
    {
        const bool graceful = event->SEND_SHUTDOWN_COMPLETE.Graceful;
        QMetaObject::invokeMethod(sender, [sender, graceful]() { sender->m_graceful = graceful; }, Qt::QueuedConnection);
    }
    else if (event->Type == QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE)
    {
        const bool connectionShutdown = event->SHUTDOWN_COMPLETE.ConnectionShutdown;
        QMetaObject::invokeMethod(sender, [sender, connectionShutdown]()
        {
            HQUIC stream = sender->m_stream;
            sender->m_stream = nullptr;
            sender->m_api->StreamClose(stream);
            sender->m_file.close();
            const bool success = sender->m_graceful && !connectionShutdown && sender->m_error.isEmpty();
            emit sender->finished(success, success ? QString() : QStringLiteral("upload stream interrupted: ")
                + sender->m_error);
        }, Qt::QueuedConnection);
    }
    return QUIC_STATUS_SUCCESS;
}
