from __future__ import annotations

import struct

from protocol.pb import envelope_pb2


class EnvelopeCodec:
    MAX_FRAME_SIZE = 16 * 1024 * 1024

    @staticmethod
    def decode(raw: bytes) -> envelope_pb2.Envelope:
        envelope = envelope_pb2.Envelope()
        envelope.ParseFromString(raw)
        return envelope

    @staticmethod
    def encode(envelope: envelope_pb2.Envelope) -> bytes:
        return envelope.SerializeToString()

    @staticmethod
    def encode_frame(envelope: envelope_pb2.Envelope) -> bytes:
        payload = EnvelopeCodec.encode(envelope)
        return struct.pack(">I", len(payload)) + payload

    @staticmethod
    def decode_frames(buffer: bytearray) -> list[envelope_pb2.Envelope]:
        items: list[envelope_pb2.Envelope] = []
        while len(buffer) >= 4:
            payload_size = struct.unpack(">I", buffer[:4])[0]
            if payload_size <= 0 or payload_size > EnvelopeCodec.MAX_FRAME_SIZE:
                raise ValueError(f"invalid frame size: {payload_size}")
            frame_size = 4 + payload_size
            if len(buffer) < frame_size:
                break
            payload = bytes(buffer[4:frame_size])
            del buffer[:frame_size]
            items.append(EnvelopeCodec.decode(payload))
        return items
