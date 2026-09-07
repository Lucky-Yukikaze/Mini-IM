// Receives a file into a resumable staging file and publishes verified content atomically.
#ifndef MINI_IM_CORE_FILE_DOWNLOADSINK_H_
#define MINI_IM_CORE_FILE_DOWNLOADSINK_H_

#include <QFile>
#include <QString>

class QIODevice;

class MiniImDownloadSink final
{
public:
    MiniImDownloadSink(const QString& targetPath, const QString& transferId);
    MiniImDownloadSink(const MiniImDownloadSink&) = delete;
    MiniImDownloadSink& operator=(const MiniImDownloadSink&) = delete;

    bool open(quint64 expectedSize, const QString& expectedSha256, quint64 resumeOffset = 0);
    bool append(const QByteArray& data);
    bool finish();
    quint64 receivedBytes() const;
    QString sha256() const;
    QString errorString() const;
    QString stagingPath() const;
    static bool writeAll(QIODevice& output, const QByteArray& data);

private:
    bool fail(const QString& error);
    QString m_targetPath;
    QString m_stagingPath;
    QFile m_file;
    quint64 m_expectedSize = 0;
    QString m_expectedSha256;
    QString m_verifiedSha256;
    QString m_error;
    bool m_finished = false;
};

#endif
