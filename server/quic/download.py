"""Bound file reads to unacknowledged stream data in aioquic 1.3.x.

Only AioquicSendBuffer accesses aioquic's private stream state. Its compatibility
tests use aioquic's real QuicStreamSender, including out-of-order ACKs and loss.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

FILE_STREAM_HEADER_PREFIX = b"MINIIMFILE1 "
CHUNK_SIZE = 64 * 1024
STREAM_BUFFER_LIMIT = 128 * 1024
MAX_DOWNLOADS = 8


class AioquicSendBuffer:
    def __init__(self, quic):
        self.quic = quic

    def pending_bytes(self, stream_id: int) -> int:
        stream = self.quic._streams.get(stream_id)
        return len(stream.sender._buffer) if stream is not None else 0

    def finished(self, stream_id: int) -> bool:
        stream = self.quic._streams.get(stream_id)
        return stream is None or stream.sender.is_finished

    def reset_requested(self, stream_id: int) -> bool:
        stream = self.quic._streams.get(stream_id)
        return stream is not None and stream.sender._reset_error_code is not None


@dataclass
class DownloadJob:
    stream_id: int
    file_id: str
    path: Path
    offset: int
    size: int
    priority: int
    eof: bool = False


def ReadChunk(path: Path, offset: int, size: int) -> bytes:
    # The worker owns the handle, including when the scheduling task is stopped.
    with path.open("rb") as source:
        source.seek(offset)
        return source.read(size)


class DownloadScheduler:
    def __init__(self, protocol):
        self.protocol = protocol
        self.buffer = AioquicSendBuffer(protocol._quic)
        self.jobs: dict[int, DownloadJob] = {}
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.closed = False
        self.peak_pending_bytes = 0

    @property
    def can_accept(self) -> bool:
        return not self.closed and len(self.jobs) < MAX_DOWNLOADS

    def start(self, file_id: str, path: Path, size: int, offset: int, priority: int = 0) -> None:
        if any(job.file_id == file_id for job in self.jobs.values()):
            return
        if not self.can_accept:
            raise ValueError("download queue is full")
        if size <= 0 or offset < 0 or offset > size:
            raise ValueError("invalid download offset or size")
        stream_id = self.protocol._quic.get_next_available_stream_id(is_unidirectional=True)
        header = FILE_STREAM_HEADER_PREFIX + file_id.encode("utf-8") + b"\n"
        if len(header) > 512:
            raise ValueError("invalid download file id")
        self.protocol._quic.send_stream_data(stream_id, header)
        self.jobs[stream_id] = DownloadJob(stream_id, file_id, path, offset, size, priority)
        self.protocol.transmit()
        self.notify()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    def cancel(self, file_id: str) -> None:
        for stream_id, job in list(self.jobs.items()):
            if job.file_id == file_id:
                self.protocol._quic.reset_stream(stream_id, 0x1005)
                job.eof = True
        self.protocol.transmit()
        self.notify()

    def notify(self) -> None:
        self.changed.set()

    def close(self) -> None:
        self.closed = True
        self.notify()

    async def wait_closed(self) -> None:
        if self.task is not None:
            await self.task

    async def _run(self) -> None:
        try:
            while self.jobs and not self.closed:
                self.changed.clear()
                progressed = False
                # Every active job gets one read per turn; priority controls order without starving others.
                for job in sorted(list(self.jobs.values()), key=lambda value: -value.priority):
                    if self.closed:
                        break
                    if self.jobs.get(job.stream_id) is not job:
                        continue
                    if self.buffer.finished(job.stream_id):
                        self.jobs.pop(job.stream_id, None)
                        progressed = True
                        continue
                    if job.eof or self.buffer.reset_requested(job.stream_id):
                        continue
                    budget = STREAM_BUFFER_LIMIT - self.buffer.pending_bytes(job.stream_id)
                    if budget <= 0:
                        continue
                    try:
                        chunk = await asyncio.to_thread(ReadChunk, job.path, job.offset, min(CHUNK_SIZE, budget))
                        if self.closed:
                            break
                        if self.jobs.get(job.stream_id) is not job:
                            continue
                        if self.buffer.reset_requested(job.stream_id) or self.buffer.finished(job.stream_id):
                            continue
                        if not chunk and job.offset != job.size:
                            raise OSError("download source ended before declared size")
                        if job.offset + len(chunk) > job.size:
                            raise OSError("download source exceeds declared size")
                        self.protocol._quic.send_stream_data(job.stream_id, chunk, end_stream=not chunk)
                        job.offset += len(chunk)
                        job.eof = not chunk
                        progressed = True
                    except (OSError, ValueError) as error:
                        self.protocol._debug(f"download failed file={job.file_id}: {error}")
                        self.protocol._quic.reset_stream(job.stream_id, 0x1004)
                        job.eof = True
                        progressed = True
                    self.peak_pending_bytes = max(
                        self.peak_pending_bytes,
                        sum(self.buffer.pending_bytes(stream_id) for stream_id in self.jobs),
                    )
                    self.protocol.transmit()
                if progressed:
                    await asyncio.sleep(0)
                elif self.jobs and not self.closed:
                    await self.changed.wait()
        finally:
            if self.closed:
                self.jobs.clear()
