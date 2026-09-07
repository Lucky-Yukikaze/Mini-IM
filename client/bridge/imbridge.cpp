#include "bridge/imbridge.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QTextStream>

namespace
{
bool IsClientDebugLogEnabled()
{
    static const QString value = qEnvironmentVariable("MINIIM_DEBUG_LOG");
    static const bool enabled = value.isEmpty() || value != QStringLiteral("0");
    return enabled;
}

void AppendBridgeLog(const QString& message)
{
    if (!IsClientDebugLogEnabled())
    {
        return;
    }
    const QString base_dir = QCoreApplication::applicationDirPath();
    QFile file(QDir(base_dir).filePath(QStringLiteral("mini_im_client.log")));
    if (!file.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text))
    {
        return;
    }

    QTextStream stream(&file);
    stream << QDateTime::currentDateTime().toString(QStringLiteral("yyyy-MM-dd HH:mm:ss.zzz"))
           << QStringLiteral(" | ")
           << message
           << Qt::endl;
}
}

ImBridge::ImBridge(QObject* parent)
    : QObject(parent)
{
    QObject::connect(&m_session_manager, &MiniImSessionManager::messageSendsChanged, this, &ImBridge::messageSendsChanged);
    QObject::connect(&m_session_manager, &MiniImSessionManager::syncProgress, this, &ImBridge::syncProgress);
    QObject::connect(
        &m_session_manager,
        &MiniImSessionManager::connectionChanged,
        this,
        [this](const QString& state, const QString& session_id)
        {
            QVariantMap payload;
            payload.insert(QStringLiteral("state"), state);
            payload.insert(QStringLiteral("sessionId"), session_id);
            emit connectionChanged(payload);
        });
    QObject::connect(
        &m_session_manager,
        &MiniImSessionManager::initialStateLoaded,
        this,
        &ImBridge::initialStateLoaded);
    QObject::connect(
        &m_session_manager,
        &MiniImSessionManager::messagePushed,
        this,
        &ImBridge::messagePushed);
    QObject::connect(
        &m_session_manager,
        &MiniImSessionManager::messageUpdated,
        this,
        &ImBridge::messageUpdated);
    QObject::connect(
        &m_session_manager,
        &MiniImSessionManager::conversationUpdated,
        this,
        &ImBridge::conversationUpdated);
    QObject::connect(
        &m_session_manager,
        &MiniImSessionManager::fileProgress,
        this,
        &ImBridge::fileProgress);
    QObject::connect(
        &m_session_manager,
        &MiniImSessionManager::errorRaised,
        this,
        &ImBridge::errorRaised);
}

bool ImBridge::connectToServer(const QString& endpoint, const QString& token, const QString& device_id)
{
    AppendBridgeLog(
        QStringLiteral("ImBridge::connectToServer endpoint=%1 device=%2").arg(endpoint).arg(device_id));
    return m_session_manager.connectToServer(endpoint, token, device_id);
}

bool ImBridge::connectToServerWithResume(
    const QString& endpoint,
    const QString& token,
    const QString& device_id,
    const QString& resume_session_id,
    quint64 global_cursor,
    const QString& last_acked_request_id)
{
    AppendBridgeLog(
        QStringLiteral("ImBridge::connectToServerWithResume endpoint=%1 device=%2 resume=%3 cursor=%4")
            .arg(endpoint)
            .arg(device_id)
            .arg(resume_session_id)
            .arg(global_cursor));
    return m_session_manager.connectToServerWithResume(
        endpoint,
        token,
        device_id,
        resume_session_id,
        global_cursor,
        last_acked_request_id);
}

void ImBridge::disconnectFromServer()
{
    m_session_manager.disconnectFromServer();
}

bool ImBridge::sendMessage(
    const QString& conversation_id,
    const QString& client_msg_id,
    const QString& text,
    quint32 burn_mode,
    quint32 burn_ttl_sec)
{
    return m_session_manager.sendMessage(conversation_id, client_msg_id, text, burn_mode, burn_ttl_sec);
}

bool ImBridge::createConversation(
    const QString& client_conv_id,
    const QString& title,
    const QVariantList& member_ids)
{
    return m_session_manager.createConversation(client_conv_id, title, member_ids);
}

bool ImBridge::createDirectConversation(const QString& client_conv_id, const QString& peer_user_id)
{
    return m_session_manager.createDirectConversation(client_conv_id, peer_user_id);
}

bool ImBridge::addMembers(const QString& conversation_id, const QVariantList& member_ids)
{
    return m_session_manager.addMembers(conversation_id, member_ids);
}

bool ImBridge::removeMembers(const QString& conversation_id, const QVariantList& member_ids)
{
    return m_session_manager.removeMembers(conversation_id, member_ids);
}

bool ImBridge::leaveConversation(const QString& conversation_id)
{
    return m_session_manager.leaveConversation(conversation_id);
}

bool ImBridge::joinConversation(const QString& conversation_id)
{
    return m_session_manager.joinConversation(conversation_id);
}

bool ImBridge::renameConversation(const QString& conversation_id, const QString& title)
{
    return m_session_manager.renameConversation(conversation_id, title);
}

bool ImBridge::sendReceipt(const QString& conversation_id, quint64 last_read_seq)
{
    return m_session_manager.sendReceipt(conversation_id, last_read_seq);
}

bool ImBridge::recallMessage(const QString& conversation_id, const QString& message_id)
{
    return m_session_manager.recallMessage(conversation_id, message_id);
}

bool ImBridge::sendFile(const QString& conversation_id, const QString& file_path, quint32 priority)
{
    return m_session_manager.sendFile(conversation_id, file_path, priority);
}

bool ImBridge::downloadFile(
    const QString& conversation_id,
    const QString& source_file_id,
    const QString& save_path,
    quint32 priority)
{
    return m_session_manager.downloadFile(conversation_id, source_file_id, save_path, priority);
}

bool ImBridge::retryMessage(const QString& conversationId, const QString& clientMsgId)
{
    return m_session_manager.retryMessage(conversationId, clientMsgId);
}
