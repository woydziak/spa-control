"""Zero-dependency HTTP + WebSocket server."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlparse

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

Handler = Callable[[dict[str, Any]], Awaitable[tuple[int, dict[str, str], bytes] | tuple]]


def _http_response(status: int, headers: dict[str, str], body: bytes) -> bytes:
    reason = {
        200: "OK",
        204: "No Content",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "OK")
    lines = [f"HTTP/1.1 {status} {reason}"]
    headers = {
        "Content-Length": str(len(body)),
        "Connection": "close",
        **headers,
    }
    for k, v in headers.items():
        lines.append(f"{k}: {v}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


def json_response(payload: Any, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload).encode("utf-8")
    return status, {"Content-Type": "application/json"}, body


def file_response(path: Path) -> tuple[int, dict[str, str], bytes]:
    if not path.is_file():
        return 404, {"Content-Type": "text/plain"}, b"not found"
    data = path.read_bytes()
    ctype = STATIC_TYPES.get(path.suffix, "application/octet-stream")
    # Home-screen iOS keeps a stale copy when this is only "no-cache".
    return 200, {"Content-Type": ctype, "Cache-Control": "no-store"}, data


async def read_request(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    header_blob = await reader.readuntil(b"\r\n\r\n")
    head, _ = header_blob.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    if not lines:
        return None
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None
    method, raw_target = parts[0], parts[1]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length") or 0)
    body = await reader.readexactly(length) if length else b""
    parsed = urlparse(raw_target)
    return {
        "method": method.upper(),
        "path": unquote(parsed.path),
        "query": parsed.query,
        "headers": headers,
        "body": body,
    }


def ws_accept_key(key: str) -> str:
    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    digest = hashlib.sha1((key + guid).encode("ascii")).digest()
    import base64

    return base64.b64encode(digest).decode("ascii")


async def ws_handshake(writer: asyncio.StreamWriter, headers: dict[str, str]) -> bool:
    key = headers.get("sec-websocket-key")
    if not key:
        return False
    accept = ws_accept_key(key)
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    writer.write(resp.encode("ascii"))
    await writer.drain()
    return True


def ws_encode(text: str) -> bytes:
    payload = text.encode("utf-8")
    n = len(payload)
    header = bytearray([0x81])
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(n.to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(n.to_bytes(8, "big"))
    return bytes(header) + payload


async def ws_read(reader: asyncio.StreamReader) -> str | None:
    hdr = await reader.readexactly(2)
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if masked else b""
    data = bytearray(await reader.readexactly(length))
    if masked:
        for i in range(len(data)):
            data[i] ^= mask[i % 4]
    if opcode == 0x8:
        return None
    if opcode == 0x9:
        return ""  # ping
    if opcode in (0x1, 0x2, 0x0, 0xA):
        return data.decode("utf-8", errors="replace")
    return ""


class WebApp:
    def __init__(self, static_root: Path, routes: dict[tuple[str, str], Handler], ws_handler) -> None:
        self.static_root = static_root
        self.routes = routes
        self.ws_handler = ws_handler

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await asyncio.wait_for(read_request(reader), timeout=30)
            if not req:
                return
            headers = req["headers"]
            upgrade = headers.get("upgrade", "").lower()
            if req["path"] == "/ws" and upgrade == "websocket":
                if await ws_handshake(writer, headers):
                    await self.ws_handler(reader, writer)
                return
            status, hdrs, body = await self.dispatch(req)
            writer.write(_http_response(status, hdrs, body))
            await writer.drain()
        except Exception:
            try:
                writer.write(_http_response(500, {"Content-Type": "text/plain"}, b"error"))
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def dispatch(self, req: dict[str, Any]):
        method, path = req["method"], req["path"]
        handler = self.routes.get((method, path))
        if handler:
            try:
                if req["body"]:
                    try:
                        req["json"] = json.loads(req["body"].decode("utf-8") or "{}")
                    except json.JSONDecodeError:
                        return json_response({"detail": "invalid json"}, 400)
                else:
                    req["json"] = {}
                return await handler(req)
            except ValueError as exc:
                return json_response({"detail": str(exc)}, 400)
            except ConnectionError as exc:
                return json_response({"detail": str(exc)}, 503)
            except Exception as exc:
                return json_response({"detail": str(exc)}, 500)
        if method == "GET":
            if path == "/":
                return file_response(self.static_root / "index.html")
            if path.startswith("/static/"):
                rel = path[len("/static/") :]
                candidate = (self.static_root / rel).resolve()
                if self.static_root.resolve() in candidate.parents or candidate == self.static_root.resolve():
                    return file_response(candidate)
        return 404, {"Content-Type": "text/plain"}, b"not found"
