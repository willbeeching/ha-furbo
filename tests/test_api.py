"""Unit tests for the standalone Furbo API client (socket mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
from aioresponses import aioresponses
import pytest

from custom_components.furbo.api import (
    MAIN_URL,
    PETGPT_URL,
    FurboAuthError,
    FurboClient,
    FurboConnectionError,
    FurboLoginError,
    FurboMfaError,
    encrypt_password,
    new_mobile_id,
)

from . import const as c


@pytest.fixture
async def session():
    """Provide a real aiohttp session with the socket mocked."""
    async with aiohttp.ClientSession() as sess:
        yield sess


def test_helpers() -> None:
    """Password encryption and mobile id generation are well-formed."""
    assert encrypt_password("secret") != "secret"
    assert encrypt_password("secret")  # base64 text
    mobile_id = new_mobile_id()
    assert len(mobile_id) == 36
    assert mobile_id == mobile_id.upper()


async def test_login_without_mfa(session: aiohttp.ClientSession) -> None:
    """A direct login stores the account id and token."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v5/account/read/login",
            payload={"AccountId": c.ACCOUNT_ID, "CognitoToken": c.COGNITO_TOKEN},
        )
        client = FurboClient(session)
        assert await client.start_login(c.EMAIL, "enc", "mob") is None
    assert client.account_id == c.ACCOUNT_ID
    assert client.cognito_token == c.COGNITO_TOKEN


async def test_login_with_mfa(session: aiohttp.ClientSession) -> None:
    """The MFA path threads the candidate through to a final token."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v5/account/read/login",
            status=400,
            payload={
                "Code": 12101,
                "AccountId": c.ACCOUNT_ID,
                "MfaAuthCodeCandidate": "C1",
            },
        )
        mock.post(
            f"{MAIN_URL}/v4/account/mfa/login/send-code",
            payload={"MfaAuthCodeCandidate": "C2"},
        )
        mock.post(f"{MAIN_URL}/v4/account/mfa/verify", payload={"MfaAuthCode": "AUTH"})
        mock.post(
            f"{MAIN_URL}/v2/account/login",
            payload={"AccountId": c.ACCOUNT_ID, "CognitoToken": "tok2"},
        )
        client = FurboClient(session)
        candidate = await client.start_login(c.EMAIL, "enc", "mob")
        assert candidate == "C1"
        assert client.account_id == c.ACCOUNT_ID
        candidate = await client.send_mfa_code("C1")
        assert candidate == "C2"
        await client.complete_login(c.EMAIL, "enc", "mob", "C2", "1234")
    assert client.cognito_token == "tok2"


async def test_login_rejected(session: aiohttp.ClientSession) -> None:
    """Wrong credentials raise FurboLoginError."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v5/account/read/login",
            status=400,
            payload={"Code": 12000, "Message": "bad"},
        )
        client = FurboClient(session)
        with pytest.raises(FurboLoginError):
            await client.start_login(c.EMAIL, "enc", "mob")


async def test_mfa_rejected(session: aiohttp.ClientSession) -> None:
    """A bad MFA code raises FurboMfaError."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v4/account/mfa/verify",
            status=400,
            payload={"Code": 12102, "Message": "bad"},
        )
        client = FurboClient(session)
        with pytest.raises(FurboMfaError):
            await client.complete_login(c.EMAIL, "enc", "mob", "C", "0000")


async def test_token_invalid_raises_auth(session: aiohttp.ClientSession) -> None:
    """A 12002 result is mapped to FurboAuthError."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v2/account/info",
            status=400,
            payload={"Code": 12002, "Message": "wrong token or token expired"},
        )
        client = FurboClient(session, c.ACCOUNT_ID, c.COGNITO_TOKEN)
        with pytest.raises(FurboAuthError):
            await client.get_account_info()


async def test_connection_error(session: aiohttp.ClientSession) -> None:
    """A transport failure raises FurboConnectionError."""
    with aioresponses() as mock:
        mock.post(f"{MAIN_URL}/v2/account/info", exception=aiohttp.ClientError())
        client = FurboClient(session, c.ACCOUNT_ID, c.COGNITO_TOKEN)
        with pytest.raises(FurboConnectionError):
            await client.get_account_info()


async def test_not_logged_in() -> None:
    """Authenticated calls without a token raise FurboAuthError."""
    async with aiohttp.ClientSession() as sess:
        client = FurboClient(sess)
        with pytest.raises(FurboAuthError):
            await client.get_devices()


async def test_rate_limit_retry(session: aiohttp.ClientSession) -> None:
    """An 80002 rate-limit response is retried after waiting."""
    with (
        aioresponses() as mock,
        patch(
            "custom_components.furbo.api.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
    ):
        mock.post(
            f"{PETGPT_URL}/v1/calendar/daily-summary/get",
            status=400,
            payload={"Code": 80002, "TimeWait": 1},
        )
        mock.post(
            f"{PETGPT_URL}/v1/calendar/daily-summary/get",
            payload={"Summary": "ok"},
        )
        client = FurboClient(session, c.ACCOUNT_ID, c.COGNITO_TOKEN)
        assert await client.get_daily_summary("2026-09-05") == "ok"
        sleep.assert_awaited_once()


async def test_reads(session: aiohttp.ClientSession) -> None:
    """The read endpoints decode their payloads."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v2/account/{c.ACCOUNT_ID}/device",
            payload=c.DEVICE_LIST_RESPONSE,
        )
        mock.post(f"{MAIN_URL}/v5/device/alert-setting", payload=dict(c.ALERTS))
        mock.post(f"{MAIN_URL}/v3/service/license", payload=c.LICENSE_RESPONSE)
        mock.post(
            f"{MAIN_URL}/v3/pet/profile/get",
            payload={"PetProfiles": [{"Name": "Percy"}]},
        )
        mock.post(
            f"{PETGPT_URL}/v1/calendar/notable-events/get",
            payload={"Events": c.NOTABLE_EVENTS},
        )
        mock.post(
            f"{PETGPT_URL}/v2/calendar/activity-report/get",
            payload=c.ACTIVITY_RESPONSE,
        )
        client = FurboClient(session, c.ACCOUNT_ID, c.COGNITO_TOKEN)
        assert (await client.get_devices())[0]["Id"] == c.DEVICE_ID
        assert (await client.get_alert_settings(c.DEVICE_ID))["Barking"] == "1"
        assert c.DEVICE_ID in (await client.get_license())["DevicesLicense"]
        assert (await client.get_pet_profiles())[0]["Name"] == "Percy"
        assert len(await client.get_notable_events("2026-09-05")) == 2
        assert "2026-09-05" in await client.get_activity_report(["2026-09-05"])


async def test_set_alert_setting(session: aiohttp.ClientSession) -> None:
    """A write posts the expected value."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v5/device/alert-setting/update",
            payload={"PersonDetection": "1"},
        )
        client = FurboClient(session, c.ACCOUNT_ID, c.COGNITO_TOKEN)
        await client.set_alert_setting(c.DEVICE_ID, "PersonDetection", True)


async def test_login_12002_is_login_error(session: aiohttp.ClientSession) -> None:
    """A 12002 at the login endpoint is a login failure, not a stale token."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v5/account/read/login",
            status=400,
            payload={"Code": 12002, "Message": "wrong token or token expired"},
        )
        client = FurboClient(session)
        with pytest.raises(FurboLoginError):
            await client.start_login(c.EMAIL, "enc", "mob")


async def test_verify_error_is_mfa_error(session: aiohttp.ClientSession) -> None:
    """Any rejection verifying the emailed code surfaces as FurboMfaError."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v4/account/mfa/verify",
            status=400,
            payload={"Code": 12002},
        )
        client = FurboClient(session)
        with pytest.raises(FurboMfaError):
            await client.complete_login(c.EMAIL, "enc", "mob", "C", "1")


async def test_send_mfa_code_without_candidate(session: aiohttp.ClientSession) -> None:
    """send_mfa_code returns the original candidate when none is echoed back."""
    with aioresponses() as mock:
        mock.post(f"{MAIN_URL}/v4/account/mfa/login/send-code", payload={})
        client = FurboClient(session)
        assert await client.send_mfa_code("ORIG") == "ORIG"


async def test_get_p2p_connection(session: aiohttp.ClientSession) -> None:
    """The P2P credential endpoint returns the auth key and account key."""
    with aioresponses() as mock:
        mock.post(
            f"{MAIN_URL}/v5/device/p2p_connection/get",
            payload={
                "AuthKey": "PLCEHLDR",
                "P2PAccountId": c.DEVICE_ID,
                "P2PAccountKey": "k",
            },
        )
        client = FurboClient(session, c.ACCOUNT_ID, c.COGNITO_TOKEN)
        creds = await client.get_p2p_connection(c.DEVICE_ID)
        assert creds["AuthKey"] == "PLCEHLDR"
