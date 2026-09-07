#include "core/file/downloadsink.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFileInfo>
#include <QRegularExpression>
#include <QSaveFile>

namespace
{
constexpr qint64 kChunkSize = 64 * 1024;
}

MiniImDownloadSink::MiniImDownloadSink(const QString& targetPath, const QString& transferId)
    : m_targetPath(targetPath),
      m_stagingPath(targetPath + QStringLiteral(".miniim-") + transferId + QStringLiteral(".part")),
      m_file(m_stagingPath)
{
}

bool MiniImDownloadSink::open(quint64 expectedSize, const QString& expectedSha256, quint64 resumeOffset)
{
    static const QRegularExpression hashPattern(QStringLiteral("^[0-9a-fA-F]{64}$"));
    if (expectedSize == 0 || resumeOffset > expectedSize || !hashPattern.match(expectedSha256).hasMatch())
    {
        return fail(QStringLiteral("invalid download metadata"));
    }
    if (!QDir().mkpath(QFileInfo(m_targetPath).absolutePath()))
    {
        return fail(QStringLiteral("failed to create download directory"));
    }
    m_expectedSize = expectedSize;
    m_expectedSha256 = expectedSha256.toLower();
    m_error.clear();
    m_finished = false;
    m_verifiedSha256.clear();
    if (!m_file.open(QIODevice::ReadWrite))
    {
        return fail(m_file.errorString());
    }
    if (static_cast<quint64>(m_file.size()) < resumeOffset || !m_file.resize(static_cast<qint64>(resumeOffset))
        || !m_file.seek(static_cast<qint64>(resumeOffset)))
    {
        return fail(QStringLiteral("download staging file does not match resume offset"));
    }
    return true;
}

bool MiniImDownloadSink::writeAll(QIODevice& output, const QByteArray& data)
{
    qint64 offset = 0;
    while (offset < data.size())
    {
        const qint64 written = output.write(data.constData() + offset, data.size() - offset);
        if (written <= 0)
        {
            return false;
        }
        offset += written;
    }
    return true;
}

bool MiniImDownloadSink::append(const QByteArray& data)
{
    if (!m_error.isEmpty() || m_finished || !m_file.isOpen())
    {
        return false;
    }
    if (static_cast<quint64>(data.size()) > m_expectedSize - receivedBytes())
    {
        return fail(QStringLiteral("download exceeds declared size"));
    }
    if (!writeAll(m_file, data) || !m_file.flush())
    {
        return fail(QStringLiteral("failed to write download staging file"));
    }
    return true;
}

bool MiniImDownloadSink::finish()
{
    if (m_finished)
    {
        return true;
    }
    if (!m_error.isEmpty() || !m_file.isOpen())
    {
        return false;
    }
    if (receivedBytes() != m_expectedSize || !m_file.flush() || !m_file.seek(0))
    {
        return fail(QStringLiteral("download is incomplete"));
    }
    QSaveFile output(m_targetPath);
    if (!output.open(QIODevice::WriteOnly))
    {
        return fail(output.errorString());
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    while (!m_file.atEnd())
    {
        const QByteArray chunk = m_file.read(kChunkSize);
        if (chunk.isEmpty() || !writeAll(output, chunk))
        {
            return fail(QStringLiteral("failed to publish download"));
        }
        hash.addData(chunk);
    }
    const QString actualHash = QString::fromLatin1(hash.result().toHex());
    if (actualHash != m_expectedSha256)
    {
        return fail(QStringLiteral("download sha256 mismatch"));
    }
    if (!output.commit())
    {
        return fail(output.errorString());
    }
    m_verifiedSha256 = actualHash;
    m_finished = true;
    m_file.close();
    QFile::remove(m_stagingPath);
    return true;
}

quint64 MiniImDownloadSink::receivedBytes() const
{
    return m_finished ? m_expectedSize : static_cast<quint64>(qMax<qint64>(0, m_file.size()));
}

QString MiniImDownloadSink::sha256() const
{
    return m_verifiedSha256;
}

QString MiniImDownloadSink::errorString() const
{
    return m_error;
}

QString MiniImDownloadSink::stagingPath() const
{
    return m_stagingPath;
}

bool MiniImDownloadSink::fail(const QString& error)
{
    m_error = error;
    m_file.close();
    return false;
}
