// Converts protocol fields to the business objects exposed by the desktop bridge.
#ifndef MINI_IM_CORE_MODEL_EVENTMAPPER_H_
#define MINI_IM_CORE_MODEL_EVENTMAPPER_H_

#include <QVariantMap>
#include <string>
namespace im
{
namespace message { class Message; class Receipt; class Recall; }
namespace sync { class DeliveryUpdated; }
namespace conversation { class ConversationUpdated; }
}

namespace miniim
{
QVariantMap BuildMessagePayload(const im::message::Message& message);
QVariantMap BuildConversationPayload(const im::conversation::ConversationUpdated& updated);
QVariantMap BuildDeliveryPayload(const im::sync::DeliveryUpdated& updated);
QVariantMap BuildReceiptPayload(const im::message::Receipt& receipt);
QVariantMap BuildRecallPayload(const im::message::Recall& recall);
QVariantMap BuildFileProgressPayload(
    const std::string& eventId, const std::string& fileId, const std::string& conversationId,
    quint64 bytes, bool completed, quint64 version, qlonglong updatedAtMs);
}

#endif
