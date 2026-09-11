"""Button platform for Furbo: camera actions via the bridge, and the diary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FurboConfigEntry
from .api import FurboError
from .bridge import FurboBridgeClient
from .const import PAN_DEGREES
from .coordinator import FurboBridgeCoordinator, FurboCoordinator
from .diary import DiaryError, async_save, diary_folder, missing
from .entity import FurboAccountEntity, FurboBridgeEntity

_LOGGER = logging.getLogger(__name__)

# Camera actions serialize per camera, on the bridge coordinator's own lock.
# A platform-wide limit would put the diary download, which can run for
# minutes and touches no camera, in the same queue as pressing pan.
PARALLEL_UPDATES = 0


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
    """Set up the camera action buttons, and the account's diary download."""
    entities: list[ButtonEntity] = [
        FurboButton(bridge, description)
        for bridge in entry.runtime_data.bridges.values()
        for description in BUTTONS
    ]
    entities.append(FurboDiaryButton(entry.runtime_data.coordinator))
    async_add_entities(entities)


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


class FurboDiaryButton(FurboAccountEntity, ButtonEntity):
    """Save any Doggie Diary videos that are not on disk yet."""

    _attr_translation_key = "download_diary"

    def __init__(self, coordinator: FurboCoordinator) -> None:
        """Bind to the account the diary belongs to."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_download_diary"

    async def async_press(self) -> None:
        """Fetch the report and download whatever is missing.

        The report is read now rather than reused from the coordinator: its
        links are presigned and expire, so they are worth having only at the
        moment they are used.
        """
        entry = self.coordinator.config_entry
        folder = diary_folder(self.hass, entry.unique_id or "", entry.entry_id)
        try:
            days = await self.coordinator.client.get_diary_report()
        except FurboError as err:
            raise DiaryError(f"Could not read the Doggie Diary: {err}") from err

        session = async_get_clientsession(self.hass)
        wanted = await self.hass.async_add_executor_job(missing, folder, days)
        if not wanted:
            _LOGGER.debug("Doggie Diary: all %d days already saved", len(days))
            return
        for date, url in wanted:
            saved = await async_save(self.hass, session, folder, date, url)
            _LOGGER.info("Saved the %s Doggie Diary video to %s", date, saved)
