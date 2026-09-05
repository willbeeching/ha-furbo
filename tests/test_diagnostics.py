"""Diagnostics content and redaction tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.furbo.const import CONF_BRIDGES, CONF_STREAM_URLS
from custom_components.furbo.diagnostics import async_get_config_entry_diagnostics

from . import const as c
from .conftest import setup_integration


async def test_diagnostics_useful_and_safe(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Diagnostics carry useful fields and no identifiers or secrets."""
    stream_url = "rtsp://user:hunter2@192.0.2.10:8554/furbo"
    await setup_integration(
        hass,
        mock_config_entry,
        {
            "scan_interval": 120,
            CONF_STREAM_URLS: {c.DEVICE_ID: stream_url},
            CONF_BRIDGES: {
                c.DEVICE_ID: {
                    "bridge_url": "http://10.9.8.7:8791",
                    "bridge_token": "tok3n",
                }
            },
        },
    )
    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    # Useful content is present.
    device = diag["devices"][0]
    assert device["ProductId"] == "FB0030"
    assert device["FirmwareVersion"] == "108"
    assert device["alerts"]["Barking"] == "1"
    assert device["subscription"]["SubscriptionStatus"] == "Active"
    assert diag["activity_today"]["Barking"] == 6
    assert diag["options"] == {"scan_interval": 120}
    assert device["has_stream_url"] is True
    assert device["bridge"] == {
        "last_update_success": True,
        "connected": True,
        "firmware": "108",
    }

    # Nothing sensitive leaks anywhere in the serialised report.
    blob = json.dumps(diag)
    for secret in (
        c.ACCOUNT_ID,
        c.DEVICE_ID,
        c.COGNITO_TOKEN,
        c.PASSWORD,
        c.DEVICE["P2PUuid"],
        c.DEVICE["P2PAccountKey"],
        c.DEVICE["AuthKey"],
        stream_url,
        "hunter2",
        "10.9.8.7",
        "tok3n",
    ):
        assert secret not in blob
    # Cooldown frequency keys are not exposed either.
    assert "Frequency:Barking" not in blob
