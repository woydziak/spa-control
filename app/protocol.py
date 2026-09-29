"""Balboa Wi-Fi framing used on TCP 4257.

Frame:
  0x7E | length | payload | crc8 | 0x7E

The length byte counts itself, the payload, and the checksum. It does not
count either 0x7E delimiter. Outgoing commands use channel 0x0A 0xBF.
Status updates arrive as broadcast 0xFF 0xAF 0x13 about once a second.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DELIM = 0x7E
SEND_PREFIX = bytes((0x0A, 0xBF))
STATUS_TYPE = 0x13
FILTER_TYPE = 0x23
INFO_TYPE = 0x24
SETUP_TYPE = 0x25
CONFIG_TYPE = 0x2E
FAULT_TYPE = 0x28
MODULE_TYPE = 0x94

TOGGLE = 0x11
SET_TEMP = 0x20
SET_TIME = 0x21
REQUEST = 0x22
DEVICE_PRESENT = 0x04

TOGGLE_CODES = {
    "pump1": 0x04,
    "pump2": 0x05,
    "pump3": 0x06,
    "pump4": 0x07,
    "blower": 0x0C,
    "mister": 0x0E,
    "light": 0x11,
    "light2": 0x12,
    "aux1": 0x16,
    "hold": 0x3C,
    "circ": 0x3D,
    "temp_range": 0x50,
    "heat_mode": 0x51,
}

HEAT_MODES = {0: "ready", 1: "rest", 2: "ready_in_rest", 3: "ready_in_rest"}
HEAT_STATES = {0: "off", 1: "heating", 2: "heat_waiting"}
PUMP_STATES = {0: "off", 1: "low", 2: "high"}
SPA_STATES = {
    0x00: "running",
    0x01: "initializing",
    0x05: "hold",
    0x14: "ab_temps",
    0x17: "test",
}


def crc8(data: bytes | bytearray) -> int:
    """CRC used by Balboa packs (poly 0x07, init/xor equivalent of 0x02/0x02)."""
    crc = 0xB5
    for cur in data:
        for i in range(8):
            bit = crc & 0x80
            crc = ((crc << 1) & 0xFF) | ((cur >> (7 - i)) & 0x01)
            if bit:
                crc ^= 0x07
        crc &= 0xFF
    for _ in range(8):
        bit = crc & 0x80
        crc = (crc << 1) & 0xFF
        if bit:
            crc ^= 0x07
    return crc ^ 0x02


def build_frame(message_type: int, *payload: int) -> bytes:
    body = bytes((*SEND_PREFIX, message_type, *payload))
    # Length covers the length byte, body, and checksum.
    length = len(body) + 2
    mid = bytes((length, *body))
    return bytes((DELIM, *mid, crc8(mid), DELIM))


def toggle_frame(item: str) -> bytes:
    code = TOGGLE_CODES.get(item)
    if code is None:
        raise ValueError(f"unknown toggle item: {item}")
    return build_frame(TOGGLE, code, 0x00)


def encode_setpoint(display_temp: float, unit: str, temp_min: int, temp_max: int) -> int:
    """Scale a display temperature into the one-byte setpoint the pack expects.

    Fahrenheit is sent as whole degrees. Celsius is sent doubled. Values
    outside the pack's current range are rejected so they cannot wrap into
    a different legal byte.
    """
    if isinstance(display_temp, bool) or not isinstance(display_temp, (int, float)):
        raise ValueError("temperature must be a number")
    if not math.isfinite(display_temp):
        raise ValueError("temperature must be a finite number")
    if unit == "C":
        raw = int(round(float(display_temp) * 2))
        shown = raw / 2
    else:
        raw = int(round(float(display_temp)))
        shown = float(raw)
    if shown < temp_min or shown > temp_max:
        raise ValueError(
            f"temperature {shown:g} is outside {temp_min}–{temp_max}°{unit or 'F'}"
        )
    if not 0 <= raw <= 255:
        raise ValueError(f"set-temp byte out of range: {raw}")
    return raw


def set_temp_frame(raw: int) -> bytes:
    if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 255:
        raise ValueError(f"set-temp byte out of range: {raw}")
    return build_frame(SET_TEMP, raw)


def set_time_frame(hour: int, minute: int, twenty_four: bool) -> bytes:
    hh = hour & 0x7F
    if twenty_four:
        hh |= 0x80
    return build_frame(SET_TIME, hh, minute & 0x3F)


def request_frame(*args: int) -> bytes:
    return build_frame(REQUEST, *args)


@dataclass
class ParsedFrame:
    channel: int
    qualifier: int
    msg_type: int
    payload: bytes
    raw: bytes


class FrameAssembler:
    """Reassemble 0x7E-delimited frames from a TCP byte stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[ParsedFrame]:
        self._buf.extend(data)
        out: list[ParsedFrame] = []
        while True:
            kind, frame = self._pop()
            if kind == "wait":
                break
            if kind != "ok" or frame is None:
                continue
            parsed = _parse(frame)
            if parsed:
                out.append(parsed)
        return out

    def reset(self) -> None:
        self._buf.clear()

    def _pop(self) -> tuple[str, bytes | None]:
        """Return ("ok", frame), ("skip", None), or ("wait", None).

        "wait" means the buffer ends mid-frame and must not be consumed.
        "skip" means a bad candidate was dropped and the caller should
        keep scanning the bytes already buffered.
        """
        buf = self._buf
        try:
            start = buf.index(DELIM)
        except ValueError:
            buf.clear()
            return "wait", None
        if start:
            del buf[:start]
        if len(buf) < 2:
            return "wait", None
        length = buf[1]
        if length < 5 or length > 200:
            del buf[0]
            return "skip", None
        total = length + 2  # start delim + (length..end delim)
        if len(buf) < total:
            return "wait", None
        frame = bytes(buf[:total])
        del buf[:total]
        if frame[-1] != DELIM:
            return "skip", None
        mid = frame[1:-1]
        if mid[0] != len(mid) or crc8(mid[:-1]) != mid[-1]:
            return "skip", None
        return "ok", frame


def _parse(frame: bytes) -> ParsedFrame | None:
    # frame: 7E LEN CH QUAL TYPE ... CRC 7E
    if len(frame) < 7:
        return None
    return ParsedFrame(
        channel=frame[2],
        qualifier=frame[3],
        msg_type=frame[4],
        payload=frame[5:-2],
        raw=frame,
    )
