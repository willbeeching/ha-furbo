"""Supervisor add-on discovery for the Furbo Bridge."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.furbo.discovery import (
    DiscoveredBridge,
    async_discover_bridge,
    rtsp_password,
)

ADDONS_URL = "http://supervisor/addons"
INFO_URL = "http://supervisor/addons/abc123_furbo_bridge/info"
CAMERAS_URL = "http://abc123-furbo-bridge:8791/api/cameras"


def _addons(*addons: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"addons": list(addons)}}


def _info(**data: Any) -> dict[str, Any]:
    return {"data": data}


async def test_no_supervisor_token_returns_none(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Supervisor there is no token, so discovery does nothing."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert await async_discover_bridge(hass) is None


async def test_discovers_started_addon_with_token(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A started add-on yields its host, URLs and configured token."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    aioclient_mock.get(
        ADDONS_URL,
        json=_addons(
            {"slug": "other_addon", "state": "started"},
            {"slug": "abc123_furbo_bridge", "state": "started"},
        ),
    )
    aioclient_mock.get(
        INFO_URL,
        json=_info(hostname="abc123-furbo-bridge", options={"api_token": "s3cret"}),
    )
    aioclient_mock.get(CAMERAS_URL, json={"primary": "1", "cameras": []})

    found = await async_discover_bridge(hass)
    assert found is not None
    assert found.slug == "abc123_furbo_bridge"
    assert found.host == "abc123-furbo-bridge"
    assert found.token == "s3cret"
    assert found.bridge_url == "http://abc123-furbo-bridge:8791"
    # The RTSP password is derived from the token (not the token itself), so the
    # raw token never appears in the stream URL.
    expected = f"rtsp://furbo:{rtsp_password('s3cret')}@abc123-furbo-bridge:8554/furbo"
    assert found.stream_url == expected
    assert "s3cret" not in found.stream_url


async def test_prefers_started_over_stopped(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A started add-on is chosen ahead of a stopped one; missing token is None."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(
        ADDONS_URL,
        json=_addons(
            {"slug": "zzz_furbo_bridge", "state": "stopped"},
            {"slug": "abc123_furbo_bridge", "state": "started"},
        ),
    )
    aioclient_mock.get(INFO_URL, json=_info(hostname="h", options={}))

    found = await async_discover_bridge(hass)
    assert found is not None
    assert found.slug == "abc123_furbo_bridge"
    assert found.token is None
    # With no token the stream URL carries no credentials.
    assert found.stream_url == "rtsp://h:8554/furbo"


def test_rtsp_password_is_derived_not_the_token() -> None:
    """The RTSP password is a hash of the token, matching run.sh's algorithm."""
    token = "a/b@c:d"  # a test value, not a real secret
    expected = hashlib.sha256(f"furbo-rtsp:{token}".encode()).hexdigest()[:32]
    assert rtsp_password(token) == expected
    # 32 hex chars: safe to drop straight into a URL, and one-way from the token.
    assert len(expected) == 32
    assert token not in DiscoveredBridge(slug="s", host="h", token=token).stream_url


async def test_no_matching_addon_returns_none(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Furbo Bridge add-on installed means no discovery."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(
        ADDONS_URL, json=_addons({"slug": "mosquitto", "state": "started"})
    )
    assert await async_discover_bridge(hass) is None


async def test_falls_back_to_slug_host_when_info_missing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the info call fails, the host is derived from the slug."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(
        ADDONS_URL, json=_addons({"slug": "abc123_furbo_bridge", "state": "started"})
    )
    aioclient_mock.get(INFO_URL, status=500)

    found = await async_discover_bridge(hass)
    assert found is not None
    assert found.host == "abc123-furbo-bridge"
    assert found.token is None


async def test_supervisor_error_returns_none(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Supervisor error is swallowed and discovery returns None."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(ADDONS_URL, status=502)
    assert await async_discover_bridge(hass) is None


async def test_invalid_json_returns_none(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-JSON Supervisor response is handled without raising."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(ADDONS_URL, text="<<not json>>")
    assert await async_discover_bridge(hass) is None


async def test_each_camera_gets_its_own_stream(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With several cameras the add-on names a stream for each."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(
        ADDONS_URL, json=_addons({"slug": "abc123_furbo_bridge", "state": "started"})
    )
    aioclient_mock.get(
        INFO_URL,
        json=_info(hostname="abc123-furbo-bridge", options={"api_token": "s3cret"}),
    )
    aioclient_mock.get(
        CAMERAS_URL,
        json={
            "primary": "111",
            "cameras": [
                {"device_id": "111", "stream": "furbo_111"},
                {"device_id": "222", "stream": "furbo_222"},
            ],
        },
    )

    found = await async_discover_bridge(hass)
    assert found is not None
    auth = f"furbo:{rtsp_password('s3cret')}@abc123-furbo-bridge:8554"
    assert found.stream_url_for("111") == f"rtsp://{auth}/furbo_111"
    assert found.stream_url_for("222") == f"rtsp://{auth}/furbo_222"
    # A camera the bridge does not serve falls back rather than inventing a name.
    assert found.stream_url_for("999") == f"rtsp://{auth}/furbo"


async def test_older_addon_without_a_camera_list(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An add-on that predates multiple cameras has no such endpoint.

    Every camera then points at the single stream it does publish, so updating
    the integration ahead of the add-on does not break the existing one.
    """
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(
        ADDONS_URL, json=_addons({"slug": "abc123_furbo_bridge", "state": "started"})
    )
    aioclient_mock.get(
        INFO_URL, json=_info(hostname="abc123-furbo-bridge", options={"api_token": "s"})
    )
    aioclient_mock.get(CAMERAS_URL, status=404)

    found = await async_discover_bridge(hass)
    assert found is not None
    assert found.streams == {}
    assert found.stream_url_for("111").endswith("/furbo")


async def test_unreachable_camera_list_falls_back(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge that cannot be reached still yields usable URLs.

    Discovery only fills in suggestions, so a stopped or slow add-on must not
    stop the options flow from offering something sensible.
    """
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(
        ADDONS_URL, json=_addons({"slug": "abc123_furbo_bridge", "state": "started"})
    )
    aioclient_mock.get(
        INFO_URL, json=_info(hostname="abc123-furbo-bridge", options={"api_token": "s"})
    )
    aioclient_mock.get(CAMERAS_URL, exc=TimeoutError())

    found = await async_discover_bridge(hass)
    assert found is not None
    assert found.streams == {}


async def test_a_malformed_camera_list_is_ignored(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries without a device id or stream name are dropped, not guessed at."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(
        ADDONS_URL, json=_addons({"slug": "abc123_furbo_bridge", "state": "started"})
    )
    aioclient_mock.get(
        INFO_URL, json=_info(hostname="abc123-furbo-bridge", options={"api_token": "s"})
    )
    aioclient_mock.get(
        CAMERAS_URL,
        json={
            "cameras": [
                {"device_id": "1", "stream": "furbo_1"},
                {"stream": "no device id"},
                "junk",
            ]
        },
    )

    found = await async_discover_bridge(hass)
    assert found is not None
    assert found.streams == {"1": "furbo_1"}


async def test_camera_list_that_is_not_a_document(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body that is not a camera list yields no streams rather than raising."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    aioclient_mock.get(
        ADDONS_URL, json=_addons({"slug": "abc123_furbo_bridge", "state": "started"})
    )
    aioclient_mock.get(
        INFO_URL, json=_info(hostname="abc123-furbo-bridge", options={"api_token": "s"})
    )
    aioclient_mock.get(CAMERAS_URL, json=["not", "a", "document"])

    found = await async_discover_bridge(hass)
    assert found is not None
    assert found.streams == {}
