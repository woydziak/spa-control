"""Persistent TCP client for the Balboa Wi-Fi module."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import protocol as proto
from .state import SpaStatus, parse_info, parse_status

log = logging.getLogger("spa.client")
Listener = Callable[[SpaStatus], Awaitable[None] | None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SpaClient:
    def __init__(self, status: SpaStatus, on_update: Listener | None = None) -> None:
        self.status = status
        self.on_update = on_update
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._connect_task: asyncio.Task | None = None
        self._emit_task: asyncio.Task | None = None
        self._emit_needed = False
        self._lock = asyncio.Lock()
        self._generation = 0
        self._drop_reason: str | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="spa-client")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        connect_task = self._connect_task
        if connect_task and not connect_task.done():
            connect_task.cancel()
        await self._close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._emit_task and not self._emit_task.done():
            self._emit_task.cancel()

    async def reconnect(self) -> None:
        """Drop the current socket and dial the configured target now.

        Closing the socket is not enough: the run loop may be asleep in
        backoff, or blocked in an 8s handshake to the previous host.
        """
        self._generation += 1
        self._drop_reason = "reconnect"
        self._wake.set()
        connect_task = self._connect_task
        if connect_task and not connect_task.done():
            connect_task.cancel()
        await self._close()

    async def send_toggle(self, item: str) -> None:
        await self._write(proto.toggle_frame(item))

    async def send_temp(self, display_temp: float) -> None:
        raw = proto.encode_setpoint(
            display_temp,
            self.status.unit,
            self.status.temp_min,
            self.status.temp_max,
        )
        await self._write(proto.set_temp_frame(raw))

    async def send_time(self, hour: int, minute: int) -> None:
        await self._write(proto.set_time_frame(hour, minute, self.status.time_24h))

    async def request_info(self) -> None:
        # Filter and panel replies are ignored unless the configuration
        # request went out first.
        await self._write(proto.build_frame(proto.DEVICE_PRESENT))
        await asyncio.sleep(0.1)
        await self._write(proto.request_frame(0x01, 0x00, 0x00))
        await self._write(proto.request_frame(0x02, 0x00, 0x00))
        await self._write(proto.request_frame(0x00, 0x00, 0x01))

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if self.status.mode == "mock":
                await self._mock_loop()
                continue
            generation = self._generation
            try:
                await self._connect(generation)
                if self._generation != generation or self.status.mode == "mock":
                    await self._close()
                    continue
                attempt = 0
                await self.request_info()
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.connected = False
                if self._drop_reason == "reconnect":
                    self.status.last_error = ""
                else:
                    self.status.last_error = str(exc)
                    log.warning("spa link down: %s", exc)
                self._schedule_emit()
            await self._close()
            if self._stop.is_set():
                break
            if self._wake.is_set() or self._drop_reason == "reconnect":
                self._wake.clear()
                self._drop_reason = None
                attempt = 0
                continue
            delay = min(2**attempt + random.random(), 30)
            attempt += 1
            log.info("reconnect in %.1fs (attempt %s)", delay, attempt)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            else:
                self._wake.clear()
                self._drop_reason = None
                attempt = 0

    async def _connect(self, generation: int) -> None:
        host, port = self.status.host, self.status.port
        log.info("connecting to %s:%s", host, port)
        self.status.last_error = ""
        self._schedule_emit()
        self._connect_task = asyncio.create_task(
            asyncio.wait_for(asyncio.open_connection(host, port), timeout=8),
            name="spa-connect",
        )
        try:
            reader, writer = await self._connect_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise ConnectionError("connect cancelled")
        finally:
            self._connect_task = None
        if (
            generation != self._generation
            or self.status.mode == "mock"
            or (self.status.host, self.status.port) != (host, port)
        ):
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raise ConnectionError("connect superseded")
        async with self._lock:
            self._reader, self._writer = reader, writer
        self.status.last_connect = _now()
        if self.status.mode != "mock":
            self.status.mode = "configured_ip"
        # Stay "not linked" until a status frame arrives. The TCP session
        # alone does not mean the pumps on screen belong to this module.
        self._schedule_emit()

    async def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            raise ConnectionError("not connected")
        assembler = proto.FrameAssembler()
        while not self._stop.is_set():
            if self._reader is not reader:
                raise ConnectionError("disconnected")
            try:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=20)
            except asyncio.TimeoutError:
                raise ConnectionError("no status frames for 20s")
            if not chunk:
                raise ConnectionError("tcp closed by module")
            for frame in assembler.feed(chunk):
                self._handle(frame)

    def _handle(self, frame: proto.ParsedFrame) -> None:
        if frame.msg_type == proto.STATUS_TYPE:
            parse_status(frame.payload, self.status)
            self.status.connected = True
            self._schedule_emit()
        elif frame.msg_type == proto.INFO_TYPE:
            parse_info(frame.payload, self.status)
            self._schedule_emit()
        elif frame.msg_type == proto.MODULE_TYPE:
            self._note_module(frame.payload)

    def _note_module(self, payload: bytes) -> None:
        if len(payload) < 9:
            return
        raw = payload[3:9]
        if raw == bytes(6) or raw == b"\xff" * 6:
            return
        mac = ":".join(f"{b:02x}" for b in raw)
        expected = (self.status.mac or "").strip().lower()
        if expected and mac != expected:
            log.warning("module at %s reports mac %s, configured mac is %s", self.status.host, mac, expected)
        else:
            log.info("module mac %s", mac)

    async def _write(self, frame: bytes) -> None:
        if self.status.mode == "mock":
            self._apply_mock_command(frame)
            self._schedule_emit()
            return
        async with self._lock:
            writer = self._writer
            if writer is None:
                raise ConnectionError("not connected")
            writer.write(frame)
            await writer.drain()

    async def _close(self) -> None:
        async with self._lock:
            writer = self._writer
            self._reader = None
            self._writer = None
            self.status.connected = False
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _schedule_emit(self) -> None:
        self._emit_needed = True
        task = self._emit_task
        if task is not None and not task.done():
            return
        self._emit_task = asyncio.create_task(self._emit_loop(), name="spa-emit")

    async def _emit_loop(self) -> None:
        try:
            while True:
                if not self._emit_needed:
                    await asyncio.sleep(0)
                    if not self._emit_needed:
                        return
                self._emit_needed = False
                await self._emit()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("status broadcast failed")
        finally:
            if self._emit_task is asyncio.current_task():
                self._emit_task = None

    async def _emit(self) -> None:
        if self.on_update:
            result = self.on_update(self.status)
            if asyncio.iscoroutine(result):
                await result

    async def _mock_loop(self) -> None:
        st = self.status
        st.connected = True
        st.mode = "mock"
        st.last_error = ""
        st.last_connect = _now()
        st.current_temp = st.current_temp if st.current_temp is not None else 99
        st.set_temp = st.set_temp if st.set_temp is not None else 100
        st.unit = "F"
        st.heat_mode = st.heat_mode or "ready"
        step = 0.5 if st.unit == "C" else 1.0
        if st.current_temp < st.set_temp:
            st.current_temp = min(st.set_temp, st.current_temp + step)
            st.heat_state = "heating"
        elif st.current_temp > st.set_temp:
            st.current_temp = max(st.set_temp, st.current_temp - step)
            st.heat_state = "off"
        else:
            st.heat_state = "off"
        st.temp_range = st.temp_range or "high"
        st.spa_state = "hold" if st.hold else "running"
        st.circ = "off" if st.hold else "on"
        st.model = st.model or "MS40E (mock)"
        now = datetime.now()
        st.hour, st.minute = now.hour, now.minute
        st.last_update = _now()
        self._schedule_emit()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        else:
            self._wake.clear()

    def _apply_mock_command(self, frame: bytes) -> None:
        if len(frame) < 8:
            return
        msg_type = frame[4]
        st = self.status
        if msg_type == proto.SET_TEMP:
            raw = frame[5]
            st.set_temp = raw / (2 if st.unit == "C" else 1)
            if st.current_temp is not None:
                st.heat_state = "heating" if st.current_temp < st.set_temp else "off"
        elif msg_type == proto.TOGGLE:
            code = frame[5]
            cycle = {"off": "low", "low": "high", "high": "off"}
            onoff = {"off": "on", "on": "off"}
            mapping = {
                0x04: ("pump1", cycle),
                0x05: ("pump2", cycle),
                0x06: ("pump3", cycle),
                0x0C: ("blower", cycle),
                0x11: ("light", onoff),
                0x3C: ("hold", None),
                0x50: ("temp_range", None),
                0x51: ("heat_mode", None),
            }
            spec = mapping.get(code)
            if not spec:
                return
            field, table = spec
            if field == "hold":
                st.hold = not st.hold
                st.spa_state = "hold" if st.hold else "running"
                st.circ = "off" if st.hold else "on"
            elif field == "temp_range":
                st.temp_range = "low" if st.temp_range == "high" else "high"
                if st.temp_range == "low":
                    st.temp_min, st.temp_max = 50, 80
                else:
                    st.temp_min, st.temp_max = 80, 104
            elif field == "heat_mode":
                st.heat_mode = "rest" if st.heat_mode == "ready" else "ready"
            elif table:
                setattr(st, field, table.get(getattr(st, field), "off"))
        st.last_update = _now()
