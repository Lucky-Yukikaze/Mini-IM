from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import ssl
import sys
import time
import traceback
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived

ROOT = Path(__file__).resolve().parents[3]
SERVER_DIR = ROOT / "server"
sys.path.insert(0, str(SERVER_DIR))

from protocol.codec import EnvelopeCodec  # noqa: E402
from protocol.pb import auth_pb2, common_pb2, envelope_pb2  # noqa: E402


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    parsed = urlparse(endpoint)
    if parsed.scheme == "quic":
        if not parsed.hostname or not parsed.port:
            raise ValueError("invalid quic endpoint")
        return parsed.hostname, parsed.port

    if ":" not in endpoint:
        raise ValueError("invalid endpoint")
    host, port = endpoint.rsplit(":", 1)
    return host, int(port)


class MiniImClientProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.m_envelopes: asyncio.Queue[tuple[int, envelope_pb2.Envelope]] = asyncio.Queue()

    def quic_event_received(self, event) -> None:
        if not isinstance(event, StreamDataReceived):
            return
        try:
            envelope = EnvelopeCodec.decode(event.data)
        except Exception as exc:  # pragma: no cover - defensive
            emit({"type": "error", "message": f"decode failed: {exc}"})
            return
        self.m_envelopes.put_nowait((event.stream_id, envelope))


def build_hello(args: argparse.Namespace) -> envelope_pb2.Envelope:
    now_ms = int(time.time() * 1000)
    request_id = f"{args.device_id}-{now_ms}"
    envelope = envelope_pb2.Envelope()
    envelope.version = 1
    envelope.request_id = request_id
    envelope.channel = common_pb2.CHANNEL_CONTROL
    envelope.session_id = args.resume_session_id
    envelope.device_id = args.device_id
    envelope.seq = 1
    envelope.client_time_ms = now_ms
    envelope.trace_id = request_id
    envelope.hello.CopyFrom(
        auth_pb2.Hello(
            token=args.token,
            device_id=args.device_id,
            global_cursor=max(int(args.global_cursor), 0),
            resume_session_id=args.resume_session_id,
            last_acked_request_id=args.last_acked_request_id,
        )
    )
    return envelope


def build_heartbeat(session_id: str, device_id: str, seq: int) -> envelope_pb2.Envelope:
    now_ms = int(time.time() * 1000)
    request_id = f"{device_id}-hb-{now_ms}"
    envelope = envelope_pb2.Envelope()
    envelope.version = 1
    envelope.request_id = request_id
    envelope.channel = common_pb2.CHANNEL_CONTROL
    envelope.session_id = session_id
    envelope.device_id = device_id
    envelope.seq = seq
    envelope.client_time_ms = now_ms
    envelope.trace_id = request_id
    envelope.heartbeat.CopyFrom(auth_pb2.Heartbeat(ts_ms=now_ms))
    return envelope


async def heartbeat_loop(
    protocol: MiniImClientProtocol,
    stream_id: int,
    session_id: str,
    device_id: str,
    interval_sec: int,
    seq_start: int,
) -> None:
    seq = seq_start
    while True:
        await asyncio.sleep(max(interval_sec, 3))
        seq += 1
        heartbeat = build_heartbeat(session_id=session_id, device_id=device_id, seq=seq)
        protocol._quic.send_stream_data(stream_id, EnvelopeCodec.encode(heartbeat), end_stream=False)
        protocol.transmit()


def build_initial_state(welcome: auth_pb2.Welcome) -> dict:
    return {
        "currentUser": {
            "userId": welcome.user_id,
        },
        "conversations": [],
        "recentMessages": [],
        "unreadTotal": 0,
        "globalCursor": int(welcome.global_cursor),
    }


async def run(args: argparse.Namespace) -> int:
    host, port = parse_endpoint(args.endpoint)
    emit({"type": "connection", "state": "connecting", "session_id": ""})

    configuration = QuicConfiguration(is_client=True, alpn_protocols=["mini-im"])
    configuration.verify_mode = ssl.CERT_NONE

    async with connect(
        host=host,
        port=port,
        configuration=configuration,
        create_protocol=MiniImClientProtocol,
    ) as protocol_base:
        protocol = cast(MiniImClientProtocol, protocol_base)
        stream_id = protocol._quic.get_next_available_stream_id(is_unidirectional=False)
        hello = build_hello(args)
        protocol._quic.send_stream_data(stream_id, EnvelopeCodec.encode(hello), end_stream=False)
        protocol.transmit()

        try:
            _, response = await asyncio.wait_for(protocol.m_envelopes.get(), timeout=5)
        except asyncio.TimeoutError:
            emit({"type": "error", "message": "welcome timeout"})
            return 2

        if not response.HasField("welcome"):
            emit({"type": "error", "message": "unexpected handshake response"})
            return 3

        welcome = response.welcome
        emit(
            {
                "type": "connection",
                "state": "connected",
                "session_id": welcome.session_id,
                "global_cursor": int(welcome.global_cursor),
                "heartbeat_interval_sec": int(welcome.heartbeat_interval_sec),
                "resumable": bool(welcome.resumable),
                "need_reauth": bool(welcome.need_reauth),
            }
        )
        emit(
            {
                "type": "initial_state_loaded",
                "payload": build_initial_state(welcome),
            }
        )
        emit({"type": "cursor", "global_cursor": int(welcome.global_cursor)})
        emit({"type": "ack", "request_id": response.request_id})

        heartbeat_task = asyncio.create_task(
            heartbeat_loop(
                protocol=protocol,
                stream_id=stream_id,
                session_id=welcome.session_id,
                device_id=args.device_id,
                interval_sec=int(welcome.heartbeat_interval_sec),
                seq_start=max(int(response.seq), 1),
            )
        )
        try:
            while True:
                _, incoming = await protocol.m_envelopes.get()
                if incoming.HasField("heartbeat"):
                    emit({"type": "ack", "request_id": incoming.request_id})
                    continue
                if incoming.HasField("error"):
                    emit({"type": "error", "message": incoming.error.message or "server error"})
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--resume-session-id", default="")
    parser.add_argument("--global-cursor", default="0")
    parser.add_argument("--last-acked-request-id", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # pragma: no cover - runtime safeguard
        emit({"type": "error", "message": str(exc)})
        emit({"type": "error", "message": traceback.format_exc()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
