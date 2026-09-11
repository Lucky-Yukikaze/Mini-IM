// Coordinates application requests and native transport events.
#ifndef MINI_IM_CORE_SESSION_SESSIONMANAGER_H_
#define MINI_IM_CORE_SESSION_SESSIONMANAGER_H_

#include <QObject>
#include <QString>
#include <QTimer>
#include <QElapsedTimer>
#include <QByteArray>
#include <QVariantList>
#include <QVariantMap>

#include <string>
#include "core/sync/statestore.h"
#include "core/sync/coordinator.h"
#include "core/quic/recovery.h"
#include "core/quic/connection.h"
#include "core/file/coordinator.h"
#include "core/session/writecoordinator.h"


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
    QVariantMap previewCancelledDownloads();
    QVariantMap cleanupCancelledDownloads(const QString& token);
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
    void controlWritesChanged(const QVariantMap& payload);
    void errorRaised(const QString& message);

private slots:
    void onHeartbeatTimeout();

private:
    void onTransportConnected();
    void onTransportDisconnected();

    void connectFileSignals();
    bool startConnectionAttempt();
    void restartConnection(const QString& reason, bool clearSession = false);
    void resetRuntimeState();
    bool parseEndpoint(const QString& endpoint, QString* host, uint16_t* port) const;
    bool sendEnvelope(const std::string& payload);
    bool sendHello();
    bool sendHeartbeat();
    void handleIncomingControlStreamData(const QByteArray& payload);
    im::envelope::Envelope makeRequestEnvelope(const QString& requestId, im::common::Channel channel);
    im::envelope::Envelope makeSyncRequest(quint64 cursor, quint32 limit);
    void handleIncomingEnvelope(const QByteArray& payload);
    void onSyncEventApplied(const MiniImStateEvent& event);
    QString makeRequestId(const QString& suffix = QString()) const;
    void emitConnectionError(const QString& message);
    void pumpMessageOutbox();
    bool sendQueuedMessage(const QVariantMap& item);
    bool sendQueuedControl(im::envelope::Envelope envelope);
    void publishMessageSends();
    void handleMessageResult(const QString& requestId, bool success, int code,
        const QString& error, const QString& entityId);

    MiniImStateStore m_stateStore;
    MiniImSyncCoordinator m_sync;
    MiniImQuicConnection m_transport;
    MiniImFileCoordinator m_files;
    MiniImControlWriteCoordinator m_controlWrites;
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
    quint64 m_seq;
    int m_heartbeat_interval_sec;
    QTimer m_heartbeat_timer;
    QTimer m_messageRetryTimer;
    QElapsedTimer m_messageAttemptTime;
    QString m_activeMessageRequest;
    QString m_userId;
    QByteArray m_control_stream_buffer;
};

#endif  // MINI_IM_CORE_SESSION_SESSIONMANAGER_H_
