from homeassistant.components.light import ColorMode, LightEntity, ATTR_BRIGHTNESS
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up C-Bus light entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    
    # Safely look up map arrays using brackets
    lighting_map = data.get("lighting_map") or data.get("cgl_map", {})

    async_add_entities(
        CBusLightEntity(coordinator, ga, name, entry)
        for ga, name in lighting_map.items()
    )

class CBusLightEntity(CoordinatorEntity, LightEntity):
    """Representation of an individual C-Bus Light Group Address with dimming capabilities."""

    def __init__(self, coordinator, ga, name, entry: ConfigEntry):
        """Initialize the light entity."""
        super().__init__(coordinator)
        self.ga = ga
        self._attr_name = name
        self._attr_unique_id = f"cbus_light_{ga}"
        self._entry = entry
        
        # Declare explicit support for Brightness (dimming sliders in Home Assistant UI)
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._attr_color_mode = ColorMode.BRIGHTNESS

    @property
    def device_info(self) -> DeviceInfo:
        """Link this entity to a parent C-Bus Gateway device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="C-Bus Local Gateway",
            manufacturer="Clipsal",
            model="Wiser MKII / CNI",
            sw_version="1.0.1",
        )

    @property
    def is_on(self) -> bool:
        """Return true if the group address state is active."""
        # Read directly from the coordinator's updated data dictionary
        if self.coordinator.data and self.ga in self.coordinator.data:
            ga_data = self.coordinator.data[self.ga]
            if isinstance(ga_data, dict):
                return ga_data.get("state", False)
            return bool(ga_data)
        return False

    @property
    def brightness(self) -> int | None:
        """Return the current brightness level of the light (0-255)."""
        if self.coordinator.data and self.ga in self.coordinator.data:
            ga_data = self.coordinator.data[self.ga]
            if isinstance(ga_data, dict):
                return ga_data.get("brightness", 0)
            return 255 if bool(ga_data) else 0
        return 0

    async def async_turn_on(self, **kwargs) -> None:
        """Instruct C-Bus network group address to turn ON or set a brightness level."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        
        if brightness is not None:
            # Send set explicit level
            await self.coordinator.send_command(self.ga, True, brightness=brightness)
        else:
            # Default Recall / ON
            await self.coordinator.send_command(self.ga, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Instruct C-Bus network group address to turn OFF."""
        await self.coordinator.send_command(self.ga, False)