"""Dependency-free tests for the C-Bus Native options flow."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


class _Schema:
    def __init__(self, schema) -> None:
        self.schema = schema


class _Required:
    def __init__(self, key, default=None) -> None:
        self.key = key
        self.default = default

    def __hash__(self) -> int:
        return hash((self.key, self.default))


class _ConfigFlow:
    def __init_subclass__(cls, *, domain=None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        cls.domain = domain


class _OptionsFlowWithReload:
    def add_suggested_values_to_schema(self, schema, suggested_values):
        return schema, suggested_values

    def async_create_entry(self, *, data):
        return {"type": "create_entry", "data": data}

    def async_show_form(self, *, step_id, data_schema, errors=None):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
        }


class _NumberSelectorMode:
    BOX = "box"


class _NumberSelectorConfig(dict):
    pass


class _NumberSelector:
    def __init__(self, config) -> None:
        self.config = config


def _load_config_flow_module():
    """Load config_flow with minimal Home Assistant and voluptuous doubles."""
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Schema = _Schema
    voluptuous.Required = _Required
    voluptuous.In = lambda choices: choices

    homeassistant = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlow = _ConfigFlow
    config_entries.OptionsFlowWithReload = _OptionsFlowWithReload
    homeassistant.config_entries = config_entries

    homeassistant_core = types.ModuleType("homeassistant.core")
    homeassistant_core.HomeAssistant = object
    homeassistant_core.callback = lambda function: function

    homeassistant_helpers = types.ModuleType("homeassistant.helpers")
    selector = types.ModuleType("homeassistant.helpers.selector")
    selector.NumberSelector = _NumberSelector
    selector.NumberSelectorConfig = _NumberSelectorConfig
    selector.NumberSelectorMode = _NumberSelectorMode
    homeassistant_helpers.selector = selector

    sys.modules["voluptuous"] = voluptuous
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = homeassistant_core
    sys.modules["homeassistant.helpers"] = homeassistant_helpers
    sys.modules["homeassistant.helpers.selector"] = selector

    package = sys.modules.get("cbus_native")
    if package is None:
        package = types.ModuleType("cbus_native")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules["cbus_native"] = package

    return importlib.import_module("cbus_native.config_flow")


config_flow_module = _load_config_flow_module()


class _FakeConfigEntry:
    def __init__(self, *, data=None, options=None) -> None:
        self.data = data or {}
        self.options = options or {}


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_default_ramp_selector_is_four_seconds(self) -> None:
        self.assertEqual(config_flow_module.DEFAULT_TRANSITION, 4.0)
        self.assertEqual(
            config_flow_module.DEFAULT_TRANSITION_SELECTOR.config,
            {
                "min": 0,
                "max": 1020.0,
                "step": 1,
                "mode": "box",
                "unit_of_measurement": "s",
            },
        )

    async def test_options_flow_suggests_current_setting(self) -> None:
        flow = config_flow_module.CBusNativeOptionsFlow()
        flow.config_entry = _FakeConfigEntry(
            data={"default_transition": 4.0},
            options={"default_transition": 8.0},
        )

        result = await flow.async_step_init()

        self.assertEqual(result["type"], "form")
        _, suggested_values = result["data_schema"]
        self.assertEqual(suggested_values, {"default_transition": 8.0})

    async def test_options_flow_saves_zero_to_disable_default_ramp(self) -> None:
        flow = config_flow_module.CBusNativeOptionsFlow()
        flow.config_entry = _FakeConfigEntry()

        result = await flow.async_step_init({"default_transition": 0.0})

        self.assertEqual(
            result,
            {"type": "create_entry", "data": {"default_transition": 0.0}},
        )


if __name__ == "__main__":
    unittest.main()
