from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from protocol.pb import common_pb2, file_pb2, message_pb2
from storage.repo import ConversationRepo, FileRepo, MessageRepo, StoredFileTransfer, StoredSyncEvent
from storage.repo.control_write_repo import ControlWriteRepo, ControlWriteResult


@dataclass
class FileServiceResult:
    ack: message_pb2.Ack
    file_updated: file_pb2.FileUpdated | None
    sync_events: list[StoredSyncEvent]
    start_download: bool = False
    download_file_id: str = ""
    download_offset: int = 0
    cancelled_file_id: str = ""


class FileService:
    def __init__(
        self,
        file_repo: FileRepo,
        conversation_repo: ConversationRepo,
        message_repo: MessageRepo,
        file_root: Path,
        stale_timeout_ms: int,
    ) -> None:
        self.m_file_repo = file_repo
        self.m_cancel_requests = ControlWriteRepo(file_repo.m_db)
        self.m_conversation_repo = conversation_repo
        self.m_message_repo = message_repo
        self.m_file_root = file_root
        self.m_stale_timeout_ms = max(stale_timeout_ms, 0)
        self.m_file_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _build_file_message_content(transfer: StoredFileTransfer) -> bytes:
        payload = {
            "kind": "file",
            "fileId": transfer.file_id,
            "fileName": transfer.file_name,
            "fileSize": transfer.file_size,
            "sha256": transfer.sha256,
            "version": transfer.version,
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

    def handle_file_init(
        self,
        user_id: str,
        request_id: str,
        file_init: file_pb2.FileInit,
    ) -> FileServiceResult:
        now_ms = self._now_ms()
        ack = message_pb2.Ack(
            request_id=request_id,
            success=False,
            code=400,
            message="invalid file_init",
            entity_id="",
            server_time_ms=now_ms,
        )
        if not file_init.client_file_id.strip():
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

        if self.m_file_repo.is_cancelled(user_id, file_init.client_file_id):
            ack.code = 409
            ack.message = "file intent was cancelled"
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

        if file_init.direction not in (common_pb2.FILE_DIRECTION_UPLOAD, common_pb2.FILE_DIRECTION_DOWNLOAD):
            ack.message = "invalid file direction"
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])
        is_download = int(file_init.direction) == int(common_pb2.FILE_DIRECTION_DOWNLOAD)
        if not is_download and file_init.source_file_id:
            ack.message = "uploads cannot reference a source file"
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])
        effective_conversation_id = file_init.conversation_id
        effective_file_name = file_init.file_name
        effective_file_size = int(file_init.file_size)
        effective_sha256 = file_init.sha256

        if is_download:
            if not file_init.source_file_id.strip():
                ack.code = 400
                ack.message = "missing source_file_id"
                return FileServiceResult(ack=ack, file_updated=None, sync_events=[])
            source = self.m_file_repo.get_transfer_by_file_id(file_init.source_file_id)
            if (source is None or source.status != "completed"
                    or source.direction != common_pb2.FILE_DIRECTION_UPLOAD):
                ack.code = 404
                ack.message = "source file not found"
                return FileServiceResult(ack=ack, file_updated=None, sync_events=[])
            if file_init.conversation_id and file_init.conversation_id != source.conversation_id:
                ack.code = 409
                ack.message = "download conversation does not match source"
                return FileServiceResult(ack=ack, file_updated=None, sync_events=[])
            effective_conversation_id = source.conversation_id
            effective_file_name = source.file_name
            effective_file_size = source.file_size
            effective_sha256 = source.sha256

        if (
            not effective_conversation_id.strip()
            or not effective_file_name.strip()
            or int(effective_file_size) <= 0
            or int(file_init.resume_offset) > int(effective_file_size)
        ):
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

        if not self.m_conversation_repo.is_member(effective_conversation_id, user_id):
            ack.code = 403
            ack.message = "sender is not a conversation member"
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

        member_ids = self.m_conversation_repo.list_member_ids(effective_conversation_id)
        file_id = self._make_file_id(user_id=user_id, client_file_id=file_init.client_file_id)
        relative_path = self._relative_storage_path(file_id=file_id, file_name=effective_file_name)
        normalized_init = file_pb2.FileInit(
            conversation_id=effective_conversation_id,
            client_file_id=file_init.client_file_id,
            file_name=effective_file_name,
            file_size=effective_file_size,
            sha256=effective_sha256,
            direction=file_init.direction,
            resume_offset=file_init.resume_offset,
            priority=file_init.priority,
            source_file_id=file_init.source_file_id,
        )
        with self.m_file_repo.m_db.transaction():
            result = self.m_file_repo.create_or_resume_transfer(
                user_id=user_id,
                request_id=request_id,
                file_init=normalized_init,
                member_ids=member_ids,
                storage_relative_path=relative_path,
                stale_timeout_ms=self.m_stale_timeout_ms,
            )
            if not result.conflict and not is_download and result.transfer.status != "completed":
                repaired = self._reconcile_upload(result.transfer, member_ids)
                result = replace(result, transfer=repaired.transfer,
                    sync_events=[*result.sync_events, *repaired.sync_events])

        if result.conflict:
            ack.code = 409
            ack.message = result.conflict
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

        ack.success = True
        ack.code = 0
        ack.message = "ok" if result.created else "ok(idempotent)"
        ack.entity_id = result.transfer.file_id
        ack.server_time_ms = self._now_ms()
        updated = self._file_updated(result.transfer, result.sync_events, user_id)
        return FileServiceResult(
            ack=ack,
            file_updated=updated,
            sync_events=result.sync_events,
            start_download=is_download,
            download_file_id=result.transfer.file_id,
            download_offset=max(int(file_init.resume_offset), 0),
        )

    def append_file_chunk(
        self,
        user_id: str,
        file_id: str,
        chunk: bytes,
    ) -> tuple[file_pb2.FileUpdated | None, list[StoredSyncEvent]]:
        transfer = self.m_file_repo.get_transfer_by_file_id(file_id)
        if (transfer is None or transfer.owner_id != user_id
                or transfer.direction != common_pb2.FILE_DIRECTION_UPLOAD
                or transfer.status not in {"init", "uploading", "uploaded"}):
            return None, []
        if not chunk or transfer.received_bytes + len(chunk) > transfer.file_size:
            return None, []

        target_path = self.m_file_root / transfer.storage_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if target_path.exists() else "w+b"
        with target_path.open(mode) as file:
            file.seek(0, os.SEEK_END)
            if file.tell() != transfer.received_bytes:
                raise OSError("upload storage diverged; initialize the original intent again")
            remaining = memoryview(chunk)
            while remaining:
                written = file.write(remaining)
                if written is None or written <= 0 or written > len(remaining):
                    raise OSError("upload storage write made no valid progress")
                remaining = remaining[written:]
            file.flush()
            os.fsync(file.fileno())

        member_ids = self.m_conversation_repo.list_member_ids(transfer.conversation_id)
        result = self.m_file_repo.apply_progress(
            file_id=file_id,
            received_bytes=int(transfer.received_bytes) + len(chunk),
            member_ids=member_ids,
        )
        if result is None:
            return None, []

        updated = self._file_updated(result.transfer, result.sync_events, user_id)
        return updated, result.sync_events

    def handle_file_finish(
        self,
        user_id: str,
        request_id: str,
        file_finish: file_pb2.FileFinish,
    ) -> FileServiceResult:
        now_ms = self._now_ms()
        ack = message_pb2.Ack(
            request_id=request_id,
            success=False,
            code=400,
            message="invalid file_finish",
            entity_id="",
            server_time_ms=now_ms,
        )
        if not file_finish.file_id.strip():
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

        transfer = self.m_file_repo.get_transfer_by_file_id(file_finish.file_id)
        if transfer is None or transfer.owner_id != user_id:
            ack.code = 403
            ack.message = "file_finish rejected"
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

        if transfer.status == "cancelled":
            ack.code = 409
            ack.message = "file intent was cancelled"
            return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

        is_download = transfer.direction == common_pb2.FILE_DIRECTION_DOWNLOAD
        if bool(file_finish.success) and transfer.status != "completed":
            if is_download:
                if int(file_finish.transferred_bytes) != transfer.file_size:
                    ack.message = "download confirmation size mismatch"
                    return FileServiceResult(ack=ack, file_updated=None, sync_events=[])
                if file_finish.sha256.lower() != transfer.sha256.lower():
                    ack.code = 409
                    ack.message = "download confirmation sha256 mismatch"
                    return FileServiceResult(ack=ack, file_updated=None, sync_events=[])
            else:
                if int(transfer.received_bytes) != int(transfer.file_size):
                    ack.message = "status incomplete"
                    return FileServiceResult(ack=ack, file_updated=None, sync_events=[])
                if not self._verify_file_sha256(transfer):
                    ack.code = 409
                    ack.message = "sha256 mismatch; retry the original upload"
                    member_ids = self.m_conversation_repo.list_member_ids(transfer.conversation_id)
                    repaired = self.m_file_repo.rewind_upload(transfer.file_id, 0, member_ids, integrity_failed=True)
                    updated = self._file_updated(repaired.transfer, repaired.sync_events, user_id)
                    return FileServiceResult(ack=ack, file_updated=updated, sync_events=repaired.sync_events)

        with self.m_file_repo.m_db.transaction():
            member_ids = self.m_conversation_repo.list_member_ids(transfer.conversation_id)
            result = self.m_file_repo.apply_finish(
                file_id=transfer.file_id,
                success=bool(file_finish.success),
                member_ids=member_ids,
            )
            if result is None:
                return FileServiceResult(ack=ack, file_updated=None, sync_events=[])

            all_sync_events = list(result.sync_events)
            if bool(file_finish.success) and not is_download and result.transfer.status == "completed":
                file_message_sync_events = self._append_file_message(
                    user_id=user_id,
                    transfer=result.transfer,
                    request_id=request_id,
                    member_ids=member_ids,
                )
                all_sync_events.extend(file_message_sync_events)
        ack.success = True
        ack.code = 0
        ack.message = "ok" if result.changed else "ok(idempotent)"
        ack.entity_id = transfer.file_id
        ack.server_time_ms = self._now_ms()
        updated = self._file_updated(result.transfer, result.sync_events, user_id)
        return FileServiceResult(ack=ack, file_updated=updated, sync_events=all_sync_events)

    def handle_file_cancel(self, user_id: str, request_id: str, request: file_pb2.FileCancel) -> FileServiceResult:
        def apply():
            if not request.client_file_id.strip():
                return self.m_cancel_requests.reject(request_id, 400, "file cancellation requires intent id")
            result = self.m_file_repo.cancel_transfer(user_id, request.client_file_id, request.file_id)
            ack = message_pb2.Ack(request_id=request_id, success=result.code == 0, code=result.code,
                message=result.error or "cancelled", entity_id=request.client_file_id, server_time_ms=self._now_ms())
            return ControlWriteResult(ack, result.sync_events)
        result = self.m_cancel_requests.execute(user_id, request_id, "file_cancel",
            request.SerializeToString(deterministic=True), apply)
        file_id = self.m_file_repo.cancelled_file_id(user_id, request.client_file_id) if result.ack.success else ""
        return FileServiceResult(result.ack, None, result.sync_events, cancelled_file_id=file_id)

    def _reconcile_upload(self, transfer: StoredFileTransfer, member_ids: list[str]):
        target = self.m_file_root / transfer.storage_path
        try:
            disk_size = target.stat().st_size
        except FileNotFoundError:
            disk_size = 0
        offset = min(disk_size, transfer.received_bytes, transfer.file_size)
        if disk_size > offset:
            with target.open("r+b") as file:
                file.truncate(offset)
                file.flush()
                os.fsync(file.fileno())
        return self.m_file_repo.rewind_upload(transfer.file_id, offset, member_ids)

    @staticmethod
    def _file_updated(transfer: StoredFileTransfer, events: list[StoredSyncEvent], user_id: str) -> file_pb2.FileUpdated:
        event = next((event for event in reversed(events)
            if event.user_id == user_id and event.event_type == "file_updated"), None)
        return file_pb2.FileUpdated(
            event_id=event.event_id if event else "", file_id=transfer.file_id,
            conversation_id=transfer.conversation_id, transferred_bytes=transfer.received_bytes,
            completed=transfer.status == "completed", version=transfer.version,
            updated_at_ms=transfer.updated_at_ms, file_size=transfer.file_size, sha256=transfer.sha256,
            direction=transfer.direction, status=transfer.status, file_name=transfer.file_name,
        )

    def get_transfer_for_upload(self, user_id: str, file_id: str) -> StoredFileTransfer | None:
        transfer = self.m_file_repo.get_transfer_by_file_id(file_id)
        if transfer is None or transfer.owner_id != user_id:
            return None
        return transfer

    def get_transfer_by_file_id(self, file_id: str) -> StoredFileTransfer | None:
        return self.m_file_repo.get_transfer_by_file_id(file_id)

    def get_storage_path(self, file_id: str) -> Path | None:
        transfer = self.m_file_repo.get_transfer_by_file_id(file_id)
        if transfer is None:
            return None
        return self.m_file_root / transfer.storage_path

    def _append_file_message(
        self,
        user_id: str,
        transfer: StoredFileTransfer,
        request_id: str,
        member_ids: list[str],
    ) -> list[StoredSyncEvent]:
        client_msg_id = f"file-msg-{transfer.file_id}"
        existing = self.m_message_repo.get_message_by_client_msg_id(
            transfer.conversation_id,
            user_id,
            client_msg_id,
        )
        if existing is not None:
            return []
        send_message = message_pb2.SendMessage(
            conversation_id=transfer.conversation_id,
            client_msg_id=client_msg_id,
            type=common_pb2.MSG_FILE,
            content=self._build_file_message_content(transfer),
        )
        stored = self.m_message_repo.append_message(
            request_id=f"{request_id}-filemsg",
            sender_id=user_id,
            send_message=send_message,
            member_ids=member_ids,
        )
        return stored.sync_events

    @staticmethod
    def _make_file_id(user_id: str, client_file_id: str) -> str:
        source = f"{user_id}:{client_file_id}"
        return hashlib.sha1(source.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_suffix(file_name: str) -> str:
        suffix = Path(file_name).suffix.strip().lower()
        if not suffix:
            return ".bin"
        if len(suffix) > 16:
            return ".bin"
        return suffix

    def _relative_storage_path(self, file_id: str, file_name: str) -> str:
        prefix = file_id[:2]
        suffix = self._safe_suffix(file_name)
        return str(Path(prefix) / f"{file_id}{suffix}")

    def _verify_file_sha256(self, transfer: StoredFileTransfer) -> bool:
        target_path = self.m_file_root / transfer.storage_path
        if not target_path.exists() or target_path.stat().st_size != transfer.file_size:
            return False
        hasher = hashlib.sha256()
        with target_path.open("rb") as file:
            while True:
                chunk = file.read(64 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest().lower() == transfer.sha256.lower()
