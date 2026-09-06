"""Select platform for Furbo: camera settings and alert frequency."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FurboConfigEntry
from .api import FurboError
from .bridge import (
    BARK_LEVELS,
    NIGHT_MODES,
    TREAT_SIZES,
    VIDEO_QUALITIES,
    BridgeState,
)
from .const import ALERT_FREQUENCIES, DOMAIN, FREQUENCY_ALERTS
from .coordinator import FurboBridgeCoordinator, FurboCoordinator
from .entity import FurboBridgeEntity, FurboDeviceEntity

# Writes go through the bridge's single P2P session (or the cloud); serialize them.
PARALLEL_UPDATES = 1

_SECONDS_FOR_OPTION = {option: seconds for seconds, option in ALERT_FREQUENCIES.items()}


def _snake(key: str) -> str:
    """Convert an alert key like 'PersonDetection' to 'person_detection'."""
    out: list[str] = []
    for index, char in enumerate(key):
        if char.isupper() and index and not key[index - 1].isupper():
            out.append("_")
        out.append(char.lower())
    return "".join(out)


@dataclass(frozen=True, kw_only=True)
class FurboSelectDescription(SelectEntityDescription):
    """Describes a multi-choice camera setting served by the bridge."""

    value_fn: Callable[[BridgeState], str | None]
    setting: str


SELECTS: tuple[FurboSelectDescription, ...] = (
    FurboSelectDescription(
        key="night_mode",
        translation_key="night_mode",
        entity_category=EntityCategory.CONFIG,
        options=list(NIGHT_MODES),
        value_fn=lambda state: state.night_mode,
        setting="night_mode",
    ),
    FurboSelectDescription(
        key="bark_sensitivity",
        translation_key="bark_sensitivity",
        entity_category=EntityCategory.CONFIG,
        options=list(BARK_LEVELS),
        value_fn=lambda state: state.bark_sensitivity,
        setting="bark_sensitivity",
    ),
    FurboSelectDescription(
        key="treat_size",
        translation_key="treat_size",
        entity_category=EntityCategory.CONFIG,
        options=list(TREAT_SIZES),
        value_fn=lambda state: state.treat_size,
        setting="treat_size",
    ),
    FurboSelectDescription(
        key="video_quality",
        translation_key="video_quality",
        entity_category=EntityCategory.CONFIG,
        options=list(VIDEO_QUALITIES),
        value_fn=lambda state: state.quality,
        setting="quality",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FurboConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up bridge selects per camera and alert-frequency selects per device."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SelectEntity] = [
        FurboSelect(bridge, description)
        for bridge in entry.runtime_data.bridges.values()
        for description in SELECTS
    ]
    entities.extend(
        FurboAlertFrequencySelect(coordinator, device_id, alert)
        for device_id, device in coordinator.data.devices.items()
        for alert in FREQUENCY_ALERTS
        if f"Frequency:{alert}" in device.alerts
    )
    async_add_entities(entities)


class FurboSelect(FurboBridgeEntity, SelectEntity):
    """A multi-choice camera setting written through the bridge."""

    entity_description: FurboSelectDescription

    def __init__(
        self, coordinator: FurboBridgeCoordinator, description: FurboSelectDescription
    ) -> None:
        """Initialise the select for one bridge setting."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"

    @property
    def current_option(self) -> str | None:
        """Return the setting, or None while the camera has not reported it."""
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_select_option(self, option: str) -> None:
        """Change the setting."""
        await self._async_apply(**{self.entity_description.setting: option})


class FurboAlertFrequencySelect(FurboDeviceEntity, SelectEntity):
    """How often one smart alert may notify (a cloud setting)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: FurboCoordinator, device_id: str, alert: str
    ) -> None:
        """Initialise the frequency select for one alert."""
        super().__init__(coordinator, device_id)
        self._alert = alert
        self._attr_options = list(ALERT_FREQUENCIES.values())
        self._attr_translation_key = f"frequency_{_snake(alert)}"
        self._attr_unique_id = f"{device_id}_frequency_{alert}"

    @property
    def current_option(self) -> str | None:
        """Return the current frequency, or None if unset/unknown."""
        return ALERT_FREQUENCIES.get(
            self.device.alerts.get(f"Frequency:{self._alert}", "")
        )

    async def async_select_option(self, option: str) -> None:
        """Write the new frequency and reflect it locally on success."""
        seconds = _SECONDS_FOR_OPTION[option]
        try:
            await self.coordinator.client.set_alert_frequency(
                self._device_id, self._alert, seconds
            )
        except FurboError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_alert_failed",
                translation_placeholders={"name": self._alert, "error": str(err)},
            ) from err
        self.device.alerts[f"Frequency:{self._alert}"] = seconds
        self.async_write_ha_state()
