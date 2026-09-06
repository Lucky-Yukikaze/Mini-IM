from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from protocol.pb import file_pb2
from storage.repo.conversation_repo import StoredSyncEvent
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


@dataclass
class FileTransferInitResult:
    transfer: StoredFileTransfer
    sync_events: list[StoredSyncEvent]
    created: bool
    size_conflict: bool


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


class FileRepo:
    def __init__(self, db: MiniImSqliteDb) -> None:
        self.m_db = db

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _next_global_seq(connection, user_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM sync_events WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["next_seq"])

    @staticmethod
    def _is_uploading_status(status: str) -> bool:
        return status in {"init", "uploading", "uploaded"}

    def create_or_resume_transfer(
        self,
        user_id: str,
        request_id: str,
        file_init: file_pb2.FileInit,
        member_ids: list[str],
        storage_relative_path: str,
        stale_timeout_ms: int,
    ) -> FileTransferInitResult:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection
        created = False
        size_conflict = False

        with connection:
            row = connection.execute(
                """
                SELECT
                  file_id,
                  conversation_id,
                  owner_id,
                  client_file_id,
                  file_name,
                  file_size,
                  sha256,
                  storage_path,
                  direction,
                  priority,
                  received_bytes,
                  version,
                  status,
                  updated_at_ms
                FROM file_transfers
                WHERE owner_id = ? AND conversation_id = ? AND client_file_id = ?
                """,
                (user_id, file_init.conversation_id, file_init.client_file_id),
            ).fetchone()

            if row is None:
                created = True
                file_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO file_transfers(
                      file_id,
                      conversation_id,
                      owner_id,
                      client_file_id,
                      request_id,
                      file_name,
                      file_size,
                      sha256,
                      storage_path,
                      direction,
                      priority,
                      received_bytes,
                      version,
                      status,
                      created_at_ms,
                      updated_at_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 'init', ?, ?)
                    """,
                    (
                        file_id,
                        file_init.conversation_id,
                        user_id,
                        file_init.client_file_id,
                        request_id,
                        file_init.file_name,
                        int(file_init.file_size),
                        file_init.sha256,
                        storage_relative_path,
                        int(file_init.direction),
                        int(file_init.priority),
                        now_ms,
                        now_ms,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT
                      file_id,
                      conversation_id,
                      owner_id,
                      client_file_id,
                      file_name,
                      file_size,
                      sha256,
                      storage_path,
                      direction,
                      priority,
                      received_bytes,
                      version,
                      status,
                      updated_at_ms
                    FROM file_transfers
                    WHERE file_id = ?
                    """,
                    (file_id,),
                ).fetchone()
            else:
                existing_size = int(row["file_size"])
                if existing_size != int(file_init.file_size):
                    size_conflict = True
                else:
                    status = str(row["status"])
                    updated_at_ms = int(row["updated_at_ms"])
                    if (
                        stale_timeout_ms > 0
                        and self._is_uploading_status(status)
                        and now_ms - updated_at_ms > stale_timeout_ms
                    ):
                        next_version = int(row["version"]) + 1
                        connection.execute(
                            """
                            UPDATE file_transfers
                            SET
                              status = 'failed_stale',
                              version = ?,
                              updated_at_ms = ?
                            WHERE file_id = ?
                            """,
                            (next_version, now_ms, str(row["file_id"])),
                        )
                        connection.execute(
                            """
                            UPDATE file_transfers
                            SET
                              request_id = ?,
                              file_name = ?,
                              file_size = ?,
                              sha256 = ?,
                              storage_path = ?,
                              direction = ?,
                              priority = ?,
                              status = 'init',
                              version = ?,
                              updated_at_ms = ?
                            WHERE file_id = ?
                            """,
                            (
                                request_id,
                                file_init.file_name,
                                int(file_init.file_size),
                                file_init.sha256,
                                storage_relative_path,
                                int(file_init.direction),
                                int(file_init.priority),
                                next_version + 1,
                                now_ms,
                                str(row["file_id"]),
                            ),
                        )
                        row = connection.execute(
                            """
                            SELECT
                              file_id,
                              conversation_id,
                              owner_id,
                              client_file_id,
                              file_name,
                              file_size,
                              sha256,
                              storage_path,
                              direction,
                              priority,
                              received_bytes,
                              version,
                              status,
                              updated_at_ms
                            FROM file_transfers
                            WHERE file_id = ?
                            """,
                            (str(row["file_id"]),),
                        ).fetchone()

            transfer = self._row_to_transfer(row)
            if size_conflict:
                sync_events = []
            else:
                sync_events = self._append_file_updated_sync_events(
                    connection=connection,
                    member_ids=member_ids,
                    transfer=transfer,
                )

        return FileTransferInitResult(
            transfer=transfer,
            sync_events=sync_events,
            created=created,
            size_conflict=size_conflict,
        )

    def get_transfer_by_file_id(self, file_id: str) -> StoredFileTransfer | None:
        row = self.m_db.execute_fetchone(
            """
            SELECT
              file_id,
              conversation_id,
              owner_id,
              client_file_id,
              file_name,
              file_size,
              sha256,
              storage_path,
              direction,
              priority,
              received_bytes,
              version,
              status,
              updated_at_ms
            FROM file_transfers
            WHERE file_id = ?
            """,
            (file_id,),
        )
        if row is None:
            return None
        return self._row_to_transfer(row)

    def apply_progress(
        self,
        file_id: str,
        received_bytes: int,
        member_ids: list[str],
    ) -> FileTransferProgressResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection

        with connection:
            row = connection.execute(
                """
                SELECT
                  file_id,
                  conversation_id,
                  owner_id,
                  client_file_id,
                  file_name,
                  file_size,
                  sha256,
                  storage_path,
                  direction,
                  priority,
                  received_bytes,
                  version,
                  status,
                  updated_at_ms
                FROM file_transfers
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()
            if row is None:
                return None

            transfer = self._row_to_transfer(row)
            normalized = max(0, min(int(received_bytes), transfer.file_size))
            changed = normalized > transfer.received_bytes
            if changed:
                status = "uploading"
                if normalized >= transfer.file_size:
                    status = "uploaded"
                transfer.received_bytes = normalized
                transfer.status = status
                transfer.version += 1
                transfer.updated_at_ms = now_ms
                connection.execute(
                    """
                    UPDATE file_transfers
                    SET received_bytes = ?, status = ?, version = ?, updated_at_ms = ?
                    WHERE file_id = ?
                    """,
                    (normalized, status, transfer.version, now_ms, file_id),
                )

            sync_events = self._append_file_updated_sync_events(
                connection=connection,
                member_ids=member_ids,
                transfer=transfer,
            )

        return FileTransferProgressResult(transfer=transfer, sync_events=sync_events, changed=changed)

    def apply_finish(
        self,
        file_id: str,
        success: bool,
        member_ids: list[str],
    ) -> FileTransferFinishResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection

        with connection:
            row = connection.execute(
                """
                SELECT
                  file_id,
                  conversation_id,
                  owner_id,
                  client_file_id,
                  file_name,
                  file_size,
                  sha256,
                  storage_path,
                  direction,
                  priority,
                  received_bytes,
                  version,
                  status,
                  updated_at_ms
                FROM file_transfers
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()
            if row is None:
                return None

            transfer = self._row_to_transfer(row)
            target_status = "completed" if success else "failed"
            target_received = transfer.file_size if success else transfer.received_bytes

            changed = target_status != transfer.status or target_received != transfer.received_bytes
            if changed:
                transfer.received_bytes = target_received
                transfer.status = target_status
                transfer.version += 1
                transfer.updated_at_ms = now_ms
                connection.execute(
                    """
                    UPDATE file_transfers
                    SET received_bytes = ?, status = ?, version = ?, updated_at_ms = ?
                    WHERE file_id = ?
                    """,
                    (target_received, target_status, transfer.version, now_ms, file_id),
                )

            sync_events = self._append_file_updated_sync_events(
                connection=connection,
                member_ids=member_ids,
                transfer=transfer,
            )

        return FileTransferFinishResult(transfer=transfer, sync_events=sync_events, changed=changed)

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
        )
        payload_event_id = str(uuid.uuid4())
        updated.event_id = payload_event_id
        payload = updated.SerializeToString()

        sync_events: list[StoredSyncEvent] = []
        for member_id in member_ids:
            event_id = str(uuid.uuid4())
            global_seq = self._next_global_seq(connection, member_id)
            connection.execute(
                """
                INSERT INTO sync_events(event_id, user_id, seq, conversation_id, event_type, payload, created_at_ms)
                VALUES(?, ?, ?, ?, 'file_updated', ?, ?)
                """,
                (event_id, member_id, global_seq, transfer.conversation_id, payload, transfer.updated_at_ms),
            )
            sync_events.append(
                StoredSyncEvent(
                    user_id=member_id,
                    global_seq=global_seq,
                    event_id=payload_event_id,
                    event_type="file_updated",
                    payload=payload,
                )
            )

        return sync_events

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
        )
