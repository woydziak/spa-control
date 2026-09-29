"""Persisted spa connection settings. Discovery is never the only path."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .schedule import ScheduleError, normalize_months, normalize_windows

log = logging.getLogger("spa.config")


class ConfigError(ValueError):
    """The settings file or a settings change cannot be used."""

_ENV_LOADED = False


def load_env_file(path: Path | None = None) -> None:
    """Fill unset environment variables from a KEY=VALUE file.

    Existing variables win, so a shell export or a systemd EnvironmentFile
    is not overwritten by .env. An explicit path is applied immediately.
    The automatic path is the repo .env, or /etc/spa-control.env when this
    process can read it. The unit file is mode 0600 and is read by systemd
    as root, so the spa user skipping that file is normal.
    """
    global _ENV_LOADED
    if path is None:
        if _ENV_LOADED:
            return
        _ENV_LOADED = True
        path = _default_env_path()
        if path is None:
            return
    _apply_env_file(path)


def _default_env_path() -> Path | None:
    explicit = os.environ.get("SPA_ENV")
    if explicit:
        return Path(explicit)
    local = Path(__file__).resolve().parent.parent / ".env"
    if local.is_file():
        return local
    # systemd EnvironmentFile reads this as root before User=spa. Do not
    # warn when the service user cannot open it; the variables are already
    # in the environment, and a hand start without them fails on SPA_HOST.
    system = Path("/etc/spa-control.env")
    if system.is_file() and os.access(system, os.R_OK):
        return system
    return None


def _apply_env_file(path: Path) -> None:
    try:
        text = path.read_text()
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _defaults() -> dict[str, Any]:
    load_env_file()
    port = os.environ.get("SPA_PORT", "4257")
    http_port = os.environ.get("SPA_HTTP_PORT", "8080")
    return {
        "host": os.environ.get("SPA_HOST", ""),
        "port": int(port) if str(port).isdigit() else port,
        "mac": os.environ.get("SPA_MAC", ""),
        "oui": "00:15:27",
        "label": os.environ.get("SPA_LABEL", "Spa"),
        "bind": os.environ.get("SPA_BIND", "0.0.0.0"),
        "http_port": int(http_port) if str(http_port).isdigit() else http_port,
        "pin": os.environ.get("SPA_PIN", ""),
        "mock": os.environ.get("SPA_MOCK", "").lower() in {"1", "true", "yes"},
        "scan_on_start": False,
        "show_pump3": True,
        "show_blower": True,
        # Pump 1 defaults to two-speed. The others are on/off until changed.
        "pump1_speeds": 2,
        "pump2_speeds": 1,
        "pump3_speeds": 1,
        # Off until someone saves hours. A default window would hold the spa.
        "tou_enabled": False,
        "tou_windows": [],
        # Whole year until narrowed. An old file with no months must not
        # go quiet in winter on its own.
        "tou_months": list(range(1, 13)),
    }


def load() -> dict[str, Any]:
    cfg, err = _read()
    if err:
        raise ConfigError(err)
    _check(cfg)
    return cfg


def save(cfg: dict[str, Any]) -> dict[str, Any]:
    merged, err = _read()
    if err:
        # A broken file must not keep the process on a stale address.
        # The caller is replacing settings, so start from defaults and
        # write a new file over the bad one.
        log.warning("%s; replacing it", err)
        merged = _defaults()
    for key in (
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
    ):
        if key in cfg:
            merged[key] = cfg[key]
    _check(merged)
    _write_json(_path(), merged)
    return merged


def public_view(cfg: dict[str, Any]) -> dict[str, Any]:
    view = {
        k: cfg.get(k)
        for k in (
            "host",
            "port",
            "mac",
            "label",
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
    }
    view["pin_set"] = bool(cfg.get("pin"))
    view["mode"] = "configured_ip"
    return view


def _read() -> tuple[dict[str, Any], str | None]:
    cfg = _defaults()
    path = _path()
    if not path.exists():
        return cfg, None
    try:
        saved = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return cfg, f"{path} is unreadable ({exc})"
    if not isinstance(saved, dict):
        return cfg, f"{path} must be a JSON object"
    cfg.update({k: v for k, v in saved.items() if v is not None})
    try:
        _check(cfg)
    except ConfigError as exc:
        return cfg, str(exc)
    return cfg, None


def _check(cfg: dict[str, Any]) -> None:
    host = cfg.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("SPA_HOST is not set. Copy .env.example to .env or /etc/spa-control.env.")
    if any(c in host for c in " \t\r\n/\\"):
        raise ConfigError("host must be an IP address or hostname")
    cfg["host"] = host.strip()

    port = cfg.get("port")
    if isinstance(port, str) and port.isdigit():
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("port must be an integer from 1 to 65535")
    cfg["port"] = port

    if not isinstance(cfg.get("mock"), bool):
        raise ConfigError("mock must be true or false")
    for key in ("scan_on_start", "show_pump3", "show_blower"):
        if not isinstance(cfg.get(key), bool):
            raise ConfigError(f"{key} must be true or false")

    label = cfg.get("label") or "Spa"
    if not isinstance(label, str):
        raise ConfigError("label must be text")
    cfg["label"] = label.strip()[:80] or "Spa"

    mac = cfg.get("mac") or ""
    if not isinstance(mac, str):
        raise ConfigError("mac must be text")
    cfg["mac"] = mac.strip()[:32]

    for key in ("pump1_speeds", "pump2_speeds", "pump3_speeds"):
        speeds = cfg.get(key)
        if isinstance(speeds, str) and speeds.isdigit():
            speeds = int(speeds)
        if isinstance(speeds, bool) or speeds not in (1, 2):
            raise ConfigError(f"{key} must be 1 (on/off) or 2 (off, speed 1, speed 2)")
        cfg[key] = speeds

    enabled = cfg.get("tou_enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("tou_enabled must be true or false")
    cfg["tou_enabled"] = enabled
    try:
        cfg["tou_windows"] = normalize_windows(cfg.get("tou_windows", []))
        cfg["tou_months"] = normalize_months(cfg.get("tou_months", list(range(1, 13))))
    except ScheduleError as exc:
        raise ConfigError(str(exc)) from exc


def read_tou_owning() -> bool:
    """Whether the rate schedule turned hold on and still owes a release.

    Kept beside the settings file so a settings save cannot clear it.
    """
    return _read_tou_state()["owning"]


def write_tou_owning(owning: bool) -> None:
    if not isinstance(owning, bool):
        raise ConfigError("tou owning must be true or false")
    state = _read_tou_state()
    state["owning"] = owning
    _write_tou_state(state)


def read_tou_override() -> datetime | None:
    """When a soak override ends, or None if there is not one.

    Stored with the owning flag so a settings save cannot clear a soak
    that is already running.
    """
    raw = _read_tou_state()["override_until"]
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("%s has an unreadable soak time", _tou_state_path())
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def write_tou_override(until: datetime | None) -> None:
    if until is not None and until.tzinfo is None:
        raise ConfigError("soak time must include a timezone")
    state = _read_tou_state()
    state["override_until"] = None if until is None else until.isoformat()
    _write_tou_state(state)


def _read_tou_state() -> dict[str, Any]:
    path = _tou_state_path()
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        return {"owning": False, "override_until": None}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {"owning": False, "override_until": None}
    if not isinstance(saved, dict) or not isinstance(saved.get("owning"), bool):
        log.warning("%s does not hold a boolean owning flag", path)
        return {"owning": False, "override_until": None}
    until = saved.get("override_until")
    if until is not None and not isinstance(until, str):
        log.warning("%s has a soak time that is not text", path)
        until = None
    return {"owning": saved["owning"], "override_until": until}


def _write_tou_state(state: dict[str, Any]) -> None:
    _write_json(
        _tou_state_path(),
        {"owning": state["owning"], "override_until": state.get("override_until")},
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def _tou_state_path() -> Path:
    return _path().with_suffix(".tou.json")


def _path() -> Path:
    load_env_file()
    override = os.environ.get("SPA_CONFIG")
    if override:
        return Path(override)
    default = Path("/var/lib/spa-control/config.json")
    # Fall back to a writable location when /var/lib is not available
    if os.access("/var/lib", os.W_OK) or default.parent.exists():
        return default
    return Path(__file__).resolve().parent.parent / "data" / "config.json"
