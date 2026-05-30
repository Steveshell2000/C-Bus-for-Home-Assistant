import os
import voluptuous as vol
from homeassistant import config_entries
import logging

DOMAIN = "cbus_native"
_LOGGER = logging.getLogger(__name__)

class CBusNativeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle installation onboarding requests through frontend wizardry."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        
        # Scan folder for available .cgl configuration files
        dir_path = os.path.dirname(__file__)
        cgl_files = await self.hass.async_add_executor_job(
            lambda: [f for f in os.listdir(dir_path) if f.endswith('.cgl')]
        )

        if user_input is not None:
            selected_file = user_input["cgl_filename"]
            if selected_file in cgl_files or os.path.exists(os.path.join(dir_path, selected_file)):
                return self.async_create_entry(title=f"C-Bus Network ({user_input['host']})", data=user_input)
            else:
                errors["base"] = "cgl_not_found"

        # Build dynamic UI schema
        if cgl_files:
            DATA_SCHEMA = vol.Schema({
                vol.Required("host", default="192.168.1.20"): str,
                vol.Required("port", default=10001): int,
                vol.Required("cgl_filename", default=cgl_files[0]): vol.In(cgl_files),
            })
        else:
            errors["base"] = "missing_cgl_files"
            DATA_SCHEMA = vol.Schema({
                vol.Required("host", default="192.168.1.20"): str,
                vol.Required("port", default=10001): int,
                vol.Required("cgl_filename", default="project.cgl"): str,
            })

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )