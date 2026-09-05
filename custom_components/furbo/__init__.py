"""The Furbo integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FurboClient
from .bridge import FurboBridgeClient
from .const import (
    CONF_ACCOUNT_ID,
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_BRIDGES,
    CONF_COGNITO_TOKEN,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import FurboBridgeCoordinator, FurboCoordinator

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class FurboRuntimeData:
    """Objects owned by a loaded config entry."""

    coordinator: FurboCoordinator
    # One bridge coordinator per camera that has a bridge URL configured.
    bridges: dict[str, FurboBridgeCoordinator] = field(default_factory=dict)


type FurboConfigEntry = ConfigEntry[FurboRuntimeData]


async def async_migrate_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Migrate an old config entry to the current schema.

    Version 1 stored the account password, which setup never uses. Version 2
    drops it; reauth and reconfigure ask for it again when needed.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # Downgrade from a newer schema is not supported.
        return False
    if entry.version == 1:
        data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Set up Furbo from a config entry."""
    client = FurboClient(
        async_get_clientsession(hass),
        account_id=entry.data[CONF_ACCOUNT_ID],
        cognito_token=entry.data[CONF_COGNITO_TOKEN],
    )
    coordinator = FurboCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    # Register the account hub device up front so cameras can reference it via
    # via_device regardless of platform order or whether calendar sensors exist.
    account_id = entry.unique_id or entry.entry_id
    device_registry = dr.async_get(hass)
    hub = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"account_{account_id}")},
        manufacturer=MANUFACTURER,
        model="Cloud account",
        name="Furbo account",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    coordinator.hub_device_id = hub.id

    # A bridge that is down at startup must not keep the cloud entities from
    # loading: refresh without raising, and let its entities be unavailable
    # until it answers.
    bridges: dict[str, FurboBridgeCoordinator] = {}
    configured: dict[str, dict[str, str]] = entry.options.get(CONF_BRIDGES, {})
    for device_id, bridge_conf in configured.items():
        if device_id not in coordinator.data.devices:
            continue
        bridge_client = FurboBridgeClient(
            async_get_clientsession(hass),
            bridge_conf[CONF_BRIDGE_URL],
            bridge_conf.get(CONF_BRIDGE_TOKEN),
        )
        bridge = FurboBridgeCoordinator(
            hass, entry, device_id, bridge_client, coordinator
        )
        await bridge.async_refresh()
        bridges[device_id] = bridge

    entry.runtime_data = FurboRuntimeData(coordinator=coordinator, bridges=bridges)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
