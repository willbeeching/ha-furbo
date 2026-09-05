"""Button platform for Furbo: pan, treat toss and treat sound, via the bridge."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FurboConfigEntry
from .bridge import FurboBridgeClient
from .const import PAN_DEGREES
from .coordinator import FurboBridgeCoordinator
from .entity import FurboBridgeEntity

# Actions go through the bridge's single P2P session; serialize them.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class FurboButtonDescription(ButtonEntityDescription):
    """Describes a one-shot camera action served by the bridge."""

    press_fn: Callable[[FurboBridgeClient], Awaitable[None]]


BUTTONS: tuple[FurboButtonDescription, ...] = (
    FurboButtonDescription(
        key="pan_left",
        translation_key="pan_left",
        press_fn=lambda client: client.async_pan("left", PAN_DEGREES),
    ),
    FurboButtonDescription(
        key="pan_right",
        translation_key="pan_right",
        press_fn=lambda client: client.async_pan("right", PAN_DEGREES),
    ),
    FurboButtonDescription(
        key="toss_treat",
        translation_key="toss_treat",
        press_fn=lambda client: client.async_toss(),
    ),
    FurboButtonDescription(
        key="treat_sound",
        translation_key="treat_sound",
        press_fn=lambda client: client.async_treat_sound(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FurboConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the action buttons per configured bridge."""
    async_add_entities(
        FurboButton(bridge, description)
        for bridge in entry.runtime_data.bridges.values()
        for description in BUTTONS
    )


class FurboButton(FurboBridgeEntity, ButtonEntity):
    """A one-shot camera action sent through the bridge."""

    entity_description: FurboButtonDescription

    def __init__(
        self, coordinator: FurboBridgeCoordinator, description: FurboButtonDescription
    ) -> None:
        """Initialise the button for one bridge action."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"

    async def async_press(self) -> None:
        """Send the action."""
        await self._async_action(
            self.entity_description.press_fn(self.coordinator.client)
        )
