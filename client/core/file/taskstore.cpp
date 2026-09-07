#include "core/file/taskstore.h"

#include <QJsonDocument>
#include <QJsonObject>
#include <QSqlError>
#include <stdexcept>

namespace
{
QVariantMap Decode(const QVariant& value)
{
    QJsonParseError error;
    const auto document = QJsonDocument::fromJson(value.toByteArray(), &error);
    if (error.error != QJsonParseError::NoError || !document.isObject())
    {
        throw std::runtime_error("invalid persisted file task");
    }
    return document.object().toVariantMap();
}

QByteArray Encode(const QVariantMap& value)
{
    return QJsonDocument(QJsonObject::fromVariantMap(value)).toJson(QJsonDocument::Compact);
}
}

QSqlQuery MiniImFileTaskStore::run(const QString& sql, const QVariantList& values) const
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

void MiniImFileTaskStore::open(const QSqlDatabase& database)
{
    m_db = database;
    run(QStringLiteral("CREATE TABLE IF NOT EXISTS file_tasks("
        "id TEXT PRIMARY KEY,init_request TEXT UNIQUE NOT NULL,finish_request TEXT UNIQUE NOT NULL,"
        "file_id TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,data BLOB NOT NULL)"));
}

void MiniImFileTaskStore::close()
{
    m_db = QSqlDatabase();
}

void MiniImFileTaskStore::create(const QVariantMap& task)
{
    for (const char* key : {"clientFileId", "requestId", "finishRequestId", "conversationId", "path"})
    {
        if (task.value(QLatin1String(key)).toString().isEmpty())
        {
            throw std::runtime_error("file task requires stable identity and path");
        }
    }
    run(QStringLiteral("INSERT INTO file_tasks(id,init_request,finish_request,status,data) VALUES(?,?,?,'pending',?)"),
        {task.value("clientFileId"), task.value("requestId"), task.value("finishRequestId"), Encode(task)});
}

QVariantMap MiniImFileTaskStore::find(const QString& field, const QString& value) const
{
    if (value.isEmpty())
    {
        return {};
    }
    auto query = run(QStringLiteral("SELECT data FROM file_tasks WHERE ") + field + QStringLiteral("=?"), {value});
    return query.next() ? Decode(query.value(0)) : QVariantMap();
}

QVariantMap MiniImFileTaskStore::task(const QString& id) const
{
    return find(QStringLiteral("id"), id);
}

QVariantMap MiniImFileTaskStore::byFile(const QString& fileId) const
{
    return find(QStringLiteral("file_id"), fileId);
}

QVariantMap MiniImFileTaskStore::byRequest(const QString& requestId) const
{
    auto result = find(QStringLiteral("init_request"), requestId);
    return result.isEmpty() ? find(QStringLiteral("finish_request"), requestId) : result;
}

QVariantList MiniImFileTaskStore::pending() const
{
    auto query = run(QStringLiteral("SELECT data FROM file_tasks WHERE status NOT IN ('completed','cancelled') ORDER BY rowid"));
    QVariantList result;
    while (query.next())
    {
        result.append(Decode(query.value(0)));
    }
    return result;
}

void MiniImFileTaskStore::update(const QString& id, const QVariantMap& changes)
{
    auto value = task(id);
    if (value.isEmpty())
    {
        throw std::runtime_error("file task not found");
    }
    const auto previousStatus = value.value("status").toString();
    if (previousStatus == QStringLiteral("completed") || previousStatus == QStringLiteral("cancelled"))
    {
        return;
    }
    const bool metadataWasReady = value.value("metadataReady").toBool();
    const QStringList immutable{"clientFileId", "requestId", "finishRequestId", "conversationId",
        "path", "direction", "sourceFileId", "priority"};
    for (auto it = changes.begin(); it != changes.end(); ++it)
    {
        if ((immutable.contains(it.key()) || (it.key() == "fileId" && !value.value("fileId").toString().isEmpty()))
            && value.value(it.key()) != it.value())
        {
            throw std::runtime_error("file task identity cannot change");
        }
        if ((it.key() == "fileSize" || it.key() == "sha256") && metadataWasReady
            && value.value(it.key()) != it.value())
        {
            throw std::runtime_error("file task metadata cannot change");
        }
        value.insert(it.key(), it.value());
    }
    run(QStringLiteral("UPDATE file_tasks SET file_id=?,status=?,data=? WHERE id=?"),
        {value.value("fileId", QStringLiteral("")), value.value("status"), Encode(value), id});
}
