import json
import os
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .coordinator import CBusCoordinator

_LOGGER = logging.getLogger(__name__)
DOMAIN = "cbus_native"

def load_cgl_file(cgl_path):
    """Synchronous file loading run safely inside an executor thread."""
    with open(cgl_path, "r") as f:
        return json.load(f)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up C-Bus Native from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    cgl_filename = entry.data.get("cgl_filename", "project.cgl")
    cgl_path = os.path.join(os.path.dirname(__file__), cgl_filename)

    if not os.path.exists(cgl_path):
        _LOGGER.error("C-Bus configuration file missing! Please place your project file into: %s", cgl_path)
        return False

    try:
        # Offload file IO to the executor to prevent freezing the main asynchronous event loop
        cgl_data = await hass.async_add_executor_job(load_cgl_file, cgl_path)
    except Exception as err:
        _LOGGER.error("Failed to parse CGL JSON file: %s", err)
        return False

    lighting_map = {}
    networks = cgl_data.get("networks", [])
    _LOGGER.info("CGL Parser: Scanning %d network(s) inside %s", len(networks), cgl_filename)

    for network in networks:
        for app in network.get("applications", []):
            if str(app.get("address")) == "56":  # Lighting Application (56)
                groups = app.get("groups", [])
                _LOGGER.info("CGL Parser: Found Lighting Application (56) containing %d group addresses.", len(groups))
                for group in groups:
                    try:
                        ga = int(group["address"])
                        lighting_map[ga] = group["name"]
                    except (KeyError, ValueError) as err:
                        _LOGGER.warning("CGL Parser: Skipping invalid group item %s: %s", group, err)

    _LOGGER.info("CGL Parser: Successfully compiled %d lighting entries.", len(lighting_map))

    if not lighting_map:
        _LOGGER.error("CGL Parser: Completed with 0 entities mapped. Verify your .cgl key structure names match 'networks' -> 'applications' -> 'groups'.")

    host = entry.data.get("host", "192.168.1.20")
    port = entry.data.get("port", 10001)

    coordinator = CBusCoordinator(hass, host, port, lighting_map)
    await coordinator.connect()

    # Expose both key naming patterns to guarantee backwards compatibility
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "lighting_map": lighting_map,
        "cgl_map": lighting_map
    }

    await hass.config_entries.async_forward_entry_setups(entry, ["light"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry safely."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["light"])
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data and "coordinator" in data:
            await data["coordinator"].disconnect()
    return unload_ok