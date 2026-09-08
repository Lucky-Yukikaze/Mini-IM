#include "core/session/writestore.h"

#include <QDateTime>
#include <QSqlError>
#include <stdexcept>

namespace
{
const QString kColumns = QStringLiteral(
    "request_id,operation,conversation_id,client_conv_id,status,code,error,entity_id,created_at_ms,attempts,payload");
}

QSqlQuery MiniImControlWriteStore::run(const QString& sql, const QVariantList& values) const
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

void MiniImControlWriteStore::open(const QSqlDatabase& database)
{
    m_db = database;
    run(QStringLiteral("CREATE TABLE IF NOT EXISTS control_outbox("
        "request_id TEXT PRIMARY KEY,operation TEXT NOT NULL,conversation_id TEXT NOT NULL,"
        "client_conv_id TEXT NOT NULL,payload BLOB NOT NULL,status TEXT NOT NULL DEFAULT 'pending',"
        "code INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',entity_id TEXT NOT NULL DEFAULT '',"
        "created_at_ms INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0)"));
    run(QStringLiteral("CREATE UNIQUE INDEX IF NOT EXISTS control_create_intent "
        "ON control_outbox(client_conv_id) WHERE client_conv_id!=''"));
}

void MiniImControlWriteStore::close()
{
    m_db = QSqlDatabase();
}

QVariantMap MiniImControlWriteStore::row(const QSqlQuery& query) const
{
    const QStringList keys = {"requestId", "operation", "conversationId", "clientConvId", "status",
        "code", "error", "entityId", "createdAtMs", "attempts", "payload"};
    QVariantMap item;
    for (int index = 0; index < keys.size(); ++index)
    {
        item.insert(keys[index], query.value(index));
    }
    return item;
}

QVariantMap MiniImControlWriteStore::enqueue(const QString& requestId, const QString& operation,
    const QString& conversationId, const QString& clientConvId, const QByteArray& payload)
{
    if (requestId.isEmpty() || operation.isEmpty() || payload.isEmpty())
    {
        throw std::runtime_error("control write requires a stable request and body");
    }
    run(QStringLiteral("BEGIN IMMEDIATE"));
    try
    {
        auto existing = run(QStringLiteral("SELECT ") + kColumns + QStringLiteral(
            " FROM control_outbox WHERE request_id=? OR (client_conv_id=? AND client_conv_id!='')"),
            {requestId, clientConvId.isNull() ? QStringLiteral("") : clientConvId});
        if (existing.next())
        {
            const auto item = row(existing);
            if (item.value("operation").toString() != operation || item.value("payload").toByteArray() != payload)
            {
                throw std::runtime_error("control intent already belongs to different content");
            }
            existing.finish();
            run(QStringLiteral("COMMIT"));
            return item;
        }
        existing.finish();
        run(QStringLiteral("INSERT INTO control_outbox(request_id,operation,conversation_id,client_conv_id,"
            "payload,created_at_ms) VALUES(?,?,?,?,?,?)"), {requestId, operation,
            conversationId.isNull() ? QStringLiteral("") : conversationId,
            clientConvId.isNull() ? QStringLiteral("") : clientConvId, payload, QDateTime::currentMSecsSinceEpoch()});
        run(QStringLiteral("COMMIT"));
    }
    catch (...)
    {
        m_db.rollback();
        throw;
    }
    auto inserted = run(QStringLiteral("SELECT ") + kColumns
        + QStringLiteral(" FROM control_outbox WHERE request_id=?"), {requestId});
    if (!inserted.next())
    {
        throw std::runtime_error("persisted control intent missing");
    }
    return row(inserted);
}

QVariantMap MiniImControlWriteStore::nextPending() const
{
    auto query = run(QStringLiteral("SELECT ") + kColumns
        + QStringLiteral(" FROM control_outbox WHERE status='pending' ORDER BY rowid LIMIT 1"));
    return query.next() ? row(query) : QVariantMap();
}

QVariantList MiniImControlWriteStore::pending() const
{
    auto query = run(QStringLiteral("SELECT ") + kColumns
        + QStringLiteral(" FROM control_outbox WHERE status!='confirmed' ORDER BY rowid"));
    QVariantList items;
    while (query.next())
    {
        auto item = row(query);
        item.remove(QStringLiteral("payload"));
        items.append(item);
    }
    return items;
}

void MiniImControlWriteStore::markAttempt(const QString& requestId)
{
    run(QStringLiteral("UPDATE control_outbox SET attempts=attempts+1 WHERE request_id=? AND status='pending'"),
        {requestId});
}

bool MiniImControlWriteStore::acknowledge(
    const QString& requestId, bool success, int code, const QString& error, const QString& entityId)
{
    auto query = run(QStringLiteral("SELECT status FROM control_outbox WHERE request_id=?"), {requestId});
    if (!query.next())
    {
        return false;
    }
    if (query.value(0).toString() != QStringLiteral("pending"))
    {
        return true;
    }
    query.finish();
    if (success && entityId.isEmpty())
    {
        throw std::runtime_error("control confirmation has no entity id");
    }
    const bool retryable = code == 401 || code == 408 || code == 429 || code >= 500;
    const QString status = success ? QStringLiteral("confirmed")
        : retryable ? QStringLiteral("pending") : QStringLiteral("failed");
    run(QStringLiteral("UPDATE control_outbox SET status=?,code=?,error=?,entity_id=? WHERE request_id=?"),
        {status, code, error.isNull() ? QStringLiteral("") : error,
        entityId.isNull() ? QStringLiteral("") : entityId, requestId});
    return true;
}
