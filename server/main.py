import asyncio

from quic.server import run_server


def main() -> None:
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
