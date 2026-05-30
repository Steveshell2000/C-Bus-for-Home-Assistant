from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.helpers.update_coordinator import CoordinatorEntity

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data["cbus_native"][entry.entry_id]
    entities = []
    for ga, name in coordinator.cgl_map.items():
        entities.append(CBusLight(coordinator, ga, name))
    async_add_entities(entities)

class CBusLight(CoordinatorEntity, LightEntity):
    def __init__(self, coordinator, ga, name):
        super().__init__(coordinator)
        self.ga = ga
        self._attr_name = name
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_color_mode = ColorMode.ONOFF

    @property
    def is_on(self):
        return self._attr_is_on

    def _handle_coordinator_update(self) -> None:
        # Update state if the packet ga matches self.ga
        self.async_write_ha_state()