#include "core/file/uploadstream.h"

#include <QCoreApplication>
#include <QFile>
#include <QTemporaryDir>
#include <deque>
#include <iostream>
#include <stdexcept>

namespace
{
void Require(bool condition, const char* message)
{
    if (!condition)
    {
        throw std::runtime_error(message);
    }
}

struct PendingSend
{
    QByteArray bytes;
    void* context;
};

struct FakeTransport
{
    QUIC_API_TABLE api = {};
    QUIC_STREAM_CALLBACK_HANDLER callback = nullptr;
    void* context = nullptr;
    std::deque<PendingSend> pending;
    QByteArray delivered;
    bool graceful = false;
    bool aborted = false;
    bool rejectSend = false;

    FakeTransport()
    {
        api.StreamOpen = Open;
        api.StreamStart = Start;
        api.StreamSend = Send;
        api.StreamShutdown = Shutdown;
        api.StreamClose = Close;
    }

    HQUIC handle() { return reinterpret_cast<HQUIC>(this); }

    static QUIC_STATUS QUIC_API Open(
        HQUIC connection, QUIC_STREAM_OPEN_FLAGS, QUIC_STREAM_CALLBACK_HANDLER handler, void* owner, HQUIC* stream)
    {
        auto& fake = *reinterpret_cast<FakeTransport*>(connection);
        fake.callback = handler;
        fake.context = owner;
        *stream = connection;
        return QUIC_STATUS_SUCCESS;
    }
    static QUIC_STATUS QUIC_API Start(HQUIC, QUIC_STREAM_START_FLAGS) { return QUIC_STATUS_SUCCESS; }

    static QUIC_STATUS QUIC_API Send(
        HQUIC stream, const QUIC_BUFFER* buffers, uint32_t count, QUIC_SEND_FLAGS, void* context)
    {
        auto& fake = *reinterpret_cast<FakeTransport*>(stream);
        if (fake.rejectSend)
        {
            return QUIC_STATUS_INTERNAL_ERROR;
        }
        QByteArray bytes;
        for (uint32_t index = 0; index < count; ++index)
        {
            bytes.append(reinterpret_cast<const char*>(buffers[index].Buffer), buffers[index].Length);
        }
        fake.pending.push_back({bytes, context});
        return QUIC_STATUS_SUCCESS;
    }
    static QUIC_STATUS QUIC_API Shutdown(HQUIC stream, QUIC_STREAM_SHUTDOWN_FLAGS flags, QUIC_UINT62)
    {
        auto& fake = *reinterpret_cast<FakeTransport*>(stream);
        fake.graceful = (flags & QUIC_STREAM_SHUTDOWN_FLAG_GRACEFUL) != 0;
        fake.aborted = (flags & QUIC_STREAM_SHUTDOWN_FLAG_ABORT_SEND) != 0;
        return QUIC_STATUS_SUCCESS;
    }
    static void QUIC_API Close(HQUIC stream)
    {
        auto& fake = *reinterpret_cast<FakeTransport*>(stream);
        while (!fake.pending.empty())
        {
            fake.completeOne(true);
        }
    }

    void completeOne(bool canceled = false)
    {
        Require(!pending.empty(), "a send must be pending");
        auto packet = pending.front();
        pending.pop_front();
        if (!canceled)
        {
            delivered += packet.bytes;
        }
        QUIC_STREAM_EVENT event = {};
        event.Type = QUIC_STREAM_EVENT_SEND_COMPLETE;
        event.SEND_COMPLETE.ClientContext = packet.context;
        event.SEND_COMPLETE.Canceled = canceled;
        callback(handle(), context, &event);
    }

    void finish(bool success)
    {
        QUIC_STREAM_EVENT event = {};
        event.Type = QUIC_STREAM_EVENT_SEND_SHUTDOWN_COMPLETE;
        event.SEND_SHUTDOWN_COMPLETE.Graceful = success;
        callback(handle(), context, &event);
        event = {};
        event.Type = QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE;
        callback(handle(), context, &event);
        QCoreApplication::processEvents();
    }

    void checkBound()
    {
        qint64 bytes = 0;
        for (const auto& packet : pending)
        {
            bytes += packet.bytes.size();
        }
        Require(pending.size() <= 2, "at most two outstanding sends");
        Require(bytes <= 131072, "at most 128 KiB of outstanding send data");
    }
};

void Write(const QString& path, const QByteArray& content)
{
    QFile file(path);
    Require(file.open(QIODevice::WriteOnly), "open fixture");
    Require(file.write(content) == content.size(), "write fixture");
}

void VerifyBoundAndResume(const QString& path, const QByteArray& payload, quint64 offset)
{
    FakeTransport transport;
    MiniImUploadStream sender(&transport.api, transport.handle(), path, payload.size());
    bool completed = false;
    QObject::connect(&sender, &MiniImUploadStream::finished, [&completed](bool success, const QString&)
    {
        completed = success;
    });
    Require(sender.start(QStringLiteral("file"), offset), "start bounded upload");
    transport.checkBound();
    Require(transport.pending.size() == 2, "prime two sends");
    int iterations = 0;
    while (!transport.pending.empty())
    {
        Require(++iterations < 100, "upload must make progress");
        transport.completeOne();
        QCoreApplication::processEvents();
        transport.checkBound();
    }
    Require(transport.graceful && !transport.aborted, "request graceful finish after reading the file");
    Require(!completed, "enqueue and send completion do not complete the stream");
    transport.finish(true);
    Require(completed, "acknowledged graceful shutdown completes the stream");
    Require(transport.delivered == QByteArrayLiteral("MINIIMFILE1 file\n") + payload.mid(offset), "correct resume bytes");
}

void VerifyFailure(const QString& path, const QByteArray& payload, bool truncate)
{
    Write(path, payload);
    FakeTransport transport;
    MiniImUploadStream sender(&transport.api, transport.handle(), path, payload.size());
    bool success = true;
    QObject::connect(&sender, &MiniImUploadStream::finished, [&success](bool result, const QString&)
    {
        success = result;
    });
    Require(sender.start(QStringLiteral("file"), 0), "start failure fixture");
    if (truncate)
    {
        Write(path, QByteArrayLiteral("truncated"));
    }
    else
    {
        transport.rejectSend = true;
    }
    transport.completeOne();
    QCoreApplication::processEvents();
    Require(transport.aborted, "changed file or rejected send must abort");
    transport.finish(false);
    Require(!success, "interruption must not be reported as success");
}
}

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    try
    {
        QTemporaryDir directory;
        Require(directory.isValid(), "temporary directory");
        const QString path = directory.filePath(QStringLiteral("upload.bin"));
        const QByteArray payload(2 * 1024 * 1024, 'x');
        Write(path, payload);
        VerifyBoundAndResume(path, payload, 0);
        VerifyBoundAndResume(path, payload, 123);
        VerifyFailure(path, payload, true);
        VerifyFailure(path, payload, false);
        std::cout << "upload stream: bounded sends, resume, changed source and send rejection passed\n";
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
