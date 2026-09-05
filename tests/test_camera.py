"""Camera platform tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.components.camera import (
    CameraEntityFeature,
    async_get_stream_source,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.furbo.camera import FurboCamera
from custom_components.furbo.const import CONF_STREAM_URLS

from . import const as c
from .conftest import setup_integration

STREAM_URL = "rtsp://go2rtc.local:8554/furbo"


async def test_no_camera_without_stream_url(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A device without a configured stream URL gets no camera entity."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("camera.test_camera") is None
    assert not hass.states.async_entity_ids("camera")


async def test_camera_streams_from_configured_url(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A configured URL produces a streaming camera as the device's main entity."""
    await setup_integration(
        hass, mock_config_entry, {CONF_STREAM_URLS: {c.DEVICE_ID: STREAM_URL}}
    )

    state = hass.states.get("camera.test_camera")
    assert state is not None
    assert state.state == "idle"
    assert state.attributes["friendly_name"] == "Test Camera"
    assert state.attributes["brand"] == "Furbo"
    assert state.attributes["model_name"] == "Furbo 360"
    assert state.attributes["supported_features"] == CameraEntityFeature.STREAM
    assert await async_get_stream_source(hass, "camera.test_camera") == STREAM_URL

    registry = er.async_get(hass)
    entry = registry.async_get("camera.test_camera")
    assert entry is not None
    assert entry.unique_id == f"{c.DEVICE_ID}_camera"

    camera = hass.data["camera"].get_entity("camera.test_camera")
    assert isinstance(camera, FurboCamera)
    assert camera.use_stream_for_stills is True
    assert await camera.async_camera_image() is None


async def test_camera_for_unknown_model(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """An unknown product id is shown raw; a missing one leaves model unset."""
    mock_client.get_devices.return_value = [
        {**c.DEVICE, "ProductId": "FB9999"},
        {**c.DEVICE, "Id": "ZZ99", "DeviceName": "Kitchen", "ProductId": None},
    ]
    await setup_integration(
        hass,
        mock_config_entry,
        {CONF_STREAM_URLS: {c.DEVICE_ID: STREAM_URL, "ZZ99": STREAM_URL}},
    )
    assert hass.states.get("camera.test_camera").attributes["model_name"] == "FB9999"
    assert "model_name" not in hass.states.get("camera.kitchen").attributes
