"""Coordinator behaviour: recovery, event handling, stale devices."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.furbo.api import (
    FurboAuthError,
    FurboConnectionError,
    FurboError,
)

from . import const as c
from .conftest import setup_integration


async def test_offline_then_recovery(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Entities go unavailable on a failed refresh and recover afterwards."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    assert hass.states.get("sensor.test_camera_subscription_days_left").state == "24"

    mock_client.get_devices.side_effect = FurboConnectionError("down")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    days = hass.states.get("sensor.test_camera_subscription_days_left")
    assert days.state == "unavailable"

    mock_client.get_devices.side_effect = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.test_camera_subscription_days_left").state == "24"


async def test_failed_refresh_keeps_no_stale_value(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A failed refresh raises UpdateFailed rather than returning empty data."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    mock_client.get_alert_settings.side_effect = FurboConnectionError("down")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_events_pick_latest_per_device(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The most recent event for a device is the one exposed."""
    await setup_integration(hass, mock_config_entry)
    last = hass.states.get("sensor.test_camera_last_event")
    assert last.state == "2026-09-05T10:31:14+00:00"


async def test_event_for_other_device_ignored(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Events for an unknown device id are not attached to our camera."""
    mock_client.get_notable_events.return_value = [
        {
            "DeviceId": "SOMEONE_ELSE",
            "LocalTime": "2026-09-05 12:00:00",
            "Caption": "not ours",
            "ActionCaption": "walking",
        }
    ]
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("sensor.test_camera_last_event").state == "unknown"


async def test_device_removed_becomes_unavailable(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A device dropping out of the account makes its entities unavailable."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    mock_client.get_devices.return_value = []
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    days = hass.states.get("sensor.test_camera_subscription_days_left")
    assert days.state == "unavailable"


async def test_setup_generic_error_retries(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A generic API error during first-time setup triggers retry."""
    mock_client.get_account_info.side_effect = FurboError("nope")
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_generic_error_during_update(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A generic API error during refresh raises UpdateFailed."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    mock_client.get_license.side_effect = FurboError("nope")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_malformed_event_time_skipped(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """An event with an unparseable timestamp is ignored, not fatal."""
    mock_client.get_notable_events.return_value = [
        {
            "DeviceId": c.DEVICE_ID,
            "LocalTime": "not-a-timestamp",
            "Caption": "bad",
            "ActionCaption": "x",
        }
    ]
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("sensor.test_camera_last_event").state == "unknown"


async def test_auth_error_during_update(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """An auth error during a later refresh raises ConfigEntryAuthFailed."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    mock_client.get_devices.side_effect = FurboAuthError("expired")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_calendar_rate_limit_does_not_fail_setup(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """An 80002 rate limit on the calendar host is best-effort, not fatal."""
    mock_client.get_daily_summary.side_effect = FurboError("rate limited", code=80002)
    await setup_integration(hass, mock_config_entry)

    # The entry still loads and the non-calendar data is present.
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.test_camera_subscription_days_left").state == "24"

    # No calendar data this cycle (there was no previous cycle to carry over).
    data = mock_config_entry.runtime_data.coordinator.data
    assert data.notable_events_today == 0
    assert data.activity_today == {}
    assert data.daily_summary == ""


async def test_calendar_failure_carries_over_previous(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A later calendar failure keeps the previous cycle's event data."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.data.activity_today["Barking"] == 6
    assert coordinator.data.notable_events_today == 2
    last_before = hass.states.get("sensor.test_camera_last_event").state

    mock_client.get_activity_report.side_effect = FurboError("rate", code=80002)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Previous calendar values are retained rather than blanked.
    assert coordinator.data.activity_today["Barking"] == 6
    assert coordinator.data.notable_events_today == 2
    assert hass.states.get("sensor.test_camera_last_event").state == last_before


async def test_calendar_auth_error_triggers_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """An auth failure on the calendar still raises for reauth, not best-effort."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    mock_client.get_notable_events.side_effect = FurboAuthError("bad token")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
