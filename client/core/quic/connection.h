// Owns native connection and stream lifetimes; application messages stay with the session.
#ifndef MINI_IM_CORE_QUIC_CONNECTION_H_
#define MINI_IM_CORE_QUIC_CONNECTION_H_

#include <QByteArray>
#include <QHash>
#include <QObject>
#include <QString>
#include <memory>
#include <msquic.h>

class MiniImUploadStream;

class MiniImQuicConnection final : public QObject
{
    Q_OBJECT

public:
    explicit MiniImQuicConnection(QObject* parent = nullptr);
    ~MiniImQuicConnection() override;
    MiniImQuicConnection(const MiniImQuicConnection&) = delete;
    MiniImQuicConnection& operator=(const MiniImQuicConnection&) = delete;

    bool open(const QString& host, quint16 port);
    void shutdown();
    bool isActive() const;
    bool sendControl(const QByteArray& frame);
    bool sendFile(const QString& fileId, const QString& path, quint64 offset, quint64 fileSize);
    // Each file batch stays pending until its consumer explicitly releases it.
    void completeFileReceive(quint64 streamId, bool abort = false);

signals:
    void connected();
    void disconnected();
    void errorRaised(const QString& error);
    void controlData(const QByteArray& payload);
    void fileData(quint64 streamId, const QByteArray& payload);
    void fileEnded(quint64 streamId);
    void fileClosed(quint64 streamId, bool connectionShutdown);
    void uploadFinished(const QString& fileId, bool success, const QString& error);

private:
    struct CallbackContext;
    struct PendingReceive
    {
        HQUIC stream;
        quint64 bytes;
    };

    static QUIC_STATUS QUIC_API handleConnectionEvent(
        HQUIC connection, void* context, QUIC_CONNECTION_EVENT* event);
    static QUIC_STATUS QUIC_API handleStreamEvent(HQUIC stream, void* context, QUIC_STREAM_EVENT* event);
    static QUIC_STATUS queueReceive(HQUIC stream, const CallbackContext& callback,
        quint64 streamId, const QUIC_STREAM_EVENT& event);
    bool initialize();
    void release();
    void closeConnection();
    void openControlStream();
    void interruptControl(const QString& error);
    void receive(HQUIC stream, quint64 streamId, const QByteArray& payload);
    void streamEnded(HQUIC stream, quint64 streamId);
    void streamClosed(HQUIC stream, quint64 streamId, bool connectionShutdown);

    const QUIC_API_TABLE* m_api = nullptr;
    HQUIC m_registration = nullptr;
    HQUIC m_configuration = nullptr;
    HQUIC m_connection = nullptr;
    HQUIC m_controlStream = nullptr;
    quint64 m_generation = 0;
    bool m_stopping = true;
    std::unique_ptr<CallbackContext> m_callbackContext;
    QHash<quint64, PendingReceive> m_pendingReceives;
    QHash<QString, MiniImUploadStream*> m_uploads;
};

#endif  // MINI_IM_CORE_QUIC_CONNECTION_H_
