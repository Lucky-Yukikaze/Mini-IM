"""Control envelope byte framing boundaries, independent of QUIC chunk sizes."""
from pathlib import Path
import struct
import sys
import unittest
from google.protobuf.message import DecodeError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.codec import EnvelopeCodec
from protocol.pb import envelope_pb2


class EnvelopeCodecTest(unittest.TestCase):
    def test_every_split_preserves_one_envelope(self):
        expected = envelope_pb2.Envelope(version=1, request_id="split-\u4e2d\u6587")
        expected.heartbeat.ts_ms = 123
        frame = EnvelopeCodec.encode_frame(expected)
        for offset in range(len(frame)):
            with self.subTest(offset=offset):
                buffer = bytearray(frame[:offset])
                self.assertEqual([], EnvelopeCodec.decode_frames(buffer))
                buffer.extend(frame[offset:])
                self.assertEqual([expected], EnvelopeCodec.decode_frames(buffer))
                self.assertEqual(b"", buffer)

    def test_coalesced_frames_preserve_order_and_partial_tail(self):
        messages = [envelope_pb2.Envelope(version=1, request_id=str(i)) for i in range(3)]
        frames = [EnvelopeCodec.encode_frame(item) for item in messages]
        buffer = bytearray(frames[0] + frames[1] + frames[2][:-1])
        self.assertEqual(messages[:2], EnvelopeCodec.decode_frames(buffer))
        self.assertEqual(frames[2][:-1], buffer)
        buffer.extend(frames[2][-1:])
        self.assertEqual(messages[2:], EnvelopeCodec.decode_frames(buffer))
        self.assertEqual(b"", buffer)

    def test_invalid_lengths_are_rejected_from_header_alone(self):
        for size in (0, EnvelopeCodec.MAX_FRAME_SIZE + 1, 0xffffffff):
            with self.subTest(size=size), self.assertRaises(ValueError):
                EnvelopeCodec.decode_frames(bytearray(struct.pack(">I", size)))

    def test_maximum_payload_is_accepted(self):
        expected = envelope_pb2.Envelope(request_id="x" * (EnvelopeCodec.MAX_FRAME_SIZE - 5))
        frame = EnvelopeCodec.encode_frame(expected)
        self.assertEqual(EnvelopeCodec.MAX_FRAME_SIZE + 4, len(frame))
        buffer = bytearray(frame)
        self.assertEqual([expected], EnvelopeCodec.decode_frames(buffer))
        self.assertEqual(b"", buffer)

    def test_malformed_protobuf_is_rejected(self):
        with self.assertRaises(DecodeError):
            EnvelopeCodec.decode_frames(bytearray(b"\x00\x00\x00\x01\xff"))


if __name__ == "__main__":
    unittest.main()
