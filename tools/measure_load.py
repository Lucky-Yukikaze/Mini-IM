"""Measure chat alone and chat with simultaneous native uploads/downloads on Windows.

Every case starts two real Qt drivers and an isolated production service process.
Use --sustained to refill each transfer slot until the measurement deadline.
This is a reproducible measurement, not a throughput or latency acceptance SLA.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time

from process_metrics import ProcessMetrics
from test_native_flow import NativeClient
from test_server_restart import ServerProcess

ROOT = Path(__file__).resolve().parents[1]


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return dict(count=0)
    result = dict(count=len(values), mean=sum(values) / len(values), min=ordered[0], max=ordered[-1])
    for percentile in (50, 95, 99):
        result[f'p{percentile}'] = ordered[math.ceil(len(ordered) * percentile / 100) - 1]
    return result


def rows(path, query, parameters=()):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, parameters)]


async def server_command(server, operation, event):
    mark = len(server.events)
    server.process.stdin.write((json.dumps(dict(op=operation)) + '\n').encode())
    await server.process.stdin.drain()
    return await server.wait(event, since=mark)


async def settled(root, db_path, clients):
    async with asyncio.timeout(20):
        while True:
            ready = True
            for user in clients:
                maximum = rows(db_path, 'SELECT COALESCE(MAX(seq),0) AS value FROM sync_events WHERE user_id=?', (user,))[0]['value']
                cache = next((root / f'state-{user}').glob('*.sqlite'))
                metadata = {row['key']: row['value'] for row in rows(cache, 'SELECT key,value FROM metadata')}
                if int(metadata.get('sync_confirmed_cursor', 0)) != maximum or metadata.get('sync_confirmation'):
                    ready = False
            if ready:
                return
            await asyncio.sleep(0.1)


async def run_case(args, output, concurrency):
    output.mkdir()
    report = dict(ok=False, mode='sustained' if args.sustained else 'burst',
        concurrencyPerDirection=concurrency, fileBytes=args.file_bytes,
        minimumChatSeconds=args.seconds, chatIntervalSeconds=args.interval, started=time.strftime('%Y-%m-%d %H:%M:%S'))
    clients, resources = {}, {}
    server = None
    samples, messages, files = [], [], []
    with tempfile.TemporaryDirectory(prefix='fixture-', dir=output) as temporary:
        root = Path(temporary)
        db_path = root / 'server/storage/sqlite/miniim.db'
        try:
            server = await ServerProcess.start(root / 'server', 0, output, 0, 0,
                fixture=ROOT / 'tools/load_server_fixture.py')
            for user in ('alice', 'bob'):
                client = await NativeClient.start(args.client, output / f'{user}.log', root / f'state-{user}')
                clients[user] = client
                await client.connect(f'quic://127.0.0.1:{server.port}', user)
            alice, bob = clients['alice'], clients['bob']
            mark = await alice.command('group', intent='load-group', title='Load measurement', members=['bob'])
            conversation = (await alice.wait('conversation', since=mark))['conversationId']
            await bob.wait('conversation', lambda item: item['conversationId'] == conversation)
            locks = {user: asyncio.Lock() for user in clients}

            async def command(user, operation, **data):
                async with locks[user]:
                    return await clients[user].command(operation, **data)

            payload = (bytes(range(256)) * ((args.file_bytes + 255) // 256))[:args.file_bytes]
            digest = hashlib.sha256(payload).hexdigest()
            upload_digests = {'seed.bin': digest}

            async def transfer(user, operation, path, source=None):
                start = time.perf_counter()
                data = dict(conversation=conversation, path=str(path))
                if source is not None:
                    data['source'] = source
                mark = await command(user, operation, **data)
                task_event = await clients[user].wait('file-tasks', lambda event: any(
                    item.get('fileName') == path.name and item.get('fileId') for item in event['items']), since=mark)
                file_id = next(item['fileId'] for item in task_event['items'] if item.get('fileName') == path.name)
                completed = await clients[user].wait('file',
                    lambda item: item.get('completed') and item.get('fileId') == file_id, since=mark, timeout=75)
                elapsed = time.perf_counter() - start
                if operation == 'download':
                    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
                return dict(direction=operation, fileId=completed['fileId'], name=path.name,
                    bytes=args.file_bytes, seconds=elapsed, bytesPerSecond=args.file_bytes / elapsed,
                    startedMonotonic=start, finishedMonotonic=start + elapsed)

            seed = None
            if concurrency:
                source = root / 'seed.bin'
                source.write_bytes(payload)
                seed = (await transfer('alice', 'upload', source))['fileId']
            await settled(root, db_path, clients)
            for name, pid in [('server', server.pid), ('controller', os.getpid())] + [(name, client.process.pid) for name, client in clients.items()]:
                resources[name] = ProcessMetrics(pid)
            await server_command(server, 'begin', 'begun')
            initial = {name: process.sample() for name, process in resources.items()}
            started = time.perf_counter()
            done = asyncio.Event()

            async def sample():
                while not done.is_set():
                    samples.append(dict(seconds=time.perf_counter() - started,
                        processes={name: process.sample() for name, process in resources.items()}))
                    await asyncio.sleep(0.1)

            async def chat():
                while not done.is_set():
                    index = len(messages)
                    intent = f'load-message-{index}'
                    before = time.perf_counter()
                    mark = len(bob.events)
                    await command('alice', 'message', conversation=conversation, intent=intent, text='x' * 128)
                    accepted = time.perf_counter()
                    await bob.wait('message', lambda item: item['clientMsgId'] == intent, since=mark, timeout=30)
                    elapsed = time.perf_counter() - before
                    messages.append(dict(intent=intent, startSeconds=before - started, deliveryMs=elapsed * 1000,
                        acceptedMs=(accepted - before) * 1000, afterAcceptMs=elapsed * 1000 - (accepted - before) * 1000))
                    await asyncio.sleep(max(0, args.interval - elapsed))

            async def transfer_slot(direction, index):
                iteration = 0
                while iteration == 0 or (args.sustained and time.perf_counter() < started + args.seconds):
                    suffix = f'{index}-{iteration}' if args.sustained else str(index)
                    path = root / f'{direction}-{suffix}.bin'
                    if direction == 'upload':
                        content = bytes([(index + iteration + 1) % 256]) + payload[1:]
                        path.write_bytes(content)
                        upload_digests[path.name] = hashlib.sha256(content).hexdigest()
                        result = await transfer('alice', direction, path)
                    else:
                        result = await transfer('bob', direction, path, seed)
                    files.append(dict(slot=index, iteration=iteration, **result))
                    iteration += 1

            async def transfers():
                async with asyncio.TaskGroup() as group:
                    for index in range(concurrency):
                        for direction in ('upload', 'download'):
                            group.create_task(transfer_slot(direction, index))
                await asyncio.sleep(max(0, args.seconds - (time.perf_counter() - started)))
                done.set()

            async with asyncio.timeout(90):
                async with asyncio.TaskGroup() as group:
                    group.create_task(sample())
                    group.create_task(chat())
                    group.create_task(transfers())
            elapsed = time.perf_counter() - started
            final = {name: process.sample() for name, process in resources.items()}
            measured = (await server_command(server, 'metrics', 'metrics'))['values']
            upload_count = sum(file['direction'] == 'upload' for file in files)
            assert measured.get('uploadBytes', 0) == upload_count * args.file_bytes
            for file in files:
                file['startSeconds'] = file.pop('startedMonotonic') - started
                file['finishSeconds'] = file.pop('finishedMonotonic') - started
            transfer_window = max((file['finishSeconds'] for file in files), default=0)
            overlapping = [message['deliveryMs'] for message in messages if message['startSeconds'] < transfer_window]
            # A transfer slot includes command acceptance, protocol work and completion
            # observation; it does not mean wire bytes flow for the entire interval.
            for point in samples:
                point['activeTransfers'] = {direction: sum(file['direction'] == direction
                    and file['startSeconds'] <= point['seconds'] < file['finishSeconds'] for file in files)
                    for direction in ('upload', 'download')}
            active_messages = [message['deliveryMs'] for message in messages if any(
                file['startSeconds'] <= message['startSeconds'] < file['finishSeconds'] for file in files)]
            await settled(root, db_path, clients)
            transfers_rows = rows(db_path, 'SELECT * FROM file_transfers')
            assert len(transfers_rows) == len(files) + (1 if concurrency else 0)
            assert all(row['status'] == 'completed' for row in transfers_rows)
            for row in transfers_rows:
                if row['direction'] == 1:
                    stored = root / 'server/storage/files' / row['storage_path']
                    assert stored.stat().st_size == args.file_bytes
                    assert hashlib.sha256(stored.read_bytes()).hexdigest() == upload_digests[row['file_name']]
            stored_messages = rows(db_path, 'SELECT client_msg_id FROM messages')
            assert len(stored_messages) == len(messages) + upload_count + (1 if concurrency else 0)
            assert {item['intent'] for item in messages} <= {row['client_msg_id'] for row in stored_messages}
            assert rows(db_path, 'PRAGMA integrity_check') == [{'integrity_check': 'ok'}]
            assert rows(db_path, 'PRAGMA foreign_key_check') == []
            report.update(ok=True, seconds=elapsed, chatDeliveryMs=distribution([m['deliveryMs'] for m in messages]),
                chatAcceptedMs=distribution([m['acceptedMs'] for m in messages]),
                transferBytes=sum(file['bytes'] for file in files),
                aggregateBytesPerSecond=sum(file['bytes'] for file in files) / elapsed,
                fileWindowSeconds=transfer_window,
                fileWindowBytesPerSecond=sum(file['bytes'] for file in files) / transfer_window if transfer_window else 0,
                chatDuringFileWindowMs=distribution(overlapping),
                chatWhileTransferActiveMs=distribution(active_messages),
                completedTransfers={direction: sum(file['direction'] == direction for file in files)
                    for direction in ('upload', 'download')},
                serverMeasurements=measured, resources={name: dict(
                    cpuSeconds=final[name]['cpuSeconds'] - initial[name]['cpuSeconds'],
                    cpuPercentOneCore=100 * (final[name]['cpuSeconds'] - initial[name]['cpuSeconds']) / elapsed,
                    sampledMaxWorkingSetBytes=max([initial[name]['workingSetBytes'], final[name]['workingSetBytes']]
                        + [sample['processes'][name]['workingSetBytes'] for sample in samples])) for name in resources},
                messages=messages, files=files, resourceSamples=samples,
                verified=dict(transfers=len(transfers_rows), messages=len(stored_messages), seedSha256=digest, uploadSha256=upload_digests,
                    syncConfirmed=True, databaseIntegrity=True))
        except BaseException as error:
            report['error'] = repr(error)
            # Preserve failure evidence before the isolated runtime is removed.
            try:
                report['partialFiles'] = files
                report['partialMessages'] = messages
                report['failureFiles'] = [dict(path=str(path.relative_to(root)), bytes=path.stat().st_size)
                    for path in root.rglob('*') if path.is_file()]
                for path in root.rglob('*.sqlite'):
                    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(
                            output / ('failure-' + '-'.join(path.relative_to(root).parts)))) as target:
                        source.backup(target)
                if db_path.exists():
                    with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(
                            output / 'failure-server.sqlite')) as target:
                        source.backup(target)
                if server:
                    report['failureServer'] = await server_command(server, 'diagnostics', 'diagnostics')
            except Exception as capture_error:
                report['evidenceError'] = repr(capture_error)
            raise
        finally:
            try:
                for name, client in clients.items():
                    (output / f'{name}-events.json').write_text(json.dumps(client.events), encoding='utf-8')
                for process in resources.values():
                    process.close()
                results = await asyncio.gather(*(client.close() for client in clients.values()), return_exceptions=True)
                if server:
                    await server.close()
                    report['serviceExitCode'] = server.service_exit_code
                    report['launcherExitCode'] = server.process.returncode
                report['clientExitCodes'] = {name: client.process.returncode for name, client in clients.items()}
                errors = [repr(result) for result in results if isinstance(result, BaseException)]
                if errors:
                    raise RuntimeError(str(errors))
            except BaseException as error:
                report.update(ok=False, cleanupError=repr(error))
                raise
            finally:
                report['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
                (output / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


async def main(args):
    output = args.output / time.strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True)
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
        processor_name = winreg.QueryValueEx(key, 'ProcessorNameString')[0]
    metadata = dict(platform=platform.platform(), processor=processor_name, cpuCount=os.cpu_count(),
        aioquic=version('aioquic'), protobuf=version('protobuf'),
        clientSha256=hashlib.sha256(args.client.read_bytes()).hexdigest(),
        toolSha256={name: hashlib.sha256((ROOT / 'tools' / name).read_bytes()).hexdigest()
            for name in ('measure_load.py', 'load_server_fixture.py', 'process_metrics.py')},
        python=sys.version, client=str(args.client.resolve()), fileBytes=args.file_bytes, repeats=args.repeats,
        seconds=args.seconds, interval=args.interval, concurrency=args.concurrency, sustained=args.sustained,
        gitCommit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        gitStatus=subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True))
    (output / 'environment.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    summaries = []
    for repeat in range(args.repeats):
        for concurrency in args.concurrency:
            name = f'run-{repeat + 1}-concurrency-{concurrency}'
            result = await run_case(args, output / name, concurrency)
            summary = {key: value for key, value in result.items() if key not in ('messages', 'resourceSamples', 'files')}
            summaries.append(dict(case=name, **summary))
            (output / 'summary.json').write_text(json.dumps(summaries, indent=2), encoding='utf-8')
            print(json.dumps(dict(case=name, seconds=result['seconds'], chat=result['chatDeliveryMs'], output=str(output))), flush=True)
    print('LOAD MEASUREMENT PASSED ' + str(output), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', type=Path, default=ROOT / 'build/client-manifest/Release/mini_im_native_driver.exe')
    parser.add_argument('--output', type=Path, default=ROOT / 'tmp/load-measurement')
    parser.add_argument('--sustained', action='store_true',
        help='refill each upload/download slot until --seconds, then drain all accepted tasks')
    parser.add_argument('--file-bytes', type=int, default=1048576)
    parser.add_argument('--seconds', type=float, default=5)
    parser.add_argument('--interval', type=float, default=0.1)
    parser.add_argument('--concurrency', type=int, nargs='+', default=[0, 1, 4])
    parser.add_argument('--repeats', type=int, default=2)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('resource sampling currently requires Windows')
    if not (1 <= args.file_bytes <= 2097152 and 1 <= args.seconds <= 30 and 0.02 <= args.interval <= 5
            and 1 <= args.repeats <= 10 and all(0 <= value <= 8 for value in args.concurrency)):
        parser.error('use file-bytes 1..2097152, seconds 1..30, interval .02..5, repeats 1..10, concurrency 0..8')
    asyncio.run(main(args))
