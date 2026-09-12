"""Measure production cache open, snapshot and JSON encoding with synthetic local projections.

Each sample uses a fresh Qt process. No network or page rendering is measured.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def invoke(driver, cache, *options):
    started = time.perf_counter()
    result = subprocess.run([str(driver), str(cache), *options], capture_output=True, text=True,
        encoding='utf-8', errors='strict', timeout=120)
    if result.returncode:
        raise RuntimeError(f'cache driver exited {result.returncode}: {result.stderr}')
    data = json.loads(result.stdout)
    data['processWallMs'] = (time.perf_counter() - started) * 1000
    return data


def seed(path, count, conversations):
    def objects():
        for c in range(conversations):
            conversation = f'conversation-{c}'
            yield 'conversation', conversation, conversation, 0, dict(id=conversation,
                conversationId=conversation, title=conversation, members=['alice', 'bob'])
            yield 'receipt', json.dumps([conversation, 'alice'], separators=(',', ':')), conversation, 0, dict(type='receipt',
                conversationId=conversation, readerId='alice', lastReadSeq=0)
        for index in range(count):
            conversation = f'conversation-{index % conversations}'
            message = f'message-{index}'
            position = index * 3 + 1
            yield 'message', message, conversation, position, dict(id=message, conversationId=conversation,
                clientMsgId=message, senderId='bob', seq=index // conversations + 1, text='x' * 128,
                createdAtMs=index, recalled=False, burned=False, burnMode=0, burnTtlSec=0)
            yield 'readCount', message, conversation, position + 1, dict(type='readCount', messageId=message,
                conversationId=conversation, globalSeq=position + 1, unreadCount=1)
            yield 'delivery', json.dumps([message, 'alice'], separators=(',', ':')), conversation, position + 2, dict(type='delivery', messageId=message,
                conversationId=conversation, userId='alice', status='delivered', deliveredAtMs=index)
    with closing(sqlite3.connect(path)) as db:
        db.executemany('INSERT INTO objects(kind,id,conversation,position,data) VALUES(?,?,?,?,?)',
            ((*row[:4], json.dumps(row[4], separators=(',', ':')).encode()) for row in objects()))
        db.commit()
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def main(args):
    output = args.output / time.strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True)
    metadata = dict(platform=platform.platform(), processor=platform.processor(), python=platform.python_version(),
        sqlite=sqlite3.sqlite_version, toolSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        driverSourceSha256=hashlib.sha256((ROOT / 'client/tests/cache_benchmark.cpp').read_bytes()).hexdigest(),
        driver=str(args.driver.resolve()),
        driverSha256=hashlib.sha256(args.driver.read_bytes()).hexdigest(), counts=args.messages,
        conversations=args.conversations, repeats=args.repeats, textBytes=128,
        gitCommit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        gitStatus=subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True))
    (output / 'environment.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    results = []
    try:
        for count in args.messages:
            with tempfile.TemporaryDirectory(prefix='cache-', dir=output) as temporary:
                cache = Path(temporary)
                initialized = invoke(args.driver, cache)
                database = Path(initialized['database'])
                assert database.parent == cache.resolve()
                seed(database, count, args.conversations)
                for repeat in range(args.repeats):
                    data = invoke(args.driver, cache)
                    expected_page = sum(min(50, (count + args.conversations - c - 1) // args.conversations)
                        for c in range(args.conversations))
                    assert data['messages'] == data['deliveries'] == data['readCounts'] == expected_page
                    assert data['unreadTotal'] == count
                    assert data['conversations'] == args.conversations
                    results.append(dict(messageCount=count, repeat=repeat + 1,
                        databaseBytes=database.stat().st_size, **data))
                    (output / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
                    print(json.dumps(results[-1]), flush=True)
                verification = invoke(args.driver, cache, '--verify-history')
                assert verification['historyMessages'] == verification['historyDeliveries'] == verification['historyReadCounts'] == count
                # Measure upgrading an existing populated cache separately from steady-state samples.
                with closing(sqlite3.connect(database)) as db:
                    for index in ('objects_message_order', 'objects_message_unread', 'objects_delivery_message'):
                        db.execute('DROP INDEX ' + index)
                    db.commit()
                upgrade = invoke(args.driver, cache)
                assert upgrade['unreadTotal'] == count and upgrade['messages'] == expected_page
                (output / f'history-{count}.json').write_text(json.dumps(dict(verification=verification,
                    indexUpgrade=upgrade), indent=2), encoding='utf-8')
        print('CACHE MEASUREMENT PASSED ' + str(output), flush=True)
    except BaseException as error:
        (output / 'error.json').write_text(json.dumps(dict(error=repr(error))), encoding='utf-8')
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', type=Path, default=ROOT / 'build/client-manifest/Release/mini_im_cache_benchmark.exe')
    parser.add_argument('--output', type=Path, default=ROOT / 'tmp/cache-measurement')
    parser.add_argument('--messages', type=int, nargs='+', default=[100, 10000, 100000])
    parser.add_argument('--conversations', type=int, default=10)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if not args.driver.is_file() or not (1 <= args.conversations <= 100 and 1 <= args.repeats <= 10
            and all(0 <= count <= 100000 for count in args.messages)):
        parser.error('require built driver, conversations 1..100, repeats 1..10, messages 0..100000')
    main(args)
