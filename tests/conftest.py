"""Fixtures for the Furbo tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.furbo.bridge import BridgeState
from custom_components.furbo.const import (
    CONF_ACCOUNT_ID,
    CONF_COGNITO_TOKEN,
    CONF_MOBILE_ID,
    DOMAIN,
)

from . import const as c


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading of the custom integration in every test."""
    yield


def _make_client() -> AsyncMock:
    """Return an AsyncMock shaped like FurboClient with live-sample data."""
    client = AsyncMock()
    client.account_id = c.ACCOUNT_ID
    client.cognito_token = c.COGNITO_TOKEN
    client.start_login.return_value = "CANDIDATE"
    client.send_mfa_code.return_value = "CANDIDATE2"
    client.get_account_info.return_value = c.ACCOUNT_INFO
    client.get_devices.return_value = [dict(c.DEVICE)]
    client.get_license.return_value = c.LICENSE_RESPONSE["DevicesLicense"]
    client.get_alert_settings.return_value = dict(c.ALERTS)
    # Return the sampled totals for whatever date the coordinator asks about,
    # so the tests do not depend on the real wall-clock date.
    _totals = next(iter(c.ACTIVITY_TOTALS.values()))
    client.get_activity_report.side_effect = lambda dates: {
        day: dict(_totals) for day in dates
    }
    client.get_daily_summary.return_value = c.DAILY_SUMMARY
    client.get_notable_events.return_value = list(c.NOTABLE_EVENTS)
    return client


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Patch FurboClient everywhere it is constructed."""
    client = _make_client()
    with (
        patch("custom_components.furbo.FurboClient", return_value=client),
        patch("custom_components.furbo.config_flow.FurboClient", return_value=client),
    ):
        yield client


BRIDGE_STATE = BridgeState(
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
    firmware="108",
)


def _make_bridge() -> AsyncMock:
    """Return an AsyncMock shaped like FurboBridgeClient."""
    bridge = AsyncMock()
    bridge.async_get_status.return_value = BRIDGE_STATE
    bridge.async_set.return_value = BRIDGE_STATE
    return bridge


@pytest.fixture
def mock_bridge() -> Generator[AsyncMock]:
    """Patch FurboBridgeClient where the integration constructs it."""
    bridge = _make_bridge()
    with patch("custom_components.furbo.FurboBridgeClient", return_value=bridge):
        yield bridge


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the sample account."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=c.ACCOUNT_ID,
        title=c.EMAIL,
        data={
            CONF_EMAIL: c.EMAIL,
            CONF_PASSWORD: c.PASSWORD,
            CONF_ACCOUNT_ID: c.ACCOUNT_ID,
            CONF_COGNITO_TOKEN: c.COGNITO_TOKEN,
            CONF_MOBILE_ID: "MOBILE-ID",
        },
    )


async def setup_integration(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Add and set up a config entry, optionally with options, returning it."""
    entry.add_to_hass(hass)
    if options is not None:
        hass.config_entries.async_update_entry(entry, options=options)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
