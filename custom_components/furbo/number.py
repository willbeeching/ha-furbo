"""Number platform for Furbo: speaker volume, via the HTTP bridge."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FurboConfigEntry
from .coordinator import FurboBridgeCoordinator
from .entity import FurboBridgeEntity

# Writes go through the bridge's single P2P session; serialize them.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FurboConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a volume entity per configured bridge."""
    async_add_entities(
        FurboVolumeNumber(bridge) for bridge in entry.runtime_data.bridges.values()
    )


class FurboVolumeNumber(FurboBridgeEntity, NumberEntity):
    """Speaker volume of the camera, 0 to 100."""

    _attr_translation_key = "volume"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: FurboBridgeCoordinator) -> None:
        """Initialise the volume entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_volume"

    @property
    def native_value(self) -> int | None:
        """Return the volume, or None while the camera has not reported it."""
        return self.coordinator.data.volume

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume."""
        await self._async_apply(volume=round(value))
