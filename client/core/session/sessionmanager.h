#ifndef MINI_IM_CORE_SESSION_SESSIONMANAGER_H_
#define MINI_IM_CORE_SESSION_SESSIONMANAGER_H_

#include <QObject>
#include <QHash>
#include <QString>
#include <QSet>
#include <QTimer>
#include <QByteArray>
#include <QVariantList>
#include <QVariantMap>

#include <msquic.h>

class QByteArray;

class MiniImSessionManager final : public QObject
{
    Q_OBJECT

public:
    explicit MiniImSessionManager(QObject* parent = nullptr);
    ~MiniImSessionManager() override;

    bool connectToServer(const QString& endpoint, const QString& token, const QString& device_id);
    bool connectToServerWithResume(
        const QString& endpoint,
        const QString& token,
        const QString& device_id,
        const QString& resume_session_id,
        quint64 global_cursor,
        const QString& last_acked_request_id);
    void disconnectFromServer();
    bool sendMessage(
        const QString& conversation_id,
        const QString& client_msg_id,
        const QString& text,
        quint32 burn_mode = 0,
        quint32 burn_ttl_sec = 0);
    bool createConversation(const QString& client_conv_id, const QString& title, const QVariantList& member_ids);
    bool createDirectConversation(const QString& client_conv_id, const QString& peer_user_id);
    bool addMembers(const QString& conversation_id, const QVariantList& member_ids);
    bool removeMembers(const QString& conversation_id, const QVariantList& member_ids);
    bool leaveConversation(const QString& conversation_id);
    bool joinConversation(const QString& conversation_id);
    bool renameConversation(const QString& conversation_id, const QString& title);
    bool sendReceipt(const QString& conversation_id, quint64 last_read_seq);
    bool recallMessage(const QString& conversation_id, const QString& message_id);
    bool sendFile(const QString& conversation_id, const QString& file_path, quint32 priority);
    bool downloadFile(const QString& conversation_id, const QString& source_file_id, const QString& save_path, quint32 priority);

signals:
    void connectionChanged(const QString& state, const QString& session_id);
    void initialStateLoaded(const QVariantMap& payload);
    void messagePushed(const QVariantMap& payload);
    void messageUpdated(const QVariantMap& payload);
    void conversationUpdated(const QVariantMap& payload);
    void fileProgress(const QVariantMap& payload);
    void errorRaised(const QString& message);

private slots:
    void onHeartbeatTimeout();

private:
    static QUIC_STATUS QUIC_API handleConnectionEvent(
        HQUIC connection,
        void* context,
        QUIC_CONNECTION_EVENT* event);
    static QUIC_STATUS QUIC_API handleStreamEvent(HQUIC stream, void* context, QUIC_STREAM_EVENT* event);

    bool initializeMsQuic();
    void releaseMsQuic();
    void resetRuntimeState();
    void closeControlStreamHandle();
    void closeConnectionHandle();
    bool parseEndpoint(const QString& endpoint, QString* host, uint16_t* port) const;
    bool sendEnvelope(const std::string& payload);
    bool sendHello();
    bool sendHeartbeat();
    void handleIncomingControlStreamData(const QByteArray& payload);
    bool sendSyncRequest(quint64 global_cursor, quint32 limit = 200);
    bool sendFileInitRequest(
        const QString& request_id,
        const QString& conversation_id,
        const QString& client_file_id,
        const QString& file_name,
        quint64 file_size,
        const QString& sha256,
        quint64 resume_offset,
        quint32 priority,
        int direction,
        const QString& source_file_id);
    bool sendFileFinish(const QString& file_id, bool success);
    bool sendFileStreamData(const QString& file_id, const QString& file_path, quint64 offset);
    void handleIncomingFileStream(HQUIC stream, const QByteArray& payload);
    void handleIncomingEnvelope(const QByteArray& payload);
    void flushPendingDownloadBuffers(const QString& file_id);
    QVariantMap buildInitialStatePayload(const QString& user_id, quint64 global_cursor) const;
    QString makeRequestId(const QString& suffix = QString()) const;
    void emitConnectionError(const QString& message);
    void handleFileUpdated(
        const std::string& event_id,
        const std::string& file_id,
        const std::string& conversation_id,
        quint64 transferred_bytes,
        bool completed,
        quint64 version,
        qlonglong updated_at_ms,
        bool check_event_id = true);

    struct PendingFileUpload
    {
        QString conversation_id;
        QString file_path;
        QString file_name;
        QString client_file_id;
        QString sha256;
        quint64 file_size = 0;
        quint32 priority = 0;
        QString file_id;
        bool stream_started = false;
    };

    struct PendingFileDownload
    {
        QString conversation_id;
        QString source_file_id;
        QString save_path;
        QString client_file_id;
        QString file_id;
    };

    struct DownloadStreamState
    {
        QByteArray buffer;
        QString file_id;
        bool header_parsed = false;
    };

    bool m_connected;
    bool m_connecting;
    bool m_hello_sent;
    QString m_session_id;
    QString m_resume_session_id;
    QString m_endpoint;
    QString m_token;
    QString m_device_id;
    QString m_last_acked_request_id;
    quint64 m_global_cursor;
    quint64 m_seq;
    int m_heartbeat_interval_sec;
    QTimer m_heartbeat_timer;
    QSet<QString> m_seen_event_ids;
    QByteArray m_control_stream_buffer;
    QHash<QString, quint64> m_latest_file_versions;
    QHash<QString, PendingFileUpload> m_pending_file_init_requests;
    QHash<QString, PendingFileUpload> m_pending_file_uploads;
    QHash<QString, PendingFileDownload> m_pending_file_download_init_requests;
    QHash<QString, PendingFileDownload> m_pending_file_downloads;
    QSet<QString> m_failed_file_downloads;
    QHash<quintptr, QString> m_file_stream_to_file_id;
    QHash<quintptr, DownloadStreamState> m_download_stream_states;

    const QUIC_API_TABLE* m_msquic;
    HQUIC m_registration;
    HQUIC m_configuration;
    HQUIC m_connection;
    HQUIC m_stream;
};

#endif  // MINI_IM_CORE_SESSION_SESSIONMANAGER_H_
