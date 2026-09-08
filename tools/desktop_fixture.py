"""Launch a real Qt desktop with isolated QUIC services and inspectable test data.

Read context.json for endpoint, seeded group and local WebEngine debugging URL.
Write {"id": "unique-command", "op": ...} to command.json and wait for the same
id in response.json. Operations: snapshot, message(text), reject(code),
drop-ack(count), cache-fault(enabled,user,device), restart-client, stop.
Only the private fixture databases are changed. No default development data is used.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aioquic.quic.configuration import QuicConfiguration
from protocol.pb import common_pb2, conversation_pb2, message_pb2
from quic.endpoint import serve_quic
from quic.server import FaultConfig, MiniImQuicProtocol, OnlineSessionHub, ensure_dev_cert
from services.auth.service import AuthService
from services.control.service import ControlWriteService
from services.conversation.service import ConversationService
from services.delivery.service import DeliveryService
from services.file.service import FileService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, FileRepo, MessageRepo, SyncRepo
from storage.repo.control_write_repo import ControlWriteRepo, ControlWriteResult
from storage.sqlite.db import MiniImSqliteDb


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class DesktopFixture:
    def __init__(self, args, root, output):
        self.args, self.root, self.output = args, root, output
        self.db = MiniImSqliteDb(root / "server.db")
        self.db.init_schema()
        self.server = self.client = self.client_log = None
        self.protocols = []
        self.attempts = []
        self.rejected = 0
        self.drop_acks = 0
        self.context = {}
        self.hub = OnlineSessionHub()
        self.repo = ConversationRepo(self.db)
        self.conversations = ConversationService(self.repo)
        self.messages = MessageService(MessageRepo(self.db), self.repo)
        self.server_log = (output / "server.log").open("w", encoding="utf-8")

    async def start(self):
        for user in ("alice", "bob", "cindy"):
            self.conversations.ensure_user(user)
        group = self.conversations.handle_create_conversation("alice", "setup", conversation_pb2.CreateConversation(
            client_conv_id="desktop-group", title="Desktop QA", type=common_pb2.CONVERSATION_GROUP, member_ids=["bob"]))
        self.group = group.ack.entity_id
        scenario = self
        delivery = DeliveryService(DeliveryRepo(self.db))

        class Controls(ControlWriteService):
            def handle(self, user_id, envelope):
                operation = envelope.WhichOneof("body")
                scenario.attempts.append(dict(user=user_id, requestId=envelope.request_id, operation=operation,
                    body=getattr(envelope, operation).SerializeToString().hex()))
                if scenario.rejected:
                    return ControlWriteResult(message_pb2.Ack(request_id=envelope.request_id, success=False,
                        code=scenario.rejected, message="injected desktop control failure"), [])
                return super().handle(user_id, envelope)

        controls = Controls(ControlWriteRepo(self.db), self.conversations, delivery)
        files = FileService(FileRepo(self.db), self.repo, MessageRepo(self.db), self.root / "files", 900000)
        auth = AuthService()

        class Protocol(MiniImQuicProtocol):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                scenario.protocols.append(self)

            def _send(self, stream_id, envelope):
                if envelope.HasField("ack") and "-control-" in envelope.request_id and scenario.drop_acks:
                    scenario.drop_acks -= 1
                    return
                super()._send(stream_id, envelope)

            def _debug(self, message):
                scenario.server_log.write(message + "\n")
                scenario.server_log.flush()

        cert, key = self.root / "cert.pem", self.root / "key.pem"
        ensure_dev_cert(cert, key)
        config = QuicConfiguration(is_client=False, alpn_protocols=["mini-im"])
        config.load_cert_chain(str(cert), str(key))
        self.server = await serve_quic("127.0.0.1", 0, configuration=config,
            create_protocol=lambda *args, **kwargs: Protocol(*args, auth_service=auth,
                conversation_service=self.conversations, control_write_service=controls, file_service=files,
                message_service=self.messages, sync_service=SyncService(SyncRepo(self.db)),
                online_hub=self.hub, fault_config=FaultConfig(), **kwargs))
        self.port = self.server._transport.get_extra_info("sockname")[1]
        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            debug_port = probe.getsockname()[1]
        self.context = dict(endpoint=f"quic://127.0.0.1:{self.port}", group=self.group,
            debug=f"http://127.0.0.1:{debug_port}", root=str(self.root), output=str(self.output),
            state=str(self.root / "state"), users=["alice", "bob", "cindy"], device="desktop-device")
        await self.start_client()

    async def start_client(self):
        env = dict(os.environ, MINIIM_DEBUG_LOG="0", MINIIM_STATE_ROOT=self.context["state"],
            QTWEBENGINE_REMOTE_DEBUGGING=self.context["debug"].removeprefix("http://"), QT_QPA_PLATFORM="offscreen",
            QT_QPA_PLATFORM_PLUGIN_PATH=str(self.args.qt_root / "plugins/platforms"))
        env.pop("MINIIM_WEB_URL", None)
        env.pop("MINIIM_WEB_SMOKE_TEST", None)
        self.client_log = (self.output / "qt.log").open("ab")
        self.client = await asyncio.create_subprocess_exec(str(self.args.client), env=env,
            stdout=self.client_log, stderr=self.client_log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.context["pid"] = self.client.pid
        write_json(self.output / "context.json", self.context)

    async def stop_client(self):
        if self.client and self.client.returncode is None:
            self.client.terminate()
            await self.client.wait()
        if self.client_log:
            self.client_log.close()

    def snapshot(self):
        state = dict(attempts=self.attempts,
            conversations=[dict(row) for row in self.db.execute_fetchall("SELECT * FROM conversations")],
            members=[dict(row) for row in self.db.execute_fetchall("SELECT * FROM conversation_members")],
            events=self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0],
            messages=[dict(row) for row in self.db.execute_fetchall(
                "SELECT server_msg_id,conversation_id,sender_id,conversation_seq,recalled FROM messages")], caches={})
        for path in (self.root / "state").glob("*.sqlite"):
            with closing(sqlite3.connect(path)) as cache:
                cache.row_factory = sqlite3.Row
                state["caches"][path.name] = [dict(row) for row in cache.execute(
                    "SELECT request_id,operation,status,code,error,attempts FROM control_outbox")]
        return state

    async def command(self, data):
        op = data["op"]
        if op == "reject":
            self.rejected = int(data["code"])
        elif op == "drop-ack":
            self.drop_acks = int(data.get("count", 1))
        elif op == "message":
            result = self.messages.handle_send_message("bob", data["id"], message_pb2.SendMessage(
                conversation_id=self.group, client_msg_id=data["id"], type=common_pb2.MSG_TEXT,
                content=data["text"].encode("utf-8")))
            if not result.ack.success:
                raise ValueError(result.ack.message)
            self.hub.fanout_sync_events(result.sync_events)
        elif op == "cache-fault":
            identity = json.dumps([f"127.0.0.1:{self.port}", data.get("user", "alice"),
                data.get("device", "desktop-device")], separators=(",", ":"), ensure_ascii=False)
            path = self.root / "state" / (hashlib.sha256(identity.encode()).hexdigest() + ".sqlite")
            if not path.is_file():
                raise ValueError("connect the selected fixture account before injecting its cache fault")
            with closing(sqlite3.connect(path)) as cache, cache:
                if data["enabled"]:
                    cache.execute("CREATE TRIGGER qa_reject_control BEFORE INSERT ON control_outbox "
                        "BEGIN SELECT RAISE(ABORT, 'desktop injected save failure'); END")
                else:
                    cache.execute("DROP TRIGGER qa_reject_control")
        elif op == "restart-client":
            await self.stop_client()
            await self.start_client()
        elif op not in ("snapshot", "stop"):
            raise ValueError("unknown fixture operation")
        return self.snapshot()

    async def close(self):
        await self.stop_client()
        if self.server:
            self.server.close()
        for protocol in self.protocols:
            protocol.m_download_sender.close()
            await protocol.m_download_sender.wait_closed()
        write_json(self.output / "final-state.json", self.snapshot())
        self.db.close()
        self.server_log.close()


async def run(args):
    output = args.output.resolve() / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=output) as temporary:
        fixture = DesktopFixture(args, Path(temporary), output)
        try:
            await fixture.start()
            print(json.dumps(fixture.context), flush=True)
            seen = None
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                try:
                    data = json.loads((output / "command.json").read_text(encoding="utf-8-sig"))
                except (FileNotFoundError, json.JSONDecodeError):
                    await asyncio.sleep(0.1)
                    continue
                if data["id"] != seen:
                    seen = data["id"]
                    try:
                        state = await fixture.command(data)
                        write_json(output / "response.json", dict(id=seen, ok=True, state=state))
                    except Exception as error:
                        write_json(output / "response.json", dict(id=seen, ok=False, error=str(error)))
                    if data["op"] == "stop":
                        break
                await asyncio.sleep(0.1)
        finally:
            await fixture.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, default=ROOT / "build/client_qt611/Release/mini_im_client.exe")
    parser.add_argument("--qt-root", type=Path, required=True, help="Qt kit directory containing plugins/platforms")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/desktop-integration")
    parser.add_argument("--timeout", type=int, default=900, help="maximum fixture lifetime in seconds")
    args = parser.parse_args()
    if not args.client.is_file() or not (args.qt_root / "plugins/platforms").is_dir() or args.timeout <= 0:
        parser.error("provide a built desktop, installed Qt kit and positive timeout")
    args.client = args.client.resolve()
    asyncio.run(run(args))
