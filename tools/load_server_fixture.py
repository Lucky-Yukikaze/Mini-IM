"""Measure production service calls in an isolated process; never inject failures."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout, suppress
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from quic import server as production
from services.file.service import FileService
from storage.sqlite.write_queue import SqliteWriteQueue


def emit(event, **data):
    sys.__stdout__.write(json.dumps(dict(event=event, pid=os.getpid(), **data)) + "\n")
    sys.__stdout__.flush()


async def main(args):
    metrics = {}
    active = False
    service = None

    def record(name, elapsed):
        value = metrics.setdefault(name, dict(count=0, seconds=0.0, maxSeconds=0.0))
        value['count'] += 1
        value['seconds'] += elapsed
        value['maxSeconds'] = max(value['maxSeconds'], elapsed)

    original_append = FileService.append_file_chunk
    original_fsync = os.fsync
    original_enqueue = SqliteWriteQueue.enqueue
    original_listen = production.serve_quic

    def append(instance, *pos, **kw):
        started = time.perf_counter()
        result = original_append(instance, *pos, **kw)
        if active:
            record('append', time.perf_counter() - started)
            metrics['uploadBytes'] = metrics.get('uploadBytes', 0) + len(kw['chunk'])
            metrics['fileSyncEvents'] = metrics.get('fileSyncEvents', 0) + len(result[1])
        return result

    def fsync(fd):
        started = time.perf_counter()
        result = original_fsync(fd)
        if active:
            record('fsync', time.perf_counter() - started)
        return result

    def enqueue(instance, operation):
        queued = time.perf_counter()
        measured = active
        def run():
            started = time.perf_counter()
            if measured and active:
                record('queueWait', started - queued)
            try:
                return operation()
            finally:
                if measured and active:
                    record('queueWork', time.perf_counter() - started)
        result = original_enqueue(instance, run)
        if active:
            metrics['maxQueuedOperations'] = max(metrics.get('maxQueuedOperations', 0), len(instance.m_queue))
        return result

    async def listen(*pos, **kw):
        nonlocal service
        service = await original_listen(*pos, **kw)
        emit('ready', port=service._transport.get_extra_info('sockname')[1])
        return service

    async def lag():
        while True:
            started = time.perf_counter()
            await asyncio.sleep(0.02)
            if active:
                record('loopLag', max(0, time.perf_counter() - started - 0.02))

    async def commands(task):
        nonlocal active
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command['op'] == 'begin':
                metrics.clear()
                active = True
                emit('begun')
            elif command['op'] == 'metrics':
                active = False
                emit('metrics', values=metrics)
            elif command['op'] == 'stop':
                task.cancel()
                return
            else:
                raise ValueError('unknown load fixture command')
        task.cancel()

    with (patch.object(FileService, 'append_file_chunk', append), patch.object(os, 'fsync', fsync),
          patch.object(SqliteWriteQueue, 'enqueue', enqueue), patch.object(production, 'serve_quic', listen),
          redirect_stdout(sys.stderr)):
        task = asyncio.create_task(production.run_server(data_root=args.root, port=args.port))
        reader = asyncio.create_task(commands(task))
        timer = asyncio.create_task(lag())
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            reader.cancel()
            timer.cancel()
            for pending in (reader, timer):
                with suppress(asyncio.CancelledError):
                    await pending
            if service:
                emit('stopped')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--heartbeat', type=int, default=0)
    asyncio.run(main(parser.parse_args()))
