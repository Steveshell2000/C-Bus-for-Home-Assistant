"""Config and options flows for C-Bus Native."""

import logging
import os

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DEFAULT_TRANSITION,
    DEFAULT_TRANSITION,
    DOMAIN,
    MAX_TRANSITION,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TRANSITION_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=MAX_TRANSITION,
        step=1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="s",
    )
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_DEFAULT_TRANSITION, default=DEFAULT_TRANSITION
        ): DEFAULT_TRANSITION_SELECTOR,
    }
)


class CBusNativeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle C-Bus Native setup through the Home Assistant UI."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Collect CNI, project-file, and default-ramp settings."""
        errors = {}

        dir_path = os.path.dirname(__file__)
        cgl_files = await self.hass.async_add_executor_job(
            lambda: [name for name in os.listdir(dir_path) if name.endswith(".cgl")]
        )

        if user_input is not None:
            selected_file = user_input["cgl_filename"]
            if selected_file in cgl_files or os.path.exists(
                os.path.join(dir_path, selected_file)
            ):
                return self.async_create_entry(
                    title=f"C-Bus Network ({user_input['host']})", data=user_input
                )
            errors["base"] = "cgl_not_found"

        cgl_selector = (
            vol.In(cgl_files) if cgl_files else str
        )
        cgl_default = cgl_files[0] if cgl_files else "project.cgl"
        if not cgl_files:
            errors["base"] = "missing_cgl_files"

        data_schema = vol.Schema(
            {
                vol.Required("host", default="192.168.1.20"): str,
                vol.Required("port", default=10001): int,
                vol.Required("cgl_filename", default=cgl_default): cgl_selector,
                vol.Required(
                    CONF_DEFAULT_TRANSITION, default=DEFAULT_TRANSITION
                ): DEFAULT_TRANSITION_SELECTOR,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the editable options flow for an existing integration entry."""
        return CBusNativeOptionsFlow()


class CBusNativeOptionsFlow(config_entries.OptionsFlowWithReload):
    """Edit C-Bus lighting behavior and reload the entry after saving."""

    async def async_step_init(self, user_input=None):
        """Edit the default Home Assistant ramp duration."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_transition = self.config_entry.options.get(
            CONF_DEFAULT_TRANSITION,
            self.config_entry.data.get(CONF_DEFAULT_TRANSITION, DEFAULT_TRANSITION),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                {CONF_DEFAULT_TRANSITION: current_transition},
            ),
        )
