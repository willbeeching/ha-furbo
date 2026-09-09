"""Setup, unload, reload and failure-classification tests."""

from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import quote

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.furbo.api import FurboAuthError, FurboConnectionError
from custom_components.furbo.const import (
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_BRIDGES,
    CONF_COGNITO_TOKEN,
    CONF_EVENTS_ENABLED,
    CONF_SCAN_INTERVAL,
    CONF_STREAM_URLS,
    DOMAIN,
)
from custom_components.furbo.discovery import DiscoveredBridge, rtsp_password

from . import const as c
from .conftest import setup_integration


async def test_setup_and_unload(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A healthy entry loads and unloads cleanly."""
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_repeated_unload_is_safe(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Unloading twice does not raise."""
    await setup_integration(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A rejected token puts the entry into reauth."""
    mock_client.get_account_info.side_effect = FurboAuthError("expired")
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)


async def test_transient_failure_retries(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A connection error during first refresh triggers retry, not reauth."""
    mock_client.get_devices.side_effect = FurboConnectionError("down")
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_options_change_reloads(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Changing options reloads the entry and drops account sensors."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("sensor.furbo_account_notable_events_today") is not None

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_SCAN_INTERVAL: 300, CONF_EVENTS_ENABLED: False},
    )
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    # The account sensor is no longer produced; a device sensor still works.
    account = hass.states.get("sensor.furbo_account_notable_events_today")
    assert account is None or account.state == "unavailable"
    assert hass.states.get("sensor.test_camera_subscription_days_left").state == "24"


async def test_new_cloud_token_does_not_reload(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Storing a fresh cloud token leaves the loaded entry alone.

    The coordinator writes the token it took from the add-on into entry.data
    mid-poll; reloading there would tear the coordinator down underneath
    itself, and nothing set up from the entry depends on the token.
    """
    await setup_integration(hass, mock_config_entry)
    runtime = mock_config_entry.runtime_data

    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_COGNITO_TOKEN: "FRESH-TOKEN"},
    )
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is runtime


async def test_configured_bridge_supplies_the_token_source(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A configured bridge is wired up as the coordinator's token source."""
    await setup_integration(
        hass,
        mock_config_entry,
        options={
            CONF_BRIDGES: {
                c.DEVICE_ID: {
                    CONF_BRIDGE_URL: "http://bridge:8791",
                    CONF_BRIDGE_TOKEN: "tok",
                }
            }
        },
    )
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.token_source is mock_bridge.async_get_cloud_token


async def test_discovered_addon_supplies_the_token_source(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no bridge configured, the discovered add-on is asked instead.

    It has to be in place before the first poll: a restart after the token has
    died overnight fails on that very first call.
    """
    monkeypatch.setattr(
        "custom_components.furbo.async_discover_bridge",
        AsyncMock(
            return_value=DiscoveredBridge(
                slug="abc_furbo_bridge", host="abc-furbo-bridge", token="tok"
            )
        ),
    )
    mock_client.get_devices.side_effect = [
        FurboAuthError("expired"),
        [dict(c.DEVICE)],
    ]
    mock_bridge.async_get_cloud_token.return_value = (c.ACCOUNT_ID, "FRESH-TOKEN")

    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data[CONF_COGNITO_TOKEN] == "FRESH-TOKEN"


async def test_multiple_entries(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """Two accounts load side by side without clashing."""
    entry_a = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ACC-A",
        data={
            "email": "a@example.com",
            "password": "x",
            "account_id": "ACC-A",
            "cognito_token": "t",
            "mobile_id": "m",
        },
    )
    entry_b = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ACC-B",
        data={
            "email": "b@example.com",
            "password": "x",
            "account_id": "ACC-B",
            "cognito_token": "t",
            "mobile_id": "m",
        },
    )
    await setup_integration(hass, entry_a)
    await setup_integration(hass, entry_b)
    assert entry_a.state is ConfigEntryState.LOADED
    assert entry_b.state is ConfigEntryState.LOADED


async def test_migration_strips_password(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A version-1 entry that stored the password migrates to v2 without it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id="ACC-OLD",
        data={
            "email": "old@example.com",
            "password": "should-be-removed",
            "account_id": "ACC-OLD",
            "cognito_token": "t",
            "mobile_id": "m",
        },
    )
    await setup_integration(hass, entry)
    assert entry.version == 3
    assert "password" not in entry.data
    assert entry.data["cognito_token"] == "t"


async def test_migration_rewrites_stale_stream_url(
    hass: HomeAssistant, mock_client: AsyncMock, mock_bridge: AsyncMock
) -> None:
    """A v2 stream URL that embedded the raw token gets the derived password."""
    token = "old-token"
    stale = f"rtsp://furbo:{token}@10.0.0.5:8554/furbo"
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=c.ACCOUNT_ID,
        data={"account_id": c.ACCOUNT_ID, "cognito_token": "t", "mobile_id": "m"},
        options={
            CONF_BRIDGES: {
                c.DEVICE_ID: {
                    CONF_BRIDGE_URL: "http://10.0.0.5:8791",
                    CONF_BRIDGE_TOKEN: token,
                }
            },
            CONF_STREAM_URLS: {c.DEVICE_ID: stale},
        },
    )
    await setup_integration(hass, entry)
    assert entry.version == 3
    migrated = entry.options[CONF_STREAM_URLS][c.DEVICE_ID]
    assert migrated == f"rtsp://furbo:{rtsp_password(token)}@10.0.0.5:8554/furbo"
    assert token not in migrated


async def test_migration_leaves_unrelated_stream_url(
    hass: HomeAssistant, mock_client: AsyncMock, mock_bridge: AsyncMock
) -> None:
    """A stream URL that does not embed the token is left untouched."""
    custom = "rtsp://user:custompw@10.0.0.5:8554/furbo"
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=c.ACCOUNT_ID,
        data={"account_id": c.ACCOUNT_ID, "cognito_token": "t", "mobile_id": "m"},
        options={
            CONF_BRIDGES: {
                c.DEVICE_ID: {
                    CONF_BRIDGE_URL: "http://10.0.0.5:8791",
                    CONF_BRIDGE_TOKEN: "tok",
                }
            },
            CONF_STREAM_URLS: {c.DEVICE_ID: custom},
        },
    )
    await setup_integration(hass, entry)
    assert entry.version == 3
    assert entry.options[CONF_STREAM_URLS][c.DEVICE_ID] == custom


async def test_migration_rewrites_percent_encoded_token(
    hass: HomeAssistant, mock_client: AsyncMock, mock_bridge: AsyncMock
) -> None:
    """A token with reserved characters (stored percent-encoded) is migrated."""
    token = "a/b@c:d+e"  # reserved chars that beta.8 URL-encoded in the URL
    stale = f"rtsp://furbo:{quote(token, safe='')}@10.0.0.5:8554/furbo"
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=c.ACCOUNT_ID,
        data={"account_id": c.ACCOUNT_ID, "cognito_token": "t", "mobile_id": "m"},
        options={
            CONF_BRIDGES: {
                c.DEVICE_ID: {
                    CONF_BRIDGE_URL: "http://10.0.0.5:8791",
                    CONF_BRIDGE_TOKEN: token,
                }
            },
            CONF_STREAM_URLS: {c.DEVICE_ID: stale},
        },
    )
    await setup_integration(hass, entry)
    migrated = entry.options[CONF_STREAM_URLS][c.DEVICE_ID]
    assert migrated == f"rtsp://furbo:{rtsp_password(token)}@10.0.0.5:8554/furbo"
    assert quote(token, safe="") not in migrated
    assert token not in migrated


async def test_migration_ignores_token_only_in_path(
    hass: HomeAssistant, mock_client: AsyncMock, mock_bridge: AsyncMock
) -> None:
    """A token that appears only in the path (not as the password) is untouched."""
    token = "sekret"
    # The password is something else; the token merely appears in the path.
    url = f"rtsp://furbo:otherpw@10.0.0.5:8554/{token}/furbo"
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=c.ACCOUNT_ID,
        data={"account_id": c.ACCOUNT_ID, "cognito_token": "t", "mobile_id": "m"},
        options={
            CONF_BRIDGES: {
                c.DEVICE_ID: {
                    CONF_BRIDGE_URL: "http://10.0.0.5:8791",
                    CONF_BRIDGE_TOKEN: token,
                }
            },
            CONF_STREAM_URLS: {c.DEVICE_ID: url},
        },
    )
    await setup_integration(hass, entry)
    assert entry.options[CONF_STREAM_URLS][c.DEVICE_ID] == url


async def test_migration_downgrade_rejected(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An entry from a newer schema than we understand is not migrated."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=99,
        unique_id="ACC-NEW",
        data={"account_id": "ACC-NEW", "cognito_token": "t", "mobile_id": "m"},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
