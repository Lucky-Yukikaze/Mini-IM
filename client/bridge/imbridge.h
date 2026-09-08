#ifndef MINI_IM_BRIDGE_IMBRIDGE_H_
#define MINI_IM_BRIDGE_IMBRIDGE_H_

#include <QObject>
#include <QString>
#include <QVariantMap>

#include "core/session/sessionmanager.h"

class ImBridge final : public QObject
{
    Q_OBJECT

public:
    explicit ImBridge(QObject* parent = nullptr);

    Q_INVOKABLE bool connectToServer(const QString& endpoint, const QString& token, const QString& device_id);
    Q_INVOKABLE bool connectToServerWithResume(
        const QString& endpoint,
        const QString& token,
        const QString& device_id,
        const QString& resume_session_id,
        quint64 global_cursor,
        const QString& last_acked_request_id);
    Q_INVOKABLE void disconnectFromServer();
    Q_INVOKABLE bool sendMessage(
        const QString& conversation_id,
        const QString& client_msg_id,
        const QString& text,
        quint32 burn_mode,
        quint32 burn_ttl_sec);
    Q_INVOKABLE bool retryFile(const QString& clientFileId);
    Q_INVOKABLE bool cancelFile(const QString& clientFileId);
    Q_INVOKABLE bool retryMessage(const QString& conversationId, const QString& clientMsgId);
    Q_INVOKABLE bool createConversation(
        const QString& client_conv_id,
        const QString& title,
        const QVariantList& member_ids);
    Q_INVOKABLE bool createDirectConversation(const QString& client_conv_id, const QString& peer_user_id);
    Q_INVOKABLE bool addMembers(const QString& conversation_id, const QVariantList& member_ids);
    Q_INVOKABLE bool removeMembers(const QString& conversation_id, const QVariantList& member_ids);
    Q_INVOKABLE bool leaveConversation(const QString& conversation_id);
    Q_INVOKABLE bool joinConversation(const QString& conversation_id);
    Q_INVOKABLE bool renameConversation(const QString& conversation_id, const QString& title);
    Q_INVOKABLE bool sendReceipt(const QString& conversation_id, quint64 last_read_seq);
    Q_INVOKABLE bool recallMessage(const QString& conversation_id, const QString& message_id);
    Q_INVOKABLE bool sendFile(const QString& conversation_id, const QString& file_path, quint32 priority);
    Q_INVOKABLE bool downloadFile(
        const QString& conversation_id,
        const QString& source_file_id,
        const QString& save_path,
        quint32 priority);

signals:
    void connectionChanged(const QVariantMap& payload);
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

private:
    MiniImSessionManager m_session_manager;
};

#endif  // MINI_IM_BRIDGE_IMBRIDGE_H_
