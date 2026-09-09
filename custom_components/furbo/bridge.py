"""Client for the Furbo HTTP bridge (``furbo_p2p.py serve``).

The bridge runs on the camera's LAN, holds the proprietary P2P session and
exposes the camera's state and controls as a small JSON API. This module has
no Home Assistant imports and validates every response at the boundary, in
the same way as :mod:`api`. Response bodies never appear in exception
messages.

Endpoints used: ``GET /api/status``, ``POST /api/settings``,
``POST /api/pan``, ``POST /api/toss`` and ``POST /api/treat-sound``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

DEFAULT_TIMEOUT = 10

NIGHT_MODES = ("auto", "on", "off")
BARK_LEVELS = ("off", "low", "medium", "high")
PAN_DIRECTIONS = ("left", "right")
TREAT_SIZES = ("large", "small")
SNACK_MODES = ("default", "custom", "mute")
VIDEO_QUALITIES = ("1080p", "720p", "360p")


class FurboBridgeError(Exception):
    """Base error for the bridge. Carries the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Store the HTTP status alongside the message."""
        super().__init__(message)
        self.status = status


class FurboBridgeUnavailable(FurboBridgeError):
    """The bridge could not be reached, or it has no P2P session right now."""


class FurboBridgeAuthError(FurboBridgeError):
    """The bridge rejected the bearer token."""


@dataclass(slots=True, frozen=True)
class BridgeState:
    """Camera state as reported by the bridge. None means not reported yet."""

    connected: bool
    # The Furbo cloud device id the bridge is actually serving, so the
    # integration can detect a bridge pointed at the wrong camera. None until
    # the bridge has established a session.
    device_id: str | None = None
    camera_on: bool | None = None
    volume: int | None = None
    muted: bool | None = None
    night_mode: str | None = None
    bark_sensitivity: str | None = None
    auto_tracking: bool | None = None
    auto_zoom: bool | None = None
    voice_control: bool | None = None
    treat_size: str | None = None
    snack_call: str | None = None
    quality: str | None = None
    schedule_enabled: bool | None = None
    calm_enabled: bool | None = None
    firmware: str | None = None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _choice(value: Any, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def parse_state(data: Any) -> BridgeState:
    """Build a BridgeState from a status document, dropping malformed fields."""
    if not isinstance(data, dict):
        raise FurboBridgeError("Malformed bridge response: body is not an object")
    state = data.get("state", {})
    if not isinstance(state, dict):
        raise FurboBridgeError("Malformed bridge response: state is not an object")
    volume = state.get("volume")
    if (
        isinstance(volume, bool)
        or not isinstance(volume, int)
        or not 0 <= volume <= 100
    ):
        volume = None
    firmware = state.get("firmware")
    device = data.get("device", {})
    device_id = device.get("device_id") if isinstance(device, dict) else None
    return BridgeState(
        connected=data.get("connected") is True,
        device_id=device_id if isinstance(device_id, str) and device_id else None,
        camera_on=_bool(state.get("camera_on")),
        volume=volume,
        muted=_bool(state.get("muted")),
        night_mode=_choice(state.get("night_mode"), NIGHT_MODES),
        bark_sensitivity=_choice(state.get("bark_sensitivity"), BARK_LEVELS),
        auto_tracking=_bool(state.get("auto_tracking")),
        auto_zoom=_bool(state.get("auto_zoom")),
        voice_control=_bool(state.get("voice_control")),
        treat_size=_choice(state.get("treat_size"), TREAT_SIZES),
        snack_call=_choice(state.get("snack_call"), SNACK_MODES),
        quality=_choice(state.get("quality"), VIDEO_QUALITIES),
        schedule_enabled=_bool(state.get("schedule_enabled")),
        calm_enabled=_bool(state.get("calm_enabled")),
        firmware=firmware if isinstance(firmware, str) and firmware else None,
    )


class FurboBridgeClient:
    """Talks to one bridge instance, which fronts one camera."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str | None = None,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        device_id: str | None = None,
    ) -> None:
        """Bind to a session, the bridge's base URL, a token and one camera.

        One bridge can serve several cameras, so requests name the camera they
        are about. A bridge that predates that has no such paths, which is
        settled once on the first request rather than guessed at per call.
        """
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._device_id = device_id
        # None until the first request settles whether this bridge knows about
        # cameras individually.
        self._scoped: bool | None = None

    async def _knows_cameras(self) -> bool | None:
        """Whether this bridge serves per-camera paths; None while unknown.

        Only a definite answer is remembered. A bridge that is down or erroring
        when first asked says nothing about its age, and caching a guess there
        would leave the client talking to the wrong paths for the life of the
        config entry.
        """
        if self._scoped is not None:
            return self._scoped
        try:
            await self._request("GET", "/api/cameras", None, scoped=False)
        except FurboBridgeError as err:
            if err.status in (404, 405):
                # Definite: a bridge that serves a single camera.
                self._scoped = False
                return False
            return None
        self._scoped = True
        return True

    async def _camera_request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> Any:
        """Make a request about this client's camera."""
        if not self._device_id:
            return await self._request(method, path, body, scoped=False)
        if await self._knows_cameras() is False:
            return await self._request(method, path, body, scoped=False)
        # Known to serve cameras, or not yet known because the bridge could not
        # be reached. Naming the camera is right in the first case and safe in
        # the second: an older bridge answers 404, where the unscoped path
        # would quietly act on whichever camera it serves first. Tossing a
        # treat from the wrong camera is worse than an error.
        suffix = path.removeprefix("/api")
        return await self._request(
            method, f"/api/cameras/{self._device_id}{suffix}", body, scoped=False
        )

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        scoped: bool = True,
    ) -> Any:
        if scoped:
            return await self._camera_request(method, path, body)
        try:
            async with self._session.request(
                method,
                f"{self._url}{path}",
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            ) as resp:
                status = resp.status
                if status == 200:
                    try:
                        return await resp.json(content_type=None)
                    except ValueError as err:
                        raise FurboBridgeError(
                            f"Malformed bridge response from {path}: not JSON", status
                        ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FurboBridgeUnavailable(
                f"Bridge unreachable on {path}: {type(err).__name__}"
            ) from err
        message = f"Bridge returned {status} on {path}"
        if status == 401:
            raise FurboBridgeAuthError(message, status)
        if status == 503:
            raise FurboBridgeUnavailable(message, status)
        raise FurboBridgeError(message, status)

    async def async_get_status(self) -> BridgeState:
        """Return the bridge's cached view of the camera."""
        return parse_state(await self._request("GET", "/api/status", None))

    async def async_set(self, **settings: Any) -> BridgeState:
        """Write one or more settings and return the state read back."""
        return parse_state(await self._request("POST", "/api/settings", settings))

    async def async_pan(self, direction: str, degrees: int) -> None:
        """Rotate the camera base left or right by a relative angle."""
        if direction not in PAN_DIRECTIONS:
            raise ValueError(f"direction must be one of {PAN_DIRECTIONS}")
        await self._request(
            "POST", "/api/pan", {"direction": direction, "degrees": degrees}
        )

    async def async_toss(self) -> None:
        """Dispense a treat."""
        await self._request("POST", "/api/toss", {})

    async def async_treat_sound(self) -> None:
        """Play the treat-tossing sound without tossing."""
        await self._request("POST", "/api/treat-sound", {})
