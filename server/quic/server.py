import asyncio
import os
import random
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated, StopSendingReceived, StreamDataReceived, StreamReset
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from quic.endpoint import serve_quic
from quic.download import DownloadScheduler
from quic.upload import UploadRegistry, UploadLease, UPLOAD_HEADER_PREFIX
from protocol.codec import EnvelopeCodec
from protocol.pb import common_pb2, auth_pb2, conversation_pb2, envelope_pb2, file_pb2, message_pb2, sync_pb2
from services.auth.service import AuthService
from services.conversation.service import ConversationService
from services.control.service import ControlWriteService
from services.delivery.service import DeliveryService
from services.file.service import FileService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, FileRepo, MessageRepo, StoredSyncEvent, SyncRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.repo.control_write_repo import ControlWriteRepo
from storage.sqlite.init_db import init_db
from storage.sqlite.write_queue import SqliteWriteQueue


FILE_STREAM_HEADER_MAX = 512
UPLOAD_COMMIT_BYTES = 65536
UPLOAD_COMMIT_DELAY = 0.02


@dataclass
class FileStreamState:
    buffer: bytearray = field(default_factory=bytearray)
    file_id: str = ""
    lease: UploadLease | None = None
    flush_timer: asyncio.TimerHandle | None = None
    flush_queued: bool = False


@dataclass
class FaultConfig:
    file_drop_after_bytes: int = 0
    file_drop_probability: float = 0.0


class OnlineSessionHub:
    def __init__(self, upload_idle_ms: int = 900000) -> None:
        self.uploads = UploadRegistry(upload_idle_ms)
        self.writes = SqliteWriteQueue()
        self.m_protocols: dict[str, set["MiniImQuicProtocol"]] = {}

    def register(self, user_id: str, protocol: "MiniImQuicProtocol") -> None:
        bucket = self.m_protocols.get(user_id)
        if bucket is None:
            bucket = set()
            self.m_protocols[user_id] = bucket
        bucket.add(protocol)

    def cancel_file(self, user_id: str, file_id: str) -> None:
        lease = self.uploads.by_file.get(file_id)
        if lease and lease.user_id == user_id:
            self.uploads.release(lease)
        for protocol in list(self.m_protocols.get(user_id, ())):
            protocol.m_download_sender.cancel(file_id)
            for stream_id, state in list(protocol.m_file_stream_states.items()):
                if state.file_id == file_id:
                    protocol._reject_file_stream(stream_id)

    def unregister(self, user_id: str, protocol: "MiniImQuicProtocol") -> None:
        self.uploads.release_owner(protocol)
        bucket = self.m_protocols.get(user_id)
        if bucket is None:
            return
        bucket.discard(protocol)
        if not bucket:
            self.m_protocols.pop(user_id, None)

    def fanout_sync_events(
        self,
        events: list[StoredSyncEvent],
        exclude_protocol: "MiniImQuicProtocol | None" = None,
    ) -> None:
        for event in events:
            for protocol in list(self.m_protocols.get(event.user_id, set())):
                if exclude_protocol is not None and protocol is exclude_protocol:
                    continue
                protocol.send_sync_event(event)


class MiniImQuicProtocol(QuicConnectionProtocol):
    def __init__(
        self,
        *args,
        auth_service: AuthService,
        conversation_service: ConversationService,
        control_write_service: ControlWriteService,
        file_service: FileService,
        message_service: MessageService,
        sync_service: SyncService,
        online_hub: OnlineSessionHub,
        fault_config: FaultConfig,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.m_auth_service = auth_service
        self.m_conversation_service = conversation_service
        self.m_control_writes = control_write_service
        self.m_file_service = file_service
        self.m_message_service = message_service
        self.m_sync_service = sync_service
        self.m_online_hub = online_hub
        self.m_upload_generation = online_hub.uploads.next_generation()
        self.m_fault_config = fault_config
        self.m_user_id = ""
        self.m_session_id = ""
        self.m_device_id = ""
        self.m_control_stream_id: int | None = None
        self.m_control_closed = False
        self.m_control_stream_buffers: dict[int, bytearray] = {}
        self.m_file_stream_states: dict[int, FileStreamState] = {}
        self.m_file_storage_failed = False
        self.m_rejected_file_streams: set[int] = set()
        self.m_pending_receive_ends: set[int] = set()
        self.m_download_sender = DownloadScheduler(self)

    @staticmethod
    def _debug(message: str) -> None:
        print(f"mini-im server | {message}")

    @staticmethod
    def _new_response_from_request(request: envelope_pb2.Envelope) -> envelope_pb2.Envelope:
        response = envelope_pb2.Envelope()
        response.version = 1
        response.request_id = request.request_id
        response.channel = request.channel
        response.session_id = request.session_id
        response.device_id = request.device_id
        response.seq = request.seq
        response.client_time_ms = request.client_time_ms
        response.trace_id = request.trace_id
        return response

    def _send(self, stream_id: int, envelope: envelope_pb2.Envelope) -> None:
        self._quic.send_stream_data(stream_id, EnvelopeCodec.encode_frame(envelope), end_stream=False)
        self.transmit()

    def _send_error(self, stream_id: int, request: envelope_pb2.Envelope, code: int, message: str) -> None:
        response = self._new_response_from_request(request)
        response.error.code = int(code)
        response.error.message = message
        response.error.detail = ""
        self._send(stream_id, response)

    def _send_file_updated(
        self,
        stream_id: int,
        request: envelope_pb2.Envelope,
        updated: file_pb2.FileUpdated,
        sender_event: StoredSyncEvent | None,
    ) -> None:
        response = self._new_response_from_request(request)
        if sender_event is not None:
            response.seq = int(sender_event.global_seq)
        response.file_updated.CopyFrom(updated)
        if sender_event is not None:
            response.file_updated.event_id = sender_event.event_id
        self._send(stream_id, response)

    def _send_download_stream(self, owner_user_id: str, source_file_id: str, target_file_id: str, offset: int) -> None:
        source_path = self.m_file_service.get_storage_path(source_file_id)
        target = self.m_file_service.get_transfer_for_upload(owner_user_id, target_file_id)
        if source_path is None or target is None:
            raise ValueError("download source or target missing")
        self.m_download_sender.start(
            target_file_id, source_path, target.file_size, offset, target.priority,
        )

    def transmit(self) -> None:
        super().transmit()
        # Incoming datagrams and timers run transmit after aioquic processes acknowledgements.
        self.m_download_sender.notify()

    def close(self, error_code=0, reason_phrase="") -> None:
        self.m_control_closed = True
        self._clear_upload_streams()
        self.m_download_sender.close()
        super().close(error_code=error_code, reason_phrase=reason_phrase)

    def send_sync_event(self, event: StoredSyncEvent) -> None:
        if self.m_control_stream_id is None or not self.m_session_id:
            return

        envelope = envelope_pb2.Envelope()
        envelope.version = 1
        envelope.channel = 1
        envelope.session_id = self.m_session_id
        envelope.device_id = self.m_device_id
        envelope.seq = int(event.global_seq)
        envelope.client_time_ms = 0
        envelope.trace_id = event.event_id

        sync_event = sync_pb2.SyncEvent()
        sync_event.event_id = event.event_id
        sync_event.global_seq = event.global_seq
        if event.event_type == "message":
            item = message_pb2.Message()
            item.ParseFromString(event.payload)
            sync_event.message.CopyFrom(item)
        elif event.event_type == "receipt":
            item = message_pb2.Receipt()
            item.ParseFromString(event.payload)
            sync_event.receipt.CopyFrom(item)
        elif event.event_type == "recall":
            item = message_pb2.Recall()
            item.ParseFromString(event.payload)
            sync_event.recall.CopyFrom(item)
        elif event.event_type == "conversation_updated":
            item = conversation_pb2.ConversationUpdated()
            item.ParseFromString(event.payload)
            sync_event.conversation_updated.CopyFrom(item)
        elif event.event_type == "file_updated":
            item = file_pb2.FileUpdated()
            item.ParseFromString(event.payload)
            sync_event.file_updated.CopyFrom(item)
        elif event.event_type == "read_count_updated":
            sync_event.read_count_updated.ParseFromString(event.payload)
        elif event.event_type == "delivery_updated":
            sync_event.delivery_updated.ParseFromString(event.payload)
        else:
            return

        envelope.sync_response.new_global_cursor = int(event.global_seq)
        envelope.sync_response.has_more = False
        envelope.sync_response.events.append(sync_event)
        self._send(self.m_control_stream_id, envelope)

    def connection_lost(self, exc) -> None:
        self.m_control_closed = True
        self._clear_upload_streams()
        self.m_download_sender.close()
        if self.m_user_id:
            self.m_online_hub.unregister(self.m_user_id, self)
        super().connection_lost(exc)

    def _reject_file_stream(self, stream_id: int) -> None:
        self.m_rejected_file_streams.add(stream_id)
        self._discard_upload_stream(stream_id)
        self.m_online_hub.uploads.release_owner(self, stream_id)
        # aioquic may retire a finished stream before its queued event runs.
        if stream_id not in self.m_pending_receive_ends:
            self._quic.stop_stream(stream_id, 0x1006)
            self.transmit()

    def _bind_upload_stream(self, stream_id: int, state: FileStreamState, end_stream: bool) -> bool:
        uploads = self.m_online_hub.uploads
        split = state.buffer.find(b"\n")
        if split < 0 and len(state.buffer) <= FILE_STREAM_HEADER_MAX and not end_stream:
            return False
        if split < 0 or split > FILE_STREAM_HEADER_MAX:
            self._reject_file_stream(stream_id)
            return False
        header = bytes(state.buffer[:split])
        state.buffer = state.buffer[split + 1:]
        try:
            prefix, file_id, offset = header.decode("utf-8").split(" ")
            if prefix.encode() + b" " != UPLOAD_HEADER_PREFIX or not offset.isascii() or not offset.isdecimal():
                raise ValueError("invalid upload header")
            state.lease = uploads.bind(self, file_id, stream_id, int(offset))
        except (ValueError, UnicodeDecodeError):
            state.lease = None
        if state.lease is None:
            self._reject_file_stream(stream_id)
            return False
        state.file_id = file_id
        return True

    def _discard_upload_stream(self, stream_id: int) -> None:
        state = self.m_file_stream_states.pop(stream_id, None)
        if state is not None and state.flush_timer is not None:
            state.flush_timer.cancel()

    def _clear_upload_streams(self) -> None:
        for stream_id in list(self.m_file_stream_states):
            self._discard_upload_stream(stream_id)

    def _upload_transfer(self, stream_id: int, state: FileStreamState):
        lease = state.lease
        transfer = self.m_file_service.get_transfer_for_upload(self.m_user_id, state.file_id)
        if (lease is None or not self.m_online_hub.uploads.current(lease) or transfer is None
                or transfer.direction != common_pb2.FILE_DIRECTION_UPLOAD
                or transfer.status not in {"init", "uploading", "uploaded"}
                or transfer.received_bytes != lease.offset):
            self._reject_file_stream(stream_id)
            return None
        return transfer

    def _schedule_upload_flush(self, stream_id: int, state: FileStreamState) -> None:
        if not state.buffer or state.flush_timer is not None or state.flush_queued:
            return

        def flush():
            state.flush_queued = False
            if (self.m_file_stream_states.get(stream_id) is state
                    and not self.m_control_closed and not self.m_file_storage_failed):
                self._flush_upload_buffer(stream_id, state, force=True)

        def enqueue():
            state.flush_timer = None
            state.flush_queued = True
            try:
                result = self.m_online_hub.writes.enqueue(flush)
            except RuntimeError:
                # A stopped queue will not accept a new timer operation. The uncommitted
                # tail remains recoverable from the original client's durable intent.
                self._discard_upload_stream(stream_id)
                return
            result.add_done_callback(self._write_completed)

        state.flush_timer = asyncio.get_running_loop().call_later(UPLOAD_COMMIT_DELAY, enqueue)

    def _flush_upload_buffer(self, stream_id: int, state: FileStreamState, *, force: bool) -> bool:
        if self._upload_transfer(stream_id, state) is None:
            return False
        if state.flush_timer is not None:
            state.flush_timer.cancel()
            state.flush_timer = None
        while state.buffer and (force or len(state.buffer) >= UPLOAD_COMMIT_BYTES):
            chunk = bytes(state.buffer[:UPLOAD_COMMIT_BYTES])
            projected = state.lease.offset + len(chunk)
            try:
                updated, sync_events = self.m_file_service.append_file_chunk(
                    user_id=self.m_user_id, file_id=state.file_id, chunk=chunk)
            except (OSError, sqlite3.Error) as error:
                self._debug(f"upload storage failed: {error}")
                self.m_file_storage_failed = True
                self._clear_upload_streams()
                self.m_online_hub.uploads.release_owner(self)
                self._quic.close(error_code=0x1004, reason_phrase="upload_storage_failed")
                self.transmit()
                return False
            if updated is None or updated.transferred_bytes != projected:
                self._reject_file_stream(stream_id)
                return False
            self.m_online_hub.uploads.advance(state.lease, projected)
            del state.buffer[:len(chunk)]
            self.m_online_hub.fanout_sync_events(sync_events)
        self._schedule_upload_flush(stream_id, state)
        return True

    def _handle_file_stream_data(self, stream_id: int, data: bytes, end_stream: bool) -> None:
        if not self.m_user_id or stream_id in self.m_rejected_file_streams:
            return
        state = self.m_file_stream_states.setdefault(stream_id, FileStreamState())
        state.buffer.extend(data)
        if state.lease is None and not self._bind_upload_stream(stream_id, state, end_stream):
            return
        transfer = self._upload_transfer(stream_id, state)
        if transfer is None:
            return
        projected = state.lease.offset + len(state.buffer)
        if projected > transfer.file_size:
            self._reject_file_stream(stream_id)
            return
        if state.buffer:
            if self.m_fault_config.file_drop_after_bytes > 0 and projected >= self.m_fault_config.file_drop_after_bytes:
                self._quic.close(error_code=0x1001, reason_phrase="fault_injection_drop_after_bytes")
                self.transmit()
                self._discard_upload_stream(stream_id)
                return
            if self.m_fault_config.file_drop_probability > 0.0 and random.random() < self.m_fault_config.file_drop_probability:
                self._quic.close(error_code=0x1002, reason_phrase="fault_injection_random_drop")
                self.transmit()
                self._discard_upload_stream(stream_id)
                return
            # Receiving bytes keeps the owner alive but does not advance the durable offset.
            self.m_online_hub.uploads.advance(state.lease, state.lease.offset)
            if len(state.buffer) >= UPLOAD_COMMIT_BYTES or end_stream or projected == transfer.file_size:
                if not self._flush_upload_buffer(stream_id, state, force=end_stream or projected == transfer.file_size):
                    return
            else:
                self._schedule_upload_flush(stream_id, state)
        if end_stream:
            self._discard_upload_stream(stream_id)
            if state.lease.offset != transfer.file_size:
                self.m_online_hub.uploads.release(state.lease)

    def _close_control(self, reason: str) -> None:
        self.m_control_closed = True
        self._clear_upload_streams()
        self.m_control_stream_buffers.clear()
        self.m_download_sender.close()
        self.m_online_hub.unregister(self.m_user_id, self)
        self._quic.close(error_code=0x1003, reason_phrase=reason)
        self.transmit()

    def quic_event_received(self, event):
        if not isinstance(event, (ConnectionTerminated, StreamReset, StopSendingReceived, StreamDataReceived)):
            return
        receive_ended = isinstance(event, StreamReset) or (
            isinstance(event, StreamDataReceived) and event.end_stream)
        if receive_ended:
            self.m_pending_receive_ends.add(event.stream_id)

        def process():
            try:
                self._process_quic_event(event)
            finally:
                if receive_ended:
                    self.m_pending_receive_ends.discard(event.stream_id)

        try:
            result = self.m_online_hub.writes.enqueue(process)
        except RuntimeError:
            # Shutdown closes transport after accepted operations have drained.
            return
        result.add_done_callback(self._write_completed)

    def _write_completed(self, result):
        try:
            result.result()
        except (Exception, asyncio.CancelledError) as error:
            self._debug(f"queued operation failed: {error}")
            self._close_control("operation_failed")

    def _process_quic_event(self, event):
        if isinstance(event, ConnectionTerminated):
            self.m_control_closed = True
            self._clear_upload_streams()
            self.m_control_stream_buffers.clear()
            self.m_download_sender.close()
            if self.m_user_id:
                self.m_online_hub.unregister(self.m_user_id, self)
            return
        if self.m_control_closed:
            return
        if isinstance(event, StopSendingReceived) and event.stream_id == self.m_control_stream_id:
            self._close_control("control_stream_stopped")
            return
        if isinstance(event, StreamReset):
            if event.stream_id == self.m_control_stream_id or (self.m_control_stream_id is None and event.stream_id % 4 == 0):
                self._close_control("control_stream_reset")
                return
            self._discard_upload_stream(event.stream_id)
            self.m_rejected_file_streams.add(event.stream_id)
            self.m_online_hub.uploads.release_owner(self, event.stream_id)
            return
        if self.m_file_storage_failed or not isinstance(event, StreamDataReceived):
            return

        if self.m_control_stream_id is None:
            if event.stream_id % 4 != 0:
                self._close_control("invalid_control_stream")
                return
            self.m_control_stream_id = event.stream_id
        if event.stream_id != self.m_control_stream_id:
            self._handle_file_stream_data(event.stream_id, event.data, event.end_stream)
            return

        control_buffer = self.m_control_stream_buffers.get(event.stream_id)
        if control_buffer is None:
            control_buffer = bytearray()
            self.m_control_stream_buffers[event.stream_id] = control_buffer
        control_buffer.extend(event.data)

        try:
            envelopes = EnvelopeCodec.decode_frames(control_buffer)
        except Exception:
            self._close_control("invalid_control_frame")
            return
        if event.end_stream and control_buffer:
            self._close_control("incomplete_control_frame")
            return

        for envelope in envelopes:
            if envelope.HasField("hello"):
                welcome = self.m_auth_service.handle_hello(envelope.hello)
                self.m_conversation_service.ensure_user(welcome.user_id)
                response = self._new_response_from_request(envelope)
                response.session_id = welcome.session_id
                response.welcome.CopyFrom(welcome)
                self._send(event.stream_id, response)
                continue

            session = self.m_auth_service.get_session(envelope.session_id)
            if session is None:
                self._send_error(event.stream_id, envelope, 401, "invalid session")
                continue

            self.m_user_id = session.user_id
            self.m_session_id = envelope.session_id
            self.m_device_id = session.device_id
            self.m_online_hub.register(session.user_id, self)

            if envelope.HasField("heartbeat"):
                self.m_auth_service.touch_session(envelope.session_id)
                ack = self._new_response_from_request(envelope)
                ack.heartbeat.CopyFrom(auth_pb2.Heartbeat(ts_ms=envelope.heartbeat.ts_ms))
                self._send(event.stream_id, ack)
                continue

            if envelope.HasField("send_message"):
                try:
                    result = self.m_message_service.handle_send_message(
                        user_id=session.user_id,
                        request_id=envelope.request_id,
                        send_message=envelope.send_message,
                    )
                except sqlite3.Error as error:
                    self._debug(f"message transaction failed: {error}")
                    self._send_error(event.stream_id, envelope, 503, "message could not be committed")
                    continue
                ack_envelope = self._new_response_from_request(envelope)
                ack_envelope.ack.CopyFrom(result.ack)
                self._send(event.stream_id, ack_envelope)

                if result.message_push is not None:
                    sender_event = next(
                        (item for item in result.sync_events if item.user_id == session.user_id),
                        None,
                    )
                    push_envelope = self._new_response_from_request(envelope)
                    if sender_event is not None:
                        push_envelope.seq = int(sender_event.global_seq)
                    push_envelope.message_push.CopyFrom(result.message_push)
                    if sender_event is not None:
                        push_envelope.message_push.event_id = sender_event.event_id
                    self._send(event.stream_id, push_envelope)

                self.m_online_hub.fanout_sync_events(result.sync_events, exclude_protocol=self)
                continue

            if self.m_control_writes.handles(envelope):
                try:
                    result = self.m_control_writes.handle(session.user_id, envelope)
                except sqlite3.Error as error:
                    self._debug(f"control write transaction failed: {error}")
                    self._send_error(event.stream_id, envelope, 503, "control write could not be committed")
                    continue
                ack_envelope = self._new_response_from_request(envelope)
                ack_envelope.ack.CopyFrom(result.ack)
                self._send(event.stream_id, ack_envelope)
                self.m_online_hub.fanout_sync_events(result.sync_events)
                continue

            if envelope.HasField("file_cancel"):
                try:
                    result = self.m_file_service.handle_file_cancel(
                        session.user_id, envelope.request_id, envelope.file_cancel)
                except (OSError, sqlite3.Error) as error:
                    self._debug(f"file cancellation storage failed: {error}")
                    self._send_error(event.stream_id, envelope, 503, "file cancellation could not be committed")
                    continue
                if result.ack.success and result.cancelled_file_id:
                    self.m_online_hub.cancel_file(session.user_id, result.cancelled_file_id)
                response = self._new_response_from_request(envelope)
                response.ack.CopyFrom(result.ack)
                self._send(event.stream_id, response)
                self.m_online_hub.fanout_sync_events(result.sync_events)
                continue

            if envelope.HasField("file_init"):
                is_upload = envelope.file_init.direction == common_pb2.FILE_DIRECTION_UPLOAD
                if is_upload and not self.m_online_hub.uploads.available(
                        session.user_id, envelope.file_init.client_file_id, self, session.device_id, self.m_upload_generation):
                    self._send_error(event.stream_id, envelope, 429, "upload is active; retry after its owner releases it")
                    continue
                if (envelope.file_init.direction == common_pb2.FILE_DIRECTION_DOWNLOAD
                        and not self.m_download_sender.can_accept):
                    self._send_error(event.stream_id, envelope, 429, "download queue is full; retry later")
                    continue
                try:
                    result = self.m_file_service.handle_file_init(
                        user_id=session.user_id,
                        request_id=envelope.request_id,
                        file_init=envelope.file_init,
                    )
                except (OSError, sqlite3.Error) as error:
                    self._debug(f"file init storage failed: {error}")
                    self._send_error(event.stream_id, envelope, 503, "file init could not be committed")
                    continue

                if is_upload and result.ack.success and result.file_updated is not None and not result.file_updated.completed:
                    self.m_online_hub.uploads.grant(session.user_id, envelope.file_init.client_file_id,
                        result.ack.entity_id, self, result.file_updated.transferred_bytes, session.device_id, self.m_upload_generation)

                ack_envelope = self._new_response_from_request(envelope)
                ack_envelope.ack.CopyFrom(result.ack)
                self._send(event.stream_id, ack_envelope)
                sender_event = next(
                    (item for item in reversed(result.sync_events) if item.user_id == session.user_id and item.event_type == "file_updated"),
                    None,
                )
                if result.file_updated is not None:
                    self._send_file_updated(event.stream_id, envelope, result.file_updated, sender_event)
                self.m_online_hub.fanout_sync_events(result.sync_events, exclude_protocol=self)
                if result.start_download:
                    self._send_download_stream(
                        owner_user_id=session.user_id,
                        source_file_id=envelope.file_init.source_file_id,
                        target_file_id=result.download_file_id,
                        offset=result.download_offset,
                    )
                continue

            if envelope.HasField("file_finish"):
                lease = self.m_online_hub.uploads.by_file.get(envelope.file_finish.file_id)
                if lease is not None and lease.owner is not self:
                    self._send_error(event.stream_id, envelope, 429, "upload is active on another connection")
                    continue
                try:
                    result = self.m_file_service.handle_file_finish(
                        user_id=session.user_id,
                        request_id=envelope.request_id,
                        file_finish=envelope.file_finish,
                    )
                except (OSError, sqlite3.Error) as error:
                    self._debug(f"file finish storage failed: {error}")
                    self._send_error(event.stream_id, envelope, 503, "file finish could not be committed")
                    continue

                if lease is not None and (result.ack.success or (result.file_updated is not None
                        and result.file_updated.status.startswith("failed"))):
                    self.m_online_hub.uploads.release(lease)

                ack_envelope = self._new_response_from_request(envelope)
                ack_envelope.ack.CopyFrom(result.ack)
                self._send(event.stream_id, ack_envelope)
                sender_event = next(
                    (item for item in reversed(result.sync_events) if item.user_id == session.user_id and item.event_type == "file_updated"),
                    None,
                )
                if result.file_updated is not None:
                    self._send_file_updated(event.stream_id, envelope, result.file_updated, sender_event)
                self.m_online_hub.fanout_sync_events(result.sync_events)
                continue

            if envelope.HasField("sync_applied"):
                try:
                    result = self.m_sync_service.handle_sync_applied(
                        session.user_id, session.device_id, envelope.request_id, envelope.sync_applied)
                except sqlite3.Error as error:
                    self._debug(f"delivery confirmation transaction failed: {error}")
                    self._send_error(event.stream_id, envelope, 503, "delivery confirmation could not be committed")
                    continue
                response = self._new_response_from_request(envelope)
                response.ack.CopyFrom(result.ack)
                self._send(event.stream_id, response)
                self.m_online_hub.fanout_sync_events(result.sync_events)
                continue

            if envelope.HasField("sync_request"):
                sync_response, new_global_cursor = self.m_sync_service.handle_sync_request(
                    user_id=session.user_id,
                    request=envelope.sync_request,
                )
                response = self._new_response_from_request(envelope)
                response.seq = int(new_global_cursor)
                response.sync_response.CopyFrom(sync_response)
                self._send(event.stream_id, response)
                continue

            self._send_error(event.stream_id, envelope, 400, "unsupported envelope body")

        if event.end_stream:
            self._close_control("control_stream_closed")


def ensure_dev_cert(cert_path: Path, key_path: Path) -> None:
    if cert_path.exists() and key_path.exists():
        return

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Mini-IM"),
            x509.NameAttribute(NameOID.COMMON_NAME, "mini-im-dev"),
        ]
    )

    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


async def run_burn_sweeper(
    delivery_repo: DeliveryRepo,
    online_hub: OnlineSessionHub,
    interval_ms: int,
    batch_size: int,
    purge_batch_size: int,
) -> None:
    safe_interval_ms = max(int(interval_ms), 100)
    safe_batch_size = max(int(batch_size), 1)
    safe_purge_batch_size = max(int(purge_batch_size), 1)
    while True:
        try:
            def sweep():
                events = delivery_repo.collect_due_burn_sync_events(safe_batch_size)
                if events:
                    online_hub.fanout_sync_events(events)
                delivery_repo.purge_burned_message_content(safe_purge_batch_size)
            await online_hub.writes.submit(sweep)
        except Exception as exc:  # pragma: no cover
            print(f"mini-im burn sweeper error: {exc}")
        await asyncio.sleep(safe_interval_ms / 1000.0)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


async def run_server(*, data_root: Path | None = None, port: int = 4433) -> None:
    from storage.access import storage_access
    base_path = data_root if data_root is not None else Path(__file__).resolve().parents[1]
    file_root = Path(os.getenv("MINIIM_FILE_ROOT", str(base_path / "storage" / "files")))
    with storage_access(base_path, file_root):
        await _run_server(base_path=base_path, file_root=file_root, port=port)


async def _run_server(*, base_path: Path, file_root: Path, port: int) -> None:
    cert_path = base_path / "quic" / "dev_cert.pem"
    key_path = base_path / "quic" / "dev_key.pem"
    db_path = base_path / "storage" / "sqlite" / "miniim.db"
    file_stale_ms = int(os.getenv("MINIIM_FILE_STALE_MS", str(15 * 60 * 1000)))
    fault_drop_after_bytes = int(os.getenv("MINIIM_FAULT_FILE_DROP_AFTER_BYTES", "0"))
    fault_drop_probability = float(os.getenv("MINIIM_FAULT_FILE_DROP_PROBABILITY", "0"))
    burn_enabled = _env_bool("MINIIM_BURN_ENABLED", True)
    burn_sweep_interval_ms = int(os.getenv("MINIIM_BURN_SWEEP_INTERVAL_MS", "1000"))
    burn_sweep_batch_size = int(os.getenv("MINIIM_BURN_SWEEP_BATCH_SIZE", "200"))
    burn_purge_batch_size = int(os.getenv("MINIIM_BURN_PURGE_BATCH_SIZE", "200"))

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_dev_cert(cert_path, key_path)

    init_db(db_path)
    db = MiniImSqliteDb(db_path)

    configuration = QuicConfiguration(is_client=False, alpn_protocols=["mini-im"])
    configuration.load_cert_chain(str(cert_path), str(key_path))

    auth_service = AuthService()
    conversation_repo = ConversationRepo(db)
    message_repo = MessageRepo(db)
    delivery_repo = DeliveryRepo(db, burn_enabled=burn_enabled)
    file_repo = FileRepo(db)
    conversation_service = ConversationService(conversation_repo)
    delivery_service = DeliveryService(delivery_repo)
    control_write_service = ControlWriteService(ControlWriteRepo(db), conversation_service, delivery_service)
    file_service = FileService(
        file_repo,
        conversation_repo,
        message_repo,
        file_root=file_root,
        stale_timeout_ms=max(file_stale_ms, 0),
    )
    message_service = MessageService(message_repo, conversation_repo, burn_enabled=burn_enabled)
    sync_service = SyncService(SyncRepo(db))
    online_hub = OnlineSessionHub(upload_idle_ms=file_stale_ms)
    fault_config = FaultConfig(
        file_drop_after_bytes=max(fault_drop_after_bytes, 0),
        file_drop_probability=min(max(fault_drop_probability, 0.0), 1.0),
    )

    server = await serve_quic(
        host="127.0.0.1",
        port=port,
        configuration=configuration,
        create_protocol=lambda *args, **kwargs: MiniImQuicProtocol(
            *args,
            auth_service=auth_service,
            conversation_service=conversation_service,
            control_write_service=control_write_service,
            file_service=file_service,
            message_service=message_service,
            sync_service=sync_service,
            online_hub=online_hub,
            fault_config=fault_config,
            **kwargs,
        ),
    )

    burn_sweeper_task = None
    if burn_enabled:
        burn_sweeper_task = asyncio.create_task(
            run_burn_sweeper(
                delivery_repo=delivery_repo,
                online_hub=online_hub,
                interval_ms=burn_sweep_interval_ms,
                batch_size=burn_sweep_batch_size,
                purge_batch_size=burn_purge_batch_size,
            )
        )

    bound_port = server._transport.get_extra_info("sockname")[1]
    print(f"mini-im quic server listening at 127.0.0.1:{bound_port}")
    try:
        await asyncio.Future()
    finally:
        if burn_sweeper_task is not None:
            burn_sweeper_task.cancel()
            with suppress(asyncio.CancelledError):
                await burn_sweeper_task
        await online_hub.writes.stop()
        server.close()
        db.close()
