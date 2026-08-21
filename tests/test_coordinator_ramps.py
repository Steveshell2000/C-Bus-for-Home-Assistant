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


def _received_frame(*data: int) -> str:
    checksum = (-sum(data)) & 0xFF
    return "".join(f"{byte:02X}" for byte in (*data, checksum))


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


if __name__ == "__main__":
    unittest.main()
