"""Coordinator behaviour: recovery, event handling, stale devices."""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
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
from custom_components.furbo.bridge import FurboBridgeError, FurboBridgeUnavailable
from custom_components.furbo.const import (
    CONF_ACCOUNT_ID,
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_BRIDGES,
    CONF_COGNITO_TOKEN,
    CONF_TOKEN_ISSUED_AT,
)
from custom_components.furbo.coordinator import MAX_RENEWAL_ATTEMPTS

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


async def test_expired_token_replaced_from_the_bridge(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A refused token is swapped for the add-on's and the poll retried."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.token_source = AsyncMock(return_value=(c.ACCOUNT_ID, "FRESH-TOKEN"))

    attempts = 0

    def _devices() -> list[dict[str, object]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FurboAuthError("expired")
        return [dict(c.DEVICE)]

    mock_client.get_devices.side_effect = _devices
    data = await coordinator._async_update_data()

    assert attempts == 2
    assert set(data.devices) == {c.DEVICE_ID}
    assert mock_client.cognito_token == "FRESH-TOKEN"
    assert mock_config_entry.data[CONF_COGNITO_TOKEN] == "FRESH-TOKEN"
    assert mock_config_entry.data[CONF_ACCOUNT_ID] == c.ACCOUNT_ID
    assert isinstance(mock_config_entry.data[CONF_TOKEN_ISSUED_AT], float)


async def test_expired_token_without_a_bridge_still_reauths(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """With no add-on to ask, an expired token still sends the user to reauth."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.token_source is None
    mock_client.get_devices.side_effect = FurboAuthError("expired")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_a_bridge_that_refuses_still_reauths(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A bridge that answers but cannot help does not swallow the reauth."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.token_source = AsyncMock(side_effect=FurboBridgeError("no", status=404))
    mock_client.get_devices.side_effect = FurboAuthError("expired")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_a_slow_bridge_defers_instead_of_prompting(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A renewal that times out waits for the next poll, not for a person.

    Renewing takes as long as a cloud login, so a bridge that is slow or
    restarting is not evidence that recovery has failed; a sign-in prompt
    raised there is one nobody can tell from a real one.
    """
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.token_source = AsyncMock(
        side_effect=FurboBridgeUnavailable("Bridge unreachable: TimeoutError")
    )
    mock_client.get_devices.side_effect = FurboAuthError("expired")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    # And the entities go unavailable rather than the entry asking for a login.
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == SOURCE_REAUTH
    ]


async def test_a_bridge_that_stays_slow_gives_up(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Waiting is bounded: an add-on that is gone must not strand the entry."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.token_source = AsyncMock(side_effect=FurboBridgeUnavailable("down"))
    mock_client.get_devices.side_effect = FurboAuthError("expired")

    for _ in range(MAX_RENEWAL_ATTEMPTS):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_the_wait_starts_over_after_a_recovery(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A bridge that answers once has its full allowance again."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    mock_client.get_devices.side_effect = FurboAuthError("expired")
    coordinator.token_source = AsyncMock(side_effect=FurboBridgeUnavailable("down"))
    for _ in range(MAX_RENEWAL_ATTEMPTS):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    attempts = 0

    def _devices() -> list[dict[str, object]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FurboAuthError("expired")
        return [dict(c.DEVICE)]

    mock_client.get_devices.side_effect = _devices
    coordinator.token_source = AsyncMock(return_value=(c.ACCOUNT_ID, "FRESH-TOKEN"))
    await coordinator._async_update_data()

    mock_client.get_devices.side_effect = FurboAuthError("expired again")
    coordinator.token_source = AsyncMock(side_effect=FurboBridgeUnavailable("down"))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_bridge_offering_the_same_token_still_reauths(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The token just refused is not worth retrying with."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.token_source = AsyncMock(
        return_value=(c.ACCOUNT_ID, c.COGNITO_TOKEN),
    )
    mock_client.get_devices.side_effect = FurboAuthError("expired")
    before = mock_client.get_devices.await_count
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    # One attempt, not a second with the token the cloud had just refused.
    assert mock_client.get_devices.await_count == before + 1


async def test_a_bridge_on_another_account_is_refused(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A token for a different Furbo account must never be adopted.

    It would authenticate, and this entry would then be reading another
    account's cameras behind a unique id, devices and entities that belong to
    the account it was set up with.
    """
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.token_source = AsyncMock(return_value=("SOMEONE-ELSE", "THEIR-TOKEN"))
    mock_client.get_devices.side_effect = FurboAuthError("expired")

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert mock_client.cognito_token == c.COGNITO_TOKEN
    assert mock_client.account_id == c.ACCOUNT_ID
    assert mock_config_entry.data[CONF_COGNITO_TOKEN] == c.COGNITO_TOKEN
    assert mock_config_entry.data[CONF_ACCOUNT_ID] == c.ACCOUNT_ID
    assert "different Furbo account" in caplog.text


async def test_setup_after_the_token_died_overnight(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_bridge: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restart with a dead token loads from the add-on's, not a reauth.

    The very first call of a restart is the account read, before there is any
    coordinator data to work from, which is why the bridge is resolved before
    the first refresh rather than after it.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.furbo.coordinator")
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_TOKEN_ISSUED_AT: time.time() - 26 * 3600},
        options={
            CONF_BRIDGES: {
                c.DEVICE_ID: {
                    CONF_BRIDGE_URL: "http://bridge:8791",
                    CONF_BRIDGE_TOKEN: "tok",
                }
            }
        },
    )
    mock_bridge.async_get_cloud_token.return_value = (c.ACCOUNT_ID, "FRESH-TOKEN")
    mock_client.get_account_info.side_effect = [
        FurboAuthError("expired"),
        c.ACCOUNT_INFO,
    ]

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data[CONF_COGNITO_TOKEN] == "FRESH-TOKEN"
    # The log says how long the dead token had lasted, which is the only way
    # to learn the cloud's actual token lifetime.
    assert "issued 26.0h ago" in caplog.text
