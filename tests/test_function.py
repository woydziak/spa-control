"""Behavior the tub depends on: frames, setpoints, settings, reconnect."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config as cfgmod
from app.client import SpaClient
from app.protocol import FrameAssembler, build_frame, encode_setpoint, set_temp_frame
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


if __name__ == "__main__":
    unittest.main()
