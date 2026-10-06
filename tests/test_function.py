"""Behavior the tub depends on: frames, setpoints, settings, reconnect."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config as cfgmod
from app.client import SpaClient, _backoff_delay
from app.protocol import FrameAssembler, build_frame, encode_setpoint, set_temp_frame
from app.schedule import (
    HoldScheduler,
    ScheduleError,
    covers,
    describe,
    next_start,
    normalize_months,
    normalize_windows,
    override_deadline,
    windows_from_config,
)
from app.state import SpaStatus, clear_live


class SetpointTests(unittest.TestCase):
    def test_fahrenheit_65_matches_known_frame(self) -> None:
        # Published Balboa "set 65°F" frame, including the checksum.
        self.assertEqual(set_temp_frame(encode_setpoint(65, "F", 50, 104)).hex(), "7e060abf2041d27e")

    def test_celsius_is_doubled(self) -> None:
        self.assertEqual(encode_setpoint(37.5, "C", 10, 40), 75)

    def test_out_of_range_does_not_wrap(self) -> None:
        with self.assertRaises(ValueError):
            encode_setpoint(300, "F", 80, 104)
        with self.assertRaises(ValueError):
            encode_setpoint(-1, "F", 80, 104)
        with self.assertRaises(ValueError):
            encode_setpoint(True, "F", 80, 104)  # type: ignore[arg-type]

    def test_rounds_inside_the_panel_range(self) -> None:
        self.assertEqual(encode_setpoint(100.4, "F", 80, 104), 100)


class AssemblerTests(unittest.TestCase):
    def test_bad_candidate_does_not_stall_the_rest_of_the_buffer(self) -> None:
        good = set_temp_frame(100)
        blob = bytes((0x7E, 0x01)) + good  # length too small, then a real frame
        frames = FrameAssembler().feed(blob)
        self.assertEqual([frame.raw for frame in frames], [good])

    def test_corrupt_frame_is_skipped_and_the_next_one_is_kept(self) -> None:
        good = build_frame(0x11, 0x04, 0x00)
        corrupt = bytearray(good)
        corrupt[-2] ^= 0xFF
        frames = FrameAssembler().feed(bytes(corrupt) + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw, good)

    def test_incomplete_tail_waits_for_the_next_chunk(self) -> None:
        good = set_temp_frame(90)
        asm = FrameAssembler()
        self.assertEqual(asm.feed(good[:3]), [])
        frames = asm.feed(good[3:])
        self.assertEqual([frame.raw for frame in frames], [good])


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("SPA_CONFIG")
        self.path = Path(self._dir.name) / "config.json"
        os.environ["SPA_CONFIG"] = str(self.path)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("SPA_CONFIG", None)
        else:
            os.environ["SPA_CONFIG"] = self._prev
        self._dir.cleanup()

    def test_corrupt_file_refuses_to_load(self) -> None:
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load()

    def test_bad_port_does_not_replace_a_good_file(self) -> None:
        saved = cfgmod.save({"host": "192.0.2.10", "port": 4257})
        self.assertEqual(saved["port"], 4257)
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.save({"port": "nope"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_refuses_to_start_without_a_host(self) -> None:
        previous = os.environ.get("SPA_HOST")
        os.environ["SPA_HOST"] = ""
        try:
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load()
        finally:
            if previous is None:
                os.environ.pop("SPA_HOST", None)
            else:
                os.environ["SPA_HOST"] = previous

    def test_env_file_does_not_override_existing_variables(self) -> None:
        path = Path(self._dir.name) / "sample.env"
        path.write_text(
            '\n'.join(
                [
                    "# comment",
                    "SPA_HOST=192.0.2.30",
                    "export SPA_PIN=secret",
                    'SPA_LABEL="Hot Tub"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        previous_host = os.environ.get("SPA_HOST")
        previous_pin = os.environ.pop("SPA_PIN", None)
        previous_label = os.environ.pop("SPA_LABEL", None)
        os.environ["SPA_HOST"] = "192.0.2.20"
        try:
            cfgmod.load_env_file(path)
            self.assertEqual(os.environ["SPA_HOST"], "192.0.2.20")
            self.assertEqual(os.environ["SPA_PIN"], "secret")
            self.assertEqual(os.environ["SPA_LABEL"], "Hot Tub")
        finally:
            if previous_host is None:
                os.environ.pop("SPA_HOST", None)
            else:
                os.environ["SPA_HOST"] = previous_host
            if previous_pin is None:
                os.environ.pop("SPA_PIN", None)
            else:
                os.environ["SPA_PIN"] = previous_pin
            if previous_label is None:
                os.environ.pop("SPA_LABEL", None)
            else:
                os.environ["SPA_LABEL"] = previous_label

    def test_unreadable_env_file_does_not_raise(self) -> None:
        path = Path(self._dir.name) / "locked.env"
        path.write_text("SPA_LABEL=FromFile\n", encoding="utf-8")
        os.chmod(path, 0)
        previous = os.environ.pop("SPA_LABEL", None)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("this user can still read a mode 000 file")
            with self.assertLogs("spa.config", level="WARNING") as logged:
                cfgmod.load_env_file(path)
            self.assertTrue(any("could not read" in line for line in logged.output))
            self.assertNotIn("SPA_LABEL", os.environ)
        finally:
            os.chmod(path, 0o600)
            if previous is None:
                os.environ.pop("SPA_LABEL", None)
            else:
                os.environ["SPA_LABEL"] = previous

    def test_skips_system_env_file_the_process_cannot_read(self) -> None:
        repo_env = Path(cfgmod.__file__).resolve().parent.parent / ".env"
        system = Path("/etc/spa-control.env")
        real_is_file = Path.is_file
        real_access = os.access

        def is_file(path_self: Path) -> bool:
            if path_self == system:
                return True
            if path_self == repo_env:
                return False
            return real_is_file(path_self)

        def access(path: object, mode: int, **kwargs: object) -> bool:
            if Path(str(path)) == system:
                return False
            return real_access(path, mode, **kwargs)

        previous = os.environ.pop("SPA_ENV", None)
        try:
            with patch.object(Path, "is_file", is_file), patch("app.config.os.access", access):
                self.assertIsNone(cfgmod._default_env_path())
        finally:
            if previous is None:
                os.environ.pop("SPA_ENV", None)
            else:
                os.environ["SPA_ENV"] = previous

    def test_pump_speeds_default_to_two_speed_on_pump_1(self) -> None:
        saved = cfgmod.save({"host": "192.0.2.10", "port": 4257})
        self.assertEqual(saved["pump1_speeds"], 2)
        self.assertEqual(saved["pump2_speeds"], 1)
        self.assertEqual(cfgmod.public_view(saved)["pump1_speeds"], 2)
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.save({"pump1_speeds": 3})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_save_replaces_a_corrupt_file(self) -> None:
        self.path.write_text("{", encoding="utf-8")
        saved = cfgmod.save({"host": "192.0.2.20", "port": 4257, "mock": False})
        self.assertEqual(saved["host"], "192.0.2.20")
        self.assertEqual(cfgmod.load()["host"], "192.0.2.20")
        self.assertFalse(list(Path(self._dir.name).glob("*.tmp")))


class BackoffTests(unittest.TestCase):
    def test_long_outage_stays_within_thirty_seconds(self) -> None:
        for attempt in (0, 1, 4, 5, 6, 1023, 1024, 10**6):
            delay = _backoff_delay(attempt)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 30)


class ClearLiveTests(unittest.TestCase):
    def test_host_change_forgets_the_previous_pumps(self) -> None:
        status = SpaStatus(host="192.0.2.10", pump1="high", current_temp=100, set_temp=102, connected=True)
        clear_live(status)
        self.assertFalse(status.connected)
        self.assertIsNone(status.current_temp)
        self.assertEqual(status.pump1, "off")
        self.assertEqual(status.host, "192.0.2.10")


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_wakes_backoff_onto_the_new_host(self) -> None:
        attempts: list[str] = []

        async def open_connection(host, port):
            attempts.append(host)
            raise OSError("refused")

        status = SpaStatus(host="192.0.2.10", port=4257, mode="configured_ip")
        client = SpaClient(status)
        with patch("asyncio.open_connection", open_connection):
            client.start()
            for _ in range(50):
                if attempts:
                    break
                await asyncio.sleep(0.02)
            status.host = "192.0.2.20"
            await client.reconnect()
            for _ in range(50):
                if "192.0.2.20" in attempts:
                    break
                await asyncio.sleep(0.02)
            await client.stop()
        self.assertIn("192.0.2.20", attempts)

    async def test_reconnect_cancels_a_stuck_handshake(self) -> None:
        started = asyncio.Event()

        async def open_connection(host, port):
            started.set()
            await asyncio.sleep(30)
            raise OSError("late")

        status = SpaStatus(host="192.0.2.10", port=4257, mode="configured_ip")
        client = SpaClient(status)
        with patch("asyncio.open_connection", open_connection):
            client.start()
            await asyncio.wait_for(started.wait(), timeout=2)
            began = asyncio.get_running_loop().time()
            await client.reconnect()
            await client.stop()
            elapsed = asyncio.get_running_loop().time() - began
        self.assertLess(elapsed, 2)

    async def test_connect_timeout_names_the_module(self) -> None:
        async def open_connection(host, port):
            raise TimeoutError()

        status = SpaStatus(host="192.0.2.10", port=4257, mode="configured_ip")
        client = SpaClient(status)
        with patch("asyncio.open_connection", open_connection), patch(
            "app.client._backoff_delay", return_value=30
        ):
            client.start()
            for _ in range(50):
                if status.last_error:
                    break
                await asyncio.sleep(0.02)
            await client.stop()
        self.assertEqual(status.last_error, "timed out connecting to 192.0.2.10:4257")

    async def test_retries_past_the_backoff_that_used_to_overflow(self) -> None:
        calls = 0
        seen: list[int] = []

        async def open_connection(host, port):
            nonlocal calls
            calls += 1
            raise OSError("refused")

        def spy(attempt: int) -> float:
            seen.append(attempt)
            return 0

        status = SpaStatus(host="192.0.2.10", port=4257, mode="configured_ip")
        client = SpaClient(status)
        with patch("asyncio.open_connection", open_connection), patch(
            "app.client._backoff_delay", spy
        ):
            client.start()
            for _ in range(400):
                if seen and seen[-1] >= 1024:
                    break
                await asyncio.sleep(0.01)
            await client.stop()
        self.assertGreaterEqual(seen[-1], 1024)
        self.assertGreater(calls, 1024)

    async def test_close_error_does_not_end_retries(self) -> None:
        calls = 0

        async def open_connection(host, port):
            nonlocal calls
            calls += 1
            raise OSError("refused")

        status = SpaStatus(host="192.0.2.10", port=4257, mode="configured_ip")
        client = SpaClient(status)

        async def boom() -> None:
            if calls == 1:
                raise RuntimeError("close blew up")

        client._close = boom  # type: ignore[method-assign]
        with patch("asyncio.open_connection", open_connection), patch(
            "app.client._backoff_delay", return_value=0
        ):
            client.start()
            deadline = asyncio.get_running_loop().time() + 4
            while asyncio.get_running_loop().time() < deadline:
                if calls >= 3:
                    break
                await asyncio.sleep(0.05)
            await client.stop()
        self.assertGreaterEqual(calls, 3)

    async def test_mock_setpoint_moves_and_rejects_wrap(self) -> None:
        status = SpaStatus(
            mode="mock", temp_min=80, temp_max=104, unit="F", current_temp=90, set_temp=90
        )
        client = SpaClient(status)
        client.start()
        await asyncio.sleep(0.05)
        with self.assertRaises(ValueError):
            await client.send_temp(300)
        await client.send_temp(100)
        self.assertEqual(status.set_temp, 100)
        before = status.current_temp or 0
        await asyncio.sleep(2.2)
        self.assertGreater(status.current_temp or 0, before)
        await client.stop()


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-09-28 is a Monday. Tests pin that so a calendar slip fails loudly.
    when = datetime(2026, 9, 28, hour, minute, tzinfo=timezone(timedelta(hours=-5))) + timedelta(days=day)
    assert when.weekday() == day
    return when


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.peak = windows_from_config(
            [{"start": "16:00", "end": "21:00", "days": [0, 1, 2, 3, 4]}]
        )
        self.night = windows_from_config([{"start": "22:00", "end": "06:00", "days": [0]}])

    def test_weekday_window_is_exclusive_at_the_end(self) -> None:
        window = self.peak[0]
        self.assertTrue(covers(window, _at(0, 16, 0)))
        self.assertTrue(covers(window, _at(0, 20, 59)))
        self.assertFalse(covers(window, _at(0, 15, 59)))
        self.assertFalse(covers(window, _at(0, 21, 0)))
        self.assertFalse(covers(window, _at(5, 18, 0)))

    def test_overnight_window_belongs_to_the_day_it_starts(self) -> None:
        window = self.night[0]
        self.assertTrue(covers(window, _at(0, 23, 0)))
        self.assertTrue(covers(window, _at(1, 5, 59)))
        self.assertFalse(covers(window, _at(0, 5, 0)))
        self.assertFalse(covers(window, _at(1, 22, 0)))
        self.assertFalse(covers(window, _at(1, 6, 0)))

    def test_summary_names_the_active_end_and_the_next_start(self) -> None:
        active = describe(True, self.peak, _at(0, 16, 30))
        self.assertEqual(
            active["summary"],
            "Expensive hours until 9:00 PM. Hold keeps the heater and pumps off.",
        )
        waiting = describe(True, self.peak, _at(0, 10, 0))
        self.assertEqual(waiting["summary"], "Next expensive hours 4:00 PM.")
        self.assertEqual(describe(False, self.peak, _at(0, 16, 30))["summary"], "")
        self.assertIn("no hours", describe(True, [], _at(0, 12))["summary"])

    def test_season_uses_the_month_the_window_started(self) -> None:
        summer = frozenset(range(5, 10))
        tz = timezone(timedelta(hours=-5))
        september = windows_from_config(
            [{"start": "22:00", "end": "06:00", "days": [2, 3]}]
        )[0]
        self.assertTrue(covers(september, datetime(2026, 9, 30, 23, 0, tzinfo=tz), summer))
        self.assertTrue(covers(september, datetime(2026, 10, 1, 5, 0, tzinfo=tz), summer))
        self.assertFalse(covers(september, datetime(2026, 10, 1, 23, 0, tzinfo=tz), summer))
        april = windows_from_config([{"start": "22:00", "end": "06:00", "days": [3, 4]}])[0]
        self.assertFalse(covers(april, datetime(2026, 4, 30, 23, 0, tzinfo=tz), summer))
        self.assertFalse(covers(april, datetime(2026, 5, 1, 5, 0, tzinfo=tz), summer))
        self.assertTrue(covers(april, datetime(2026, 5, 1, 23, 0, tzinfo=tz), summer))
        daytime = windows_from_config(
            [{"start": "16:00", "end": "21:00", "days": list(range(7))}]
        )[0]
        self.assertFalse(covers(daytime, datetime(2026, 4, 30, 18, 0, tzinfo=tz), summer))
        self.assertTrue(covers(daytime, datetime(2026, 5, 1, 18, 0, tzinfo=tz), summer))
        self.assertTrue(covers(daytime, datetime(2026, 9, 30, 18, 0, tzinfo=tz), summer))
        self.assertFalse(covers(daytime, datetime(2026, 10, 1, 18, 0, tzinfo=tz), summer))

    def test_winter_waits_until_the_next_season_and_names_the_date(self) -> None:
        summer = frozenset(range(5, 10))
        tz = timezone(timedelta(hours=-5))
        when = datetime(2026, 10, 2, 18, 0, tzinfo=tz)
        self.assertEqual(next_start(self.peak, when, summer), datetime(2027, 5, 3, 16, 0, tzinfo=tz))
        waiting = describe(True, self.peak, when, months=summer)
        self.assertFalse(waiting["active"])
        self.assertEqual(waiting["summary"], "Next expensive hours May 3, 4:00 PM.")
        in_season = describe(True, self.peak, _at(1, 10, 0), months=summer)
        self.assertEqual(in_season["summary"], "Next expensive hours 4:00 PM.")

    def test_out_of_season_does_not_hold_and_releases_one_it_owns(self) -> None:
        summer = frozenset(range(5, 10))
        when = datetime(2026, 10, 1, 18, 0, tzinfo=timezone(timedelta(hours=-5)))
        idle = HoldScheduler()
        self.assertEqual(
            idle.step(
                enabled=True, windows=self.peak, months=summer, when=when, actual_hold=False, fresh=True
            ),
            "none",
        )
        self.assertFalse(idle.owning)
        owned = HoldScheduler(owning=True)
        self.assertEqual(
            owned.step(
                enabled=True, windows=self.peak, months=summer, when=when, actual_hold=True, fresh=True
            ),
            "toggle",
        )
        self.assertFalse(owned.pending)
        manual = HoldScheduler()
        self.assertEqual(
            manual.step(
                enabled=True, windows=self.peak, months=summer, when=when, actual_hold=True, fresh=True
            ),
            "none",
        )
        self.assertFalse(manual.owning)

    def test_override_blocks_a_new_hold_and_releases_one_the_schedule_owns(self) -> None:
        until = _at(0, 17, 0)
        when = _at(0, 16, 30)
        idle = HoldScheduler()
        self.assertEqual(
            idle.step(
                enabled=True,
                windows=self.peak,
                when=when,
                actual_hold=False,
                fresh=True,
                override_until=until,
            ),
            "none",
        )
        self.assertFalse(idle.owning)
        owned = HoldScheduler(owning=True)
        self.assertEqual(
            owned.step(
                enabled=True,
                windows=self.peak,
                when=when,
                actual_hold=True,
                fresh=True,
                override_until=until,
            ),
            "toggle",
        )
        self.assertFalse(owned.pending)
        released = owned.step(
            enabled=True,
            windows=self.peak,
            when=_at(0, 16, 31),
            actual_hold=False,
            fresh=True,
            override_until=until,
        )
        self.assertEqual(released, "none")
        self.assertFalse(owned.owning)
        manual = HoldScheduler()
        self.assertEqual(
            manual.step(
                enabled=True,
                windows=self.peak,
                when=when,
                actual_hold=True,
                fresh=True,
                override_until=until,
            ),
            "none",
        )
        self.assertFalse(manual.owning)
        text = describe(True, self.peak, when, override_until=until)
        self.assertFalse(text["active"])
        self.assertEqual(
            text["summary"],
            "Using the tub until 5:00 PM. The schedule will not hold.",
        )

    def test_override_end_holds_again_only_while_the_window_is_on(self) -> None:
        resumed = HoldScheduler()
        self.assertEqual(
            resumed.step(
                enabled=True,
                windows=self.peak,
                when=_at(0, 17, 0),
                actual_hold=False,
                fresh=True,
                override_until=_at(0, 17, 0),
            ),
            "toggle",
        )
        self.assertTrue(resumed.owning)
        quiet = HoldScheduler()
        self.assertEqual(
            quiet.step(
                enabled=True,
                windows=self.peak,
                when=_at(0, 21, 0),
                actual_hold=False,
                fresh=True,
                override_until=_at(0, 21, 0),
            ),
            "none",
        )
        self.assertFalse(quiet.owning)
        self.assertEqual(override_deadline(_at(0, 16, 0), 40), _at(0, 16, 40))
        with self.assertRaises(ScheduleError):
            override_deadline(_at(0, 16, 0), 15)
        with self.assertRaises(ScheduleError):
            override_deadline(_at(0, 16, 0), True)

    def test_all_year_still_holds_in_january(self) -> None:
        scheduler = HoldScheduler()
        action = scheduler.step(
            enabled=True,
            windows=self.peak,
            when=datetime(2027, 1, 4, 16, 30, tzinfo=timezone(timedelta(hours=-6))),
            actual_hold=False,
            fresh=True,
        )
        self.assertEqual(action, "toggle")
        self.assertTrue(scheduler.owning)

    def test_rejects_a_zero_length_window_and_a_bad_day(self) -> None:
        with self.assertRaises(ScheduleError):
            normalize_windows([{"start": "16:00", "end": "16:00", "days": [0]}])
        with self.assertRaises(ScheduleError):
            normalize_windows([{"start": "16:00", "end": "17:00", "days": []}])
        self.assertEqual(
            normalize_windows([{"start": "16:00:00", "end": "21:00", "days": [1]}])[0]["start"],
            "16:00",
        )
        self.assertEqual(normalize_months(None), list(range(1, 13)))
        self.assertEqual(normalize_months([9, 5, 6, 5, 7, 8]), [9, 5, 6, 7, 8])
        with self.assertRaises(ScheduleError):
            normalize_months([])
        with self.assertRaises(ScheduleError):
            normalize_months([0, 5])
        with self.assertRaises(ScheduleError):
            normalize_months([True])

    def test_presses_hold_only_to_enter_and_claims_it(self) -> None:
        scheduler = HoldScheduler()
        action = scheduler.step(
            enabled=True, windows=self.peak, when=_at(0, 16, 30), actual_hold=False, fresh=True
        )
        self.assertEqual(action, "toggle")
        self.assertTrue(scheduler.owning)
        self.assertTrue(scheduler.pending)
        again = scheduler.step(
            enabled=True,
            windows=self.peak,
            when=_at(0, 16, 30) + timedelta(seconds=10),
            actual_hold=False,
            fresh=True,
        )
        self.assertEqual(again, "none")
        confirmed = scheduler.step(
            enabled=True, windows=self.peak, when=_at(0, 16, 31), actual_hold=True, fresh=True
        )
        self.assertEqual(confirmed, "none")
        self.assertTrue(scheduler.owning)
        self.assertIsNone(scheduler.pending)

    def test_a_hold_already_on_is_not_claimed_or_cleared(self) -> None:
        scheduler = HoldScheduler()
        entered = scheduler.step(
            enabled=True, windows=self.peak, when=_at(0, 16, 0), actual_hold=True, fresh=True
        )
        self.assertEqual(entered, "none")
        self.assertFalse(scheduler.owning)
        ended = scheduler.step(
            enabled=True, windows=self.peak, when=_at(0, 21, 0), actual_hold=True, fresh=True
        )
        self.assertEqual(ended, "none")
        self.assertFalse(scheduler.owning)

    def test_releases_only_a_hold_the_schedule_owns(self) -> None:
        scheduler = HoldScheduler(owning=True)
        action = scheduler.step(
            enabled=True, windows=self.peak, when=_at(0, 21, 0), actual_hold=True, fresh=True
        )
        self.assertEqual(action, "toggle")
        self.assertFalse(scheduler.pending)
        done = scheduler.step(
            enabled=True, windows=self.peak, when=_at(0, 21, 1), actual_hold=False, fresh=True
        )
        self.assertEqual(done, "none")
        self.assertFalse(scheduler.owning)

    def test_turning_the_schedule_off_releases_its_hold(self) -> None:
        scheduler = HoldScheduler(owning=True)
        action = scheduler.step(
            enabled=False, windows=self.peak, when=_at(0, 18, 0), actual_hold=True, fresh=True
        )
        self.assertEqual(action, "toggle")
        self.assertFalse(scheduler.pending)

    def test_a_missed_press_does_not_turn_hold_on_by_releasing(self) -> None:
        scheduler = HoldScheduler()
        scheduler.step(
            enabled=True, windows=self.peak, when=_at(0, 16, 0), actual_hold=False, fresh=True
        )
        later = scheduler.step(
            enabled=True,
            windows=self.peak,
            when=_at(0, 21, 1),
            actual_hold=False,
            fresh=True,
        )
        self.assertEqual(later, "none")
        self.assertFalse(scheduler.owning)

    def test_stale_status_does_not_press_hold(self) -> None:
        scheduler = HoldScheduler()
        action = scheduler.step(
            enabled=True, windows=self.peak, when=_at(0, 16, 0), actual_hold=False, fresh=False
        )
        self.assertEqual(action, "none")
        self.assertFalse(scheduler.owning)


class ScheduleConfigTests(ConfigTests):
    def test_settings_save_keeps_windows_and_ownership(self) -> None:
        cfgmod.save(
            {
                "host": "192.0.2.10",
                "tou_enabled": True,
                "tou_windows": [{"start": "16:00", "end": "21:00", "days": [0, 1, 2, 3, 4]}],
                "tou_months": [5, 6, 7, 8, 9],
            }
        )
        cfgmod.write_tou_owning(True)
        saved = cfgmod.save({"host": "192.0.2.20"})
        self.assertEqual(saved["host"], "192.0.2.20")
        self.assertTrue(saved["tou_enabled"])
        self.assertEqual(saved["tou_windows"][0]["end"], "21:00")
        self.assertEqual(saved["tou_months"], [5, 6, 7, 8, 9])
        self.assertTrue(cfgmod.read_tou_owning())

    def test_a_file_without_months_stays_in_effect_all_year(self) -> None:
        cfgmod.save({"host": "192.0.2.10", "tou_enabled": True})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        saved.pop("tou_months")
        self.path.write_text(json.dumps(saved), encoding="utf-8")
        self.assertEqual(cfgmod.load()["tou_months"], list(range(1, 13)))

    def test_owning_and_soak_timer_do_not_erase_each_other(self) -> None:
        until = datetime(2026, 9, 29, 18, 0, tzinfo=timezone(timedelta(hours=-5)))
        cfgmod.write_tou_override(until)
        cfgmod.write_tou_owning(True)
        self.assertTrue(cfgmod.read_tou_owning())
        self.assertEqual(cfgmod.read_tou_override(), until)
        cfgmod.write_tou_override(None)
        self.assertIsNone(cfgmod.read_tou_override())
        self.assertTrue(cfgmod.read_tou_owning())
        state = self.path.with_suffix(".tou.json")
        state.write_text('{"owning": true, "override_until": "nope"}\n', encoding="utf-8")
        self.assertIsNone(cfgmod.read_tou_override())
        self.assertTrue(cfgmod.read_tou_owning())

    def test_a_bad_window_does_not_replace_the_file(self) -> None:
        cfgmod.save({"host": "192.0.2.10", "tou_windows": []})
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.save({"tou_windows": [{"start": "10:00", "end": "10:00", "days": [0]}]})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.save({"tou_months": []})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
