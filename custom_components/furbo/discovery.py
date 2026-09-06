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

from dataclasses import dataclass
import logging
import os
from typing import Any
from urllib.parse import quote

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
# The add-on protects RTSP with these credentials (username fixed; password is
# the api_token). Keep in step with furbo-bridge/go2rtc.yaml.
RTSP_USERNAME = "furbo"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass(slots=True, frozen=True)
class DiscoveredBridge:
    """A Furbo Bridge add-on found through the Supervisor."""

    slug: str
    host: str
    token: str | None

    @property
    def bridge_url(self) -> str:
        """The add-on's HTTP API base URL."""
        return f"http://{self.host}:{BRIDGE_PORT}"

    @property
    def stream_url(self) -> str:
        """The add-on's go2rtc RTSP URL, with credentials when a token is set.

        The add-on requires RTSP authentication for non-loopback clients, so the
        token is carried as the RTSP password (URL-encoded).
        """
        auth = f"{RTSP_USERNAME}:{quote(self.token, safe='')}@" if self.token else ""
        return f"rtsp://{auth}{self.host}:{RTSP_PORT}/furbo"


async def _get(session: aiohttp.ClientSession, path: str, token: str) -> Any:
    async with session.get(
        f"{SUPERVISOR_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    ) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


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
        return DiscoveredBridge(slug=slug, host=str(host), token=api_token)
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
        _LOGGER.debug("Furbo Bridge add-on discovery failed: %s", err)
        return None
