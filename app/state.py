"""Decoded spa status shared with the web clients."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .protocol import HEAT_MODES, HEAT_STATES, PUMP_STATES, SPA_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SpaStatus:
    connected: bool = False
    mode: str = "configured_ip"  # configured_ip | scan | mock
    host: str = ""
    port: int = 4257
    mac: str = ""
    label: str = ""
    last_error: str = ""
    last_update: str = ""
    last_connect: str = ""
    spa_state: str = "unknown"
    current_temp: float | None = None
    set_temp: float | None = None
    unit: str = "F"
    hour: int = 0
    minute: int = 0
    time_24h: bool = False
    heat_mode: str = "ready"
    heat_state: str = "off"
    temp_range: str = "high"
    priming: bool = False
    hold: bool = False
    filter1: bool = False
    filter2: bool = False
    pump1: str = "off"
    pump2: str = "off"
    pump3: str = "off"
    pump4: str = "off"
    circ: str = "off"
    blower: str = "off"
    light: str = "off"
    mister: str = "off"
    model: str = ""
    software: str = ""
    wifi: int | None = None
    temp_min: int = 80
    temp_max: int = 104

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Runtime:
    status: SpaStatus = field(default_factory=SpaStatus)
    subscribers: set = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        return self.status.as_dict()


def clear_live(status: SpaStatus) -> None:
    """Drop readings that belonged to the previous target.

    Host, port, label, and mode are connection settings and stay put.
    Pump badges are edge-triggered, so leaving the old module's state
    on screen makes the next tap do the opposite of what it shows.
    """
    status.connected = False
    status.current_temp = None
    status.set_temp = None
    status.spa_state = "unknown"
    status.heat_state = "off"
    status.hold = False
    status.priming = False
    status.filter1 = False
    status.filter2 = False
    status.pump1 = status.pump2 = status.pump3 = status.pump4 = "off"
    status.circ = status.blower = status.light = status.mister = "off"
    status.last_update = ""
    status.last_error = ""


def parse_status(payload: bytes, status: SpaStatus) -> None:
    """Parse the 24-byte body after type 0x13."""
    if len(payload) < 21:
        return
    data = payload
    status.spa_state = SPA_STATES.get(data[0], f"0x{data[0]:02x}")
    status.hold = data[0] == 0x05
    status.priming = bool(data[1] & 0x01)

    unit_c = bool(data[9] & 0x01)
    status.unit = "C" if unit_c else "F"
    divisor = 2.0 if unit_c else 1.0
    ct = data[2]
    status.current_temp = None if ct == 0xFF else ct / divisor
    status.set_temp = data[20] / divisor if len(data) > 20 else None

    status.hour = data[3] & 0x7F
    status.minute = data[4]
    status.heat_mode = HEAT_MODES.get(data[5] & 0x03, "ready")
    status.time_24h = bool(data[9] & 0x02)
    status.filter1 = bool(data[9] & 0x04)
    status.filter2 = bool(data[9] & 0x08)

    f4 = data[10]
    status.temp_range = "high" if (f4 >> 2) & 0x01 else "low"
    status.heat_state = HEAT_STATES.get((f4 >> 4) & 0x03, "off")
    if status.temp_range == "low":
        status.temp_min, status.temp_max = (10, 26) if unit_c else (50, 80)
    else:
        status.temp_min, status.temp_max = (26, 40) if unit_c else (80, 104)

    pumps = data[11]
    status.pump1 = PUMP_STATES.get(pumps & 0x03, "off")
    status.pump2 = PUMP_STATES.get((pumps >> 2) & 0x03, "off")
    status.pump3 = PUMP_STATES.get((pumps >> 4) & 0x03, "off")
    status.pump4 = PUMP_STATES.get((pumps >> 6) & 0x03, "off")

    circ_blow = data[13] if len(data) > 13 else 0
    status.circ = "on" if ((circ_blow & 0x03) >> 1) else "off"
    blow = (circ_blow >> 2) & 0x03
    status.blower = PUMP_STATES.get(blow, "off") if blow else "off"

    lights = data[14] if len(data) > 14 else 0
    status.light = "on" if (lights & 0x03) else "off"

    extra = data[15] if len(data) > 15 else 0
    status.mister = "on" if (extra & 0x01) else "off"
    if len(data) > 22:
        status.wifi = data[22]
    status.last_update = _now()


def parse_info(payload: bytes, status: SpaStatus) -> None:
    if len(payload) < 16:
        return
    # software id (2) + version (2) + model (8)
    try:
        sid = payload[0:2].hex()
        ver = payload[2:4].hex()
        model = payload[4:12].decode("ascii", errors="ignore").strip()
        status.model = model
        status.software = f"{sid}/{ver}"
    except Exception:
        pass
