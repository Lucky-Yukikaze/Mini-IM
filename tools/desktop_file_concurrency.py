"""Actual page operations with nine queued files and mixed transfer recovery."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def run_concurrent_files(check):
    directory = Path(check.context["transferSource"]).parent
    original = b"keep destination until verified"
    paths = []
    for index in range(12):
        path = directory / f"parallel-source-{index:02}.bin"
        path.write_bytes(bytes([index]) + bytes(range(251)) * 8192)
        paths.append(path)
    checkpoints = []
    completion_measurements = []

    def save(name, state):
        (check.output / (name + ".json")).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        checkpoints.append(name)
        return state

    def cache_name(user):
        identity = json.dumps([check.context["endpoint"].removeprefix("quic://"), user,
            check.context["device"]], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest() + ".sqlite"

    def tasks(state, user):
        return state["fileTasks"].get(cache_name(user), [])

    def wait_synced(user):
        def synced(state):
            with closing(sqlite3.connect(Path(check.context["root"]) / "server.db")) as server, closing(
                    sqlite3.connect(Path(check.context["state"]) / cache_name(user))) as cache:
                expected = server.execute("SELECT seq,event_id FROM sync_events WHERE user_id=? ORDER BY seq", (user,)).fetchall()
                metadata = dict(cache.execute("SELECT key,value FROM metadata"))
                if int(metadata.get("sync_confirmed_cursor", 0)) != len(expected) or metadata.get("sync_confirmation"):
                    return False
                assert int(metadata["cursor"]) == len(expected)
                assert cache.execute("SELECT position,event_id FROM seen ORDER BY position").fetchall() == expected
                assert all(row["status"] in ("completed", "cancelled") for row in tasks(state, user))
                return True
        return check.wait_state(synced, timeout=60)

    def identities(state, user):
        return {item["id"]: (item["init_request"], item["finish_request"]) for item in tasks(state, user)}

    def uploads(state, user):
        return [row for row in state["transfers"] if row["direction"] == 1 and row["owner_id"] == user]

    def upload(path):
        check.phase("file-upload", transferSource=str(path), fileName=path.name)

    def verify_uploads(rows):
        for row in rows:
            source = next(path for path in paths if path.name == row["file_name"])
            stored = Path(check.context["root"]) / "files" / row["storage_path"]
            if row["status"] == "completed":
                assert stored.read_bytes() == source.read_bytes(), row
            else:
                assert row["status"] == "cancelled", row
                assert stored.read_bytes() == source.read_bytes()[:row["received_bytes"]], row

    check.attach_client()
    check.phase("login", user="alice", title="Desktop QA")
    check.command("pause-upload", offset=65536)
    for path in paths[:9]:
        upload(path)
    state = check.wait_state(lambda state: len(uploads(state, "alice")) == 8
        and all(0 < row["received_bytes"] < row["file_size"] for row in uploads(state, "alice")))
    assert len(tasks(state, "alice")) == 9
    initial_alice = identities(state, "alice")
    assert sum(not row["file_id"] for row in tasks(state, "alice")) == 1
    check.phase("file-concurrent", count=9, names=[path.name for path in paths[:9]],
        image=str(check.artifacts / "nine-uploads.png"))
    save("concurrent-upload-queued", state)
    cancelled = next(row for row in uploads(state, "alice") if row["file_name"] == paths[0].name)
    check.phase("file-cancel-active", fileName=paths[0].name)
    state = check.wait_state(lambda state: len(uploads(state, "alice")) == 9
        and any(row["status"] == "cancelled" and row["file_id"] == cancelled["file_id"] for row in state["transfers"])
        and all(row["received_bytes"] > 0 for row in uploads(state, "alice")))
    assert len([row for row in uploads(state, "alice") if row["status"] != "cancelled"]) == 8
    check.phase("send-during-files", text="message while eight uploads wait")
    state = check.wait_state(lambda state: len(state["messages"]) == 1)
    before = save("concurrent-upload-before-restart", state)
    check.restart_client("alice")
    check.phase("file-concurrent", count=8, names=[path.name for path in paths[1:9]],
        image=str(check.artifacts / "uploads-restored.png"))
    state = check.wait_state(lambda state: all(len([item for item in state["fileAttempts"]
        if item["intent"] == row["client_file_id"]]) >= 2 for row in uploads(before, "alice") if row["status"] != "cancelled"))
    assert identities(state, "alice") == initial_alice
    for row in uploads(before, "alice"):
        attempts = [item for item in state["fileAttempts"] if item["intent"] == row["client_file_id"]]
        if row["status"] == "cancelled":
            assert len(attempts) == 1
        else:
            assert attempts[-1]["acceptedOffset"] >= row["received_bytes"] > 0
            assert len({item["requestId"] for item in attempts}) == 1
    save("concurrent-upload-restored", state)
    started = time.monotonic()
    event_start = state["events"]
    check.command("pause-upload", offset=0)
    state = check.wait_state(lambda state: len([row for row in uploads(state, "alice") if row["status"] == "completed"]) == 8, timeout=60)
    check.phase("file-complete")
    completion_measurements.append(dict(batch="eight-uploads", seconds=round(time.monotonic() - started, 3),
        newSyncEvents=state["events"] - event_start, fileSize=paths[0].stat().st_size, tasks=8))
    verify_uploads(uploads(state, "alice"))
    assert len(state["messages"]) == 9

    completed = sorted([row for row in uploads(state, "alice") if row["status"] == "completed"], key=lambda row: row["file_name"])
    check.phase("login", user="bob", title="Desktop QA", switch=True)
    check.command("pause-upload", offset=65536)
    check.command("pause-download", offset=65536)
    destinations = []
    for index, source in enumerate(completed[:6]):
        target = directory / f"parallel-download-{index:02}.bin"
        target.write_bytes(original)
        destinations.append((target, source))
        check.phase("file-fill", fileId=source["file_id"], fileName=source["file_name"], transferTarget=str(target))
        check.phase("file-download", fileId=source["file_id"], transferTarget=str(target), targetName=target.name, pending=True)
    for path in paths[9:]:
        upload(path)
    state = check.wait_state(lambda state: len(uploads(state, "bob")) == 2
        and all(row["received_bytes"] > 0 for row in uploads(state, "bob"))
        and all(any(name.startswith(target.name + ".miniim-") and item["size"] >= 65536
            for name, item in state["artifacts"].items()) for target, _ in destinations))
    assert len(tasks(state, "bob")) == 9
    assert sum(not row["file_id"] for row in tasks(state, "bob")) == 1
    initial_bob = identities(state, "bob")
    names = [target.name for target, _ in destinations] + [path.name for path in paths[9:]]
    check.phase("file-concurrent", count=9, names=names, image=str(check.artifacts / "mixed-nine-files.png"))
    save("concurrent-mixed-queued", state)
    assert all(target.read_bytes() == original for target, _ in destinations)
    cancelling_target, cancelling_source = destinations[0]
    check.phase("file-cancel-active", fileName=cancelling_target.name)
    state = check.wait_state(lambda state: len(uploads(state, "bob")) == 3
        and all(row["received_bytes"] > 0 for row in uploads(state, "bob")))
    downloads = [row for row in state["transfers"] if row["direction"] == 2]
    assert len(downloads) == 6
    cancelled_download = next(row for row in downloads if row["source_file_id"] == cancelling_source["file_id"])
    assert cancelled_download["status"] == "cancelled"
    check.phase("send-during-files", text="message while uploads and downloads wait")
    state = check.wait_state(lambda state: len(state["messages"]) == 10)
    before = save("concurrent-mixed-before-restart", state)
    offsets = {target.name: next(item["size"] for name, item in state["artifacts"].items()
        if name.startswith(target.name + ".miniim-")) for target, _ in destinations[1:]}
    check.restart_client("bob")
    check.phase("file-concurrent", count=8, names=names[1:], image=str(check.artifacts / "mixed-files-restored.png"))
    pending = [row for row in tasks(before, "bob") if row["status"] not in ("completed", "cancelled")]
    state = check.wait_state(lambda state: all(len([item for item in state["fileAttempts"] if item["intent"] == row["id"]]) >= 2 for row in pending))
    assert identities(state, "bob") == initial_bob
    for row in pending:
        attempts = [item for item in state["fileAttempts"] if item["intent"] == row["id"]]
        assert len({item["requestId"] for item in attempts}) == 1
        if attempts[-1]["direction"] == 2:
            target = next(target for target, source in destinations if source["file_id"] == attempts[-1]["source"])
            assert attempts[-1]["offset"] == offsets[target.name] > 0
        else:
            transfer = next(item for item in before["transfers"] if item["client_file_id"] == row["id"])
            assert attempts[-1]["acceptedOffset"] >= transfer["received_bytes"] > 0
    assert len([item for item in state["fileAttempts"] if item["intent"] == cancelled_download["client_file_id"]]) == 1
    save("concurrent-mixed-restored", state)
    started = time.monotonic()
    event_start = state["events"]
    check.command("pause-upload", offset=0)
    check.command("pause-download", offset=0)
    state = check.wait_state(lambda state: len(state["transfers"]) == 18
        and all(row["status"] in ("completed", "cancelled") for row in state["transfers"]), timeout=60)
    check.phase("file-complete")
    completion_measurements.append(dict(batch="five-downloads-three-uploads", seconds=round(time.monotonic() - started, 3),
        newSyncEvents=state["events"] - event_start, fileSize=paths[0].stat().st_size, tasks=8))
    verify_uploads(uploads(state, "alice") + uploads(state, "bob"))
    for target, source in destinations:
        if target == cancelling_target:
            assert target.read_bytes() == original
        else:
            content = next(path for path in paths if path.name == source["file_name"]).read_bytes()
            assert target.read_bytes() == content
            assert not list(directory.glob(target.name + ".miniim-*.part"))
    assert len(state["messages"]) == 13
    assert len([row for row in state["transfers"] if row["status"] == "cancelled"]) == 2
    assert len(state["clientExits"]) == 2 and all(row["exitCode"] != 0 for row in state["clientExits"])
    assert identities(state, "alice") == initial_alice and identities(state, "bob") == initial_bob
    assert len({row["file_id"] for row in state["transfers"]}) == 18
    with closing(sqlite3.connect(Path(check.context["root"]) / "server.db")) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    state = wait_synced("bob")
    check.phase("login", user="alice", title="Desktop QA", switch=True)
    check.phase("file-complete")
    state = wait_synced("alice")
    assert all(row["status"] in ("completed", "cancelled") for user in ("alice", "bob") for row in tasks(state, user))
    state["concurrencyCheckpoints"] = checkpoints
    state["completionMeasurements"] = completion_measurements
    return save("concurrent-final", state)
