"""Run the production server in a private process with precise crash checkpoints.

Only this test entry installs checkpoint wrappers. Production run_server wiring,
QUIC framing, repositories and on-disk commits remain the real implementation.
A reached checkpoint blocks until the parent kills this process; no rollback or
normal shutdown is performed at that checkpoint.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, redirect_stdout
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from quic import server as production
from quic import download
from storage.sqlite.db import MiniImSqliteDb
from services.control.service import ControlWriteService
from services.message.service import MessageService
from services.file.service import FileService
from storage.repo import FileRepo, DeliveryRepo


def emit(event, **data):
    sys.__stdout__.write(json.dumps(dict(event=event, pid=os.getpid(), **data)) + "\n")
    sys.__stdout__.flush()


class Checkpoints:
    def __init__(self):
        self.armed = None
        self.operation = ""
        self.request_id = ""
        self.requests = {}

    @contextmanager
    def request(self, operation, user, request_id, body):
        previous = self.operation, self.request_id
        self.operation, self.request_id = operation, request_id
        self.requests[request_id] = operation
        emit("request", operation=operation, user=user, requestId=request_id,
             body=body.SerializeToString(deterministic=True).hex())
        try:
            yield
        finally:
            self.operation, self.request_id = previous

    def check(self, point, *, operation=None, request_id=None, position=0, **details):
        armed = self.armed
        operation = operation or self.operation
        if (not armed or armed["point"] != point or armed.get("operation", operation) != operation
                or position < armed.get("position", 0)):
            return
        self.armed = None
        emit("checkpoint", point=point, operation=operation, requestId=request_id or self.request_id,
             position=position, **details)
        # Block this callback at the exact boundary. The parent performs the hard kill.
        threading.Event().wait()


async def main(args):
    gate = Checkpoints()
    service = None

    class CrashDb(MiniImSqliteDb):
        @contextmanager
        def transaction(self):
            outermost = not self.m_connection.in_transaction
            with super().transaction() as connection:
                changes = connection.total_changes
                yield connection
                if outermost and (gate.operation != "burn" or connection.total_changes > changes):
                    gate.check("before-commit")

    class Deliveries(DeliveryRepo):
        def collect_due_burn_sync_events(self, limit):
            previous = gate.operation
            gate.operation = "burn"
            try:
                events = super().collect_due_burn_sync_events(limit)
                if events:
                    gate.check("after-commit", eventIds=[event.event_id for event in events])
                emit("burn-scan", events=len(events))
                return events
            finally:
                gate.operation = previous

    class Controls(ControlWriteService):
        def handle(self, user_id, envelope):
            operation = envelope.WhichOneof("body")
            with gate.request(operation, user_id, envelope.request_id, getattr(envelope, operation)):
                return super().handle(user_id, envelope)

    class Messages(MessageService):
        def handle_send_message(self, user_id, request_id, send_message):
            with gate.request("send_message", user_id, request_id, send_message):
                return super().handle_send_message(user_id, request_id, send_message)

    class Syncs(production.SyncService):
        def handle_sync_applied(self, user_id, device_id, request_id, request):
            operation = "sync_applied" if user_id == "bob" else "sender_sync_applied"
            with gate.request(operation, user_id, request_id, request):
                return super().handle_sync_applied(user_id, device_id, request_id, request)

    class Files(FileService):
        def handle_file_init(self, user_id, request_id, file_init):
            with gate.request("file_init", user_id, request_id, file_init):
                result = super().handle_file_init(user_id, request_id, file_init)
            if result.file_updated is not None:
                emit("file-init", user=user_id, requestId=request_id, intent=file_init.client_file_id,
                     fileId=result.file_updated.file_id, offset=result.file_updated.transferred_bytes,
                     requestedOffset=file_init.resume_offset, direction=file_init.direction)
            return result

        def handle_file_finish(self, user_id, request_id, file_finish):
            with gate.request("file_finish", user_id, request_id, file_finish):
                return super().handle_file_finish(user_id, request_id, file_finish)

        def handle_file_cancel(self, user_id, request_id, request):
            with gate.request("file_cancel", user_id, request_id, request):
                return super().handle_file_cancel(user_id, request_id, request)

        def append_file_chunk(self, user_id, file_id, chunk):
            result = super().append_file_chunk(user_id, file_id, chunk)
            if result[0] is not None:
                gate.check("file-progress", operation="upload", position=result[0].transferred_bytes, fileId=file_id)
            return result

    class FileStorage(FileRepo):
        def apply_progress(self, file_id, received_bytes, member_ids):
            gate.check("file-flushed", operation="upload", position=received_bytes, fileId=file_id,
                       committed=self.get_transfer_by_file_id(file_id).received_bytes)
            return super().apply_progress(file_id, received_bytes, member_ids)

    class Protocol(production.MiniImQuicProtocol):
        def _send(self, stream_id, envelope):
            if envelope.HasField("welcome"):
                # Shorten only this isolated server's failure detection interval.
                if args.heartbeat:
                    envelope.welcome.heartbeat_interval_sec = args.heartbeat
                emit("welcome", user=envelope.welcome.user_id, session=envelope.welcome.session_id,
                     resumed=envelope.welcome.resumable)
            if envelope.HasField("ack") and gate.requests.get(envelope.request_id) == "send_message":
                emit("message-ack", requestId=envelope.request_id, payload=envelope.ack.SerializeToString().hex())
            if envelope.HasField("ack") and gate.requests.get(envelope.request_id) == "file_finish":
                emit("finish-ack", requestId=envelope.request_id, payload=envelope.ack.SerializeToString().hex())
            if envelope.HasField("ack") and gate.requests.get(envelope.request_id) in ("file_cancel", "sync_applied"):
                emit("durable-ack", operation=gate.requests[envelope.request_id],
                     requestId=envelope.request_id, payload=envelope.ack.SerializeToString().hex())
            if envelope.HasField("ack") and envelope.ack.success:
                gate.check("before-ack", operation=gate.requests.get(envelope.request_id, ""),
                           request_id=envelope.request_id, entityId=envelope.ack.entity_id)
            super()._send(stream_id, envelope)

    original_listen = production.serve_quic

    async def listen(*args, **kwargs):
        nonlocal service
        service = await original_listen(*args, **kwargs)
        emit("ready", port=service._transport.get_extra_info("sockname")[1])
        return service

    original_read = download.ReadChunk

    def read_chunk(path, offset, size):
        gate.check("download-read", operation="download", position=offset)
        return original_read(path, offset, size)

    async def commands(task):
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["op"] == "arm":
                gate.armed = command["checkpoint"]
                emit("armed", checkpoint=gate.armed)
            elif command["op"] == "stop":
                task.cancel()
                return
            else:
                raise ValueError("unknown fixture command")
        task.cancel()

    with (patch.object(production, "MiniImSqliteDb", CrashDb),
          patch.object(production, "ControlWriteService", Controls),
          patch.object(production, "MessageService", Messages),
          patch.object(production, "SyncService", Syncs),
          patch.object(production, "FileService", Files),
          patch.object(production, "FileRepo", FileStorage),
          patch.object(production, "DeliveryRepo", Deliveries),
          patch.object(production, "MiniImQuicProtocol", Protocol),
          patch.object(production, "serve_quic", listen),
          patch.object(download, "ReadChunk", read_chunk), redirect_stdout(sys.stderr)):
        task = asyncio.create_task(production.run_server(data_root=args.root, port=args.port))
        reader = asyncio.create_task(commands(task))
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            reader.cancel()
            if service:
                emit("stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--heartbeat", type=int, default=0)
    asyncio.run(main(parser.parse_args()))
