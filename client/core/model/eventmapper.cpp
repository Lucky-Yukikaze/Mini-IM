#include "core/model/eventmapper.h"

#include "message.pb.h"
#include "sync.pb.h"
#include "conversation.pb.h"
#include <QVariantList>

namespace miniim
{
QVariantMap BuildMessagePayload(const im::message::Message& message)
{
    const std::string content = message.content();
    QVariantMap item;
    item.insert(QStringLiteral("id"), QString::fromStdString(message.message_id()));
    item.insert(QStringLiteral("conversationId"), QString::fromStdString(message.conversation_id()));
    item.insert(QStringLiteral("senderId"), QString::fromStdString(message.sender_id()));
    item.insert(QStringLiteral("clientMsgId"), QString::fromStdString(message.client_msg_id()));
    item.insert(QStringLiteral("seq"), static_cast<qulonglong>(message.seq()));
    item.insert(QStringLiteral("text"), QString::fromUtf8(content.data(), static_cast<int>(content.size())));
    item.insert(QStringLiteral("createdAtMs"), static_cast<qlonglong>(message.created_at_ms()));
    item.insert(QStringLiteral("recalled"), message.recalled());
    item.insert(QStringLiteral("burned"), false);
    item.insert(QStringLiteral("unreadCount"), static_cast<uint>(message.unread_count()));
    item.insert(QStringLiteral("burnMode"), static_cast<uint>(message.burn_mode()));
    item.insert(QStringLiteral("burnTtlSec"), static_cast<uint>(message.burn_ttl_sec()));
    return item;
}

QVariantMap BuildConversationPayload(const im::conversation::ConversationUpdated& updated)
{
    QVariantMap payload;
    QVariantList member_ids;
    for (const auto& member_id : updated.member_ids())
    {
        member_ids.append(QString::fromStdString(member_id));
    }
    payload.insert(QStringLiteral("eventId"), QString::fromStdString(updated.event_id()));
    payload.insert(QStringLiteral("conversationId"), QString::fromStdString(updated.conversation_id()));
    payload.insert(QStringLiteral("updatedAtMs"), static_cast<qlonglong>(updated.updated_at_ms()));
    payload.insert(QStringLiteral("title"), QString::fromStdString(updated.title()));
    payload.insert(QStringLiteral("type"), updated.type() == im::common::CONVERSATION_GROUP ? QStringLiteral("group") : QStringLiteral("direct"));
    payload.insert(QStringLiteral("ownerId"), QString::fromStdString(updated.owner_id()));
    payload.insert(QStringLiteral("memberIds"), member_ids);
    return payload;
}

QVariantMap BuildReadCountPayload(const im::sync::ReadCountUpdated& updated)
{
    return {{"type", "readCount"}, {"eventId", QString::fromStdString(updated.event_id())},
        {"conversationId", QString::fromStdString(updated.conversation_id())},
        {"messageId", QString::fromStdString(updated.message_id())},
        {"unreadCount", static_cast<uint>(updated.unread_count())}};
}

QVariantMap BuildDeliveryPayload(const im::sync::DeliveryUpdated& updated)
{
    return {{"type", "delivery"}, {"eventId", QString::fromStdString(updated.event_id())},
        {"conversationId", QString::fromStdString(updated.conversation_id())},
        {"messageId", QString::fromStdString(updated.message_id())},
        {"userId", QString::fromStdString(updated.user_id())}, {"status", QString::fromStdString(updated.status())},
        {"sentAtMs", static_cast<qlonglong>(updated.sent_at_ms())},
        {"deliveredAtMs", static_cast<qlonglong>(updated.delivered_at_ms())},
        {"readAtMs", static_cast<qlonglong>(updated.read_at_ms())},
        {"failedAtMs", static_cast<qlonglong>(updated.failed_at_ms())},
        {"failureReason", QString::fromStdString(updated.failure_reason())}};
}

QVariantMap BuildReceiptPayload(const im::message::Receipt& receipt)
{
    QVariantMap payload;
    payload.insert(QStringLiteral("type"), QStringLiteral("receipt"));
    payload.insert(QStringLiteral("eventId"), QString::fromStdString(receipt.event_id()));
    payload.insert(QStringLiteral("conversationId"), QString::fromStdString(receipt.conversation_id()));
    payload.insert(QStringLiteral("lastReadSeq"), static_cast<qulonglong>(receipt.last_read_seq()));
    payload.insert(QStringLiteral("readAtMs"), static_cast<qlonglong>(receipt.read_at_ms()));
    payload.insert(QStringLiteral("readerId"), QString::fromStdString(receipt.reader_id()));
    return payload;
}

QVariantMap BuildRecallPayload(const im::message::Recall& recall)
{
    QVariantMap payload;
    const QString operator_id = QString::fromStdString(recall.operator_id());
    payload.insert(
        QStringLiteral("type"),
        operator_id == QStringLiteral("system-burn") ? QStringLiteral("burn") : QStringLiteral("recall"));
    payload.insert(QStringLiteral("eventId"), QString::fromStdString(recall.event_id()));
    payload.insert(QStringLiteral("conversationId"), QString::fromStdString(recall.conversation_id()));
    payload.insert(QStringLiteral("messageId"), QString::fromStdString(recall.message_id()));
    payload.insert(QStringLiteral("tsMs"), static_cast<qlonglong>(recall.ts_ms()));
    payload.insert(QStringLiteral("operatorId"), operator_id);
    return payload;
}

QVariantMap BuildFileProgressPayload(
    const std::string& event_id,
    const std::string& file_id,
    const std::string& conversation_id,
    quint64 transferred_bytes,
    bool completed,
    quint64 version,
    qlonglong updated_at_ms)
{
    QVariantMap payload;
    payload.insert(QStringLiteral("eventId"), QString::fromStdString(event_id));
    payload.insert(QStringLiteral("fileId"), QString::fromStdString(file_id));
    payload.insert(QStringLiteral("conversationId"), QString::fromStdString(conversation_id));
    payload.insert(QStringLiteral("transferredBytes"), static_cast<qulonglong>(transferred_bytes));
    payload.insert(QStringLiteral("completed"), completed);
    payload.insert(QStringLiteral("version"), static_cast<qulonglong>(version));
    payload.insert(QStringLiteral("updatedAtMs"), updated_at_ms);
    return payload;
}


}
