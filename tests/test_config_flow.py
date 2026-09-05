"""Config, options, reauth and reconfigure flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.furbo.api import (
    MAIN_URL,
    FurboConnectionError,
    FurboError,
    FurboLoginError,
    FurboMfaError,
)
from custom_components.furbo.const import (
    CONF_ACCOUNT_ID,
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_BRIDGES,
    CONF_EVENTS_ENABLED,
    CONF_MFA_CODE,
    CONF_SCAN_INTERVAL,
    CONF_STREAM_URL,
    CONF_STREAM_URLS,
    DOMAIN,
)

from . import const as c
from .conftest import setup_integration

USER_INPUT = {CONF_EMAIL: c.EMAIL, CONF_PASSWORD: c.PASSWORD}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_user_flow_with_mfa(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """A full email/password then MFA login creates the entry."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "mfa"
    mock_client.send_mfa_code.assert_awaited_once_with("CANDIDATE")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: " 1234 "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == c.EMAIL
    assert result["data"][CONF_ACCOUNT_ID] == c.ACCOUNT_ID
    assert mock_client.complete_login.await_args.args[4] == "1234"


async def test_user_flow_without_mfa(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """When no MFA is required the entry is created straight away."""
    mock_client.start_login.return_value = None
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mock_client.send_mfa_code.assert_not_awaited()


async def test_invalid_auth(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """Bad credentials show an error and let the user retry."""
    mock_client.start_login.side_effect = FurboLoginError("no")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.start_login.side_effect = None
    mock_client.start_login.return_value = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_cannot_connect_on_login(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A connection error at login is reported."""
    mock_client.start_login.side_effect = FurboConnectionError("down")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_invalid_and_cannot_connect_mfa(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """MFA errors are reported and recoverable."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    mock_client.complete_login.side_effect = FurboMfaError("bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "0000"}
    )
    assert result["errors"] == {"base": "invalid_mfa"}

    mock_client.complete_login.side_effect = FurboConnectionError("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "0000"}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    mock_client.complete_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_aborts(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A second entry for the same account is aborted."""
    mock_config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_success(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth updates the token on the existing entry."""
    await setup_integration(hass, mock_config_entry)
    mock_client.cognito_token = "new-token"

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data["cognito_token"] == "new-token"


async def test_reauth_wrong_account(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth with a different account is rejected."""
    await setup_integration(hass, mock_config_entry)
    mock_client.account_id = "DIFFERENT"

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_reauth_without_mfa(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth completes directly when MFA is not required."""
    await setup_integration(hass, mock_config_entry)
    mock_client.start_login.return_value = None
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reconfigure_success(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure re-enters credentials for the same account."""
    await setup_integration(hass, mock_config_entry)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_wrong_account(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure onto a different account is rejected."""
    await setup_integration(hass, mock_config_entry)
    mock_client.account_id = "DIFFERENT"
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_reconfigure_without_mfa(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure completes directly when MFA is not required."""
    await setup_integration(hass, mock_config_entry)
    mock_client.start_login.return_value = None
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_options_flow(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The options flow stores the polling options and each camera's URL."""
    await setup_integration(hass, mock_config_entry)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 120, CONF_EVENTS_ENABLED: False},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "camera"
    assert result["description_placeholders"] == {"name": "Test Camera"}

    # A URL with an unsupported scheme is rejected and the step is shown again.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STREAM_URL: "exec:python3 bridge.py"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "camera"
    assert result["errors"] == {CONF_STREAM_URL: "invalid_stream_url"}

    # A bridge URL with an unsupported scheme is rejected on its own field.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_STREAM_URL: "rtsp://ok/furbo", CONF_BRIDGE_URL: "ftp://bridge"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BRIDGE_URL: "invalid_bridge_url"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STREAM_URL: "  RTSP://go2rtc.local:8554/furbo  ",
            CONF_BRIDGE_URL: "http://bridge.local:8791/",
            CONF_BRIDGE_TOKEN: "s3cret",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_SCAN_INTERVAL: 120,
        CONF_EVENTS_ENABLED: False,
        CONF_STREAM_URLS: {c.DEVICE_ID: "RTSP://go2rtc.local:8554/furbo"},
        CONF_BRIDGES: {
            c.DEVICE_ID: {
                CONF_BRIDGE_URL: "http://bridge.local:8791",
                CONF_BRIDGE_TOKEN: "s3cret",
            }
        },
    }


async def test_options_flow_clears_stream_url(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Leaving the field empty removes the camera's URL; stale ids are dropped."""
    await setup_integration(
        hass,
        mock_config_entry,
        {
            CONF_STREAM_URLS: {c.DEVICE_ID: "rtsp://old/furbo", "GONE": "rtsp://x"},
            CONF_BRIDGES: {
                c.DEVICE_ID: {CONF_BRIDGE_URL: "http://old:8791"},
                "GONE": {CONF_BRIDGE_URL: "http://x"},
            },
        },
    )
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 300, CONF_EVENTS_ENABLED: True}
    )
    assert result["step_id"] == "camera"
    # The current values are offered back as suggested values.
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
    }
    assert suggested == {
        CONF_STREAM_URL: "rtsp://old/furbo",
        CONF_BRIDGE_URL: "http://old:8791",
        CONF_BRIDGE_TOKEN: "",
    }

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_SCAN_INTERVAL: 300,
        CONF_EVENTS_ENABLED: True,
        CONF_STREAM_URLS: {},
        CONF_BRIDGES: {},
    }


async def test_options_flow_two_cameras(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Each camera gets its own stream step, in a stable order."""
    mock_client.get_devices.return_value = [
        dict(c.DEVICE),
        {**c.DEVICE, "Id": "ZZ99", "DeviceName": "Kitchen"},
    ]
    await setup_integration(hass, mock_config_entry)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 300, CONF_EVENTS_ENABLED: True}
    )
    assert result["description_placeholders"] == {"name": "Test Camera"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STREAM_URL: "rtsp://h/a"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"name": "Kitchen"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_STREAM_URL: "https://h/b.m3u8", CONF_BRIDGE_URL: "https://h:8791"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_STREAM_URLS] == {
        c.DEVICE_ID: "rtsp://h/a",
        "ZZ99": "https://h/b.m3u8",
    }
    # A bridge without a token stores no token key at all.
    assert mock_config_entry.options[CONF_BRIDGES] == {
        "ZZ99": {CONF_BRIDGE_URL: "https://h:8791"}
    }


async def test_options_flow_without_cameras(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """With no camera devices the flow finishes after the polling step."""
    mock_client.get_devices.return_value = []
    await setup_integration(hass, mock_config_entry)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 300, CONF_EVENTS_ENABLED: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_STREAM_URLS] == {}
    assert mock_config_entry.options[CONF_BRIDGES] == {}


async def test_login_12002_shows_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A real 12002 from the login endpoint shows invalid_auth, not a crash."""
    aioclient_mock.post(
        f"{MAIN_URL}/v5/account/read/login",
        status=400,
        json={"Code": 12002, "Message": "wrong token or token expired"},
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_unknown_error_on_send_code(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An unexpected API error while sending the code shows 'unknown'."""
    mock_client.send_mfa_code.side_effect = FurboError("boom")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "unknown"}


async def test_unknown_error_on_verify(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An unexpected API error while verifying the code shows 'unknown'."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    mock_client.complete_login.side_effect = FurboError("boom")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "1234"}
    )
    assert result["errors"] == {"base": "unknown"}


async def test_created_entry_has_no_password(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """The created entry stores the token but never the password."""
    mock_client.start_login.return_value = None
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PASSWORD not in result["data"]
    assert result["data"][CONF_ACCOUNT_ID] == c.ACCOUNT_ID


async def test_cooldown_on_login(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """An 80001 cooldown at login shows the dedicated message."""
    mock_client.start_login.side_effect = FurboLoginError("slow down", 80001)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "too_many_attempts"}


async def test_cooldown_on_mfa(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """An 80001 cooldown while verifying the code shows the dedicated message."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    mock_client.complete_login.side_effect = FurboMfaError("slow down", 80001)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "1234"}
    )
    assert result["errors"] == {"base": "too_many_attempts"}


async def test_cooldown_on_send_code(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An 80001 while sending the code (a plain FurboError) shows cooldown."""
    mock_client.send_mfa_code.side_effect = FurboError("slow", 80001)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "too_many_attempts"}
