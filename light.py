from homeassistant.components.light import ColorMode, LightEntity
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
    """Representation of an individual C-Bus Light Group Address."""

    def __init__(self, coordinator, ga, name):
        """Initialize the light entity."""
        super().__init__(coordinator)
        self.ga = ga
        self._attr_name = name
        self._attr_unique_id = f"cbus_light_{ga}"
        
        # Declare supported color modes to satisfy core registration validation rules
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_color_mode = ColorMode.ONOFF

    @property
    def is_on(self) -> bool:
        """Return true if the group address state is active."""
        return self.coordinator.states.get(self.ga, False)

    async def async_turn_on(self, **kwargs) -> None:
        """Instruct C-Bus network group address to turn ON."""
        await self.coordinator.send_command(self.ga, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Instruct C-Bus network group address to turn OFF."""
        await self.coordinator.send_command(self.ga, False)