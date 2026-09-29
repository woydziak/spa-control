"""Optional UDP 30303 scan. Never required for control."""

from __future__ import annotations

import asyncio
import socket
from typing import Any


OUI = "00:15:27"


async def scan(timeout: float = 2.0, oui: str = OUI) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)
    sock.bind(("", 0))
    found: dict[str, dict[str, Any]] = {}

    def _recv() -> None:
        try:
            data, addr = sock.recvfrom(256)
        except BlockingIOError:
            return
        text = data.decode("ascii", errors="ignore")
        lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
        mac = lines[1] if len(lines) > 1 else ""
        mac_norm = mac.replace("-", ":").lower()
        if oui and not mac_norm.startswith(oui.lower()):
            return
        ip = addr[0]
        found[ip] = {
            "host": ip,
            "port": 4257,
            "name": lines[0] if lines else "BWGSPA",
            "mac": mac_norm,
        }

    loop.add_reader(sock.fileno(), _recv)
    try:
        try:
            sock.sendto(b"", ("255.255.255.255", 30303))
        except OSError:
            pass
        await asyncio.sleep(timeout)
    finally:
        loop.remove_reader(sock.fileno())
        sock.close()
    return list(found.values())
