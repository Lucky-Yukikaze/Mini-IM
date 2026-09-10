import argparse
import asyncio
from pathlib import Path

from quic.server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Mini-IM development server")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--port", type=int, default=4433)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    asyncio.run(run_server(data_root=args.data_root, port=args.port))


if __name__ == "__main__":
    main()
