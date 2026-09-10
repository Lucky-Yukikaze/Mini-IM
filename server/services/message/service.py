from __future__ import annotations

import time
from dataclasses import dataclass

from protocol.pb import common_pb2, message_pb2
from storage.repo import ConversationRepo, MessageRepo, StoredSyncEvent
from storage.repo.message_intent import MessageIntentFingerprint


@dataclass
class SendMessageResult:
    ack: message_pb2.Ack
    message_push: message_pb2.MessagePush | None
    sync_events: list[StoredSyncEvent]


class MessageService:
    BURN_MODE_OFF = 0
    BURN_MODE_AFTER_READ = 1
    MIN_BURN_TTL_SEC = 5
    MAX_BURN_TTL_SEC = 604800

    def __init__(
        self,
        message_repo: MessageRepo,
        conversation_repo: ConversationRepo,
        burn_enabled: bool = True,
    ) -> None:
        self.m_message_repo = message_repo
        self.m_conversation_repo = conversation_repo
        self.m_burn_enabled = bool(burn_enabled)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def handle_send_message(
        self,
        user_id: str,
        request_id: str,
        send_message: message_pb2.SendMessage,
    ) -> SendMessageResult:
        now_ms = self._now_ms()
        ack = message_pb2.Ack(
            request_id=request_id,
            success=False,
            code=400,
            message="invalid send_message",
            entity_id="",
            server_time_ms=now_ms,
        )
        if not send_message.conversation_id or not send_message.client_msg_id:
            return SendMessageResult(ack=ack, message_push=None, sync_events=[])

        if not self.m_conversation_repo.is_member(send_message.conversation_id, user_id):
            ack.code = 403
            ack.message = "sender is not a conversation member"
            return SendMessageResult(ack=ack, message_push=None, sync_events=[])

        fingerprint = MessageIntentFingerprint(send_message)
        existing = self.m_message_repo.get_message_by_client_msg_id(
            send_message.conversation_id,
            user_id,
            send_message.client_msg_id,
        )
        if existing is not None:
            original = self.m_message_repo.get_intent_fingerprint(existing.message_id)
            if original is None or original != fingerprint:
                ack.code = 409
                ack.message = ("original message intent unavailable after legacy content purge" if original is None
                               else "client_msg_id reused with different message content or settings")
                return SendMessageResult(ack=ack, message_push=None, sync_events=[])
            ack.success = True
            ack.code = 0
            ack.message = "ok(idempotent)"
            ack.entity_id = existing.message_id
            ack.server_time_ms = self._now_ms()
            return SendMessageResult(ack=ack, message_push=None, sync_events=[])

        normalized = message_pb2.SendMessage()
        normalized.CopyFrom(send_message)
        if normalized.type == int(common_pb2.MSG_UNSPECIFIED):
            normalized.type = int(common_pb2.MSG_TEXT)
        if normalized.type == int(common_pb2.MSG_SYSTEM):
            normalized.burn_mode = self.BURN_MODE_OFF
            normalized.burn_ttl_sec = 0

        burn_mode = int(normalized.burn_mode)
        burn_ttl_sec = int(normalized.burn_ttl_sec)
        if not self.m_burn_enabled:
            burn_mode = self.BURN_MODE_OFF
            burn_ttl_sec = 0
            normalized.burn_mode = self.BURN_MODE_OFF
            normalized.burn_ttl_sec = 0
        if burn_mode not in (self.BURN_MODE_OFF, self.BURN_MODE_AFTER_READ):
            ack.code = 400
            ack.message = "invalid burn_mode"
            return SendMessageResult(ack=ack, message_push=None, sync_events=[])
        if burn_mode == self.BURN_MODE_OFF:
            normalized.burn_ttl_sec = 0
        else:
            if burn_ttl_sec < self.MIN_BURN_TTL_SEC or burn_ttl_sec > self.MAX_BURN_TTL_SEC:
                ack.code = 400
                ack.message = "invalid burn_ttl_sec"
                return SendMessageResult(ack=ack, message_push=None, sync_events=[])
            normalized.burn_ttl_sec = burn_ttl_sec

        member_ids = self.m_conversation_repo.list_member_ids(send_message.conversation_id)
        stored = self.m_message_repo.append_message(
            request_id=request_id,
            sender_id=user_id,
            send_message=normalized,
            member_ids=member_ids,
            intent_fingerprint=fingerprint,
        )

        message_push = message_pb2.MessagePush(
            event_id=stored.sync_events[0].event_id if stored.sync_events else "",
        )
        message_push.messages.append(stored.message)

        ack.success = True
        ack.code = 0
        ack.message = "ok"
        ack.entity_id = stored.message.message_id
        ack.server_time_ms = self._now_ms()
        return SendMessageResult(ack=ack, message_push=message_push, sync_events=stored.sync_events)
