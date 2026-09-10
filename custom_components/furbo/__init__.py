"""The Furbo integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

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
from .coordinator import (
    FurboBridgeCoordinator,
    FurboCoordinator,
    clear_renewal_budget,
)
from .discovery import (
    RTSP_USERNAME,
    DiscoveredBridge,
    async_discover_bridge,
    rtsp_password,
)

_LOGGER = logging.getLogger(__name__)

# How often to ask a discovered add-on again which camera is which, when it
# could not say at setup. It is usually just starting up alongside Home
# Assistant, so the answer normally comes on the first or second ask.
DISCOVERY_RETRY = timedelta(minutes=2)

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
    # The options this runtime was built from, so the update listener can tell
    # an options change (rebuild) from a data change (nothing to rebuild).
    options: dict[str, Any] = field(default_factory=dict)


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


def _token_source(
    hass: HomeAssistant,
    entry: FurboConfigEntry,
    discovered: DiscoveredBridge | None,
) -> Callable[[], Awaitable[tuple[str, str]]] | None:
    """Build a way to fetch a current cloud token from a bridge.

    The cloud token is short lived and comes with nothing to refresh it with,
    and this integration deliberately does not store the account password. The
    add-on does, and logs in again by itself, so it can supply a current one.
    Any bridge will do: the token belongs to the account, not to a camera, so
    this asks the first configured one and falls back to the discovered add-on.
    """
    for conf in (entry.options.get(CONF_BRIDGES) or {}).values():
        url = conf.get(CONF_BRIDGE_URL) if isinstance(conf, dict) else None
        if not url:
            continue
        client = FurboBridgeClient(
            async_get_clientsession(hass), url, conf.get(CONF_BRIDGE_TOKEN)
        )
        return client.async_get_cloud_token
    if discovered is None:
        return None
    client = FurboBridgeClient(
        async_get_clientsession(hass), discovered.bridge_url, discovered.token
    )
    return client.async_get_cloud_token


def _retry_discovery(hass: HomeAssistant, entry: FurboConfigEntry) -> None:
    """Reload the entry once the add-on can say which camera is which.

    Only reached when the account has several cameras and the add-on could not
    be asked, which leaves every one of them without video. Nothing else would
    ever ask again, so the cameras stayed missing until someone reloaded the
    integration by hand, however long after the add-on came up.
    """

    async def _ask_again(_now: datetime) -> None:
        bridge = await async_discover_bridge(hass)
        if bridge is not None and bridge.names_cameras:
            _LOGGER.debug("The Furbo Bridge add-on named its cameras; reloading")
            # Reloading unloads the entry, and unloading cancels this timer.
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(async_track_time_interval(hass, _ask_again, DISCOVERY_RETRY))


async def async_setup_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Set up Furbo from a config entry."""
    client = FurboClient(
        async_get_clientsession(hass),
        account_id=entry.data[CONF_ACCOUNT_ID],
        cognito_token=entry.data[CONF_COGNITO_TOKEN],
    )
    coordinator = FurboCoordinator(hass, entry, client)
    # Find the bridge before the first poll, not after it: a restart once the
    # cloud token has expired overnight fails on that very first call, and the
    # add-on is the only thing that can hand over a fresh token without asking
    # a person for an emailed code.
    discovered = await async_discover_bridge(hass)
    coordinator.token_source = _token_source(hass, entry, discovered)
    await coordinator.async_config_entry_first_refresh()
    # The token works, whether it was already good or a person has just signed
    # in again after the wait for the add-on ran out. Either way the next time
    # a token dies this entry gets the full wait over again.
    clear_renewal_budget(hass, entry.entry_id)

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
    configured_bridges: dict[str, dict[str, str]] = entry.options.get(CONF_BRIDGES, {})
    configured_streams: dict[str, str] = entry.options.get(CONF_STREAM_URLS, {})

    # An add-on that lists its cameras says which stream belongs to which, so
    # every camera can be bound to its own. One that does not publishes a
    # single stream under the plain name: that is the right stream only when
    # the account has one camera, and binding it to several would show the
    # first camera's video under all of them.
    sole_camera = len(coordinator.data.devices) == 1
    auto_bind = (
        discovered
        if discovered is not None and (sole_camera or discovered.names_cameras)
        else None
    )
    if discovered is not None and auto_bind is None:
        _LOGGER.warning(
            "Found the Furbo Bridge add-on but could not read which of this "
            "account's %d cameras it serves, so none were bound to it "
            "automatically. Update the add-on, or set each camera's bridge and "
            "stream URL in the Furbo integration's options.",
            len(coordinator.data.devices),
        )
        if discovered.camera_list_unavailable:
            # It could not be asked, as opposed to asked and found too old.
            # Everything else here still loads; only video waits.
            _retry_discovery(hass, entry)

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
                device_id=device_id,
            )
            bridge = FurboBridgeCoordinator(
                hass, entry, device_id, bridge_client, coordinator
            )
            await bridge.async_refresh()
            bridges[device_id] = bridge

        stream = configured_streams.get(device_id)
        if not stream and auto_bind is not None:
            # One bridge serves several cameras, each on its own stream. A
            # camera it cannot name gets none: no video is better than another
            # camera's, which is indistinguishable from this one working.
            stream = auto_bind.stream_url_for(device_id, sole_camera=sole_camera)
            if stream is None:
                _LOGGER.warning(
                    "The Furbo Bridge add-on does not publish a stream for "
                    "camera %s, so it has no video. Check the add-on's "
                    "device_id option, or set the stream URL by hand",
                    device_id,
                )
        if stream:
            stream_urls[device_id] = stream

    entry.runtime_data = FurboRuntimeData(
        coordinator=coordinator,
        bridges=bridges,
        stream_urls=stream_urls,
        options=dict(entry.options),
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    # Only a loaded entry is unloaded, so this does not run between the setup
    # retries the count exists to survive. Setup clears it too: an entry that
    # never loads is never unloaded.
    clear_renewal_budget(hass, entry.entry_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> None:
    """Reload the entry when its options change.

    The listener fires for any update, including the coordinator writing a
    fresh cloud token into ``entry.data``. Nothing set up here depends on the
    token, and reloading mid-poll would tear the coordinator down underneath
    itself, so only an options change rebuilds the entry.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None and runtime.options == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)
