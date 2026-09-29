"""HTTP + WebSocket front end for the spa service."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as cfgmod
from .client import SpaClient
from .discovery import scan
from .server import WebApp, json_response, ws_encode, ws_read
from .state import Runtime, clear_live

log = logging.getLogger("spa")
logging.basicConfig(
    level=os.environ.get("SPA_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
SESSIONS: set[str] = set()
runtime = Runtime()
client = SpaClient(runtime.status, on_update=lambda _s: broadcast())


def apply_config(cfg: dict[str, Any]) -> None:
    st = runtime.status
    host = str(cfg["host"])
    port = int(cfg["port"])
    mode = "mock" if cfg.get("mock") else "configured_ip"
    target_changed = (st.host, st.port, st.mode) != (host, port, mode)
    st.host = host
    st.port = port
    st.mac = cfg.get("mac") or ""
    st.label = cfg.get("label") or "Spa"
    st.mode = mode
    if target_changed:
        clear_live(st)


async def broadcast() -> None:
    dead = []
    payload = runtime.snapshot()
    message = ws_encode(__import__("json").dumps({"type": "status", "data": payload}))
    for writer in list(runtime.subscribers):
        try:
            writer.write(message)
            await writer.drain()
        except Exception:
            dead.append(writer)
    for writer in dead:
        runtime.subscribers.discard(writer)


async def health(_req):
    return json_response(
        {
            "ok": True,
            "connected": runtime.status.connected,
            "host": runtime.status.host,
            "port": runtime.status.port,
            "mode": runtime.status.mode,
        }
    )


async def api_status(_req):
    return json_response(runtime.snapshot())


async def api_config_get(_req):
    return json_response(cfgmod.public_view(cfgmod.load()))


async def api_config_put(req):
    body = req.get("json") or {}
    allowed = {
        k: body[k]
        for k in (
            "host",
            "port",
            "mac",
            "label",
            "pin",
            "mock",
            "scan_on_start",
            "show_pump3",
            "show_blower",
            "pump1_speeds",
            "pump2_speeds",
            "pump3_speeds",
        )
        if k in body
    }
    cfg = cfgmod.save(allowed)
    apply_config(cfg)
    await client.reconnect()
    return json_response(cfgmod.public_view(cfg))


async def api_login(req):
    cfg = cfgmod.load()
    pin = cfg.get("pin") or ""
    given = (req.get("json") or {}).get("pin", "")
    if pin and given != pin:
        return json_response({"detail": "bad pin"}, 401)
    token = secrets.token_urlsafe(24)
    SESSIONS.add(token)
    return json_response({"token": token, "required": bool(pin)})


async def api_scan(_req):
    cfg = cfgmod.load()
    found = await scan(timeout=2.0, oui=cfg.get("oui") or "00:15:27")
    return json_response({"found": found, "configured": {"host": cfg["host"], "mac": cfg.get("mac")}})


async def api_command(req):
    body = req.get("json") or {}
    action = body.get("action")
    if action == "toggle":
        item = body.get("item")
        if not item:
            return json_response({"detail": "item required"}, 400)
        await client.send_toggle(item)
    elif action == "set_temp":
        value = body.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return json_response({"detail": "value required"}, 400)
        await client.send_temp(float(value))
    elif action == "set_time":
        now = datetime.now()
        await client.send_time(now.hour, now.minute)
    elif action == "reconnect":
        await client.reconnect()
    else:
        return json_response({"detail": f"unknown action {action}"}, 400)
    return json_response({"ok": True})


async def ws_handler(reader, writer):
    runtime.subscribers.add(writer)
    try:
        writer.write(ws_encode(__import__("json").dumps({"type": "status", "data": runtime.snapshot()})))
        await writer.drain()
        while True:
            msg = await ws_read(reader)
            if msg is None:
                break
            if msg == "ping":
                writer.write(ws_encode('{"type":"pong"}'))
                await writer.drain()
    except Exception:
        pass
    finally:
        runtime.subscribers.discard(writer)


ROUTES = {
    ("GET", "/health"): health,
    ("GET", "/api/status"): api_status,
    ("GET", "/api/config"): api_config_get,
    ("PUT", "/api/config"): api_config_put,
    ("POST", "/api/login"): api_login,
    ("POST", "/api/scan"): api_scan,
    ("POST", "/api/command"): api_command,
}


async def amain() -> None:
    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError as exc:
        log.error("%s", exc)
        log.error("refusing to start so a broken settings file cannot retarget the spa")
        raise SystemExit(1) from exc
    apply_config(cfg)
    if cfg.get("scan_on_start") and not cfg.get("mock"):
        try:
            found = await scan(timeout=1.5, oui=cfg.get("oui") or "00:15:27")
            log.info("optional scan heard %s device(s)", len(found))
        except Exception as exc:
            log.info("optional scan skipped: %s", exc)
    client.start()
    app = WebApp(STATIC, ROUTES, ws_handler)
    host = cfg.get("bind") or "0.0.0.0"
    port = int(cfg.get("http_port") or 8080)
    server = await asyncio.start_server(app.handle, host, port)
    log.info("spa ui on http://%s:%s  module %s:%s  mode=%s", host, port, cfg["host"], cfg["port"], runtime.status.mode)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await client.stop()


def run() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
