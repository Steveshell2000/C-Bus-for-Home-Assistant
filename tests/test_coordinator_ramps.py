"""Coordinator tests for smooth ramp state and native HA transitions."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest


class _FakeDataUpdateCoordinator:
    def __init__(self, hass, logger, *, name: str) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.data = None

    def async_set_updated_data(self, data) -> None:
        self.data = data


def _load_coordinator_module():
    """Load the integration module with minimal Home Assistant test doubles."""
    homeassistant = types.ModuleType("homeassistant")
    homeassistant_core = types.ModuleType("homeassistant.core")
    homeassistant_helpers = types.ModuleType("homeassistant.helpers")
    update_coordinator = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )
    homeassistant_core.HomeAssistant = object
    update_coordinator.DataUpdateCoordinator = _FakeDataUpdateCoordinator

    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.core", homeassistant_core)
    sys.modules.setdefault("homeassistant.helpers", homeassistant_helpers)
    sys.modules.setdefault(
        "homeassistant.helpers.update_coordinator", update_coordinator
    )

    package = types.ModuleType("cbus_native")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules.setdefault("cbus_native", package)
    return importlib.import_module("cbus_native.coordinator")


coordinator_module = _load_coordinator_module()
CBusCoordinator = coordinator_module.CBusCoordinator
protocol_module = importlib.import_module("cbus_native.protocol")


def _received_frame(*data: int) -> str:
    checksum = (-sum(data)) & 0xFF
    return "".join(f"{byte:02X}" for byte in (*data, checksum))


def _level_status_frame(start_group: int, levels: list[int]) -> str:
    data = [0x86, 0xFE, 0xFE, 0x00, 0xF7, 0x07, 0x38, start_group]
    for level in levels:
        data.extend(
            (
                protocol_module.LEVEL_STATUS_NIBBLE_CODES[level & 0x0F],
                protocol_module.LEVEL_STATUS_NIBBLE_CODES[level >> 4],
            )
        )
    return _received_frame(*data)


class _FakeHass:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()


class _FakeWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def drain(self) -> None:
        return None


class CoordinatorRampTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.coordinator = CBusCoordinator(
            _FakeHass(), "192.0.2.1", 10001, {1: "Test light"}
        )

    async def asyncTearDown(self) -> None:
        self.coordinator._cancel_ramp(1)
        await asyncio.sleep(0)

    async def test_incoming_ramp_tracks_progress_instead_of_jumping(self) -> None:
        frame = _received_frame(0x05, 0x23, 0x38, 0x00, 0x0A, 0x01, 0xFF)

        self.assertTrue(self.coordinator._process_event_update(frame))
        self.assertEqual(self.coordinator.states[1]["brightness"], 0)
        self.assertIn(1, self.coordinator._active_ramps)

        self.coordinator._active_ramps[1]["started_at"] = (
            self.coordinator.hass.loop.time() - 2
        )
        self.assertAlmostEqual(
            self.coordinator._estimated_ramp_level(1), 128, delta=1
        )

    async def test_terminate_ramp_holds_estimated_current_level(self) -> None:
        ramp = _received_frame(0x05, 0x23, 0x38, 0x00, 0x0A, 0x01, 0xFF)
        terminate = _received_frame(0x05, 0x23, 0x38, 0x00, 0x09, 0x01)
        self.coordinator._process_event_update(ramp)
        self.coordinator._active_ramps[1]["started_at"] = (
            self.coordinator.hass.loop.time() - 2
        )

        self.assertTrue(self.coordinator._process_event_update(terminate))
        self.assertAlmostEqual(
            self.coordinator.states[1]["brightness"], 128, delta=1
        )
        self.assertNotIn(1, self.coordinator._active_ramps)

    async def test_ha_transition_sends_scaled_cbus_ramp_command(self) -> None:
        writer = _FakeWriter()
        self.coordinator.writer = writer
        self.coordinator.states[1] = {"state": True, "brightness": 127}

        await self.coordinator.send_command(
            1, True, brightness=255, transition=4
        )

        self.assertEqual(writer.frames, [b"\\0538001201FFB1g\r"])
        self.assertIn(1, self.coordinator._active_ramps)

    async def test_default_transition_is_used_when_ha_omits_it(self) -> None:
        writer = _FakeWriter()
        self.coordinator.writer = writer

        await self.coordinator.send_command(1, True, brightness=255)

        self.assertEqual(writer.frames, [b"\\0538000A01FFB9g\r"])
        self.assertIn(1, self.coordinator._active_ramps)

    async def test_explicit_zero_transition_overrides_default(self) -> None:
        writer = _FakeWriter()
        self.coordinator.writer = writer

        await self.coordinator.send_command(
            1, True, brightness=255, transition=0
        )

        self.assertEqual(writer.frames, [b"\\0538000201FFC1g\r"])
        self.assertNotIn(1, self.coordinator._active_ramps)

    async def test_terminal_ramp_level_survives_delayed_mmi_off(self) -> None:
        ramp = _received_frame(0x05, 0x23, 0x38, 0x00, 0x0A, 0x01, 0xFF)
        mmi_off = _received_frame(0xD8, 0x38, 0x00, 0x08)

        self.assertTrue(self.coordinator._process_event_update(ramp))
        self.coordinator._active_ramps[1]["duration"] = 0.001
        self.coordinator._active_ramps[1]["started_at"] = (
            self.coordinator.hass.loop.time() - 1
        )
        await asyncio.sleep(0.02)

        self.assertNotIn(1, self.coordinator._active_ramps)
        self.assertEqual(self.coordinator.states[1]["brightness"], 255)
        self.coordinator._process_mmi_response(mmi_off)
        self.assertEqual(self.coordinator.states[1]["brightness"], 255)

    async def test_mmi_nonexistent_and_error_codes_do_not_mean_off(self) -> None:
        self.coordinator.states[1] = {"state": True, "brightness": 200}
        nonexistent = _received_frame(0xD8, 0x38, 0x00, 0x00)
        error = _received_frame(0xD8, 0x38, 0x00, 0x0C)

        self.coordinator._process_mmi_response(nonexistent)
        self.coordinator._process_mmi_response(error)

        self.assertEqual(
            self.coordinator.states[1], {"state": True, "brightness": 200}
        )

    async def test_exact_level_response_rebuilds_brightness(self) -> None:
        self.coordinator._initial_status_pending = {0}
        levels = [0] * 32
        levels[1] = 137

        self.assertTrue(
            self.coordinator._process_level_status_response(
                _level_status_frame(0, levels)
            )
        )

        self.assertEqual(
            self.coordinator.states[1], {"state": True, "brightness": 137}
        )
        self.assertFalse(self.coordinator._initial_status_pending)
        self.assertTrue(self.coordinator._initial_status_event.is_set())

    async def test_startup_sync_requests_only_needed_status_blocks(self) -> None:
        coordinator = CBusCoordinator(
            _FakeHass(),
            "192.0.2.1",
            10001,
            {1: "One", 2: "Two", 33: "Thirty-three", 200: "Two hundred"},
        )
        writer = _FakeWriter()
        coordinator.writer = writer
        coordinator.is_connected = True
        original_interval = coordinator_module.STATUS_REQUEST_INTERVAL
        coordinator_module.STATUS_REQUEST_INTERVAL = 0
        try:
            sync_task = asyncio.create_task(coordinator._sync_initial_levels())
            for _ in range(10):
                if len(writer.frames) == 3:
                    break
                await asyncio.sleep(0)

            expected_starts = (0, 0x20, 0xC0)
            self.assertEqual(
                writer.frames,
                [
                    protocol_module.build_level_status_request(start).encode("ascii")
                    for start in expected_starts
                ],
            )

            for start in expected_starts:
                levels = [0] * 32
                if start == 0:
                    levels[1] = 50
                    levels[2] = 75
                elif start == 0x20:
                    levels[1] = 100
                else:
                    levels[8] = 125
                coordinator._process_level_status_response(
                    _level_status_frame(start, levels)
                )
            await sync_task
        finally:
            coordinator_module.STATUS_REQUEST_INTERVAL = original_interval

        self.assertTrue(coordinator.initial_sync_complete)
        self.assertEqual(coordinator.states[1]["brightness"], 50)
        self.assertEqual(coordinator.states[2]["brightness"], 75)
        self.assertEqual(coordinator.states[33]["brightness"], 100)
        self.assertEqual(coordinator.states[200]["brightness"], 125)


if __name__ == "__main__":
    unittest.main()
