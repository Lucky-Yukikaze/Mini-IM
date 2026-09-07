#include "core/message/outbox.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSqlError>
#include <stdexcept>

namespace
{
const QString kColumns = QStringLiteral(
    "request_id,conversation_id,client_msg_id,status,server_msg_id,code,error,created_at_ms,attempts,payload");
}

QSqlQuery MiniImMessageOutbox::run(const QString& sql, const QVariantList& values) const
{
    QSqlQuery query(m_db);
    if (!query.prepare(sql))
    {
        throw std::runtime_error(query.lastError().text().toStdString());
    }
    for (const auto& value : values)
    {
        query.addBindValue(value);
    }
    if (!query.exec())
    {
        throw std::runtime_error(query.lastError().text().toStdString());
    }
    return query;
}

void MiniImMessageOutbox::open(const QSqlDatabase& database)
{
    m_db = database;
    run(QStringLiteral("CREATE TABLE IF NOT EXISTS message_outbox("
        "request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, client_msg_id TEXT NOT NULL,"
        "payload BLOB NOT NULL, fingerprint BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',"
        "server_msg_id TEXT NOT NULL DEFAULT '', code INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',"
        "created_at_ms INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,"
        "UNIQUE(conversation_id,client_msg_id))"));
}

void MiniImMessageOutbox::close()
{
    m_db = QSqlDatabase();
}

QVariantMap MiniImMessageOutbox::row(const QSqlQuery& query) const
{
    QVariantMap result;
    const auto bytes = query.value(9).toByteArray();
    if (!bytes.isEmpty())
    {
        QJsonParseError error;
        const auto document = QJsonDocument::fromJson(bytes, &error);
        if (error.error != QJsonParseError::NoError || !document.isObject())
        {
            throw std::runtime_error("invalid persisted message intent");
        }
        result = document.object().toVariantMap();
    }
    const QStringList keys = {"requestId", "conversationId", "clientMsgId", "status",
        "serverMsgId", "code", "error", "createdAtMs", "attempts"};
    for (int index = 0; index < keys.size(); ++index)
    {
        result.insert(keys[index], query.value(index));
    }
    return result;
}

QVariantMap MiniImMessageOutbox::enqueue(const QString& requestId, const QVariantMap& intent)
{
    const QString conversation = intent.value(QStringLiteral("conversationId")).toString();
    const QString clientId = intent.value(QStringLiteral("clientMsgId")).toString();
    if (requestId.isEmpty() || conversation.isEmpty() || clientId.isEmpty())
    {
        throw std::runtime_error("message intent requires stable identifiers");
    }
    const auto payload = QJsonDocument(QJsonObject::fromVariantMap(intent)).toJson(QJsonDocument::Compact);
    const auto fingerprint = QCryptographicHash::hash(payload, QCryptographicHash::Sha256);
    run(QStringLiteral("BEGIN IMMEDIATE"));
    try
    {
        auto existing = run(QStringLiteral("SELECT ") + kColumns
            + QStringLiteral(",fingerprint FROM message_outbox WHERE conversation_id=? AND client_msg_id=?"),
            {conversation, clientId});
        if (existing.next())
        {
            if (existing.value(10).toByteArray() != fingerprint)
            {
                throw std::runtime_error("message intent already belongs to different content");
            }
            const auto result = row(existing);
            existing.finish();
            run(QStringLiteral("COMMIT"));
            return result;
        }
        existing.finish();
        run(QStringLiteral("INSERT INTO message_outbox(request_id,conversation_id,client_msg_id,"
            "payload,fingerprint,created_at_ms) VALUES(?,?,?,?,?,?)"),
            {requestId, conversation, clientId, payload, fingerprint, QDateTime::currentMSecsSinceEpoch()});
        run(QStringLiteral("COMMIT"));
    }
    catch (...)
    {
        m_db.rollback();
        throw;
    }
    auto inserted = run(QStringLiteral("SELECT ") + kColumns
        + QStringLiteral(" FROM message_outbox WHERE request_id=?"), {requestId});
    if (!inserted.next())
    {
        throw std::runtime_error("persisted message intent missing");
    }
    return row(inserted);
}

QVariantMap MiniImMessageOutbox::nextPending() const
{
    auto query = run(QStringLiteral("SELECT ") + kColumns
        + QStringLiteral(" FROM message_outbox WHERE status='pending' ORDER BY rowid LIMIT 1"));
    return query.next() ? row(query) : QVariantMap();
}

QVariantList MiniImMessageOutbox::pending() const
{
    auto query = run(QStringLiteral("SELECT ") + kColumns
        + QStringLiteral(" FROM message_outbox WHERE status!='confirmed' ORDER BY rowid"));
    QVariantList items;
    while (query.next())
    {
        items.append(row(query));
    }
    return items;
}

bool MiniImMessageOutbox::acknowledge(
    const QString& requestId, bool success, int code, const QString& error, const QString& entityId)
{
    auto query = run(QStringLiteral("SELECT status FROM message_outbox WHERE request_id=?"), {requestId});
    if (!query.next())
    {
        return false;
    }
    if (query.value(0).toString() == QStringLiteral("confirmed"))
    {
        return true;
    }
    query.finish();
    if (success && entityId.isEmpty())
    {
        throw std::runtime_error("message confirmation has no server message id");
    }
    const bool retryable = code == 401 || code == 408 || code == 429 || code >= 500;
    const QString status = success ? QStringLiteral("confirmed")
        : retryable ? QStringLiteral("pending") : QStringLiteral("failed");
    run(QStringLiteral("UPDATE message_outbox SET status=?,server_msg_id=?,code=?,error=?,"
        "payload=CASE WHEN ? THEN X'' ELSE payload END WHERE request_id=?"),
        {status, entityId.isNull() ? QStringLiteral("") : entityId, code,
         error.isNull() ? QStringLiteral("") : error, success, requestId});
    return true;
}

void MiniImMessageOutbox::markAttempt(const QString& requestId)
{
    run(QStringLiteral("UPDATE message_outbox SET attempts=attempts+1 WHERE request_id=? AND status='pending'"),
        {requestId});
}

bool MiniImMessageOutbox::retry(const QString& conversationId, const QString& clientMsgId)
{
    auto query = run(QStringLiteral("UPDATE message_outbox SET status='pending',code=0,error='' "
        "WHERE conversation_id=? AND client_msg_id=? AND status='failed'"), {conversationId, clientMsgId});
    return query.numRowsAffected() > 0;
}

void MiniImMessageOutbox::settleTerminal(
    const QString& conversationId, const QString& clientMsgId, const QString& entityId)
{
    run(QStringLiteral("UPDATE message_outbox SET status='confirmed',server_msg_id=?,payload=X'',code=0,error='' "
        "WHERE conversation_id=? AND client_msg_id=?"), {entityId, conversationId, clientMsgId});
}
