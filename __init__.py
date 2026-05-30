import json
import os
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .coordinator import CBusCoordinator

_LOGGER = logging.getLogger(__name__)
DOMAIN = "cbus_native"

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up C-Bus Native from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    cgl_filename = entry.data.get("cgl_filename", "project.cgl")
    cgl_path = os.path.join(os.path.dirname(__file__), cgl_filename)

    if not os.path.exists(cgl_path):
        _LOGGER.error("C-Bus configuration file missing! Please place your project file into: %s", cgl_path)
        return False

    try:
        with open(cgl_path, "r") as f:
            cgl_data = json.load(f)
    except Exception as err:
        _LOGGER.error("Failed to parse CGL JSON file: %s", err)
        return False

    lighting_map = {}
    
    # Diagnostic telemetry tracking
    networks = cgl_data.get("networks", [])
    _LOGGER.info("CGL Parser: Scanning %d network(s) inside %s", len(networks), cgl_filename)

    for network in networks:
        for app in network.get("applications", []):
            # Casting to string handles both integer 56 and string "56" layout structures safely
            if str(app.get("address")) == "56":  
                groups = app.get("groups", []):
                _LOGGER.info("CGL Parser: Found Lighting Application (56) containing %d group addresses.", len(groups))
                for group in groups:
                    try:
                        ga = int(group["address"])
                        lighting_map[ga] = group["name"]
                    except (KeyError, ValueError) as err:
                        _LOGGER.warning("CGL Parser: Skipping invalid group item %s: %s", group, err)

    _LOGGER.info("CGL Parser: Successfully compiled %d lighting entries.", len(lighting_map))

    if not lighting_map:
        _LOGGER.error("CGL Parser: Completed with 0 entities mapped. Verify your .cgl key structure names matches 'networks' -> 'applications' -> 'groups'.")

    host = entry.data.get("host", "192.168.1.20")
    port = entry.data.get("port", 10001)

    coordinator = CBusCoordinator(hass, host, port, lighting_map)
    await coordinator.connect()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "lighting_map": lighting_map
    }

    await hass.config_entries.async_forward_entry_setups(entry, ["light"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry safely without crashing on missing keys."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["light"])
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data and "coordinator" in data:
            await data["coordinator"].disconnect()
    return unload_ok