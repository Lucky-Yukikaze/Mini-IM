from pathlib import Path
import re
from subprocess import check_call
import sys


def patch_imports(out_dir: Path) -> None:
    pattern = re.compile(r"^import ([a-zA-Z0-9_]+_pb2) as ([a-zA-Z0-9_]+)$", re.MULTILINE)
    for pb_file in out_dir.glob("*_pb2.py"):
        content = pb_file.read_text(encoding="utf-8")
        patched = pattern.sub(r"from . import \1 as \2", content)
        if patched != content:
            pb_file.write_text(patched, encoding="utf-8")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    proto_dir = repo_root / "proto"
    out_dir = repo_root / "server" / "protocol" / "pb"
    out_dir.mkdir(parents=True, exist_ok=True)

    proto_files = sorted(proto_dir.glob("*.proto"))
    if not proto_files:
        raise RuntimeError("No proto files found")

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        "-I",
        str(proto_dir),
        "--python_out",
        str(out_dir),
        *[str(item) for item in proto_files],
    ]
    check_call(cmd)
    patch_imports(out_dir)


if __name__ == "__main__":
    main()
