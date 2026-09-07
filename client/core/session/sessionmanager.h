// Coordinates application requests and native transport events.
#ifndef MINI_IM_CORE_SESSION_SESSIONMANAGER_H_
#define MINI_IM_CORE_SESSION_SESSIONMANAGER_H_

#include <QObject>
#include <QHash>
#include <QString>
#include <QSet>
#include <QTimer>
#include <QElapsedTimer>
#include <QByteArray>
#include <QVariantList>
#include <QVariantMap>

#include <memory>
#include <string>
#include "core/sync/statestore.h"
#include "core/quic/recovery.h"
#include "core/quic/connection.h"

class MiniImDownloadSink;
namespace im { namespace file { class FileUpdated; } }

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
    bool retryMessage(const QString& conversationId, const QString& clientMsgId);
    bool retryFile(const QString& clientFileId);
    bool cancelFile(const QString& clientFileId);
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
    void syncProgress(const QVariantMap& payload);
    void messageSendsChanged(const QVariantMap& payload);
    void fileTasksChanged(const QVariantMap& payload);
    void errorRaised(const QString& message);

private slots:
    void onHeartbeatTimeout();

private:
    void onTransportConnected();
    void onTransportDisconnected();
    void onFileData(quint64 streamId, const QByteArray& payload);
    void onFileEnded(quint64 streamId);
    void onFileClosed(quint64 streamId, bool connectionShutdown);
    void onUploadFinished(const QString& fileId, bool success, const QString& error);

    bool startConnectionAttempt();
    void restartConnection(const QString& reason, bool clearSession = false);
    void resetRuntimeState();
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
    bool sendFileFinish(
        const QString& file_id, bool success, quint64 transferredBytes = 0, const QString& sha256 = QString());
    bool sendFileStreamData(const QString& file_id, const QString& file_path, quint64 offset, quint64 fileSize);
    void handleIncomingFileStream(quint64 streamId, const QByteArray& payload);
    void handleIncomingEnvelope(const QByteArray& payload);
    void flushPendingDownloadBuffers(const QString& file_id);
    void completeFileReceive(quint64 streamId);
    bool applySyncEvents(const QVector<MiniImStateEvent>& events);
    QString makeRequestId(const QString& suffix = QString()) const;
    void emitConnectionError(const QString& message);
    void handleFileUpdated(const im::file::FileUpdated& updated);
    void finishDownload(const QString& file_id);
    void pumpFileTasks();
    void startFileTask(const QVariantMap& task);
    void publishFileTasks();
    void failFileTask(const QString& fileId, const QString& error);
    void handleFileResult(const QString& requestId, bool success, int code,
        const QString& error, const QString& fileId);
    void pumpMessageOutbox();
    bool sendQueuedMessage(const QVariantMap& item);
    void publishMessageSends();
    void handleMessageResult(const QString& requestId, bool success, int code,
        const QString& error, const QString& entityId);


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
        std::shared_ptr<MiniImDownloadSink> sink;
        bool finish_sent = false;
        quint64 resume_offset = 0;
    };

    struct DownloadStreamState
    {
        QByteArray buffer;
        QString file_id;
        bool header_parsed = false;
        bool finished = false;
    };

    MiniImQuicConnection m_transport;
    MiniImConnectionRecovery m_recovery;
    QElapsedTimer m_lastResponseTime;
    bool m_transportStopping = false;
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
    QTimer m_messageRetryTimer;
    QElapsedTimer m_messageAttemptTime;
    QElapsedTimer m_syncAttemptTime;
    QString m_activeMessageRequest;
    std::string m_syncRequestPayload;
    bool m_messageSyncReady = false;
    MiniImStateStore m_stateStore;
    QString m_userId;
    QString m_syncRequestId;
    QByteArray m_control_stream_buffer;
    QSet<QString> m_activeFileTasks;
    QHash<QString, QElapsedTimer> m_fileControlAttempts;
    QHash<QString, quint64> m_latest_file_versions;
    QHash<QString, PendingFileUpload> m_pending_file_init_requests;
    QHash<QString, PendingFileUpload> m_pending_file_uploads;
    QHash<QString, PendingFileDownload> m_pending_file_download_init_requests;
    QHash<QString, PendingFileDownload> m_pending_file_downloads;
    QSet<QString> m_failed_file_downloads;
    QHash<QString, QString> m_pending_file_finish_requests;
    QHash<quint64, DownloadStreamState> m_download_stream_states;


};

#endif  // MINI_IM_CORE_SESSION_SESSIONMANAGER_H_
