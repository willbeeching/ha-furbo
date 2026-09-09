"""Unit tests for the HTTP bridge client."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.furbo.bridge import (
    BridgeState,
    FurboBridgeAuthError,
    FurboBridgeClient,
    FurboBridgeError,
    FurboBridgeUnavailable,
    parse_state,
)

BASE = "http://bridge.local:8791"
BODY_MARKER = "do-not-leak-this-body"

FULL_STATUS: dict[str, Any] = {
    "connected": True,
    "updated_at": 1.0,
    "last_error": None,
    "device": {"id": "UID", "name": "Cam", "product": "FB0030"},
    "state": {
        "camera_on": True,
        "volume": 40,
        "muted": False,
        "night_mode": "auto",
        "bark_sensitivity": "medium",
        "auto_tracking": False,
        "auto_zoom": True,
        "voice_control": True,
        "treat_size": "large",
        "snack_call": "default",
        "quality": "1080p",
        "schedule_enabled": False,
        "calm_enabled": True,
        "firmware": "108",
    },
}


def _client(hass: HomeAssistant, token: str | None = "tok") -> FurboBridgeClient:
    return FurboBridgeClient(async_get_clientsession(hass), BASE + "/", token)


def test_parse_state_full() -> None:
    """A complete status document maps field for field."""
    assert parse_state(FULL_STATUS) == BridgeState(
        connected=True,
        camera_on=True,
        volume=40,
        muted=False,
        night_mode="auto",
        bark_sensitivity="medium",
        auto_tracking=False,
        auto_zoom=True,
        voice_control=True,
        treat_size="large",
        snack_call="default",
        quality="1080p",
        schedule_enabled=False,
        calm_enabled=True,
        firmware="108",
    )


def test_parse_state_drops_malformed_fields() -> None:
    """Fields of the wrong type or value are dropped, not propagated."""
    state = parse_state(
        {
            "connected": "yes",
            "state": {
                "camera_on": 1,
                "volume": 101,
                "muted": "no",
                "night_mode": "dusk",
                "bark_sensitivity": 2,
                "auto_tracking": None,
                "auto_zoom": "true",
                "firmware": "",
            },
        }
    )
    assert state == BridgeState(connected=False)
    assert parse_state({"connected": True}) == BridgeState(connected=True)
    assert parse_state({"state": {"volume": True}}).volume is None


@pytest.mark.parametrize("body", [[], "text", {"state": []}, {"state": "x"}])
def test_parse_state_rejects_bad_shapes(body: Any) -> None:
    """A body that is not a status document raises."""
    with pytest.raises(FurboBridgeError):
        parse_state(body)


async def test_status_sends_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """GET /api/status carries the bearer token and parses the reply."""
    aioclient_mock.get(f"{BASE}/api/status", json=FULL_STATUS)
    state = await _client(hass).async_get_status()
    assert state.volume == 40
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer tok"


async def test_status_without_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without a token no Authorization header is sent."""
    aioclient_mock.get(f"{BASE}/api/status", json=FULL_STATUS)
    await _client(hass, token=None).async_get_status()
    assert "Authorization" not in aioclient_mock.mock_calls[0][3]


async def test_set_and_actions(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Writes post the right bodies and return the state read back."""
    aioclient_mock.post(f"{BASE}/api/settings", json=FULL_STATUS)
    aioclient_mock.post(f"{BASE}/api/pan", json={"ok": True})
    aioclient_mock.post(f"{BASE}/api/toss", json={"ok": True})
    aioclient_mock.post(f"{BASE}/api/treat-sound", json={"ok": True})
    client = _client(hass)
    state = await client.async_set(volume=55, night_mode="off")
    assert state.connected is True
    await client.async_pan("left", 60)
    await client.async_toss()
    await client.async_treat_sound()
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert bodies == [
        {"volume": 55, "night_mode": "off"},
        {"direction": "left", "degrees": 60},
        {},
        {},
    ]


async def test_pan_direction_validated(hass: HomeAssistant) -> None:
    """A bad pan direction is rejected before any request is made."""
    with pytest.raises(ValueError):
        await _client(hass).async_pan("up", 60)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, FurboBridgeAuthError),
        (503, FurboBridgeUnavailable),
        (400, FurboBridgeError),
        (500, FurboBridgeError),
    ],
)
async def test_error_statuses(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    expected: type[FurboBridgeError],
) -> None:
    """HTTP errors map to typed exceptions without echoing the body."""
    aioclient_mock.post(
        f"{BASE}/api/toss", status=status, json={"error": "x", "detail": BODY_MARKER}
    )
    with pytest.raises(expected) as err:
        await _client(hass).async_toss()
    assert err.value.status == status
    assert BODY_MARKER not in str(err.value)
    assert str(status) in str(err.value)


async def test_transport_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Connection failures and timeouts are 'unavailable'."""
    aioclient_mock.get(f"{BASE}/api/status", exc=aiohttp.ClientError(BODY_MARKER))
    with pytest.raises(FurboBridgeUnavailable) as err:
        await _client(hass).async_get_status()
    assert BODY_MARKER not in str(err.value)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/status", exc=TimeoutError())
    with pytest.raises(FurboBridgeUnavailable):
        await _client(hass).async_get_status()


async def test_non_json_200(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 200 that is not JSON is an error, and the body is not echoed."""
    aioclient_mock.get(f"{BASE}/api/status", text=f"<html>{BODY_MARKER}</html>")
    with pytest.raises(FurboBridgeError) as err:
        await _client(hass).async_get_status()
    assert BODY_MARKER not in str(err.value)


# --- one bridge, several cameras ---------------------------------------------

CAMERAS = {
    "primary": "cam-a",
    "cameras": [
        {"device_id": "cam-a", "stream": "furbo_cam-a"},
        {"device_id": "cam-b", "stream": "furbo_cam-b"},
    ],
}


async def test_requests_name_the_camera(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each camera's controls reach that camera, not the bridge's first one.

    Without this every camera talked to the unscoped paths, so a second camera
    received the primary's state and its own identity check rejected it.
    """
    aioclient_mock.get(f"{BASE}/api/cameras", json=CAMERAS)
    aioclient_mock.get(f"{BASE}/api/cameras/cam-b/status", json=FULL_STATUS)
    aioclient_mock.post(f"{BASE}/api/cameras/cam-b/toss", json={"ok": True})
    client = FurboBridgeClient(
        async_get_clientsession(hass), BASE + "/", "tok", device_id="cam-b"
    )
    assert (await client.async_get_status()).volume == 40
    await client.async_toss()
    paths = [str(call[1].path) for call in aioclient_mock.mock_calls]
    assert "/api/cameras/cam-b/status" in paths
    assert "/api/cameras/cam-b/toss" in paths
    assert "/api/status" not in paths


async def test_the_camera_list_is_only_asked_for_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Whether the bridge knows about cameras is settled once, not per call."""
    aioclient_mock.get(f"{BASE}/api/cameras", json=CAMERAS)
    aioclient_mock.get(f"{BASE}/api/cameras/cam-a/status", json=FULL_STATUS)
    client = FurboBridgeClient(
        async_get_clientsession(hass), BASE + "/", "tok", device_id="cam-a"
    )
    await client.async_get_status()
    await client.async_get_status()
    lists = [c for c in aioclient_mock.mock_calls if str(c[1].path) == "/api/cameras"]
    assert len(lists) == 1


async def test_an_older_bridge_uses_the_unscoped_paths(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bridge serving one camera has no per-camera paths.

    The integration can be updated ahead of the add-on, so it must fall back
    rather than 404 against the bridge someone is already running.
    """
    aioclient_mock.get(f"{BASE}/api/cameras", status=404)
    aioclient_mock.get(f"{BASE}/api/status", json=FULL_STATUS)
    aioclient_mock.post(f"{BASE}/api/toss", json={"ok": True})
    client = FurboBridgeClient(
        async_get_clientsession(hass), BASE + "/", "tok", device_id="cam-a"
    )
    assert (await client.async_get_status()).volume == 40
    await client.async_toss()
    paths = [str(call[1].path) for call in aioclient_mock.mock_calls]
    assert "/api/status" in paths
    assert "/api/toss" in paths


async def test_a_client_without_a_camera_stays_unscoped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With no camera named, nothing is probed and the plain paths are used."""
    aioclient_mock.get(f"{BASE}/api/status", json=FULL_STATUS)
    client = FurboBridgeClient(async_get_clientsession(hass), BASE + "/", "tok")
    await client.async_get_status()
    paths = [str(call[1].path) for call in aioclient_mock.mock_calls]
    assert paths == ["/api/status"]


async def test_a_bridge_offline_at_startup_recovers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bridge that is down when first asked must not be misremembered.

    Caching a guess from a transport failure left the client using paths the
    bridge does not have for the life of the config entry: an older bridge that
    came up later answered 404 to everything until the integration reloaded.
    """
    client = FurboBridgeClient(
        async_get_clientsession(hass), BASE + "/", "tok", device_id="cam-a"
    )
    # Down at startup: the capability question is left unanswered.
    aioclient_mock.get(f"{BASE}/api/cameras", exc=aiohttp.ClientError("offline"))
    aioclient_mock.get(f"{BASE}/api/cameras/cam-a/status", exc=aiohttp.ClientError("x"))
    with pytest.raises(FurboBridgeUnavailable):
        await client.async_get_status()

    # It comes back, and it turns out to be an older single-camera bridge.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/cameras", status=404)
    aioclient_mock.get(f"{BASE}/api/status", json=FULL_STATUS)
    assert (await client.async_get_status()).volume == 40
    assert "/api/status" in [str(c[1].path) for c in aioclient_mock.mock_calls]


async def test_a_server_error_on_the_probe_is_not_remembered(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Only 404 and 405 say the bridge is an older one; 500 says nothing."""
    client = FurboBridgeClient(
        async_get_clientsession(hass), BASE + "/", "tok", device_id="cam-a"
    )
    aioclient_mock.get(f"{BASE}/api/cameras", status=500)
    aioclient_mock.get(f"{BASE}/api/cameras/cam-a/status", json=FULL_STATUS)
    await client.async_get_status()

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/cameras", status=404)
    aioclient_mock.get(f"{BASE}/api/status", json=FULL_STATUS)
    await client.async_get_status()
    assert "/api/status" in [str(c[1].path) for c in aioclient_mock.mock_calls]


async def test_an_unknown_bridge_still_names_its_camera(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """While the bridge's age is unknown, requests still name the camera.

    The unscoped path acts on whichever camera the bridge serves first, so
    guessing it for a second camera could toss a treat from the wrong one. An
    error is the better failure.
    """
    client = FurboBridgeClient(
        async_get_clientsession(hass), BASE + "/", "tok", device_id="cam-b"
    )
    aioclient_mock.get(f"{BASE}/api/cameras", exc=TimeoutError())
    aioclient_mock.post(f"{BASE}/api/cameras/cam-b/toss", json={"ok": True})
    await client.async_toss()
    assert "/api/cameras/cam-b/toss" in [
        str(c[1].path) for c in aioclient_mock.mock_calls
    ]


async def test_cloud_token_is_read_and_sends_the_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The account's current cloud credentials come back as a pair."""
    aioclient_mock.get(
        f"{BASE}/api/cloud-token",
        json={"account_id": "ACC1", "cognito_token": "T1"},
    )
    assert await _client(hass).async_get_cloud_token() == ("ACC1", "T1")
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer tok"


async def test_cloud_token_is_never_camera_scoped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The token belongs to the account, so it is read from the plain path."""
    aioclient_mock.get(
        f"{BASE}/api/cloud-token",
        json={"account_id": "ACC1", "cognito_token": "T1"},
    )
    client = FurboBridgeClient(
        async_get_clientsession(hass), BASE + "/", "tok", device_id="cam-a"
    )
    await client.async_get_cloud_token()
    assert [str(c[1].path) for c in aioclient_mock.mock_calls] == ["/api/cloud-token"]


@pytest.mark.parametrize(
    "body",
    [
        {"account_id": "ACC1"},
        {"cognito_token": "T1"},
        {"account_id": "ACC1", "cognito_token": ""},
        {"account_id": "", "cognito_token": "T1"},
        {"account_id": 1, "cognito_token": "T1"},
        ["ACC1", "T1"],
    ],
)
async def test_cloud_token_rejects_a_useless_answer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: Any
) -> None:
    """Half a credential is worse than none: it would look like a live token."""
    aioclient_mock.get(f"{BASE}/api/cloud-token", json=body)
    with pytest.raises(FurboBridgeError):
        await _client(hass).async_get_cloud_token()


async def test_cloud_token_before_the_addon_has_logged_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bridge with no session yet answers 503, which is not an auth failure."""
    aioclient_mock.get(f"{BASE}/api/cloud-token", status=503, json={"error": "x"})
    with pytest.raises(FurboBridgeUnavailable):
        await _client(hass).async_get_cloud_token()


async def test_cloud_token_with_the_wrong_bridge_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bridge token that does not match is an auth error, not a bad body."""
    aioclient_mock.get(f"{BASE}/api/cloud-token", status=401, json={"error": "x"})
    with pytest.raises(FurboBridgeAuthError):
        await _client(hass).async_get_cloud_token()


async def test_a_slow_renewal_is_waited_for(
    hass: HomeAssistant, aiohttp_server: Any, socket_enabled: None
) -> None:
    """Renewing the cloud token gets longer than a status read.

    The bridge checks the token with the cloud and may log in before it can
    answer, which is bounded by its own 20-second cloud timeout per call, not
    by how long a read of state it already holds takes. Served by a real
    server: only a live request applies the timeout.
    """

    async def slowly(request: web.Request) -> web.Response:
        await asyncio.sleep(0.3)
        return web.json_response({"account_id": "ACC1", "cognito_token": "T1"})

    app = web.Application()
    app.router.add_get("/api/cloud-token", slowly)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = FurboBridgeClient(session, str(server.make_url("/")), "tok", timeout=1)
        # A read of cached state would have given up long before this answered.
        client._timeout = aiohttp.ClientTimeout(total=0.05)
        assert await client.async_get_cloud_token() == ("ACC1", "T1")


async def test_a_renewal_that_never_answers_is_unavailable(
    hass: HomeAssistant, aiohttp_server: Any, socket_enabled: None
) -> None:
    """A bridge that hangs is reported as unreachable, not as a bad answer.

    The coordinator waits and retries on that, rather than asking someone to
    sign in again.
    """

    async def never(request: web.Request) -> web.Response:
        await asyncio.sleep(30)
        raise AssertionError("should not get here")

    app = web.Application()
    app.router.add_get("/api/cloud-token", never)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = FurboBridgeClient(session, str(server.make_url("/")), "tok")
        client._renewal_timeout = aiohttp.ClientTimeout(total=0.1)
        with pytest.raises(FurboBridgeUnavailable):
            await client.async_get_cloud_token()
