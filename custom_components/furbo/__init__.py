"""The Furbo integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

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
    CONF_STREAM_URLS,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import FurboBridgeCoordinator, FurboCoordinator
from .discovery import RTSP_USERNAME, async_discover_bridge, rtsp_password

_LOGGER = logging.getLogger(__name__)

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
    # One bridge coordinator per camera that has a bridge (configured or the
    # auto-discovered Furbo Bridge add-on).
    bridges: dict[str, FurboBridgeCoordinator] = field(default_factory=dict)
    # Per-camera live-stream URL (configured, or the discovered add-on's).
    stream_urls: dict[str, str] = field(default_factory=dict)


type FurboConfigEntry = ConfigEntry[FurboRuntimeData]


def _rewrite_stale_stream_url(url: str, token: str) -> str | None:
    """Return the URL with the derived RTSP password, or None if it is not stale.

    Up to and including 0.1.2 the auto-filled stream URL carried the api_token
    itself as the RTSP password (``rtsp://furbo:<token>@host:8554/furbo``,
    percent-encoded). From 0.1.3 the add-on derives a distinct RTSP password, so
    a saved old URL no longer authenticates. Only the userinfo is examined — the
    URL is rebuilt from parts, never string-replaced — so a token that merely
    appears in the path or query is left untouched, and reserved characters
    (``/``, ``@``, ``:``) in the token are handled correctly.
    """
    parts = urlsplit(url)
    if parts.username != RTSP_USERNAME or parts.password is None:
        return None
    if unquote(parts.password) != token:
        return None
    host = parts.hostname or ""
    netloc = f"{RTSP_USERNAME}:{rtsp_password(token)}@{host}"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _migrate_stream_urls(options: Mapping[str, Any]) -> dict[str, Any] | None:
    """Rewrite stream URLs whose RTSP password is the raw bridge token.

    Returns new options, or None if nothing needed changing.
    """
    bridges: dict[str, dict[str, str]] = options.get(CONF_BRIDGES, {})
    streams: dict[str, str] = options.get(CONF_STREAM_URLS, {})
    updated: dict[str, str] = {}
    for device_id, url in streams.items():
        token = bridges.get(device_id, {}).get(CONF_BRIDGE_TOKEN)
        if token and (rewritten := _rewrite_stale_stream_url(url, token)):
            updated[device_id] = rewritten
    if not updated:
        return None
    return {**options, CONF_STREAM_URLS: {**streams, **updated}}


async def async_migrate_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Migrate an old config entry to the current schema.

    Version 1 stored the account password, which setup never uses; version 2
    drops it. Version 3 rewrites stream URLs that embedded the raw token as the
    RTSP password (see :func:`_migrate_stream_urls`).
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # Downgrade from a newer schema is not supported.
        return False
    if entry.version == 1:
        data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    if entry.version == 2:
        options = _migrate_stream_urls(entry.options) or entry.options
        hass.config_entries.async_update_entry(entry, options=options, version=3)
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

    # Resolve each camera's bridge and stream. Explicit options win; otherwise
    # fall back to the Furbo Bridge add-on if it is running, so the camera and
    # its controls appear automatically with no manual configuration.
    discovered = await async_discover_bridge(hass)
    configured_bridges: dict[str, dict[str, str]] = entry.options.get(CONF_BRIDGES, {})
    configured_streams: dict[str, str] = entry.options.get(CONF_STREAM_URLS, {})

    # A discovered add-on serves exactly one camera, so it can only be bound
    # automatically when the account has a single camera; with more than one we
    # cannot tell which camera the add-on is paired with, and binding it to all
    # of them would show one camera's video and controls under every device.
    # In that case, require the user to map each bridge to its camera explicitly
    # (Configure -> per-camera bridge/stream URLs).
    auto_bind = discovered if len(coordinator.data.devices) == 1 else None
    if discovered is not None and auto_bind is None:
        _LOGGER.warning(
            "Found the Furbo Bridge add-on but this account has %d cameras; "
            "not auto-binding it. Configure each camera's bridge and stream URL "
            "in the Furbo integration's options to avoid crossing streams.",
            len(coordinator.data.devices),
        )

    # A bridge that is down at startup must not keep the cloud entities from
    # loading: refresh without raising, and let its entities be unavailable
    # until it answers.
    bridges: dict[str, FurboBridgeCoordinator] = {}
    stream_urls: dict[str, str] = {}
    for device_id in coordinator.data.devices:
        bridge_conf = configured_bridges.get(device_id)
        if bridge_conf is None and auto_bind is not None:
            bridge_conf = {CONF_BRIDGE_URL: auto_bind.bridge_url}
            if auto_bind.token:
                bridge_conf[CONF_BRIDGE_TOKEN] = auto_bind.token
        if bridge_conf is not None:
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

        stream = configured_streams.get(device_id)
        if not stream and auto_bind is not None:
            stream = auto_bind.stream_url
        if stream:
            stream_urls[device_id] = stream

    entry.runtime_data = FurboRuntimeData(
        coordinator=coordinator, bridges=bridges, stream_urls=stream_urls
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
