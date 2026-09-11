"""Inspect or maintain an offline Mini-IM file store; mutations require --apply."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from services.file.maintenance import FileMaintenance
from storage.access import storage_access


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "server")
    parser.add_argument("--file-root", type=Path, help="defaults to MINIIM_FILE_ROOT or DATA_ROOT/storage/files")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="check completed upload size and SHA-256")
    inspect.add_argument("--file-id")
    restore = commands.add_parser("restore", help="replace only with verified original content")
    restore.add_argument("--file-id", required=True)
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--apply", action="store_true")
    clean = commands.add_parser("clean-cancelled", help="remove only unreferenced cancelled upload bytes")
    clean.add_argument("--older-than-days", type=int, required=True)
    clean.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = args.data_root.resolve()
    files = (args.file_root or Path(os.getenv("MINIIM_FILE_ROOT", str(data / "storage/files")))).resolve()
    try:
        if not (data / "storage/sqlite/miniim.db").is_file():
            raise ValueError("existing database required; maintenance does not initialize or migrate data")
        with storage_access(data, files):
            maintenance = FileMaintenance(data, files)
            if args.command == "inspect":
                result = maintenance.inspect(args.file_id)
            elif args.command == "restore":
                result = maintenance.restore(args.file_id, args.source, apply=args.apply)
            else:
                result = maintenance.clean_cancelled(args.older_than_days, apply=args.apply)
        failed = isinstance(result, list) and any(item.get("health") in ("missing", "damaged", "error")
            or item.get("action") == "error" for item in result)
        print(json.dumps(dict(ok=not failed, command=args.command, result=result), ensure_ascii=False))
        return 2 if failed else 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(json.dumps(dict(ok=False, error=str(error)), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
