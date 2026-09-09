from __future__ import annotations

import json

from storage.repo.control_write_repo import ControlWriteRepo, ControlWriteResult
from protocol.pb import conversation_pb2, file_pb2, message_pb2, sync_pb2
from storage.repo import SyncRepo


class SyncService:
    def __init__(self, sync_repo: SyncRepo) -> None:
        self.m_sync_repo = sync_repo
        self.m_requests = ControlWriteRepo(sync_repo.m_db)

    def handle_sync_applied(self, user_id: str, device_id: str, request_id: str,
                            request: sync_pb2.SyncApplied) -> ControlWriteResult:
        # Bind the fingerprint to the authenticated device, not the envelope's claimed device.
        fingerprint = json.dumps([device_id, request.SerializeToString(deterministic=True).hex()],
                                 separators=(",", ":")).encode("utf-8")
        return self.m_requests.execute(user_id, request_id, "sync_applied", fingerprint,
            lambda: self.m_sync_repo.confirm_applied(user_id, device_id, request_id, int(request.global_cursor)))

    def handle_sync_request(self, user_id: str, request: sync_pb2.SyncRequest) -> tuple[sync_pb2.SyncResponse, int]:
        limit = int(request.limit) if request.limit > 0 else 50
        limit = max(1, min(limit, 200))

        rows = self.m_sync_repo.load_events(
            user_id=user_id,
            after_global_cursor=int(request.global_cursor),
            limit=limit + 1,
            conversation_id=request.conversation_id,
        )

        has_more = len(rows) > limit
        selected_rows = rows[:limit]

        response = sync_pb2.SyncResponse()
        response.has_more = has_more
        new_global_cursor = int(request.global_cursor)

        for row in selected_rows:
            event = response.events.add()
            event.event_id = row.event_id
            event.global_seq = row.seq

            if row.event_type == "message":
                message = message_pb2.Message()
                message.ParseFromString(row.payload)
                event.message.CopyFrom(message)
            elif row.event_type == "recall":
                recall = message_pb2.Recall()
                recall.ParseFromString(row.payload)
                event.recall.CopyFrom(recall)
            elif row.event_type == "receipt":
                receipt = message_pb2.Receipt()
                receipt.ParseFromString(row.payload)
                event.receipt.CopyFrom(receipt)
            elif row.event_type == "conversation_updated":
                updated = conversation_pb2.ConversationUpdated()
                updated.ParseFromString(row.payload)
                event.conversation_updated.CopyFrom(updated)
            elif row.event_type == "file_updated":
                updated = file_pb2.FileUpdated()
                updated.ParseFromString(row.payload)
                event.file_updated.CopyFrom(updated)
            elif row.event_type == "delivery_updated":
                event.delivery_updated.ParseFromString(row.payload)
            else:
                continue

            new_global_cursor = max(new_global_cursor, row.seq)

        response.new_global_cursor = new_global_cursor
        return response, new_global_cursor
