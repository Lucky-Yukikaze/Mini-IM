import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aioquic.quic.packet_builder import QuicDeliveryState
from aioquic.quic.stream import QuicStream
from quic.download import DownloadScheduler, STREAM_BUFFER_LIMIT, MAX_DOWNLOADS, FILE_STREAM_HEADER_PREFIX


class FakeConnection:
    """Uses the installed aioquic sender; only packet transport is controlled."""
    def __init__(self):
        self._streams = {}
        self.next_id = 3
        self.sends = {}
        self.fin = set()
        self.resets = set()

    def get_next_available_stream_id(self, **kwargs):
        stream_id = self.next_id
        self.next_id += 4
        return stream_id

    def send_stream_data(self, stream_id, data, end_stream=False):
        stream = self._streams.setdefault(stream_id, QuicStream(stream_id, readable=False))
        stream.sender.write(data, end_stream=end_stream)
        self.sends.setdefault(stream_id, bytearray()).extend(data)
        if end_stream:
            self.fin.add(stream_id)

    def reset_stream(self, stream_id, error_code):
        self.resets.add(stream_id)
        self._streams[stream_id].sender.reset(error_code)

    def acknowledge_all(self):
        for stream in list(self._streams.values()):
            sender = stream.sender
            if sender._reset_error_code is not None:
                sender.on_reset_delivery(QuicDeliveryState.ACKED)
                continue
            while frame := sender.get_frame(4096):
                sender.on_data_delivery(QuicDeliveryState.ACKED, frame.offset,
                                        frame.offset + len(frame.data), frame.fin)


class DownloadSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "source.bin"
        self.payload = bytes(range(251)) * 4096
        self.path.write_bytes(self.payload)
        self.quic = FakeConnection()
        self.protocol = SimpleNamespace(_quic=self.quic, transmit=lambda: None, _debug=lambda message: None)
        self.sender = DownloadScheduler(self.protocol)
        self.addAsyncCleanup(self.stop)

    async def stop(self):
        self.sender.close()
        await self.sender.wait_closed()

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.001)

    async def drain(self):
        async with asyncio.timeout(5):
            while self.sender.task is not None and not self.sender.task.done():
                self.quic.acknowledge_all()
                self.sender.notify()
                await asyncio.sleep(0.001)
        await self.sender.wait_closed()

    async def test_reading_waits_for_ack_even_with_loss_and_reordered_ack(self):
        self.sender.start("bounded", self.path, len(self.payload), 0)
        await self.until(lambda: self.sender.buffer.pending_bytes(3) == STREAM_BUFFER_LIMIT)
        queued = len(self.quic.sends[3])
        await asyncio.sleep(0.03)
        self.assertEqual(queued, len(self.quic.sends[3]), "no further reads without ACK")
        sender = self.quic._streams[3].sender
        frames = [sender.get_frame(32768), sender.get_frame(32768)]
        sender.on_data_delivery(QuicDeliveryState.ACKED, frames[1].offset,
                                frames[1].offset + len(frames[1].data), False)
        sender.on_data_delivery(QuicDeliveryState.LOST, frames[0].offset,
                                frames[0].offset + len(frames[0].data), False)
        self.sender.notify()
        await asyncio.sleep(0.03)
        self.assertEqual(queued, len(self.quic.sends[3]), "out-of-order ACK cannot free the prefix")
        self.assertNotIn(3, self.quic.fin, "buffer acceptance must not end the file")
        await self.drain()
        self.assertEqual(FILE_STREAM_HEADER_PREFIX + b"bounded\n" + self.payload, bytes(self.quic.sends[3]))
        self.assertLessEqual(self.sender.peak_pending_bytes, STREAM_BUFFER_LIMIT)
        self.assertFalse(self.sender.jobs)

    async def test_parallel_limit_resume_bytes_and_disconnect(self):
        offset = 123
        for index in range(MAX_DOWNLOADS):
            self.sender.start(str(index), self.path, len(self.payload), offset, priority=index)
        with self.assertRaisesRegex(ValueError, "queue is full"):
            self.sender.start("overflow", self.path, len(self.payload), 0)
        # Repeated scheduling of the same task must not create a second stream.
        self.sender.start("0", self.path, len(self.payload), offset)
        self.assertEqual(MAX_DOWNLOADS, len(self.sender.jobs))
        await self.until(lambda: all(self.sender.buffer.pending_bytes(stream) == STREAM_BUFFER_LIMIT
                                     for stream in self.sender.jobs))
        self.assertLessEqual(self.sender.peak_pending_bytes, MAX_DOWNLOADS * STREAM_BUFFER_LIMIT)
        await self.stop()
        queued = {key: len(value) for key, value in self.quic.sends.items()}
        await asyncio.sleep(0.02)
        self.assertEqual(queued, {key: len(value) for key, value in self.quic.sends.items()})
        self.assertFalse(self.sender.jobs)
        for index, content in enumerate(self.quic.sends.values()):
            header = FILE_STREAM_HEADER_PREFIX + str(index).encode() + b"\n"
            self.assertTrue(content.startswith(header))
            self.assertEqual(self.payload[offset:offset + len(content) - len(header)], content[len(header):])
        self.assertFalse(self.quic.fin)

    async def test_source_errors_reset_stream_without_successful_eof(self):
        self.sender.start("missing", self.path.with_name("missing"), len(self.payload), 0)
        await self.until(lambda: 3 in self.quic.resets)
        self.assertNotIn(3, self.quic.fin)
        await self.drain()
        self.assertFalse(self.sender.jobs)

    async def test_truncated_source_resets_instead_of_sending_successful_eof(self):
        self.path.write_bytes(self.payload[:65536])
        self.sender.start("truncated", self.path, len(self.payload), 0)
        await self.drain()
        self.assertIn(3, self.quic.resets)
        self.assertNotIn(3, self.quic.fin)
        self.assertFalse(self.sender.jobs)

    async def test_peer_abort_stops_reading_and_releases_slot_after_reset_ack(self):
        self.sender.start("aborted", self.path, len(self.payload), 0)
        await self.until(lambda: self.sender.buffer.pending_bytes(3) == STREAM_BUFFER_LIMIT)
        self.quic.reset_stream(3, 1)
        queued = len(self.quic.sends[3])
        self.sender.notify()
        await asyncio.sleep(0.02)
        self.assertEqual(queued, len(self.quic.sends[3]))
        await self.drain()
        self.assertFalse(self.sender.jobs)
        self.assertNotIn(3, self.quic.fin)

    async def test_cancel_during_disk_read_never_sends_late_chunk_or_fin(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked_read(function, *args):
            entered.set()
            await release.wait()
            return self.payload[:65536]
        with patch("quic.download.asyncio.to_thread", blocked_read):
            self.sender.start("cancel-me", self.path, len(self.payload), 0)
            await entered.wait()
            before = bytes(self.quic.sends[3])
            self.sender.cancel("cancel-me")
            self.sender.cancel("cancel-me")
            self.assertIn(3, self.sender.jobs, "reset buffers must stay counted until acknowledgement")
            self.assertIn(3, self.quic.resets)
            release.set()
            await self.drain()
        self.assertEqual(before, bytes(self.quic.sends[3]))
        self.assertNotIn(3, self.quic.fin)
        self.assertFalse(self.sender.jobs)
        self.sender.start("unrelated", self.path, len(self.payload), 0)
        await self.drain()
        self.assertTrue(self.quic.sends[7].endswith(self.payload))

    async def test_valid_offset_completes_with_only_remaining_content(self):
        self.sender.start("resume", self.path, len(self.payload), 197)
        await self.drain()
        self.assertEqual(FILE_STREAM_HEADER_PREFIX + b"resume\n" + self.payload[197:], self.quic.sends[3])
        self.assertIn(3, self.quic.fin)
        with self.assertRaisesRegex(ValueError, "offset"):
            self.sender.start("invalid", self.path, len(self.payload), len(self.payload) + 1)


if __name__ == "__main__":
    unittest.main()
