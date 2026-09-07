#include "core/file/downloadsink.h"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QTemporaryDir>
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

QString Hash(const QByteArray& payload)
{
    return QString::fromLatin1(QCryptographicHash::hash(payload, QCryptographicHash::Sha256).toHex());
}

QByteArray Read(const QString& path)
{
    QFile file(path);
    Require(file.open(QIODevice::ReadOnly), "read test output");
    return file.readAll();
}

void Write(const QString& path, const QByteArray& data)
{
    QFile file(path);
    Require(file.open(QIODevice::WriteOnly), "open original");
    Require(file.write(data) == data.size(), "write original");
}

class ShortWriteDevice final : public QIODevice
{
public:
    explicit ShortWriteDevice(bool stop) : m_stop(stop) { open(QIODevice::WriteOnly); }
    QByteArray bytes;

protected:
    qint64 writeData(const char* data, qint64 size) override
    {
        if (m_stop && !bytes.isEmpty())
        {
            return -1;
        }
        const auto count = qMin<qint64>(2, size);
        bytes.append(data, count);
        return count;
    }
    qint64 readData(char*, qint64) override { return -1; }

private:
    bool m_stop;
};

void VerifyAtomicPublication(const QString& path, const QByteArray& payload)
{
    Write(path, QByteArrayLiteral("previous"));
    MiniImDownloadSink sink(path, QStringLiteral("success"));
    Require(sink.open(payload.size(), Hash(payload)), "open valid download");
    Require(sink.append(payload.left(2)), "first chunk");
    Require(Read(path) == "previous", "original preserved during transfer");
    Require(sink.append(payload.mid(2)), "remaining chunk");
    Require(sink.finish(), "finish download");
    Require(Read(path) == payload && sink.sha256() == Hash(payload), "verified destination");
    Require(sink.finish(), "repeat finish idempotent");
    Require(!QFile::exists(sink.stagingPath()), "staging removed after publication");
}

void VerifyRejectedDownloads(const QString& path, const QByteArray& payload)
{
    Write(path, QByteArrayLiteral("previous"));
    {
        MiniImDownloadSink sink(path, QStringLiteral("truncated"));
        Require(sink.open(payload.size(), Hash(payload)), "open truncated");
        Require(sink.append(payload.left(2)), "partial data");
        Require(!sink.finish(), "truncated download must fail");
        Require(Read(path) == "previous", "truncated preserves original");
    }
    {
        MiniImDownloadSink sink(path, QStringLiteral("hash"));
        Require(sink.open(payload.size(), Hash(payload)), "open hash");
        Require(sink.append(QByteArray(payload.size(), 'x')), "wrong content");
        Require(!sink.finish(), "wrong digest must fail");
        Require(Read(path) == "previous", "bad digest preserves original");
    }
    {
        MiniImDownloadSink sink(path, QStringLiteral("overrun"));
        Require(sink.open(payload.size(), Hash(payload)), "open overrun");
        Require(!sink.append(payload + 'x'), "overrun must fail");
    }
    {
        MiniImDownloadSink sink(path, QStringLiteral("metadata"));
        Require(!sink.open(payload.size(), QStringLiteral("bad")), "invalid metadata rejected");
    }
}

void VerifyResumeAndDestinationFailure(const QString& path, const QByteArray& payload)
{
    {
        MiniImDownloadSink partial(path, QStringLiteral("resume"));
        Require(partial.open(payload.size(), Hash(payload)), "open partial");
        Require(partial.append(payload.left(3)), "save partial");
    }
    MiniImDownloadSink resumed(path, QStringLiteral("resume"));
    Require(resumed.open(payload.size(), Hash(payload), 3), "resume existing data");
    Require(resumed.append(payload.mid(3)) && resumed.finish(), "resume completes");
    Require(Read(path) == payload, "resumed bytes match");
    const QString directoryTarget = path + QStringLiteral("-directory");
    Require(QDir().mkpath(directoryTarget), "create invalid destination");
    MiniImDownloadSink failed(directoryTarget, QStringLiteral("disk"));
    Require(failed.open(payload.size(), Hash(payload)), "open staging beside directory");
    Require(failed.append(payload), "write staging");
    Require(!failed.finish(), "destination publication error");
}
}

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    try
    {
        QTemporaryDir directory;
        Require(directory.isValid(), "temporary directory");
        const QByteArray payload("verified content");
        const QString path = directory.filePath(QStringLiteral("download.bin"));
        VerifyAtomicPublication(path, payload);
        VerifyRejectedDownloads(path, payload);
        VerifyResumeAndDestinationFailure(path, payload);
        ShortWriteDevice shortWrites(false);
        Require(MiniImDownloadSink::writeAll(shortWrites, payload), "complete partial writes");
        Require(shortWrites.bytes == payload, "partial writes preserve all bytes");
        ShortWriteDevice failure(true);
        Require(!MiniImDownloadSink::writeAll(failure, payload), "detect write failure");
        std::cout << "download sink: atomic publication, integrity, resume and write errors passed\n";
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
