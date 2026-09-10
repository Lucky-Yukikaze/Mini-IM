from __future__ import annotations

import time
import hashlib
import uuid
from dataclasses import dataclass

from protocol.pb import common_pb2, file_pb2
from storage.repo.sync_event import AppendSyncEvents, StoredSyncEvent
from storage.sqlite.db import MiniImSqliteDb


@dataclass
class StoredFileTransfer:
    file_id: str
    conversation_id: str
    owner_id: str
    client_file_id: str
    file_name: str
    file_size: int
    sha256: str
    storage_path: str
    direction: int
    priority: int
    received_bytes: int
    version: int
    status: str
    updated_at_ms: int
    source_file_id: str = ""


@dataclass
class FileTransferInitResult:
    transfer: StoredFileTransfer
    sync_events: list[StoredSyncEvent]
    created: bool
    conflict: str = ""


@dataclass
class FileTransferProgressResult:
    transfer: StoredFileTransfer
    sync_events: list[StoredSyncEvent]
    changed: bool


@dataclass
class FileTransferFinishResult:
    transfer: StoredFileTransfer
    sync_events: list[StoredSyncEvent]
    changed: bool


@dataclass
class FileTransferCancelResult:
    transfer: StoredFileTransfer | None
    sync_events: list[StoredSyncEvent]
    code: int = 0
    error: str = ""


class FileRepo:
    def __init__(self, db: MiniImSqliteDb) -> None:
        self.m_db = db

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _is_uploading_status(status: str) -> bool:
        return status in {"init", "uploading", "uploaded"}

    @staticmethod
    def init_fingerprint(request: file_pb2.FileInit) -> bytes:
        identity = file_pb2.FileInit(client_file_id=request.client_file_id, direction=request.direction,
                                    source_file_id=request.source_file_id)
        if request.direction != common_pb2.FILE_DIRECTION_DOWNLOAD:
            identity.conversation_id = request.conversation_id
            identity.file_name = request.file_name
            identity.file_size = request.file_size
            identity.sha256 = request.sha256.lower()
        return hashlib.sha256(identity.SerializeToString(deterministic=True)).digest()

    def init_request_conflict(self, user_id: str, request_id: str, fingerprint: bytes) -> bool:
        connection = self.m_db.m_connection
        if connection.execute("SELECT 1 FROM control_write_results WHERE user_id=? AND request_id=?",
                              (user_id, request_id)).fetchone():
            return True
        if connection.execute("SELECT 1 FROM messages WHERE sender_id=? AND request_id=?",
                              (user_id, request_id)).fetchone():
            return True
        previous = connection.execute("SELECT fingerprint FROM file_init_requests WHERE user_id=? AND request_id=?",
                                      (user_id, request_id)).fetchone()
        if previous is not None:
            return bytes(previous["fingerprint"]) != fingerprint
        rows = connection.execute("SELECT * FROM file_transfers WHERE owner_id=? AND request_id=? LIMIT 2",
                                  (user_id, request_id)).fetchall()
        if len(rows) > 1:
            return True
        if rows:
            row = rows[0]
            legacy = file_pb2.FileInit(client_file_id=row["client_file_id"], direction=row["direction"],
                source_file_id=row["source_file_id"], conversation_id=row["conversation_id"],
                file_name=row["file_name"], file_size=row["file_size"], sha256=row["sha256"])
            return self.init_fingerprint(legacy) != fingerprint
        return False

    def owns_init_request(self, user_id: str, request_id: str) -> bool:
        connection = self.m_db.m_connection
        return bool(connection.execute("SELECT 1 FROM file_init_requests WHERE user_id=? AND request_id=?",
                                       (user_id, request_id)).fetchone()
                    or connection.execute("SELECT 1 FROM file_transfers WHERE owner_id=? AND request_id=?",
                                          (user_id, request_id)).fetchone())

    def remember_init_request(self, user_id: str, request_id: str, fingerprint: bytes) -> None:
        self.m_db.m_connection.execute("INSERT OR IGNORE INTO file_init_requests(user_id,request_id,fingerprint) VALUES(?,?,?)",
                                      (user_id, request_id, fingerprint))

    def create_or_resume_transfer(
        self, user_id: str, request_id: str, file_init: file_pb2.FileInit,
        member_ids: list[str], storage_relative_path: str, stale_timeout_ms: int,
    ) -> FileTransferInitResult:
        now_ms = self._now_ms()
        with self.m_db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM file_transfers WHERE owner_id = ? AND client_file_id = ? ORDER BY created_at_ms",
                (user_id, file_init.client_file_id),
            ).fetchall()
            created = not rows
            changed = created
            if created:
                file_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO file_transfers(
                        file_id, conversation_id, owner_id, client_file_id, request_id, file_name,
                        file_size, sha256, storage_path, direction, source_file_id, priority,
                        received_bytes, version, status, created_at_ms, updated_at_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 'init', ?, ?)
                    """,
                    (file_id, file_init.conversation_id, user_id, file_init.client_file_id, request_id,
                     file_init.file_name, int(file_init.file_size), file_init.sha256.lower(),
                     storage_relative_path, int(file_init.direction), file_init.source_file_id,
                     int(file_init.priority), now_ms, now_ms),
                )
                transfer = self.get_transfer_by_file_id(file_id)
            else:
                transfer = self._row_to_transfer(rows[0])
                if len(rows) != 1:
                    return FileTransferInitResult(transfer, [], False, "ambiguous legacy file intent")
                for field in ("conversation_id", "file_name", "file_size", "sha256", "direction", "source_file_id"):
                    expected, actual = getattr(transfer, field), getattr(file_init, field)
                    if field == "sha256":
                        expected, actual = expected.lower(), actual.lower()
                    if expected != actual:
                        return FileTransferInitResult(transfer, [], False, f"intent_id {field} mismatch")
                if transfer.status == "cancelled":
                    return FileTransferInitResult(transfer, [], False, "file intent was cancelled")
                old_request = str(rows[0]["request_id"])
                if old_request.strip():
                    owners = connection.execute(
                        "SELECT file_id FROM file_transfers WHERE owner_id=? AND request_id=? LIMIT 2",
                        (user_id, old_request)).fetchall()
                    old_fingerprint = self.init_fingerprint(file_init) if len(owners) == 1 else b""
                    self.remember_init_request(user_id, old_request, old_fingerprint)
                stale = (stale_timeout_ms > 0 and self._is_uploading_status(transfer.status)
                         and now_ms - transfer.updated_at_ms > stale_timeout_ms)
                if transfer.status != "completed":
                    next_status = "init" if stale or transfer.status.startswith("failed") else transfer.status
                    changed = stale or transfer.status != next_status or transfer.priority != int(file_init.priority)
                    if changed:
                        transfer.status = next_status
                        transfer.priority = int(file_init.priority)
                        transfer.version += 1
                        transfer.updated_at_ms = max(now_ms, transfer.updated_at_ms + 1)
                        connection.execute(
                            "UPDATE file_transfers SET request_id = ?, status = ?, priority = ?, "
                            "version = ?, updated_at_ms = ? WHERE file_id = ?",
                            (request_id, transfer.status, transfer.priority, transfer.version,
                             transfer.updated_at_ms, transfer.file_id),
                        )
            events = self._append_file_updated_sync_events(connection, member_ids, transfer) if changed else []
            return FileTransferInitResult(transfer, events, created)

    def is_cancelled(self, user_id: str, client_file_id: str) -> bool:
        return self.m_db.execute_fetchone(
            "SELECT 1 FROM file_cancellations WHERE owner_id=? AND client_file_id=?",
            (user_id, client_file_id)) is not None

    def cancelled_file_id(self, user_id: str, client_file_id: str) -> str:
        row = self.m_db.execute_fetchone(
            "SELECT file_id FROM file_cancellations WHERE owner_id=? AND client_file_id=?", (user_id, client_file_id))
        return str(row[0]) if row else ""

    def cancel_transfer(self, user_id: str, client_file_id: str, file_id: str) -> FileTransferCancelResult:
        with self.m_db.transaction() as connection:
            rows = connection.execute("SELECT * FROM file_transfers WHERE owner_id=? AND client_file_id=?",
                (user_id, client_file_id)).fetchall()
            if len(rows) > 1:
                return FileTransferCancelResult(None, [], 409, "ambiguous legacy file intent")
            transfer = self._row_to_transfer(rows[0]) if rows else None
            if file_id and (transfer is None or transfer.file_id != file_id):
                return FileTransferCancelResult(None, [], 403, "file cancellation identity mismatch")
            if transfer and transfer.status == "completed":
                return FileTransferCancelResult(transfer, [], 409, "file already completed")
            connection.execute("INSERT OR IGNORE INTO file_cancellations(owner_id,client_file_id,file_id,created_at_ms) "
                "VALUES(?,?,?,?)", (user_id, client_file_id, transfer.file_id if transfer else "", self._now_ms()))
            events = []
            if transfer and transfer.status != "cancelled":
                transfer.status = "cancelled"
                self._save_progress(connection, transfer)
                # Include the owner even after leaving the conversation.
                users = connection.execute("SELECT user_id FROM conversation_members WHERE conversation_id=?",
                    (transfer.conversation_id,)).fetchall()
                members = sorted({user_id, *(row[0] for row in users)})
                events = self._append_file_updated_sync_events(connection, members, transfer)
            return FileTransferCancelResult(transfer, events)

    def get_transfer_by_file_id(self, file_id: str) -> StoredFileTransfer | None:
        row = self.m_db.execute_fetchone("SELECT * FROM file_transfers WHERE file_id = ?", (file_id,))
        return self._row_to_transfer(row) if row is not None else None

    def _save_progress(self, connection, transfer: StoredFileTransfer) -> None:
        transfer.version += 1
        transfer.updated_at_ms = max(self._now_ms(), transfer.updated_at_ms + 1)
        connection.execute(
            "UPDATE file_transfers SET received_bytes = ?, status = ?, version = ?, updated_at_ms = ? WHERE file_id = ?",
            (transfer.received_bytes, transfer.status, transfer.version, transfer.updated_at_ms, transfer.file_id),
        )

    def apply_progress(self, file_id: str, received_bytes: int, member_ids: list[str]) -> FileTransferProgressResult | None:
        with self.m_db.transaction() as connection:
            transfer = self.get_transfer_by_file_id(file_id)
            if transfer is None:
                return None
            if received_bytes < 0 or received_bytes > transfer.file_size:
                raise ValueError("invalid transfer progress")
            changed = transfer.status not in {"completed", "cancelled"} and received_bytes > transfer.received_bytes
            if changed:
                transfer.received_bytes = int(received_bytes)
                transfer.status = "uploaded" if received_bytes == transfer.file_size else "uploading"
                self._save_progress(connection, transfer)
            events = self._append_file_updated_sync_events(connection, member_ids, transfer) if changed else []
            return FileTransferProgressResult(transfer, events, changed)

    def rewind_upload(
        self, file_id: str, received_bytes: int, member_ids: list[str], *, integrity_failed: bool = False,
    ) -> FileTransferProgressResult | None:
        """Reconcile an unfinished upload with its verified storage boundary."""
        with self.m_db.transaction() as connection:
            transfer = self.get_transfer_by_file_id(file_id)
            if transfer is None or transfer.direction != common_pb2.FILE_DIRECTION_UPLOAD:
                return None
            if not 0 <= received_bytes <= transfer.received_bytes:
                raise ValueError("invalid upload recovery offset")
            changed = transfer.status not in {"completed", "cancelled"} and (received_bytes < transfer.received_bytes
                or (integrity_failed and transfer.status != "failed_integrity"))
            if changed:
                transfer.received_bytes = received_bytes
                transfer.status = "failed_integrity" if integrity_failed else "uploading" if received_bytes else "init"
                self._save_progress(connection, transfer)
            events = self._append_file_updated_sync_events(connection, member_ids, transfer) if changed else []
            return FileTransferProgressResult(transfer, events, changed)

    def apply_finish(self, file_id: str, success: bool, member_ids: list[str]) -> FileTransferFinishResult | None:
        with self.m_db.transaction() as connection:
            transfer = self.get_transfer_by_file_id(file_id)
            if transfer is None:
                return None
            if transfer.status == "cancelled":
                return FileTransferFinishResult(transfer, [], False)
            target_status = "completed" if success or transfer.status == "completed" else "failed"
            changed = target_status != transfer.status
            if changed:
                transfer.status = target_status
                if success:
                    transfer.received_bytes = transfer.file_size
                self._save_progress(connection, transfer)
            events = self._append_file_updated_sync_events(connection, member_ids, transfer) if changed else []
            return FileTransferFinishResult(transfer, events, changed)

    def _append_file_updated_sync_events(
        self,
        connection,
        member_ids: list[str],
        transfer: StoredFileTransfer,
    ) -> list[StoredSyncEvent]:
        updated = file_pb2.FileUpdated(
            event_id="",
            file_id=transfer.file_id,
            conversation_id=transfer.conversation_id,
            transferred_bytes=int(transfer.received_bytes),
            completed=transfer.status == "completed",
            version=int(transfer.version),
            updated_at_ms=int(transfer.updated_at_ms),
            file_size=transfer.file_size,
            sha256=transfer.sha256,
            direction=transfer.direction,
            status=transfer.status,
            file_name=transfer.file_name,
        )
        return AppendSyncEvents(
            connection, member_ids, transfer.conversation_id, "file_updated", updated, transfer.updated_at_ms,
        )

    @staticmethod
    def _row_to_transfer(row) -> StoredFileTransfer:
        return StoredFileTransfer(
            file_id=str(row["file_id"]),
            conversation_id=str(row["conversation_id"]),
            owner_id=str(row["owner_id"]),
            client_file_id=str(row["client_file_id"]),
            file_name=str(row["file_name"]),
            file_size=int(row["file_size"]),
            sha256=str(row["sha256"]),
            storage_path=str(row["storage_path"]),
            direction=int(row["direction"]),
            priority=int(row["priority"]),
            received_bytes=int(row["received_bytes"]),
            version=int(row["version"]),
            status=str(row["status"]),
            updated_at_ms=int(row["updated_at_ms"]),
            source_file_id=str(row["source_file_id"]),
        )
