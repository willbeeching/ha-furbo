"""Entity state and behaviour tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.furbo.api import FurboError

from .conftest import setup_integration


async def test_sensor_states(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Sensors reflect the sampled cloud data."""
    await setup_integration(hass, mock_config_entry)

    days = hass.states.get("sensor.hallway_subscription_days_left")
    assert days.state == "24"
    assert days.attributes["unit_of_measurement"] == "d"

    status = hass.states.get("sensor.hallway_subscription_status")
    assert status.state == "Active"

    barking = hass.states.get("sensor.furbo_account_barking_events_today")
    assert barking.state == "6"
    activity = hass.states.get("sensor.furbo_account_activity_events_today")
    assert activity.state == "55"

    events = hass.states.get("sensor.furbo_account_notable_events_today")
    assert events.state == "2"
    assert events.attributes["summary"]

    last = hass.states.get("sensor.hallway_last_event")
    # Most recent of the two sampled events (11:31 local, Europe/London).
    assert last.state == "2026-09-05T10:31:14+00:00"
    assert last.attributes["action"] == "standing"
    assert last.attributes["caption"] == "standing in the doorway"


async def test_missing_subscription_is_unknown(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """No subscription record yields unknown, not a crash."""
    mock_client.get_license.return_value = {"DevicesLicense": {}}
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("sensor.hallway_subscription_days_left").state == "unknown"
    assert hass.states.get("sensor.hallway_subscription_status").state == "unknown"


async def test_switches_reflect_and_write(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Alert switches show state and write through to the client."""
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get("switch.hallway_barking_alert").state == "on"
    assert hass.states.get("switch.hallway_person_alert").state == "off"
    # Only known alert keys present in the payload become switches.
    assert hass.states.get("switch.hallway_glass_breaking_alert") is None

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.hallway_person_alert"},
        blocking=True,
    )
    mock_client.set_alert_setting.assert_awaited_once_with(
        "AA11BB22CC33", "PersonDetection", True
    )
    assert hass.states.get("switch.hallway_person_alert").state == "on"

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.hallway_person_alert"},
        blocking=True,
    )
    assert hass.states.get("switch.hallway_person_alert").state == "off"


async def test_switch_write_failure_raises(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A failed write raises a translated HomeAssistantError."""
    await setup_integration(hass, mock_config_entry)
    mock_client.set_alert_setting.side_effect = FurboError("boom")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": "switch.hallway_barking_alert"},
            blocking=True,
        )


async def test_unknown_timezone_falls_back(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A bad account timezone does not break event timestamps."""
    mock_client.get_account_info.return_value = {"Timezone": "Not/AZone"}
    await setup_integration(hass, mock_config_entry)
    # Sensor still exists and either has a value or is unknown, never errors.
    assert hass.states.get("sensor.hallway_last_event") is not None


async def test_specialised_alerts_disabled_by_default(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Everyday alerts are enabled; specialised ones are created disabled."""
    await setup_integration(hass, mock_config_entry)
    registry = er.async_get(hass)

    barking = registry.async_get("switch.hallway_barking_alert")
    assert barking is not None and barking.disabled_by is None

    # ContinuousBarking is in the sampled payload but not enabled by default.
    entries = er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    continuous = next(
        e for e in entries if e.unique_id.endswith("_alert_ContinuousBarking")
    )
    assert continuous.disabled_by is er.RegistryEntryDisabler.INTEGRATION
