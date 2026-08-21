from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up C-Bus light entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    lighting_map = data.get("lighting_map") or data.get("cgl_map", {})

    async_add_entities(
        CBusLightEntity(coordinator, ga, name, entry)
        for ga, name in lighting_map.items()
    )


class CBusLightEntity(CoordinatorEntity, LightEntity):
    """Representation of a C-Bus Lighting group with native ramp support."""

    def __init__(self, coordinator, ga, name, entry: ConfigEntry):
        """Initialize the light entity."""
        super().__init__(coordinator)
        self.ga = ga
        self._attr_name = name
        self._attr_unique_id = f"cbus_light_{ga}"
        self._entry = entry

        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._attr_color_mode = ColorMode.BRIGHTNESS
        self._attr_supported_features = LightEntityFeature.TRANSITION

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
        """Turn on or ramp a C-Bus lighting group to a brightness level."""
        await self.coordinator.send_command(
            self.ga,
            True,
            brightness=kwargs.get(ATTR_BRIGHTNESS),
            transition=kwargs.get(ATTR_TRANSITION),
        )

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off or ramp a C-Bus lighting group to zero."""
        await self.coordinator.send_command(
            self.ga,
            False,
            transition=kwargs.get(ATTR_TRANSITION),
        )
