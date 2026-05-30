from homeassistant.components.light import ColorMode, LightEntity, ATTR_BRIGHTNESS
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up C-Bus light entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    lighting_map = data["lighting_map"]

    async_add_entities(
        CBusLightEntity(coordinator, ga, name)
        for ga, name in lighting_map.items()
    )

class CBusLightEntity(CoordinatorEntity, LightEntity):
    """Representation of an individual DALI/Dimmer C-Bus Light Group Address."""

    def __init__(self, coordinator, ga, name):
        """Initialize the light entity."""
        super().__init__(coordinator)
        self.ga = ga
        self._attr_name = name
        self._attr_unique_id = f"cbus_light_{ga}"
        
        # Expose dimming/brightness features to the Home Assistant Frontend
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._attr_color_mode = ColorMode.BRIGHTNESS

    @property
    def is_on(self) -> bool:
        """Return true if the group address state is active."""
        ga_data = self.coordinator.states.get(self.ga, False)
        # Robust check: handle both dict states and raw boolean fallbacks
        if isinstance(ga_data, dict):
            return ga_data.get("state", False)
        return bool(ga_data)

    @property
    def brightness(self) -> int | None:
        """Return the current brightness level of the light (0-255)."""
        ga_data = self.coordinator.states.get(self.ga, False)
        # Robust check: handle both dict states and raw boolean fallbacks
        if isinstance(ga_data, dict):
            return ga_data.get("brightness", 0)
        return 255 if bool(ga_data) else 0

    async def async_turn_on(self, **kwargs) -> None:
        """Instruct C-Bus network group address to turn ON or set a brightness level."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        
        if brightness is not None:
            # Set explicit dimming level
            await self.coordinator.send_command(self.ga, True, brightness=brightness)
        else:
            # Default ON
            await self.coordinator.send_command(self.ga, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Instruct C-Bus network group address to turn OFF."""
        await self.coordinator.send_command(self.ga, False)