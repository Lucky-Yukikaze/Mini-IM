// Indexed access to message history and unread counts independent of visible pages.
#include "core/sync/statestore.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <limits>
#include <stdexcept>

namespace
{
constexpr int kHistoryPageSize = 50;
const QString kSequence = QStringLiteral("CAST(json_extract(CAST(data AS TEXT),'$.seq') AS INTEGER)");
const QString kUnread = QStringLiteral("kind='message' AND "
    "COALESCE(json_extract(CAST(data AS TEXT),'$.recalled'),0)=0 AND "
    "COALESCE(json_extract(CAST(data AS TEXT),'$.burned'),0)=0");

QJsonArray ReadCursor(const QString& conversation, const QString& cursor)
{
    if (cursor.size() > 2048)
    {
        throw std::runtime_error("invalid history cursor");
    }
    const auto document = QJsonDocument::fromJson(cursor.toUtf8());
    const auto value = document.array();
    bool valid = false;
    const auto sequence = value.size() == 3 ? value[1].toString().toULongLong(&valid) : 0;
    if (!document.isArray() || value.size() != 3 || !valid
        || sequence > static_cast<quint64>(std::numeric_limits<qint64>::max())
        || value[0].toString() != conversation || value[2].toString().isEmpty())
    {
        throw std::runtime_error("invalid history cursor");
    }
    return value;
}
}

void MiniImStateStore::createHistoryIndexes()
{
    // Expression indexes preserve the existing projection table and are maintained
    // by the same transaction as every message or terminal-state update.
    run(QStringLiteral("CREATE INDEX IF NOT EXISTS objects_message_order ON objects(conversation,")
        + kSequence + QStringLiteral(",id) WHERE kind='message'"));
    run(QStringLiteral("CREATE INDEX IF NOT EXISTS objects_message_unread ON objects(conversation,")
        + kSequence + QStringLiteral(",json_extract(CAST(data AS TEXT),'$.senderId')) WHERE ") + kUnread);
    run(QStringLiteral("CREATE INDEX IF NOT EXISTS objects_delivery_message ON objects("
        "json_extract(CAST(data AS TEXT),'$.messageId'),position) WHERE kind='delivery'"));
}

quint64 MiniImStateStore::unreadTotal() const
{
    if (m_unreadTotal)
    {
        return *m_unreadTotal;
    }
    quint64 total = 0;
    auto groups = run(QStringLiteral("SELECT DISTINCT conversation FROM objects WHERE kind='message'"));
    while (groups.next())
    {
        const QString conversation = groups.value(0).toString();
        const QString receiptId = QString::fromUtf8(QJsonDocument(QJsonArray{conversation, m_user})
            .toJson(QJsonDocument::Compact));
        const auto read = object(QStringLiteral("receipt"), receiptId).value("lastReadSeq", 0);
        auto count = run(QStringLiteral("SELECT COUNT(*) FROM objects WHERE ") + kUnread
            + QStringLiteral(" AND conversation=? AND ") + kSequence
            + QStringLiteral(">? AND json_extract(CAST(data AS TEXT),'$.senderId')<>?"),
            {conversation, read, m_user});
        if (count.next())
        {
            total += count.value(0).toULongLong();
        }
    }
    m_unreadTotal = total;
    return total;
}

quint64 MiniImStateStore::unreadAffected(const MiniImStateEvent& event) const
{
    QString conversation;
    QString predicate;
    QVariantList values;
    if (event.type == "receipt")
    {
        if (event.data.value("readerId").toString() != m_user)
        {
            return 0;
        }
        conversation = event.data.value("conversationId").toString();
        predicate = QStringLiteral("conversation=? AND ") + kSequence + QStringLiteral("<=?");
        values = {conversation, event.data.value("lastReadSeq")};
    }
    else if (event.type == "message" || event.type == "recall" || event.type == "burn")
    {
        const QString id = event.data.value(event.type == "message" ? "id" : "messageId").toString();
        const auto message = object(QStringLiteral("message"), id);
        if (message.isEmpty())
        {
            return 0;
        }
        conversation = message.value("conversationId").toString();
        predicate = QStringLiteral("id=?");
        values = {id};
    }
    else
    {
        return 0;
    }
    const auto receipt = QString::fromUtf8(QJsonDocument(QJsonArray{conversation, m_user})
        .toJson(QJsonDocument::Compact));
    const auto read = object(QStringLiteral("receipt"), receipt).value("lastReadSeq", 0);
    values.append(read);
    values.append(m_user);
    // A receipt counts only its newly read interval; after projection that interval
    // is empty. Other events inspect the affected message before and after merging.
    auto count = run(QStringLiteral("SELECT COUNT(*) FROM objects WHERE ") + kUnread
        + QStringLiteral(" AND ") + predicate + QStringLiteral(" AND ") + kSequence
        + QStringLiteral(">? AND json_extract(CAST(data AS TEXT),'$.senderId')<>?"), values);
    return count.next() ? count.value(0).toULongLong() : 0;
}

QVariantMap MiniImStateStore::messagePage(
    const QString& conversation, const QString& boundary, const QString& direction) const
{
    try
    {
        if (conversation.isEmpty())
        {
            throw std::runtime_error("history requires a conversation");
        }
        const bool newer = direction == QStringLiteral("newer");
        if ((direction != "older" && direction != "latest" && !newer)
            || (newer && boundary.isEmpty()) || (direction == "latest" && !boundary.isEmpty()))
        {
            throw std::runtime_error("invalid history direction or boundary");
        }
        QString sql = QStringLiteral("SELECT id,") + kSequence
            + QStringLiteral(" FROM objects WHERE kind='message' AND conversation=?");
        QVariantList values{conversation};
        if (!boundary.isEmpty())
        {
            const auto cursor = ReadCursor(conversation, boundary);
            sql += QStringLiteral(" AND ") + kSequence
                + (newer ? QStringLiteral(">=? AND (") : QStringLiteral("<=? AND (")) + kSequence
                + (newer ? QStringLiteral(",id)>(?,?)") : QStringLiteral(",id)<(?,?)"));
            values.append(cursor[1].toString().toLongLong());
            values.append(cursor[1].toString().toLongLong());
            values.append(cursor[2].toString());
        }
        sql += QStringLiteral(" ORDER BY ") + kSequence
            + (newer ? QStringLiteral(" ASC,id ASC LIMIT ?") : QStringLiteral(" DESC,id DESC LIMIT ?"));
        values.append(kHistoryPageSize + 1);
        auto query = run(sql, values);
        QVariantList messages, deliveries, counts;
        QString cursor, firstVisited;
        bool more = false;
        while (query.next())
        {
            if (messages.size() == kHistoryPageSize)
            {
                more = true;
                break;
            }
            const QString id = query.value(0).toString();
            auto message = object(QStringLiteral("message"), id);
            const auto count = object(QStringLiteral("readCount"), id);
            message.insert("readCountKnown", !count.isEmpty());
            if (!count.isEmpty())
            {
                message.insert("unreadCount", count.value("unreadCount"));
                counts.append(count);
            }
            if (newer)
            {
                messages.append(message);
            }
            else
            {
                messages.prepend(message);
            }
            auto delivered = run(QStringLiteral("SELECT id FROM objects WHERE kind='delivery' "
                "AND json_extract(CAST(data AS TEXT),'$.messageId')=? ORDER BY position"), {id});
            while (delivered.next())
            {
                deliveries.append(object(QStringLiteral("delivery"), delivered.value(0).toString()));
            }
            cursor = QString::fromUtf8(QJsonDocument(QJsonArray{conversation,
                query.value(1).toString(), id}).toJson(QJsonDocument::Compact));
            if (firstVisited.isEmpty())
            {
                firstVisited = cursor;
            }
        }
        const auto before = messages.isEmpty() ? boundary : (newer ? firstVisited : cursor);
        const auto after = messages.isEmpty() ? boundary : (newer ? cursor : firstVisited);
        const auto hasBeyond = [&](const QString& edge, bool forward)
        {
            if (edge.isEmpty())
            {
                return false;
            }
            const auto key = ReadCursor(conversation, edge);
            // Explicit sequence range and ordering select the existing history index;
            // a tuple predicate alone can choose the general conversation index.
            const auto sequence = key[1].toString().toLongLong();
            auto exists = run(QStringLiteral("SELECT 1 FROM objects WHERE kind='message' AND conversation=? AND ")
                + kSequence + (forward ? QStringLiteral(">=? AND (") : QStringLiteral("<=? AND ("))
                + kSequence + (forward ? QStringLiteral(",id)>(?,?)") : QStringLiteral(",id)<(?,?)"))
                + QStringLiteral(" ORDER BY ") + kSequence
                + (forward ? QStringLiteral(" ASC,id ASC LIMIT 1") : QStringLiteral(" DESC,id DESC LIMIT 1")),
                {conversation, sequence, sequence, key[2].toString()});
            return exists.next();
        };
        return {{"ok", true}, {"userId", m_user}, {"conversationId", conversation},
            {"messages", messages}, {"deliveries", deliveries}, {"readCounts", counts},
            {"cursor", cursor}, {"hasMore", more}, {"beforeCursor", before}, {"afterCursor", after},
            {"hasOlder", newer ? hasBeyond(before, false) : more},
            {"hasNewer", newer ? more : hasBeyond(after, true)}};
    }
    catch (const std::exception& error)
    {
        m_error = QString::fromUtf8(error.what());
        return {{"ok", false}, {"error", m_error}};
    }
}
