"""Locate the Furbo Bridge add-on through the Supervisor.

When Home Assistant runs under the Supervisor (Home Assistant OS or
Supervised) and the Furbo Bridge add-on is installed, the integration can read
the add-on's hostname and its configured API token straight from the
Supervisor, so the options flow can offer the bridge and stream URLs already
filled in instead of asking the user to type them.

Everything here fails soft: with no Supervisor, no add-on, or any error, it
returns ``None`` and the flow falls back to manual entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import logging
import os
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
# The add-on's slug is "<repository>_furbo_bridge" (e.g. "local_furbo_bridge"
# or "abc12345_furbo_bridge"), so match on the suffix.
ADDON_SLUG_SUFFIX = "furbo_bridge"
BRIDGE_PORT = 8791
RTSP_PORT = 8554
# The add-on protects RTSP with a username and a password DERIVED from the
# api_token (never the token itself), so a leaked stream URL cannot drive the
# control API. Keep this derivation in step with furbo-bridge/run.sh.
RTSP_USERNAME = "furbo"
# The stream a bridge publishes for its first (or only) camera. Cameras beyond
# the first have a stream of their own, which the add-on names in /api/cameras.
LEGACY_STREAM = "furbo"
_RTSP_SECRET_PREFIX = "furbo-rtsp:"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


def rtsp_password(token: str) -> str:
    """Derive the RTSP password from the API token (must match run.sh)."""
    digest = hashlib.sha256(f"{_RTSP_SECRET_PREFIX}{token}".encode()).hexdigest()
    return digest[:32]


@dataclass(slots=True, frozen=True)
class DiscoveredBridge:
    """A Furbo Bridge add-on found through the Supervisor."""

    slug: str
    host: str
    token: str | None
    # Cloud device id -> go2rtc stream name, as the add-on reports it. Empty
    # for an add-on that predates multiple cameras: the only stream we can
    # name is then the single one it publishes.
    streams: Mapping[str, str] = field(default_factory=dict)
    # True when the list could not be read at all, rather than read and found
    # to be absent. The bridge may well name its cameras once it is up, so a
    # caller that needed the names should ask again instead of giving up.
    camera_list_unavailable: bool = False

    @property
    def bridge_url(self) -> str:
        """The add-on's HTTP API base URL."""
        return f"http://{self.host}:{BRIDGE_PORT}"

    def _rtsp_url(self, name: str) -> str:
        """Build one go2rtc stream URL, with credentials when the token is set."""
        auth = f"{RTSP_USERNAME}:{rtsp_password(self.token)}@" if self.token else ""
        return f"rtsp://{auth}{self.host}:{RTSP_PORT}/{name}"

    def stream_url_for(
        self, device_id: str | None = None, *, sole_camera: bool = False
    ) -> str | None:
        """One camera's go2rtc RTSP URL, or None when it cannot be named.

        The add-on requires RTSP authentication for non-loopback clients, so a
        credential derived from the token (not the token itself) is carried as
        the RTSP password.

        A bridge that lists its cameras settles this outright. One that does
        not (too old for `/api/cameras`, or unreachable when we asked)
        publishes a single stream under the plain name, which is this camera's
        only when it is the account's only camera -- hence ``sole_camera``.
        Guessing otherwise is how three cameras end up sharing one feed:
        the name resolves, the URL works, and every camera plays the first.
        """
        name = self.streams.get(device_id) if device_id else None
        if name is None:
            if self.streams or not sole_camera:
                return None
            name = LEGACY_STREAM
        return self._rtsp_url(name)

    @property
    def names_cameras(self) -> bool:
        """Whether this bridge told us which stream belongs to which camera."""
        return bool(self.streams)

    @property
    def stream_url(self) -> str:
        """The plain stream every bridge publishes for its first camera."""
        return self._rtsp_url(LEGACY_STREAM)


async def _get(session: aiohttp.ClientSession, path: str, token: str) -> Any:
    async with session.get(
        f"{SUPERVISOR_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    ) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def _stream_names(
    session: aiohttp.ClientSession, host: str, token: str | None
) -> dict[str, str] | None:
    """Ask the add-on which stream belongs to which camera.

    Returns None when the question could not be put -- the add-on was starting,
    slow, or briefly unreachable -- which is worth asking again later. An
    add-on too old to have the endpoint answers 404, which is a settled answer
    and returns an empty mapping: it publishes the one stream and no more.

    The two used to be indistinguishable, so a moment's unavailability during
    setup looked permanent and left a multi-camera account with no video at
    all until someone reloaded the integration by hand.
    """
    if not token:
        return {}
    try:
        async with session.get(
            f"http://{host}:{BRIDGE_PORT}/api/cameras",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status in (404, 405):
                return {}
            if resp.status != 200:
                _LOGGER.debug("Furbo Bridge camera list: HTTP %s", resp.status)
                return None
            body = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Furbo Bridge camera list unavailable: %s", err)
        return None
    cameras = body.get("cameras") if isinstance(body, dict) else None
    if not isinstance(cameras, list):
        return {}
    return {
        str(c["device_id"]): str(c["stream"])
        for c in cameras
        if isinstance(c, dict) and c.get("device_id") and c.get("stream")
    }


async def async_discover_bridge(hass: HomeAssistant) -> DiscoveredBridge | None:
    """Return the Furbo Bridge add-on's connection details, or None.

    Prefers a started add-on; falls back to an installed-but-stopped one so the
    URLs can still be offered.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    session = async_get_clientsession(hass)
    try:
        listing = await _get(session, "/addons", token)
        addons = (listing or {}).get("data", {}).get("addons", [])
        if not isinstance(addons, list):
            return None
        candidates = [
            a
            for a in addons
            if isinstance(a, dict)
            and str(a.get("slug", "")).endswith(ADDON_SLUG_SUFFIX)
        ]
        if not candidates:
            return None
        chosen = next(
            (a for a in candidates if a.get("state") == "started"), candidates[0]
        )
        slug = str(chosen["slug"])

        info = await _get(session, f"/addons/{slug}/info", token)
        data = (info or {}).get("data", {}) if isinstance(info, dict) else {}
        # The add-on is reachable on the Supervisor network by its hostname;
        # fall back to the slug-derived name if the field is missing.
        host = data.get("hostname") or slug.replace("_", "-")
        options = data.get("options")
        api_token = None
        if isinstance(options, dict):
            value = options.get("api_token")
            api_token = value if isinstance(value, str) and value else None
        streams = await _stream_names(session, str(host), api_token)
        return DiscoveredBridge(
            slug=slug,
            host=str(host),
            token=api_token,
            streams=streams or {},
            camera_list_unavailable=streams is None,
        )
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
        _LOGGER.debug("Furbo Bridge add-on discovery failed: %s", err)
        return None
