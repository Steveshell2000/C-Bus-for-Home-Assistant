"""Light entity tests for unanswered legacy-gateway startup state."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest


class _CoordinatorEntity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self.coordinator_added = False

    async def async_added_to_hass(self) -> None:
        self.coordinator_added = True


class _RestoreEntity:
    async def async_get_last_state(self):
        return self._last_state


class _LightEntity:
    pass


class _ColorMode:
    BRIGHTNESS = "brightness"


class _LightEntityFeature:
    TRANSITION = 1


class _DeviceInfo(dict):
    pass


def _load_light_module():
    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    light_component = types.ModuleType("homeassistant.components.light")
    light_component.ATTR_BRIGHTNESS = "brightness"
    light_component.ATTR_TRANSITION = "transition"
    light_component.ColorMode = _ColorMode
    light_component.LightEntity = _LightEntity
    light_component.LightEntityFeature = _LightEntityFeature

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    const = types.ModuleType("homeassistant.const")
    const.STATE_OFF = "off"
    const.STATE_ON = "on"
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object

    helpers = types.ModuleType("homeassistant.helpers")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = _DeviceInfo
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object
    restore_state = types.ModuleType("homeassistant.helpers.restore_state")
    restore_state.RestoreEntity = _RestoreEntity
    update_coordinator = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )
    update_coordinator.CoordinatorEntity = _CoordinatorEntity

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.light": light_component,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.entity_platform": entity_platform,
        "homeassistant.helpers.restore_state": restore_state,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }
    sys.modules.update(modules)

    package = sys.modules.get("cbus_native")
    if package is None:
        package = types.ModuleType("cbus_native")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules["cbus_native"] = package
    package.DOMAIN = "cbus_native"
    sys.modules.pop("cbus_native.light", None)
    return importlib.import_module("cbus_native.light")


light_module = _load_light_module()
CBusLightEntity = light_module.CBusLightEntity


class _State:
    def __init__(self, state: str, brightness=None) -> None:
        self.state = state
        self.attributes = {}
        if brightness is not None:
            self.attributes["brightness"] = brightness


class _Coordinator:
    def __init__(self, assumed: bool = True) -> None:
        self.data = {1: {"state": False, "brightness": 0}}
        self.assumed_state_groups = {1} if assumed else set()
        self.restored: list[tuple[int, int]] = []

    def restore_assumed_level(self, ga: int, brightness: int) -> bool:
        self.restored.append((ga, brightness))
        self.data[ga] = {"state": brightness > 0, "brightness": brightness}
        return True


class LightRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_unanswered_group_restores_last_partial_level_as_assumed(self) -> None:
        coordinator = _Coordinator()
        entity = CBusLightEntity(coordinator, 1, "DALI light", object())
        entity._last_state = _State("on", 129)

        await entity.async_added_to_hass()

        self.assertEqual(coordinator.restored, [(1, 129)])
        self.assertEqual(entity.brightness, 129)
        self.assertTrue(entity.is_on)
        self.assertTrue(entity.assumed_state)
        self.assertTrue(entity.coordinator_added)

    async def test_live_group_is_not_replaced_by_restored_state(self) -> None:
        coordinator = _Coordinator(assumed=False)
        coordinator.data[1] = {"state": True, "brightness": 200}
        entity = CBusLightEntity(coordinator, 1, "Native dimmer", object())
        entity._last_state = _State("off")

        await entity.async_added_to_hass()

        self.assertFalse(coordinator.restored)
        self.assertEqual(entity.brightness, 200)
        self.assertFalse(entity.assumed_state)

    async def test_unknown_restore_state_is_ignored(self) -> None:
        coordinator = _Coordinator()
        entity = CBusLightEntity(coordinator, 1, "DALI light", object())
        entity._last_state = _State("unknown")

        await entity.async_added_to_hass()

        self.assertFalse(coordinator.restored)


if __name__ == "__main__":
    unittest.main()
