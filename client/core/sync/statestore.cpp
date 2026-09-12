#include "core/sync/statestore.h"

#include <QCryptographicHash>
#include <QDir>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSqlError>
#include <QUuid>
#include <limits>
#include <stdexcept>

namespace
{
QByteArray Encode(const QVariantMap& object)
{
    return QJsonDocument(QJsonObject::fromVariantMap(object)).toJson(QJsonDocument::Compact);
}

QVariantMap Decode(const QVariant& value)
{
    QJsonParseError error;
    const auto document = QJsonDocument::fromJson(value.toByteArray(), &error);
    if (error.error != QJsonParseError::NoError || !document.isObject())
    {
        throw std::runtime_error("invalid cached business object");
    }
    return document.object().toVariantMap();
}

QString ReceiptId(const QString& conversation, const QString& reader)
{
    return QString::fromUtf8(QJsonDocument(QJsonArray{conversation, reader}).toJson(QJsonDocument::Compact));
}
}

MiniImStateStore::~MiniImStateStore()
{
    close();
}

void MiniImStateStore::close()
{
    m_controlWrites.close();
    m_fileTasks.close();
    m_outbox.close();
    if (!m_name.isEmpty())
    {
        m_db.close();
        m_db = QSqlDatabase();
        QSqlDatabase::removeDatabase(m_name);
        m_name.clear();
    }
    m_cursor = 0;
}

QSqlQuery MiniImStateStore::run(const QString& sql, const QVariantList& values) const
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

bool MiniImStateStore::open(const QString& root, const QString& endpoint, const QString& user, const QString& device)
{
    close();
    m_error.clear();
    try
    {
        if (root.isEmpty() || user.isEmpty() || device.isEmpty() || !QDir().mkpath(root))
        {
            throw std::runtime_error("cannot create account state directory");
        }
        const QByteArray identity = QJsonDocument(QJsonArray{endpoint, user, device}).toJson(QJsonDocument::Compact);
        const QString hash = QString::fromLatin1(QCryptographicHash::hash(identity, QCryptographicHash::Sha256).toHex());
        m_user = user;
        m_path = QDir(root).filePath(hash + QStringLiteral(".sqlite"));
        m_name = QUuid::createUuid().toString(QUuid::WithoutBraces);
        m_db = QSqlDatabase::addDatabase(QStringLiteral("QSQLITE"), m_name);
        m_db.setDatabaseName(m_path);
        m_db.setConnectOptions(QStringLiteral("QSQLITE_BUSY_TIMEOUT=5000"));
        if (!m_db.open())
        {
            throw std::runtime_error(m_db.lastError().text().toStdString());
        }
        run(QStringLiteral("PRAGMA journal_mode=WAL"));
        run(QStringLiteral("PRAGMA synchronous=FULL"));
        run(QStringLiteral("PRAGMA foreign_keys=ON"));
        run(QStringLiteral("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"));
        run(QStringLiteral("CREATE TABLE IF NOT EXISTS seen(position INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL)"));
        run(QStringLiteral("CREATE TABLE IF NOT EXISTS objects("
            "kind TEXT NOT NULL, id TEXT NOT NULL, conversation TEXT NOT NULL, position INTEGER NOT NULL,"
            "data BLOB NOT NULL, PRIMARY KEY(kind,id))"));
        run(QStringLiteral("CREATE INDEX IF NOT EXISTS objects_conversation ON objects(kind,conversation)"));
        createHistoryIndexes();
        m_outbox.open(m_db);
        m_fileTasks.open(m_db);
        m_controlWrites.open(m_db);
        m_cursor = metadata(QStringLiteral("cursor")).toULongLong();
        return true;
    }
    catch (const std::exception& error)
    {
        m_error = QString::fromUtf8(error.what());
        close();
        return false;
    }
}

QString MiniImStateStore::metadata(const QString& key) const
{
    auto query = run(QStringLiteral("SELECT value FROM metadata WHERE key=?"), {key});
    return query.next() ? query.value(0).toString() : QString();
}

QVariantMap MiniImStateStore::object(const QString& kind, const QString& id) const
{
    auto query = run(QStringLiteral("SELECT data FROM objects WHERE kind=? AND id=?"), {kind, id});
    return query.next() ? Decode(query.value(0)) : QVariantMap();
}

void MiniImStateStore::saveObject(const QString& kind, const QString& id, quint64 position, const QVariantMap& data)
{
    if (id.isEmpty())
    {
        throw std::runtime_error("business event has no entity id");
    }
    run(QStringLiteral("INSERT INTO objects(kind,id,conversation,position,data) VALUES(?,?,?,?,?) "
        "ON CONFLICT(kind,id) DO UPDATE SET conversation=excluded.conversation,"
        "position=excluded.position,data=excluded.data"),
        {kind, id, data.value(QStringLiteral("conversationId"), QStringLiteral("")),
         QVariant::fromValue(position), Encode(data)});
}

QVariantMap MiniImStateStore::protectMessage(QVariantMap data)
{
    const auto terminal = object(QStringLiteral("terminal"), data.value(QStringLiteral("id")).toString());
    const bool recalled = data.value(QStringLiteral("recalled")).toBool() || !terminal.isEmpty();
    const bool burned = data.value(QStringLiteral("burned")).toBool() || terminal.value(QStringLiteral("burned")).toBool();
    data.insert(QStringLiteral("recalled"), recalled);
    data.insert(QStringLiteral("burned"), burned);
    if (recalled || burned)
    {
        data.insert(QStringLiteral("text"), QStringLiteral(""));
        if (data.value(QStringLiteral("senderId")).toString() == m_user)
        {
            m_outbox.settleTerminal(
                data.value(QStringLiteral("conversationId")).toString(),
                data.value(QStringLiteral("clientMsgId")).toString(), data.value(QStringLiteral("id")).toString());
        }
    }
    return data;
}

QVariantMap MiniImStateStore::project(const MiniImStateEvent& event)
{
    QVariantMap data = event.data;
    const QString conversation = data.value(QStringLiteral("conversationId")).toString();
    if (event.type == QStringLiteral("message"))
    {
        const QString id = data.value(QStringLiteral("id")).toString();
        const auto previous = object(QStringLiteral("message"), id);
        data.insert(QStringLiteral("recalled"), data.value(QStringLiteral("recalled")).toBool()
            || previous.value(QStringLiteral("recalled")).toBool());
        data.insert(QStringLiteral("burned"), data.value(QStringLiteral("burned")).toBool()
            || previous.value(QStringLiteral("burned")).toBool());
        if (object(QStringLiteral("readCount"), id).isEmpty())
        {
            saveObject(QStringLiteral("readCount"), id, event.position,
                {{"type", "readCount"}, {"eventId", event.eventId}, {"conversationId", conversation},
                 {"messageId", id}, {"globalSeq", QVariant::fromValue(event.position)},
                 {"unreadCount", data.value(QStringLiteral("unreadCount"))}});
        }
        const auto count = object(QStringLiteral("readCount"), id);
        data.insert(QStringLiteral("unreadCount"), count.value(QStringLiteral("unreadCount")));
        data.insert(QStringLiteral("readCountKnown"), true);
        data = protectMessage(data);
        saveObject(QStringLiteral("message"), id, event.position, data);
    }
    else if (event.type == QStringLiteral("recall") || event.type == QStringLiteral("burn"))
    {
        const QString id = data.value(QStringLiteral("messageId")).toString();
        const auto old = object(QStringLiteral("terminal"), id);
        data.insert(QStringLiteral("burned"), event.type == QStringLiteral("burn")
            || old.value(QStringLiteral("burned")).toBool());
        saveObject(QStringLiteral("terminal"), id, event.position, data);
        auto message = object(QStringLiteral("message"), id);
        if (!message.isEmpty())
        {
            saveObject(QStringLiteral("message"), id, event.position, protectMessage(message));
        }
    }
    else if (event.type == QStringLiteral("readCount"))
    {
        const QString id = data.value(QStringLiteral("messageId")).toString();
        auto previous = run(QStringLiteral("SELECT position,data FROM objects WHERE kind='readCount' AND id=?"), {id});
        if (previous.next() && previous.value(0).toULongLong() >= event.position)
        {
            return Decode(previous.value(1));
        }
        saveObject(QStringLiteral("readCount"), id, event.position, data);
    }
    else if (event.type == QStringLiteral("delivery"))
    {
        const QString id = ReceiptId(data.value(QStringLiteral("messageId")).toString(),
            data.value(QStringLiteral("userId")).toString());
        auto previous = run(QStringLiteral("SELECT position,data FROM objects WHERE kind='delivery' AND id=?"), {id});
        if (previous.next() && previous.value(0).toULongLong() > event.position)
        {
            return Decode(previous.value(1));
        }
        saveObject(QStringLiteral("delivery"), id, event.position, data);
    }
    else if (event.type == QStringLiteral("receipt"))
    {
        const QString id = ReceiptId(conversation, data.value(QStringLiteral("readerId")).toString());
        const auto old = object(QStringLiteral("receipt"), id);
        if (data.value(QStringLiteral("lastReadSeq")).toULongLong() > old.value(QStringLiteral("lastReadSeq")).toULongLong())
        {
            saveObject(QStringLiteral("receipt"), id, event.position, data);
        }
        else
        {
            data = old;
        }
    }
    else if (event.type == QStringLiteral("conversation") || event.type == QStringLiteral("file"))
    {
        const QString id = data.value(event.type == QStringLiteral("file")
            ? QStringLiteral("fileId") : QStringLiteral("conversationId")).toString();
        auto old = run(QStringLiteral("SELECT position,data FROM objects WHERE kind=? AND id=?"), {event.type, id});
        if (old.next())
        {
            const auto previous = Decode(old.value(1));
            const bool stale = event.type == QStringLiteral("file")
                ? data.value(QStringLiteral("version")).toULongLong() < previous.value(QStringLiteral("version")).toULongLong()
                : event.position < old.value(0).toULongLong();
            if (stale)
            {
                return previous;
            }
        }
        saveObject(event.type, id, event.position, data);
    }
    else
    {
        throw std::runtime_error("unsupported sync event");
    }
    return data;
}

bool MiniImStateStore::apply(const QVector<MiniImStateEvent>& events, QVector<MiniImStateEvent>* applied)
{
    applied->clear();
    m_error.clear();
    try
    {
        run(QStringLiteral("BEGIN IMMEDIATE"));
        QVector<MiniImStateEvent> changes;
        for (const auto& event : events)
        {
            if (event.position == 0 || event.position > static_cast<quint64>(std::numeric_limits<qint64>::max())
                || event.eventId.isEmpty())
            {
                throw std::runtime_error("sync event requires a valid position and event id");
            }
            auto seen = run(QStringLiteral("SELECT position,event_id FROM seen WHERE position=? OR event_id=?"),
                {QVariant::fromValue(event.position), event.eventId});
            if (seen.next())
            {
                if (seen.value(0).toULongLong() != event.position || seen.value(1).toString() != event.eventId)
                {
                    throw std::runtime_error("sync event position conflicts with cached event");
                }
                continue;
            }
            auto result = event;
            result.data = project(event);
            run(QStringLiteral("INSERT INTO seen(position,event_id) VALUES(?,?)"),
                {QVariant::fromValue(event.position), event.eventId});
            changes.append(result);
        }
        quint64 next = m_cursor;
        auto positions = run(QStringLiteral("SELECT position FROM seen WHERE position>? ORDER BY position"),
            {QVariant::fromValue(next)});
        while (positions.next() && positions.value(0).toULongLong() == next + 1)
        {
            ++next;
        }
        positions.finish();
        run(QStringLiteral("INSERT INTO metadata(key,value) VALUES('cursor',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"), {QString::number(next)});
        run(QStringLiteral("COMMIT"));
        m_cursor = next;
        *applied = changes;
        return true;
    }
    catch (const std::exception& error)
    {
        m_db.rollback();
        m_error = QString::fromUtf8(error.what());
        return false;
    }
}

bool MiniImStateStore::saveSession(const QString& session, const QString& ack)
{
    try
    {
        run(QStringLiteral("BEGIN IMMEDIATE"));
        for (const auto& value : {qMakePair(QStringLiteral("session"), session), qMakePair(QStringLiteral("ack"), ack)})
        {
            run(QStringLiteral("INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"), {value.first, value.second.isNull() ? QStringLiteral("") : value.second});
        }
        run(QStringLiteral("COMMIT"));
        return true;
    }
    catch (const std::exception& error)
    {
        m_db.rollback();
        m_error = QString::fromUtf8(error.what());
        return false;
    }
}

QVariantMap MiniImStateStore::snapshot() const
{
    try
    {
        QVariantList conversations, messages, files, deliveries, readCounts;
        QVariantMap reads, history;
        auto query = run(QStringLiteral("SELECT kind,data FROM objects "
            "WHERE kind IN ('conversation','file','receipt') ORDER BY position"));
        while (query.next())
        {
            const QString kind = query.value(0).toString();
            const auto data = Decode(query.value(1));
            if (kind == QStringLiteral("conversation"))
            {
                conversations.append(data);
            }
            else if (kind == QStringLiteral("file"))
            {
                files.append(data);
            }
            else
            {
                const QString conversation = data.value("conversationId").toString();
                auto progress = reads.value(conversation).toMap();
                progress.insert(data.value("readerId").toString(), data.value("lastReadSeq"));
                reads.insert(conversation, progress);
            }
        }
        auto groups = run(QStringLiteral("SELECT DISTINCT conversation FROM objects WHERE kind='message'"));
        while (groups.next())
        {
            const QString conversation = groups.value(0).toString();
            const auto page = messagePage(conversation);
            if (!page.value("ok").toBool())
            {
                throw std::runtime_error(page.value("error").toString().toStdString());
            }
            messages.append(page.value("messages").toList());
            deliveries.append(page.value("deliveries").toList());
            readCounts.append(page.value("readCounts").toList());
            history.insert(conversation, QVariantMap{{"cursor", page.value("cursor")},
                {"hasMore", page.value("hasMore")}});
        }
        return {{"currentUser", QVariantMap{{"userId", m_user}}}, {"globalCursor", QVariant::fromValue(m_cursor)},
            {"conversations", conversations}, {"recentMessages", messages}, {"unreadTotal", QVariant::fromValue(unreadTotal())},
            {"unreadAuthoritative", true}, {"historyByConversation", history},
            {"readProgressByConversation", reads}, {"readCounts", readCounts}, {"deliveries", deliveries},
            {"files", files}, {"messageSends", m_outbox.pending()}, {"fileTasks", m_fileTasks.pending()},
            {"controlWrites", m_controlWrites.pending()}};
    }
    catch (const std::exception& error)
    {
        m_error = QString::fromUtf8(error.what());
        return {};
    }
}

QVariantMap MiniImStateStore::pendingConfirmation() const
{
    const auto value = metadata(QStringLiteral("sync_confirmation"));
    return value.isEmpty() ? QVariantMap() : Decode(value.toUtf8());
}

quint64 MiniImStateStore::confirmedCursor() const
{
    return metadata(QStringLiteral("sync_confirmed_cursor")).toULongLong();
}

void MiniImStateStore::saveConfirmation(const QString& requestId, quint64 cursor)
{
    if (requestId.isEmpty() || cursor == 0 || cursor > m_cursor || cursor <= confirmedCursor()
        || !pendingConfirmation().isEmpty())
    {
        throw std::runtime_error("invalid or overlapping sync confirmation");
    }
    const QVariantMap pending{{"requestId", requestId}, {"cursor", QString::number(cursor)}};
    run(QStringLiteral("INSERT INTO metadata(key,value) VALUES('sync_confirmation',?)"),
        {QString::fromUtf8(Encode(pending))});
}

void MiniImStateStore::completeConfirmation(const QString& requestId)
{
    try
    {
        run(QStringLiteral("BEGIN IMMEDIATE"));
        const auto pending = pendingConfirmation();
        if (pending.value(QStringLiteral("requestId")).toString() == requestId)
        {
            const auto cursor = pending.value(QStringLiteral("cursor")).toString();
            run(QStringLiteral("INSERT INTO metadata(key,value) VALUES('sync_confirmed_cursor',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"), {cursor});
            run(QStringLiteral("DELETE FROM metadata WHERE key='sync_confirmation'"));
        }
        run(QStringLiteral("COMMIT"));
    }
    catch (...)
    {
        m_db.rollback();
        throw;
    }
}

QString MiniImStateStore::sessionId() const { return metadata(QStringLiteral("session")); }
QString MiniImStateStore::lastAck() const { return metadata(QStringLiteral("ack")); }
quint64 MiniImStateStore::cursor() const { return m_cursor; }
bool MiniImStateStore::hasGap() const
{
    auto query = run(QStringLiteral("SELECT 1 FROM seen WHERE position>? LIMIT 1"), {QVariant::fromValue(m_cursor)});
    return query.next();
}
QString MiniImStateStore::errorString() const { return m_error; }
QString MiniImStateStore::databasePath() const { return m_path; }

MiniImMessageOutbox& MiniImStateStore::outbox() { return m_outbox; }

MiniImFileTaskStore& MiniImStateStore::fileTasks()
{
    return m_fileTasks;
}

MiniImControlWriteStore& MiniImStateStore::controlWrites() { return m_controlWrites; }
