"""Offline file inspection, verified restoration, and cancelled-upload cleanup."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from protocol.pb import common_pb2


class FileMaintenance:
    """Caller must hold storage_access for the complete lifetime of this object."""

    def __init__(self, data_root: Path, file_root: Path):
        self.file_root = file_root.resolve()
        database = data_root.resolve() / "storage/sqlite/miniim.db"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("database integrity check failed")
            self.rows = [dict(row) for row in connection.execute("SELECT * FROM file_transfers")]
            self.cancellations = [dict(row) for row in connection.execute("SELECT * FROM file_cancellations")]
            self.attachments = [row[0] for row in connection.execute("SELECT path FROM attachments WHERE path IS NOT NULL")]
            self.published = set()
            for row in connection.execute("SELECT client_msg_id,content FROM messages WHERE type=?", (common_pb2.MSG_FILE,)):
                if str(row[0]).startswith("file-msg-"):
                    self.published.add(str(row[0])[len("file-msg-"):])
                try:
                    body = json.loads(row[1])
                    if isinstance(body, dict) and isinstance(body.get("fileId"), str):
                        self.published.add(body["fileId"])
                except (ValueError, TypeError, UnicodeError):
                    pass

    def target(self, relative: str) -> Path:
        # Reject drive paths, traversal, links and maintenance-reserved names.
        parts = relative.replace("\\", "/").split("/")
        if not parts or any(part in ("", ".", "..") or ":" in part or part.startswith(".miniim") for part in parts):
            raise ValueError("unsafe storage path")
        target = self.file_root
        for part in parts:
            target = target / part
            if target.is_symlink() or target.is_junction():
                raise ValueError("linked storage path")
        if not target.resolve().is_relative_to(self.file_root):
            raise ValueError("storage path escapes file root")
        if target.exists() and not target.is_file():
            raise ValueError("storage target is not a regular file")
        return target

    @staticmethod
    def digest(path: Path) -> tuple[int, str]:
        size, digest = 0, hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                size += len(block)
                digest.update(block)
        return size, digest.hexdigest()

    def inspect(self, file_id: str | None = None) -> list[dict]:
        rows = [row for row in self.rows if row["direction"] == 1 and (file_id is None or row["file_id"] == file_id)]
        if file_id is not None and not rows:
            raise ValueError("upload file id not found")
        result = []
        for row in rows:
            item = dict(fileId=row["file_id"], status=row["status"], path=row["storage_path"])
            try:
                path = self.target(row["storage_path"])
                item["exists"] = path.exists()
                if not path.exists():
                    item["health"] = "missing" if row["status"] == "completed" else "absent"
                elif row["status"] == "completed":
                    size, digest = self.digest(path)
                    item.update(actualBytes=size, actualSha256=digest,
                        health="ok" if (size, digest) == (row["file_size"], row["sha256"].lower()) else "damaged")
                else:
                    item.update(actualBytes=path.stat().st_size, health="partial")
            except (OSError, ValueError) as error:
                item.update(health="error", error=str(error))
            result.append(item)
        return result

    def restore(self, file_id: str, source: Path, *, apply: bool = False) -> dict:
        row = next((row for row in self.rows if row["file_id"] == file_id and row["direction"] == 1), None)
        if row is None or row["status"] != "completed":
            raise ValueError("restoration requires a completed upload")
        target = self.target(row["storage_path"])
        if any(other["file_id"] != file_id and self.target(other["storage_path"]) == target for other in self.rows):
            raise ValueError("storage path is shared by another transfer")
        expected = (row["file_size"], row["sha256"].lower())
        if not source.is_file() or self.digest(source) != expected:
            raise ValueError("replacement size or SHA-256 does not match the published file")
        result = dict(fileId=file_id, path=row["storage_path"], action="restore", applied=False)
        if target.exists() and self.digest(target) == expected:
            return dict(result, action="already-valid")
        if not apply:
            return result
        target.parent.mkdir(parents=True, exist_ok=True)
        self.target(row["storage_path"])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".miniim-restore-", dir=target.parent, delete=False) as staged:
                temporary = Path(staged.name)
                with source.open("rb") as replacement:
                    shutil.copyfileobj(replacement, staged, length=1024 * 1024)
                staged.flush()
                os.fsync(staged.fileno())
            # Verify the staged bytes too: the replacement may change during copying.
            if self.digest(temporary) != expected:
                raise ValueError("replacement changed while copying")
            self.target(row["storage_path"])
            os.replace(temporary, target)
            result["applied"] = True
            return result
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def clean_cancelled(self, older_than_days: int, *, apply: bool = False) -> list[dict]:
        if older_than_days < 0:
            raise ValueError("retention days cannot be negative")
        cutoff = int(time.time() * 1000) - older_than_days * 86400000
        cancellations = {(row["owner_id"], row["client_file_id"], row["file_id"]): row["created_at_ms"]
                         for row in self.cancellations}
        # Preflight all paths before deleting anything; malformed legacy paths need review.
        paths = {row["file_id"]: self.target(row["storage_path"]) for row in self.rows}
        attachments = {(self.file_root / str(path).replace("\\", "/")).resolve() for path in self.attachments}
        result = []
        for row in self.rows:
            if row["direction"] != 1 or row["status"] != "cancelled":
                continue
            path = paths[row["file_id"]]
            cancelled = cancellations.get((row["owner_id"], row["client_file_id"], row["file_id"]))
            reason = None
            if cancelled is None:
                reason = "missing cancellation record"
            elif max(cancelled, row["updated_at_ms"]) > cutoff:
                reason = "within retention period"
            elif row["file_id"] in self.published or path.resolve() in attachments:
                reason = "published reference"
            elif any(other["file_id"] != row["file_id"] and (other["source_file_id"] == row["file_id"]
                    or paths[other["file_id"]] == path) for other in self.rows):
                reason = "transfer reference"
            item = dict(fileId=row["file_id"], path=row["storage_path"], applied=False)
            if reason:
                item.update(action="keep", reason=reason)
            elif not path.exists():
                item["action"] = "already-absent"
            else:
                item.update(action="remove-cancelled", bytes=path.stat().st_size)
                if apply:
                    try:
                        self.target(row["storage_path"]).unlink()
                        item["applied"] = True
                    except OSError as error:
                        item.update(action="error", error=str(error))
            result.append(item)
        return result
