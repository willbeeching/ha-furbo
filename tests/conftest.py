"""Fixtures for the Furbo tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    client.get_activity_report.return_value = c.ACTIVITY_TOTALS
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
    hass: HomeAssistant, entry: MockConfigEntry
) -> MockConfigEntry:
    """Add and set up a config entry, returning it."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
