"""Entities backed by the HTTP bridge: switches, volume, selects, buttons."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
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
    assert [call.kwargs for call in mock_bridge.async_set.await_args_list] == [
        {"camera_on": False},
        {"volume": 70},
        {"night_mode": "off"},
        {"auto_tracking": True},
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
