"""Supervisor add-on discovery for the Furbo Bridge."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.furbo.discovery import DiscoveredBridge, async_discover_bridge

ADDONS_URL = "http://supervisor/addons"
INFO_URL = "http://supervisor/addons/abc123_furbo_bridge/info"


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

    found = await async_discover_bridge(hass)
    assert found is not None
    assert found.slug == "abc123_furbo_bridge"
    assert found.host == "abc123-furbo-bridge"
    assert found.token == "s3cret"
    assert found.bridge_url == "http://abc123-furbo-bridge:8791"
    # The token is carried as the RTSP password so the stream is authenticated.
    assert found.stream_url == "rtsp://furbo:s3cret@abc123-furbo-bridge:8554/furbo"


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


def test_stream_url_encodes_token() -> None:
    """A token with URL-reserved characters is percent-encoded in the RTSP URL."""
    bridge = DiscoveredBridge(slug="s", host="h", token="a/b@c:d")
    assert bridge.stream_url == "rtsp://furbo:a%2Fb%40c%3Ad@h:8554/furbo"


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
