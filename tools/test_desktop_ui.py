"""Run CLI-driven UI checks against a fresh tools/desktop_fixture.py context.

Requires Node and an installed Playwright CLI entry file. Uses the built Qt
WebEngine page and actual QUIC services; only the create completion callback is
held briefly to check form waiting. Service/cache faults stay in fixture data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from protocol.pb import message_pb2


class DesktopCheck:
    def __init__(self, context, cli):
        self.context = json.loads(context.read_text(encoding="utf-8-sig"))
        self.output = Path(self.context["output"])
        self.script = (ROOT / "tools/desktop_ui.js").read_text(encoding="utf-8")
        self.cli = ["node", str(cli.resolve()), "-s=desktop-" + uuid.uuid4().hex[:10]]
        self.results = []
        self.artifacts = ROOT / "output/playwright" / self.output.name
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def call(self, *args):
        result = subprocess.run([*self.cli, *args], cwd=ROOT, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=45)
        with (self.output / "ui-cli.log").open("a", encoding="utf-8") as log:
            log.write(result.stdout + result.stderr + "\n")
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout

    def command(self, operation, **fields):
        data = dict(id=uuid.uuid4().hex, op=operation, **fields)
        temporary = self.output / "command.tmp"
        temporary.write_text(json.dumps(data), encoding="utf-8")
        deadline = time.monotonic() + 2
        while True:
            try:
                temporary.replace(self.output / "command.json")
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                response = json.loads((self.output / "response.json").read_text(encoding="utf-8"))
                if response["id"] == data["id"]:
                    if not response["ok"]:
                        raise RuntimeError(response["error"])
                    return response["state"]
            except (FileNotFoundError, PermissionError):
                pass
            time.sleep(0.1)
        raise TimeoutError("fixture command timeout: " + operation)

    def phase(self, name, **fields):
        self.call("snapshot")
        data = dict(self.context, phase=name, **fields)
        output = json.loads(self.call("run-code", self.script.replace("__CASE__", json.dumps(data)), "--json"))
        result = json.loads(output.get("result", "{}"))
        if result.get("ok") is not True:
            raise AssertionError(output)
        self.results.append(dict(phase=name, **fields))
        print("PASS " + name, flush=True)

    def wait_state(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.command("snapshot")
            if predicate(state):
                return state
            time.sleep(0.1)
        raise AssertionError("server state did not converge")

    def read_seq(self, state):
        return next(row["last_read_seq"] for row in state["members"]
                    if row["conversation_id"] == self.context["group"] and row["user_id"] == "alice")

    def run(self):
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(self.context["debug"] + "/json", timeout=1) as response:
                    if json.load(response):
                        break
            except (URLError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Qt WebEngine debugging endpoint did not start")
            time.sleep(0.1)
        self.call("attach", "--cdp", self.context["debug"])
        self.phase("login", user="alice", title="Desktop QA")
        self.phase("callback")
        for mode, fields, submit, title in (
            ("建群", [["群名称", "Saved group"], ["成员 ID", "bob"]], "创建", "Saved group"),
            ("私聊", [["对方用户名", "bob"]], "开始私聊", "bob"),
            ("加群", [["会话 ID", self.context["group"]]], "进入", "Desktop QA"),
        ):
            self.command("cache-fault", enabled=True)
            self.phase("form-failure", mode=mode, fields=fields, submit=submit,
                       image=str(self.artifacts / (submit + "-save-failed.png")))
            self.command("cache-fault", enabled=False)
            self.phase("form-retry", submit=submit, title=title)
        self.command("cache-fault", enabled=True)
        self.phase("member-failure", title="Desktop QA")
        self.command("cache-fault", enabled=False)
        self.phase("member-retry")
        self.command("reject", code=503)
        self.phase("rename")
        self.command("reject", code=0)
        self.phase("renamed")
        state = self.command("snapshot")
        attempts = [item for item in state["attempts"] if item["operation"] == "rename_conversation"]
        assert len(attempts) >= 2 and len({(a["requestId"], a["body"]) for a in attempts}) == 1
        self.phase("rejected", image=str(self.artifacts / "rejected.png"))
        self.phase("recall")
        self.command("cache-fault", enabled=True)
        self.command("message", text="Desktop read save failure")
        self.phase("incoming", text="Desktop read save failure", failed=True)
        assert self.read_seq(self.command("snapshot")) < 2
        self.command("cache-fault", enabled=False)
        self.phase("read")
        self.wait_state(lambda state: self.read_seq(state) == 2)
        self.phase("settled")
        self.command("drop-ack", count=1)
        self.command("message", text="Desktop delayed receipt")
        self.phase("incoming", text="Desktop delayed receipt", pending=True)
        self.command("message", text="Desktop later receipt")
        self.phase("incoming", text="Desktop later receipt")
        state = self.wait_state(lambda state: self.read_seq(state) == 4)
        self.phase("settled")
        receipts = {}
        delayed_attempts = 0
        for item in state["attempts"]:
            if item["operation"] == "receipt" and item["user"] == "alice":
                receipt = message_pb2.Receipt.FromString(bytes.fromhex(item["body"]))
                receipts.setdefault(receipt.last_read_seq, set()).add((item["requestId"], item["body"]))
                if receipt.last_read_seq == 3:
                    delayed_attempts += 1
        assert set(receipts) == {2, 3, 4} and all(len(value) == 1 for value in receipts.values()), receipts
        assert delayed_attempts >= 2, "lost receipt confirmation was not retried"
        self.phase("login", user="bob", title="Desktop renamed", switch=True, failure=False)
        self.phase("leave")
        self.phase("join", title="Desktop renamed")
        self.phase("login", user="alice", title="Desktop renamed", switch=True, failure=True)
        self.command("file-init-reject", code=503)
        self.command("file-cancel-reject", code=503)
        self.phase("upload")
        self.phase("cancel-file", image=str(self.artifacts / "cancel-pending.png"))
        self.command("file-cancel-reject", code=0)
        self.phase("file-cancelled")
        self.command("file-cancel-reject", code=403)
        self.phase("upload")
        self.phase("cancel-file", failed=True, image=str(self.artifacts / "cancel-failed.png"))
        self.command("file-cancel-reject", code=0)
        self.phase("cancel-file-retry")
        self.phase("file-cancelled")
        state = self.command("snapshot")
        assert len(state["cancellations"]) == 2
        attempts = state["fileCancelAttempts"]
        rejected_intent = attempts[-1]["intent"]
        assert len({item["requestId"] for item in attempts if item["intent"] == rejected_intent}) == 2
        for cancellation in state["cancellations"]:
            assert cancellation["owner_id"] == "alice"
        self.phase("delivery-send")
        self.phase("late-member")
        self.command("read-cindy")
        self.phase("late-member-count", image=str(self.artifacts / "late-member-count.png"))
        self.command("confirm-bob")
        self.phase("delivery-status", image=str(self.artifacts / "delivery-confirmed.png"))
        self.command("restart-client")
        self.call("detach")
        deadline = time.monotonic() + 12
        while True:
            try:
                with urlopen(self.context["debug"] + "/json", timeout=1) as response:
                    if json.load(response):
                        break
            except (URLError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("restarted Qt page did not start")
            time.sleep(0.1)
        self.call("attach", "--cdp", self.context["debug"])
        self.phase("login", user="alice", title="Desktop renamed", failure=True)
        self.phase("delivery-status", image=str(self.artifacts / "delivery-restored.png"))
        self.command("read-bob")
        self.phase("delivery-status", read=True)
        return self.command("snapshot")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--playwright-cli", type=Path, required=True, help="installed Playwright CLI JavaScript entry")
    args = parser.parse_args()
    check = DesktopCheck(args.context, args.playwright_cli)
    result = dict(ok=False, started=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        result["state"] = check.run()
        result["ok"] = True
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["phases"] = check.results
        result["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (check.output / "ui-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
