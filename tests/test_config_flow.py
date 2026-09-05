"""Config, options, reauth and reconfigure flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from aioresponses import aioresponses
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.furbo.api import (
    MAIN_URL,
    FurboConnectionError,
    FurboError,
    FurboLoginError,
    FurboMfaError,
)
from custom_components.furbo.const import (
    CONF_ACCOUNT_ID,
    CONF_EVENTS_ENABLED,
    CONF_MFA_CODE,
    CONF_SCAN_INTERVAL,
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
    """The options flow stores the interval and calendar toggle."""
    await setup_integration(hass, mock_config_entry)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 120, CONF_EVENTS_ENABLED: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_SCAN_INTERVAL: 120,
        CONF_EVENTS_ENABLED: False,
    }


async def test_login_12002_shows_invalid_auth(hass: HomeAssistant) -> None:
    """A real 12002 from the login endpoint shows invalid_auth, not a crash."""
    result = await _start(hass)
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v5/account/read/login",
            status=400,
            payload={"Code": 12002, "Message": "wrong token or token expired"},
            repeat=True,
        )
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
