"""Decide when the rate schedule should press Hold.

Hold is a toggle, not a set command. The schedule claims a hold only when
it is about to turn hold on, and it presses Hold again to release only a
hold it claimed. A hold turned on by hand outside a window is left alone.
A second press waits until the reported state matches, or until the wait
times out, so a slow status frame cannot flip the spa twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


class ScheduleError(ValueError):
    """A rate window could not be stored."""


PENDING_TIMEOUT = timedelta(seconds=45)
OVERRIDE_MINUTES = (20, 40, 60)
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
ALL_MONTHS = frozenset(range(1, 13))
# A summer season can sit more than a week out, and a one-month season
# can sit nearly a year out. 366 reaches that date, including Feb 29.
LOOKAHEAD_DAYS = 366


@dataclass(frozen=True)
class Window:
    start: int  # minutes from midnight, inclusive
    end: int  # minutes from midnight, exclusive
    days: frozenset[int]

    def allows(self, day: int) -> bool:
        return day in self.days


@dataclass
class HoldScheduler:
    owning: bool = False
    pending: bool | None = None
    pending_at: datetime | None = None

    def step(
        self,
        *,
        enabled: bool,
        windows: list[Window],
        when: datetime,
        actual_hold: bool,
        fresh: bool,
        months: frozenset[int] | None = None,
        override_until: datetime | None = None,
    ) -> str:
        """Return "toggle" when Hold should be pressed once, else "none"."""
        for _ in range(2):
            if self.pending is not None:
                waited = self.pending_at is not None and when - self.pending_at >= PENDING_TIMEOUT
                if fresh and actual_hold is self.pending:
                    self.owning = bool(self.pending)
                    self.pending = None
                    self.pending_at = None
                elif waited:
                    self.pending = None
                    self.pending_at = None
                else:
                    return "none"

            desired = desired_hold(
                enabled, windows, when, self.owning, months, override_until=override_until
            )
            if desired is None or not fresh:
                return "none"
            if actual_hold is desired:
                if desired is False and self.owning:
                    self.owning = False
                return "none"
            if desired is True:
                # Claim the hold before the press. A restart then still
                # releases it, and a press that never lands is not released
                # onto an idle spa because release requires hold to be on.
                self.owning = True
            self.pending = desired
            self.pending_at = when
            return "toggle"
        return "none"


def desired_hold(
    enabled: bool,
    windows: list[Window],
    when: datetime,
    owning: bool,
    months: frozenset[int] | None = None,
    override_until: datetime | None = None,
) -> bool | None:
    """The hold state to enforce, or None when the spa should be left alone.

    A soak timer pauses enforcement. Release a hold this schedule owns,
    and do not start another until the timer ends.
    """
    if override_until is not None and when < override_until:
        return False if owning else None
    if not enabled:
        return False if owning else None
    if covering(windows, when, months) is not None:
        return True
    if owning:
        return False
    return None


def normalize_windows(raw: object) -> list[dict]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ScheduleError("rate windows must be a list")
    if len(raw) > 8:
        raise ScheduleError("at most 8 rate windows")
    cleaned: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ScheduleError("each rate window must be an object")
        start = _clock(item.get("start"), "start")
        end = _clock(item.get("end"), "end")
        if start == end:
            raise ScheduleError("a rate window needs a start and a different end")
        days = _days(item.get("days", list(range(7))))
        cleaned.append({"start": start, "end": end, "days": days})
    return cleaned


def normalize_months(raw: object) -> list[int]:
    """Months 1–12 the schedule may run. Missing means the whole year."""
    if raw is None:
        return list(range(1, 13))
    if not isinstance(raw, list) or not raw:
        raise ScheduleError("pick at least one month")
    months: list[int] = []
    for month in raw:
        if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
            raise ScheduleError("months must be 1 (January) through 12 (December)")
        if month not in months:
            months.append(month)
    return months


def months_from_config(raw: object) -> frozenset[int]:
    return frozenset(normalize_months(raw))


def windows_from_config(raw: object) -> list[Window]:
    windows = []
    for item in normalize_windows(raw):
        windows.append(
            Window(start=_minutes(item["start"]), end=_minutes(item["end"]), days=frozenset(item["days"]))
        )
    return windows


def covering(
    windows: list[Window], when: datetime, months: frozenset[int] | None = None
) -> Window | None:
    for window in windows:
        if covers(window, when, months):
            return window
    return None


def covers(window: Window, when: datetime, months: frozenset[int] | None = None) -> bool:
    """True when `when` is inside the window, including a span past midnight.

    An overnight window belongs to the day it starts. Monday 22:00–06:00
    covers Monday night and Tuesday morning, not Tuesday night. The month
    is that start day too: September 30 still covers October 1 morning,
    and April 30 does not cover May 1 morning.
    """
    chosen = _months(months)
    minutes = when.hour * 60 + when.minute
    day = when.weekday()
    if window.start < window.end:
        return when.month in chosen and window.allows(day) and window.start <= minutes < window.end
    if minutes >= window.start and window.allows(day) and when.month in chosen:
        return True
    started = when - timedelta(days=1)
    previous = (day - 1) % 7
    return minutes < window.end and window.allows(previous) and started.month in chosen


def active_until(
    windows: list[Window], when: datetime, months: frozenset[int] | None = None
) -> datetime | None:
    window = covering(windows, when, months)
    if window is None:
        return None
    end_hour, end_minute = divmod(window.end, 60)
    if window.start < window.end or when.hour * 60 + when.minute < window.end:
        day = when
    else:
        day = when + timedelta(days=1)
    return day.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)


def next_start(
    windows: list[Window], when: datetime, months: frozenset[int] | None = None
) -> datetime | None:
    chosen = _months(months)
    best: datetime | None = None
    for window in windows:
        for ahead in range(LOOKAHEAD_DAYS):
            day = when + timedelta(days=ahead)
            if day.month not in chosen or not window.allows(day.weekday()):
                continue
            start_hour, start_minute = divmod(window.start, 60)
            start = day.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
            if start <= when:
                continue
            if best is None or start < best:
                best = start
            break
    return best


def describe(
    enabled: bool,
    windows: list[Window],
    when: datetime,
    *,
    hour24: bool = False,
    months: frozenset[int] | None = None,
    override_until: datetime | None = None,
) -> dict:
    if not enabled:
        result = {"enabled": False, "active": False, "summary": ""}
    else:
        result = _describe_schedule(windows, when, hour24=hour24, months=months)
    result["override_until"] = None
    if override_until is not None and when < override_until:
        result["override_until"] = override_until.isoformat()
        result["active"] = False
        result["summary"] = (
            f"Using the tub until {fmt_clock(override_until, hour24)}. The schedule will not hold."
        )
    return result


def _describe_schedule(
    windows: list[Window],
    when: datetime,
    *,
    hour24: bool,
    months: frozenset[int] | None,
) -> dict:
    chosen = _months(months)
    until = active_until(windows, when, chosen)
    if until is not None:
        return {
            "enabled": True,
            "active": True,
            "summary": f"Expensive hours until {fmt_clock(until, hour24)}. Hold keeps the heater and pumps off.",
        }
    upcoming = next_start(windows, when, chosen)
    if upcoming is None:
        return {"enabled": True, "active": False, "summary": "Rate schedule is on, but no hours are set."}
    return {
        "enabled": True,
        "active": False,
        "summary": f"Next expensive hours {fmt_upcoming(upcoming, when, hour24)}.",
    }


def override_deadline(when: datetime, minutes: object) -> datetime:
    """When a soak override ends. Only 20, 40, or 60 minutes are accepted."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes not in OVERRIDE_MINUTES:
        raise ScheduleError("choose 20, 40, or 60 minutes")
    return when + timedelta(minutes=minutes)


def fmt_upcoming(when: datetime, now: datetime, hour24: bool) -> str:
    clock = fmt_clock(when, hour24)
    if when.date() == now.date():
        return clock
    return f"{MONTH_NAMES[when.month]} {when.day}, {clock}"


def fmt_clock(when: datetime, hour24: bool) -> str:
    if hour24:
        return f"{when.hour:02d}:{when.minute:02d}"
    hour = when.hour % 12 or 12
    suffix = "AM" if when.hour < 12 else "PM"
    return f"{hour}:{when.minute:02d} {suffix}"


def _months(months: frozenset[int] | None) -> frozenset[int]:
    if months is None:
        return ALL_MONTHS
    return months


def _clock(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ScheduleError(f"window {label} must be HH:MM")
    parts = value.strip().split(":")
    if len(parts) == 3:
        parts = parts[:2]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ScheduleError(f"window {label} must be HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleError(f"window {label} must be HH:MM")
    return f"{hour:02d}:{minute:02d}"


def _minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _days(value: object) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ScheduleError("pick at least one day for each rate window")
    days: list[int] = []
    for day in value:
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise ScheduleError("days must be 0 (Monday) through 6 (Sunday)")
        if day not in days:
            days.append(day)
    return days
