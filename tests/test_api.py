"""Unit tests for the standalone Furbo API client.

The network boundary is mocked with Home Assistant's own aioclient_mock so the
tests stay compatible with the aiohttp version each Home Assistant lane ships.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.furbo.api import (
    MAIN_URL,
    PETGPT_URL,
    FurboAuthError,
    FurboClient,
    FurboConnectionError,
    FurboError,
    FurboLoginError,
    FurboMfaError,
    encrypt_password,
    new_mobile_id,
)

from . import const as c


def _client(hass: HomeAssistant, authed: bool = False) -> FurboClient:
    """Build a client bound to Home Assistant's mocked shared session."""
    session = async_get_clientsession(hass)
    if authed:
        return FurboClient(session, c.ACCOUNT_ID, c.COGNITO_TOKEN)
    return FurboClient(session)


def test_helpers() -> None:
    """Password encryption and mobile id generation are well-formed."""
    assert encrypt_password("secret") != "secret"
    assert encrypt_password("secret")
    mobile_id = new_mobile_id()
    assert len(mobile_id) == 36
    assert mobile_id == mobile_id.upper()


async def test_login_without_mfa(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A direct login stores the account id and token."""
    aioclient_mock.post(
        f"{MAIN_URL}/v5/account/read/login",
        json={"AccountId": c.ACCOUNT_ID, "CognitoToken": c.COGNITO_TOKEN},
    )
    client = _client(hass)
    assert await client.start_login(c.EMAIL, "enc", "mob") is None
    assert client.account_id == c.ACCOUNT_ID
    assert client.cognito_token == c.COGNITO_TOKEN


async def test_login_with_mfa(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The MFA path threads the candidate through to a final token."""
    aioclient_mock.post(
        f"{MAIN_URL}/v5/account/read/login",
        status=400,
        json={"Code": 12101, "AccountId": c.ACCOUNT_ID, "MfaAuthCodeCandidate": "C1"},
    )
    aioclient_mock.post(
        f"{MAIN_URL}/v4/account/mfa/login/send-code",
        json={"MfaAuthCodeCandidate": "C2"},
    )
    aioclient_mock.post(
        f"{MAIN_URL}/v4/account/mfa/verify", json={"MfaAuthCode": "AUTH"}
    )
    aioclient_mock.post(
        f"{MAIN_URL}/v2/account/login",
        json={"AccountId": c.ACCOUNT_ID, "CognitoToken": "tok2"},
    )
    client = _client(hass)
    assert await client.start_login(c.EMAIL, "enc", "mob") == "C1"
    assert client.account_id == c.ACCOUNT_ID
    assert await client.send_mfa_code("C1") == "C2"
    await client.complete_login(c.EMAIL, "enc", "mob", "C2", "1234")
    assert client.cognito_token == "tok2"


async def test_login_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Wrong credentials raise FurboLoginError."""
    aioclient_mock.post(
        f"{MAIN_URL}/v5/account/read/login",
        status=400,
        json={"Code": 12000, "Message": "bad"},
    )
    with pytest.raises(FurboLoginError):
        await _client(hass).start_login(c.EMAIL, "enc", "mob")


async def test_login_12002_is_login_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 12002 at the login endpoint is a login failure, not a stale token."""
    aioclient_mock.post(
        f"{MAIN_URL}/v5/account/read/login",
        status=400,
        json={"Code": 12002, "Message": "wrong token or token expired"},
    )
    with pytest.raises(FurboLoginError):
        await _client(hass).start_login(c.EMAIL, "enc", "mob")


async def test_mfa_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bad MFA code raises FurboMfaError."""
    aioclient_mock.post(
        f"{MAIN_URL}/v4/account/mfa/verify",
        status=400,
        json={"Code": 12102, "Message": "bad"},
    )
    with pytest.raises(FurboMfaError):
        await _client(hass).complete_login(c.EMAIL, "enc", "mob", "C", "0000")


async def test_verify_error_is_mfa_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Any rejection verifying the emailed code surfaces as FurboMfaError."""
    aioclient_mock.post(
        f"{MAIN_URL}/v4/account/mfa/verify", status=400, json={"Code": 12002}
    )
    with pytest.raises(FurboMfaError):
        await _client(hass).complete_login(c.EMAIL, "enc", "mob", "C", "1")


async def test_token_invalid_raises_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 12002 on an authenticated call maps to FurboAuthError."""
    aioclient_mock.post(
        f"{MAIN_URL}/v2/account/info",
        status=400,
        json={"Code": 12002, "Message": "wrong token or token expired"},
    )
    with pytest.raises(FurboAuthError):
        await _client(hass, authed=True).get_account_info()


async def test_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport failure raises FurboConnectionError."""
    aioclient_mock.post(f"{MAIN_URL}/v2/account/info", exc=aiohttp.ClientError())
    with pytest.raises(FurboConnectionError):
        await _client(hass, authed=True).get_account_info()


async def test_not_logged_in(hass: HomeAssistant) -> None:
    """Authenticated calls without a token raise FurboAuthError."""
    with pytest.raises(FurboAuthError):
        await _client(hass).get_devices()


async def test_rate_limit_retry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An 80002 rate-limit response is retried after waiting."""
    calls = {"n": 0}

    async def summary(method: str, url: URL, data: object) -> AiohttpClientMockResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return AiohttpClientMockResponse(
                method, url, status=400, json={"Code": 80002, "TimeWait": 1}
            )
        return AiohttpClientMockResponse(
            method, url, status=200, json={"Summary": "ok"}
        )

    aioclient_mock.post(
        f"{PETGPT_URL}/v1/calendar/daily-summary/get", side_effect=summary
    )
    with patch(
        "custom_components.furbo.api.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        assert await _client(hass, authed=True).get_daily_summary("2026-09-05") == "ok"
    sleep.assert_awaited_once()
    assert calls["n"] == 2


async def test_rate_limit_exhausted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Persistent 80002 responses eventually raise after the retries run out."""
    aioclient_mock.post(
        f"{PETGPT_URL}/v1/calendar/daily-summary/get",
        status=400,
        json={"Code": 80002, "TimeWait": 1},
    )
    with (
        patch("custom_components.furbo.api.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(FurboError) as err,
    ):
        await _client(hass, authed=True).get_daily_summary("2026-09-05")
    assert err.value.code == 80002


async def test_reads(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The read endpoints decode their payloads."""
    aioclient_mock.post(
        f"{MAIN_URL}/v2/account/{c.ACCOUNT_ID}/device", json=c.DEVICE_LIST_RESPONSE
    )
    aioclient_mock.post(f"{MAIN_URL}/v5/device/alert-setting", json=dict(c.ALERTS))
    aioclient_mock.post(f"{MAIN_URL}/v3/service/license", json=c.LICENSE_RESPONSE)
    aioclient_mock.post(
        f"{PETGPT_URL}/v1/calendar/notable-events/get",
        json={"Events": c.NOTABLE_EVENTS},
    )
    aioclient_mock.post(
        f"{PETGPT_URL}/v2/calendar/activity-report/get", json=c.ACTIVITY_RESPONSE
    )
    client = _client(hass, authed=True)
    assert (await client.get_devices())[0]["Id"] == c.DEVICE_ID
    assert (await client.get_alert_settings(c.DEVICE_ID))["Barking"] == "1"
    assert c.DEVICE_ID in (await client.get_license())["DevicesLicense"]
    assert len(await client.get_notable_events("2026-09-05")) == 2
    assert "2026-09-05" in await client.get_activity_report(["2026-09-05"])


async def test_set_alert_setting(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A write posts to the update endpoint."""
    aioclient_mock.post(
        f"{MAIN_URL}/v5/device/alert-setting/update", json={"PersonDetection": "1"}
    )
    await _client(hass, authed=True).set_alert_setting(
        c.DEVICE_ID, "PersonDetection", True
    )
    assert aioclient_mock.call_count == 1


async def test_send_mfa_code_without_candidate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """send_mfa_code returns the original candidate when none is echoed back."""
    aioclient_mock.post(f"{MAIN_URL}/v4/account/mfa/login/send-code", json={})
    assert await _client(hass).send_mfa_code("ORIG") == "ORIG"
