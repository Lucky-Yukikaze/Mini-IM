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
    const auto document = QJsonDocument::fromJson(cursor.toUtf8());
    const auto value = document.array();
    bool valid = false;
    const auto sequence = value.size() == 3 ? value[1].toString().toULongLong(&valid) : 0;
    if (cursor.size() > 2048 || !document.isArray() || value.size() != 3 || !valid
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
    return total;
}

QVariantMap MiniImStateStore::messagePage(const QString& conversation, const QString& before) const
{
    try
    {
        if (conversation.isEmpty())
        {
            throw std::runtime_error("history requires a conversation");
        }
        QString sql = QStringLiteral("SELECT id,") + kSequence
            + QStringLiteral(" FROM objects WHERE kind='message' AND conversation=?");
        QVariantList values{conversation};
        if (!before.isEmpty())
        {
            const auto cursor = ReadCursor(conversation, before);
            sql += QStringLiteral(" AND (") + kSequence + QStringLiteral(",id)<(?,?)");
            values.append(cursor[1].toString().toLongLong());
            values.append(cursor[2].toString());
        }
        sql += QStringLiteral(" ORDER BY ") + kSequence + QStringLiteral(" DESC,id DESC LIMIT ?");
        values.append(kHistoryPageSize + 1);
        auto query = run(sql, values);
        QVariantList messages, deliveries, counts;
        QString cursor;
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
            messages.prepend(message);
            auto delivered = run(QStringLiteral("SELECT id FROM objects WHERE kind='delivery' "
                "AND json_extract(CAST(data AS TEXT),'$.messageId')=? ORDER BY position"), {id});
            while (delivered.next())
            {
                deliveries.append(object(QStringLiteral("delivery"), delivered.value(0).toString()));
            }
            cursor = QString::fromUtf8(QJsonDocument(QJsonArray{conversation,
                query.value(1).toString(), id}).toJson(QJsonDocument::Compact));
        }
        return {{"ok", true}, {"userId", m_user}, {"conversationId", conversation},
            {"messages", messages}, {"deliveries", deliveries}, {"readCounts", counts},
            {"cursor", cursor}, {"hasMore", more}};
    }
    catch (const std::exception& error)
    {
        m_error = QString::fromUtf8(error.what());
        return {{"ok", false}, {"error", m_error}};
    }
}
