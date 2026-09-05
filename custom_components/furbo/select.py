"""Select platform for Furbo: night vision and bark sensitivity, via the bridge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FurboConfigEntry
from .bridge import BARK_LEVELS, NIGHT_MODES, BridgeState
from .coordinator import FurboBridgeCoordinator
from .entity import FurboBridgeEntity

# Writes go through the bridge's single P2P session; serialize them.
PARALLEL_UPDATES = 1


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
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FurboConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the select entities per configured bridge."""
    async_add_entities(
        FurboSelect(bridge, description)
        for bridge in entry.runtime_data.bridges.values()
        for description in SELECTS
    )


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
