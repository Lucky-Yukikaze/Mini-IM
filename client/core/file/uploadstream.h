// Sends at most two file chunks concurrently; completion never substitutes for an application ACK.
#ifndef MINI_IM_CORE_FILE_UPLOADSTREAM_H_
#define MINI_IM_CORE_FILE_UPLOADSTREAM_H_

#include <QFile>
#include <QObject>
#include <msquic.h>

class MiniImUploadStream final : public QObject
{
    Q_OBJECT

public:
    MiniImUploadStream(
        const QUIC_API_TABLE* api, HQUIC connection, const QString& path, quint64 expectedSize,
        QObject* parent = nullptr);
    ~MiniImUploadStream() override;
    MiniImUploadStream(const MiniImUploadStream&) = delete;
    MiniImUploadStream& operator=(const MiniImUploadStream&) = delete;
    bool start(const QString& fileId, quint64 offset);

signals:
    void finished(bool success, const QString& error);

private:
    static QUIC_STATUS QUIC_API handleEvent(HQUIC stream, void* context, QUIC_STREAM_EVENT* event);
    bool send(const QByteArray& bytes);
    void pump();
    void abort(const QString& error);

    const QUIC_API_TABLE* m_api;
    HQUIC m_connection;
    HQUIC m_stream = nullptr;
    QFile m_file;
    quint64 m_expectedSize;
    int m_inFlight = 0;
    bool m_ending = false;
    bool m_graceful = false;
    QString m_error;
};

#endif
