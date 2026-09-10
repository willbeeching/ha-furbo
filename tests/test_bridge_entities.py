"""Entities backed by the HTTP bridge: switches, volume, selects, buttons."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.camera import async_get_stream_source
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.furbo.bridge import BridgeState, FurboBridgeUnavailable
from custom_components.furbo.const import (
    BRIDGE_SCAN_INTERVAL,
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_BRIDGES,
)
from custom_components.furbo.discovery import DiscoveredBridge, rtsp_password

from . import const as c
from .conftest import BRIDGE_STATE, setup_integration

BRIDGE_OPTIONS = {
    CONF_BRIDGES: {
        c.DEVICE_ID: {
            CONF_BRIDGE_URL: "http://bridge.local:8791",
            CONF_BRIDGE_TOKEN: "tok",
        }
    }
}

ENTITIES = (
    "switch.test_camera_camera",
    "switch.test_camera_auto_pet_tracking",
    "switch.test_camera_auto_zoom",
    "number.test_camera_speaker_volume",
    "select.test_camera_night_vision",
    "select.test_camera_barking_sensitivity",
    "button.test_camera_pan_left",
    "button.test_camera_pan_right",
    "button.test_camera_toss_treat",
    "button.test_camera_play_treat_sound",
    "switch.test_camera_voice_control",
    "switch.test_camera_on_off_schedule",
    "switch.test_camera_calm_my_pet",
    "select.test_camera_treat_size",
    "select.test_camera_video_quality",
    "sensor.test_camera_treat_toss_sound",
)


async def test_no_bridge_entities_without_bridge(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Without a bridge URL none of the bridge entities exist."""
    await setup_integration(hass, mock_config_entry)
    for entity_id in ENTITIES:
        assert hass.states.get(entity_id) is None


async def test_bridge_for_unknown_device_is_skipped(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A bridge configured for a camera no longer on the account is ignored."""
    await setup_integration(
        hass,
        mock_config_entry,
        {CONF_BRIDGES: {"GONE": {CONF_BRIDGE_URL: "http://x"}}},
    )
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert not mock_config_entry.runtime_data.bridges
    mock_bridge.async_get_status.assert_not_awaited()


async def test_bridge_entities_reflect_state(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The bridge state shows up on the right entities, on the camera device."""
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    assert hass.states.get("switch.test_camera_camera").state == "on"
    assert hass.states.get("switch.test_camera_auto_pet_tracking").state == "off"
    assert hass.states.get("switch.test_camera_auto_zoom").state == "on"
    assert hass.states.get("number.test_camera_speaker_volume").state == "40"
    assert hass.states.get("select.test_camera_night_vision").state == "auto"
    assert hass.states.get("select.test_camera_barking_sensitivity").state == "medium"
    assert hass.states.get("switch.test_camera_voice_control").state == "on"
    assert hass.states.get("switch.test_camera_on_off_schedule").state == "off"
    assert hass.states.get("switch.test_camera_calm_my_pet").state == "on"
    assert hass.states.get("select.test_camera_treat_size").state == "large"
    assert hass.states.get("select.test_camera_video_quality").state == "1080p"
    assert hass.states.get("sensor.test_camera_treat_toss_sound").state == "default"
    for entity_id in ENTITIES:
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state != "unavailable", entity_id
    # Same friendly-name prefix as the cloud entities: one device.
    assert (
        hass.states.get("button.test_camera_toss_treat").attributes["friendly_name"]
        == "Test Camera Toss treat"
    )


async def test_unreported_fields_are_unknown(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Fields the camera has not reported yet read as unknown, not as off."""
    mock_bridge.async_get_status.return_value = BridgeState(connected=True)
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    assert hass.states.get("switch.test_camera_camera").state == "unknown"
    assert hass.states.get("number.test_camera_speaker_volume").state == "unknown"
    assert hass.states.get("select.test_camera_night_vision").state == "unknown"


async def test_writes_go_through_bridge(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Each control posts the matching setting and applies the state read back."""
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    mock_bridge.async_set.return_value = replace(
        BRIDGE_STATE, camera_on=False, volume=70, night_mode="off", auto_zoom=False
    )

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.test_camera_camera"}, blocking=True
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.test_camera_speaker_volume", "value": 70.4},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.test_camera_night_vision", "option": "off"},
        blocking=True,
    )
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.test_camera_auto_pet_tracking"},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.test_camera_treat_size", "option": "small"},
        blocking=True,
    )
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.test_camera_voice_control"},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.test_camera_video_quality", "option": "720p"},
        blocking=True,
    )
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.test_camera_on_off_schedule"},
        blocking=True,
    )
    assert [call.kwargs for call in mock_bridge.async_set.await_args_list] == [
        {"camera_on": False},
        {"volume": 70},
        {"night_mode": "off"},
        {"auto_tracking": True},
        {"treat_size": "small"},
        {"voice_control": False},
        {"quality": "720p"},
        {"schedule_enabled": False},
    ]
    assert hass.states.get("switch.test_camera_camera").state == "off"
    assert hass.states.get("number.test_camera_speaker_volume").state == "70"
    assert hass.states.get("select.test_camera_night_vision").state == "off"
    assert hass.states.get("switch.test_camera_auto_zoom").state == "off"


async def test_buttons(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Buttons call the bridge actions with the app's default pan angle."""
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    for key in ("pan_left", "pan_right", "toss_treat", "play_treat_sound"):
        await hass.services.async_call(
            "button", "press", {"entity_id": f"button.test_camera_{key}"}, blocking=True
        )
    assert [call.args for call in mock_bridge.async_pan.await_args_list] == [
        ("left", 60),
        ("right", 60),
    ]
    mock_bridge.async_toss.assert_awaited_once()
    mock_bridge.async_treat_sound.assert_awaited_once()


async def test_write_failure_is_translated(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A bridge error during a write or an action becomes a HomeAssistantError."""
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    mock_bridge.async_set.side_effect = FurboBridgeUnavailable("Bridge returned 503")
    mock_bridge.async_toss.side_effect = FurboBridgeUnavailable("Bridge returned 503")
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": "switch.test_camera_camera"},
            blocking=True,
        )
    assert err.value.translation_key == "bridge_command_failed"
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.test_camera_toss_treat"},
            blocking=True,
        )
    assert err.value.translation_key == "bridge_command_failed"
    # The failed write did not touch the state.
    assert hass.states.get("switch.test_camera_camera").state == "on"


async def test_bridge_down_at_startup_does_not_block_setup(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An unreachable bridge loads the entry with unavailable bridge entities."""
    mock_bridge.async_get_status.side_effect = FurboBridgeUnavailable("down")
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.test_camera_subscription_days_left").state == "24"
    assert hass.states.get("switch.test_camera_camera").state == "unavailable"

    # It recovers on the next poll.
    mock_bridge.async_get_status.side_effect = None
    freezer.tick(BRIDGE_SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("switch.test_camera_camera").state == "on"


async def test_bridge_without_p2p_session_is_unavailable(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A reachable bridge with no P2P session leaves the entities unavailable."""
    mock_bridge.async_get_status.return_value = replace(BRIDGE_STATE, connected=False)
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    for entity_id in ENTITIES:
        assert hass.states.get(entity_id).state == "unavailable", entity_id


async def test_bridge_serving_wrong_camera_is_unavailable(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A bridge reporting a different cloud device id is rejected, not trusted."""
    mock_bridge.async_get_status.return_value = replace(
        BRIDGE_STATE, device_id="9999999999999999"
    )
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    for entity_id in ENTITIES:
        assert hass.states.get(entity_id).state == "unavailable", entity_id


async def test_bridge_matching_device_id_is_trusted(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A bridge reporting the right device id drives the entities normally."""
    mock_bridge.async_get_status.return_value = replace(
        BRIDGE_STATE, device_id=c.DEVICE_ID
    )
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)
    assert hass.states.get("switch.test_camera_camera").state == "on"


async def test_discovered_addon_auto_creates_entities(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running Furbo Bridge add-on auto-creates the camera and its controls."""
    monkeypatch.setattr(
        "custom_components.furbo.async_discover_bridge",
        AsyncMock(
            return_value=DiscoveredBridge(
                slug="abc_furbo_bridge", host="abc-furbo-bridge", token="tok"
            )
        ),
    )

    # No options at all — everything comes from the discovered add-on.
    await setup_integration(hass, mock_config_entry)

    # The camera is created and streams from the add-on's RTSP URL, with the
    # derived RTSP password (not the raw token).
    assert hass.states.get("camera.test_camera") is not None
    source = await async_get_stream_source(hass, "camera.test_camera")
    assert source == f"rtsp://furbo:{rtsp_password('tok')}@abc-furbo-bridge:8554/furbo"
    # The bridge-backed controls are created too.
    assert hass.states.get("button.test_camera_toss_treat") is not None
    assert hass.states.get("switch.test_camera_camera").state == "on"
    assert hass.states.get("number.test_camera_speaker_volume").state == "40"


async def test_discovered_addon_not_auto_bound_with_multiple_cameras(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An add-on that cannot say which camera is which binds to neither.

    It publishes one stream under the plain name. With two cameras on the
    account there is no telling whose it is, and handing it to both shows the
    first camera's video under the other.
    """
    second = dict(c.DEVICE)
    second["Id"] = 1788515097680000
    second["DeviceName"] = "Second Camera"
    mock_client.get_devices.return_value = [dict(c.DEVICE), second]
    monkeypatch.setattr(
        "custom_components.furbo.async_discover_bridge",
        AsyncMock(
            return_value=DiscoveredBridge(
                slug="abc_furbo_bridge", host="abc-furbo-bridge", token="tok"
            )
        ),
    )

    await setup_integration(hass, mock_config_entry)

    # Nothing was auto-bound: no bridge coordinators, no stream URLs, no
    # bridge-backed entities, and a warning tells the user why.
    runtime = mock_config_entry.runtime_data
    assert runtime.bridges == {}
    assert runtime.stream_urls == {}
    assert hass.states.get("button.test_camera_toss_treat") is None
    # No stream URL was bound, so no camera entity is created.
    assert hass.states.get("camera.test_camera") is None
    assert "2 cameras" in caplog.text


async def test_a_bridge_that_names_its_cameras_binds_all_of_them(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every camera gets its own stream from one add-on, with no configuring.

    This is what the multi-camera release was for, and what it did not do:
    the guard predated the add-on serving more than one camera, so an account
    with several got nothing bound at all and had to be set up by hand.
    """
    second = dict(c.DEVICE)
    second["Id"] = 1788515097680000
    second["DeviceName"] = "Second Camera"
    mock_client.get_devices.return_value = [dict(c.DEVICE), second]
    monkeypatch.setattr(
        "custom_components.furbo.async_discover_bridge",
        AsyncMock(
            return_value=DiscoveredBridge(
                slug="abc_furbo_bridge",
                host="abc-furbo-bridge",
                token="tok",
                streams={
                    c.DEVICE_ID: f"furbo_{c.DEVICE_ID}",
                    "1788515097680000": "furbo_1788515097680000",
                },
            )
        ),
    )

    await setup_integration(hass, mock_config_entry)

    runtime = mock_config_entry.runtime_data
    assert set(runtime.bridges) == {c.DEVICE_ID, "1788515097680000"}
    auth = f"furbo:{rtsp_password('tok')}@abc-furbo-bridge:8554"
    # Each camera on its own stream: the whole point, and the thing that was
    # reported broken with three cameras all playing the first one's video.
    assert runtime.stream_urls == {
        c.DEVICE_ID: f"rtsp://{auth}/furbo_{c.DEVICE_ID}",
        "1788515097680000": f"rtsp://{auth}/furbo_1788515097680000",
    }
    assert len(set(runtime.stream_urls.values())) == 2


async def test_a_camera_the_bridge_does_not_serve_gets_no_stream(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A camera the add-on skips is left without video, and said so in the log.

    The add-on's device_id option can pin it to some of the account's cameras.
    The rest have no stream of their own, and the plain one is not theirs.
    """
    second = dict(c.DEVICE)
    second["Id"] = 1788515097680000
    second["DeviceName"] = "Second Camera"
    mock_client.get_devices.return_value = [dict(c.DEVICE), second]
    monkeypatch.setattr(
        "custom_components.furbo.async_discover_bridge",
        AsyncMock(
            return_value=DiscoveredBridge(
                slug="abc_furbo_bridge",
                host="abc-furbo-bridge",
                token="tok",
                streams={c.DEVICE_ID: f"furbo_{c.DEVICE_ID}"},
            )
        ),
    )

    await setup_integration(hass, mock_config_entry)

    runtime = mock_config_entry.runtime_data
    assert set(runtime.stream_urls) == {c.DEVICE_ID}
    assert "1788515097680000" in caplog.text


async def test_explicit_bridge_still_binds_with_multiple_cameras(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit per-camera config is honoured even with multiple cameras."""
    second = dict(c.DEVICE)
    second["Id"] = 1788515097680000
    second["DeviceName"] = "Second Camera"
    mock_client.get_devices.return_value = [dict(c.DEVICE), second]
    monkeypatch.setattr(
        "custom_components.furbo.async_discover_bridge",
        AsyncMock(
            return_value=DiscoveredBridge(
                slug="abc_furbo_bridge", host="abc-furbo-bridge", token="tok"
            )
        ),
    )

    # The first camera is mapped explicitly; the second is not.
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)

    runtime = mock_config_entry.runtime_data
    assert set(runtime.bridges) == {c.DEVICE_ID}
    assert hass.states.get("switch.test_camera_camera") is not None
