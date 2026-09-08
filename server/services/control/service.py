"""Route recoverable conversation and delivery writes through durable request deduplication."""
from protocol.pb import envelope_pb2
from services.conversation.service import ConversationService
from services.delivery.service import DeliveryService
from storage.repo.control_write_repo import ControlWriteRepo, ControlWriteResult


class ControlWriteService:
    def __init__(self, requests: ControlWriteRepo, conversations: ConversationService, delivery: DeliveryService):
        self.m_requests = requests
        self.m_handlers = {
            "create_conversation": conversations.handle_create_conversation,
            "add_members": conversations.handle_add_members,
            "remove_members": conversations.handle_remove_members,
            "leave_conversation": conversations.handle_leave_conversation,
            "join_conversation": conversations.handle_join_conversation,
            "rename_conversation": conversations.handle_rename_conversation,
            "receipt": delivery.handle_receipt,
            "recall": delivery.handle_recall,
        }

    def handles(self, envelope: envelope_pb2.Envelope) -> bool:
        return envelope.WhichOneof("body") in self.m_handlers

    def handle(self, user_id: str, envelope: envelope_pb2.Envelope) -> ControlWriteResult:
        operation = envelope.WhichOneof("body")
        if operation not in self.m_handlers:
            return self.m_requests.reject(envelope.request_id, 400, "unsupported control write")
        body = getattr(envelope, operation)

        def apply() -> ControlWriteResult:
            result = self.m_handlers[operation](user_id, envelope.request_id, body)
            return ControlWriteResult(result.ack, result.sync_events)

        return self.m_requests.execute(
            user_id, envelope.request_id, operation, body.SerializeToString(deterministic=True), apply)
