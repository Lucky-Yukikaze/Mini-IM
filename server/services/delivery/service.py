from __future__ import annotations

import time
from dataclasses import dataclass

from protocol.pb import message_pb2
from storage.repo import DeliveryRepo, StoredSyncEvent


@dataclass
class DeliveryServiceResult:
    ack: message_pb2.Ack
    sync_events: list[StoredSyncEvent]


class DeliveryService:
    def __init__(self, delivery_repo: DeliveryRepo) -> None:
        self.m_delivery_repo = delivery_repo

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def handle_receipt(
        self,
        user_id: str,
        request_id: str,
        receipt: message_pb2.Receipt,
    ) -> DeliveryServiceResult:
        now_ms = self._now_ms()
        ack = message_pb2.Ack(
            request_id=request_id,
            success=False,
            code=400,
            message="invalid receipt",
            entity_id="",
            server_time_ms=now_ms,
        )
        if not receipt.conversation_id:
            return DeliveryServiceResult(ack=ack, sync_events=[])

        result = self.m_delivery_repo.apply_receipt(
            user_id=user_id,
            conversation_id=receipt.conversation_id,
            last_read_seq=int(receipt.last_read_seq),
        )
        if result is None:
            ack.code = 403
            ack.message = "reader is not a conversation member"
            return DeliveryServiceResult(ack=ack, sync_events=[])

        ack.success = True
        ack.code = 0
        ack.message = "ok" if result.updated else "ok(idempotent)"
        ack.entity_id = receipt.conversation_id
        ack.server_time_ms = self._now_ms()
        return DeliveryServiceResult(ack=ack, sync_events=result.sync_events)

    def handle_recall(
        self,
        user_id: str,
        request_id: str,
        recall: message_pb2.Recall,
    ) -> DeliveryServiceResult:
        now_ms = self._now_ms()
        ack = message_pb2.Ack(
            request_id=request_id,
            success=False,
            code=400,
            message="invalid recall",
            entity_id="",
            server_time_ms=now_ms,
        )
        if not recall.conversation_id or not recall.message_id:
            return DeliveryServiceResult(ack=ack, sync_events=[])

        result = self.m_delivery_repo.apply_recall(
            user_id=user_id,
            conversation_id=recall.conversation_id,
            message_id=recall.message_id,
        )
        if result is None:
            ack.code = 403
            ack.message = "recall rejected"
            return DeliveryServiceResult(ack=ack, sync_events=[])

        ack.success = True
        ack.code = 0
        ack.message = "ok" if result.updated else "ok(idempotent)"
        ack.entity_id = recall.message_id
        ack.server_time_ms = self._now_ms()
        return DeliveryServiceResult(ack=ack, sync_events=result.sync_events)
