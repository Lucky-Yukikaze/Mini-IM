"""Create the shared QUIC listener with platform-specific UDP configuration."""
from __future__ import annotations

import asyncio
import socket
import sys

from aioquic.asyncio import serve as aioquic_serve
from aioquic.asyncio.server import QuicServer


def _disable_udp_connreset(listener: socket.socket) -> None:
    # CPython socket.ioctl exposes only three controls; use the typed Winsock API here.
    import ctypes
    from ctypes import wintypes

    winsock = ctypes.WinDLL("ws2_32")
    ioctl = winsock.WSAIoctl
    ioctl.argtypes = [
        ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    ioctl.restype = ctypes.c_int
    winsock.WSAGetLastError.argtypes = []
    winsock.WSAGetLastError.restype = ctypes.c_int
    enabled = wintypes.BOOL(False)
    returned = wintypes.DWORD()
    sio_udp_connreset = 0x9800000C  # _WSAIOW(IOC_VENDOR, 12), mstcpip.h
    result = ioctl(
        listener.fileno(), sio_udp_connreset, ctypes.byref(enabled), ctypes.sizeof(enabled),
        None, 0, ctypes.byref(returned), None, None,
    )
    if result != 0:
        raise ctypes.WinError(winsock.WSAGetLastError())


async def serve_quic(host: str, port: int, **options) -> QuicServer:
    if sys.platform != "win32":
        return await aioquic_serve(host, port, **options)

    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    family, kind, protocol, _, address = addresses[0]
    listener = socket.socket(family, kind, protocol)
    try:
        # A dead peer's ICMP port-unreachable must not stop the shared UDP reader.
        # QUIC handles individual peer failure using its own acknowledgements and timers.
        # https://learn.microsoft.com/en-us/windows/win32/winsock/winsock-ioctls#sio_udp_connreset-opcode-setting-i-t3
        _disable_udp_connreset(listener)
        listener.setblocking(False)
        listener.bind(address)
        _, server = await loop.create_datagram_endpoint(
            lambda: QuicServer(**options), sock=listener,
        )
        return server
    except BaseException:
        listener.close()
        raise
