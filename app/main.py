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
from .schedule import (
    ScheduleError,
    HoldScheduler,
    describe,
    months_from_config,
    override_deadline,
    windows_from_config,
)
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
current_cfg: dict[str, Any] = {}
scheduler = HoldScheduler()
rate_wake = asyncio.Event()
client = SpaClient(runtime.status, on_update=lambda _s: on_status())


def apply_config(cfg: dict[str, Any]) -> None:
    current_cfg.clear()
    current_cfg.update(cfg)
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


def on_status():
    # While a window can change hold, follow status so a minute boundary
    # or a manual press is handled without waiting out the slow tick.
    if current_cfg.get("tou_enabled") or scheduler.owning:
        rate_wake.set()
    return broadcast()


def status_payload() -> dict[str, Any]:
    data = runtime.snapshot()
    when = datetime.now().astimezone()
    try:
        windows = windows_from_config(current_cfg.get("tou_windows") or [])
        months = months_from_config(current_cfg.get("tou_months"))
        data["tou"] = describe(
            bool(current_cfg.get("tou_enabled")),
            windows,
            when,
            hour24=bool(runtime.status.time_24h),
            months=months,
            override_until=_live_override(when),
        )
    except ScheduleError:
        data["tou"] = {"enabled": False, "active": False, "summary": "Rate schedule could not be read."}
    return data


async def broadcast() -> None:
    dead = []
    payload = status_payload()
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
    return json_response(status_payload())


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
            "tou_enabled",
            "tou_windows",
            "tou_months",
        )
        if k in body
    }
    cfg = cfgmod.save(allowed)
    apply_config(cfg)
    # Connection edits drop the socket. A schedule edit must not, or saving
    # hours would interrupt the session the schedule is about to command.
    if set(allowed) <= {"tou_enabled", "tou_windows", "tou_months"}:
        rate_wake.set()
    else:
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
    elif action == "tou_override":
        if not current_cfg.get("tou_enabled"):
            return json_response({"detail": "Turn the rate schedule on first."}, 400)
        try:
            until = override_deadline(datetime.now().astimezone(), body.get("minutes"))
        except ScheduleError as exc:
            return json_response({"detail": str(exc)}, 400)
        cfgmod.write_tou_override(until)
        rate_wake.set()
        await broadcast()
    elif action == "tou_override_end":
        cfgmod.write_tou_override(None)
        rate_wake.set()
        await broadcast()
    else:
        return json_response({"detail": f"unknown action {action}"}, 400)
    return json_response({"ok": True})


async def ws_handler(reader, writer):
    runtime.subscribers.add(writer)
    try:
        writer.write(ws_encode(__import__("json").dumps({"type": "status", "data": status_payload()})))
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
    scheduler.owning = cfgmod.read_tou_owning()
    rate_task = asyncio.create_task(rate_loop(), name="spa-rates")
    ticker = asyncio.create_task(rate_ticker(), name="spa-rates-tick")
    app = WebApp(STATIC, ROUTES, ws_handler)
    host = cfg.get("bind") or "0.0.0.0"
    port = int(cfg.get("http_port") or 8080)
    server = await asyncio.start_server(app.handle, host, port)
    log.info("spa ui on http://%s:%s  module %s:%s  mode=%s", host, port, cfg["host"], cfg["port"], runtime.status.mode)
    try:
        async with server:
            await server.serve_forever()
    finally:
        rate_task.cancel()
        ticker.cancel()
        for task in (rate_task, ticker):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await client.stop()


def _status_is_fresh(max_age: float = 30) -> bool:
    stamp = runtime.status.last_update
    if not runtime.status.connected or not stamp:
        return False
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=datetime.now().astimezone().tzinfo)
    age = (datetime.now(seen.tzinfo) - seen).total_seconds()
    return 0 <= age <= max_age


async def reconcile_rates() -> None:
    when = datetime.now().astimezone()
    try:
        windows = windows_from_config(current_cfg.get("tou_windows") or [])
        months = months_from_config(current_cfg.get("tou_months"))
    except ScheduleError as exc:
        log.warning("rate schedule ignored: %s", exc)
        return
    before = scheduler.owning
    action = scheduler.step(
        enabled=bool(current_cfg.get("tou_enabled")),
        windows=windows,
        months=months,
        when=when,
        actual_hold=bool(runtime.status.hold),
        fresh=_status_is_fresh(),
        override_until=_live_override(when),
    )
    if scheduler.owning != before or scheduler.owning != cfgmod.read_tou_owning():
        cfgmod.write_tou_owning(scheduler.owning)
    if action != "toggle":
        return
    log.info("rate schedule pressing hold (want %s)", "on" if scheduler.pending else "off")
    try:
        await client.send_toggle("hold")
    except Exception as exc:
        log.warning("rate schedule could not press hold: %s", exc)
        scheduler.pending = None
        scheduler.pending_at = None


async def rate_loop() -> None:
    while True:
        await rate_wake.wait()
        rate_wake.clear()
        try:
            await reconcile_rates()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("rate schedule failed")
        # Status arrives about once a second. Fold that burst into one check.
        await asyncio.sleep(1)


async def rate_ticker() -> None:
    while True:
        rate_wake.set()
        delay = 15.0
        until = cfgmod.read_tou_override()
        if until is not None:
            remaining = (until - datetime.now().astimezone()).total_seconds()
            # Wake when the soak ends instead of waiting out the slow tick.
            if remaining < 15:
                delay = max(1.0, remaining + 0.5)
        await asyncio.sleep(delay)


def _live_override(when: datetime) -> datetime | None:
    until = cfgmod.read_tou_override()
    if until is not None and when >= until:
        cfgmod.write_tou_override(None)
        return None
    return until


def run() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
