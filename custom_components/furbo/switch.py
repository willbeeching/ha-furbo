"""Switch platform for Furbo smart-alert settings."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FurboConfigEntry
from .api import FurboError
from .const import ALERT_KEYS, DEFAULT_ENABLED_ALERTS, DOMAIN
from .coordinator import FurboCoordinator
from .entity import FurboDeviceEntity

# Writes go to the cloud; serialize them to avoid racing settings updates.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FurboConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one switch per known smart alert present on each device."""
    coordinator = entry.runtime_data.coordinator
    entities = [
        FurboAlertSwitch(coordinator, device_id, key)
        for device_id, device in coordinator.data.devices.items()
        for key in ALERT_KEYS
        if key in device.alerts
    ]
    async_add_entities(entities)


class FurboAlertSwitch(FurboDeviceEntity, SwitchEntity):
    """Enable or disable one smart alert."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: FurboCoordinator, device_id: str, key: str) -> None:
        """Initialise the switch for one alert key."""
        super().__init__(coordinator, device_id)
        self._key = key
        self._attr_translation_key = f"alert_{_snake(key)}"
        self._attr_unique_id = f"{device_id}_alert_{key}"
        self._attr_entity_registry_enabled_default = key in DEFAULT_ENABLED_ALERTS

    @property
    def is_on(self) -> bool | None:
        """Return whether the alert is enabled, or None if unknown."""
        value = self.device.alerts.get(self._key)
        return None if value is None else value == "1"

    async def async_turn_on(self, **kwargs: object) -> None:
        """Enable the alert."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disable the alert."""
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        """Write the alert value and reflect it locally on success."""
        try:
            await self.coordinator.client.set_alert_setting(
                self._device_id, self._key, enabled
            )
        except FurboError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_alert_failed",
                translation_placeholders={"name": self._key, "error": str(err)},
            ) from err
        self.device.alerts[self._key] = "1" if enabled else "0"
        self.async_write_ha_state()


def _snake(key: str) -> str:
    """Convert an alert key like 'ContinuousBarking' to 'continuous_barking'."""
    out: list[str] = []
    for index, char in enumerate(key):
        if char.isupper() and index and not key[index - 1].isupper():
            out.append("_")
        out.append(char.lower())
    return "".join(out)
