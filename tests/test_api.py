"""Unit tests for the standalone Furbo API client.

The network boundary is mocked with Home Assistant's own aioclient_mock so the
tests stay compatible with the aiohttp version each Home Assistant lane ships.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any
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

LOGIN = f"{MAIN_URL}/v5/account/read/login"
SEND_CODE = f"{MAIN_URL}/v4/account/mfa/login/send-code"
VERIFY = f"{MAIN_URL}/v4/account/mfa/verify"
MFA_LOGIN = f"{MAIN_URL}/v2/account/login"
INFO = f"{MAIN_URL}/v2/account/info"
DEVICES = f"{MAIN_URL}/v2/account/{c.ACCOUNT_ID}/device"
ALERTS = f"{MAIN_URL}/v5/device/alert-setting"
LICENSE = f"{MAIN_URL}/v3/service/license"
EVENTS = f"{PETGPT_URL}/v1/calendar/notable-events/get"
SUMMARY = f"{PETGPT_URL}/v1/calendar/daily-summary/get"
ACTIVITY = f"{PETGPT_URL}/v2/calendar/activity-report/get"

# A string that must never leak out of the client in an exception message.
BODY_MARKER = "do-not-leak-this-body"


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


# --- login -------------------------------------------------------------------


async def test_login_without_mfa(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A direct login stores the account id and token."""
    aioclient_mock.post(
        LOGIN, json={"AccountId": c.ACCOUNT_ID, "CognitoToken": c.COGNITO_TOKEN}
    )
    client = _client(hass)
    assert await client.start_login(c.EMAIL, "enc", "mob") is None
    assert client.account_id == c.ACCOUNT_ID
    assert client.cognito_token == c.COGNITO_TOKEN


async def test_login_names_the_fields_it_was_given(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A field the client does not keep is still named in the log.

    The client keeps two fields and drops the rest, so "the cloud gives us
    nothing to renew with" describes this parser, not the API. Nobody had
    looked. Names only ever reach the log -- never values, which are the
    credentials themselves.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.furbo.api")
    aioclient_mock.post(
        LOGIN,
        json={
            "AccountId": c.ACCOUNT_ID,
            "CognitoToken": c.COGNITO_TOKEN,
            "RefreshToken": "a-token-nobody-parses",
            "ExpiresIn": 86400,
        },
    )
    await _client(hass).start_login(c.EMAIL, "enc", "mob")

    assert "RefreshToken" in caplog.text
    assert "ExpiresIn" in caplog.text
    # The values are what must never be logged.
    assert "a-token-nobody-parses" not in caplog.text
    assert c.COGNITO_TOKEN not in caplog.text


async def test_login_with_mfa(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The MFA path threads the candidate through to a final token."""
    aioclient_mock.post(
        LOGIN,
        status=400,
        json={"Code": 12101, "AccountId": c.ACCOUNT_ID, "MfaAuthCodeCandidate": "C1"},
    )
    aioclient_mock.post(SEND_CODE, json={"MfaAuthCodeCandidate": "C2"})
    aioclient_mock.post(VERIFY, json={"MfaAuthCode": "AUTH"})
    aioclient_mock.post(
        MFA_LOGIN, json={"AccountId": c.ACCOUNT_ID, "CognitoToken": "tok2"}
    )
    client = _client(hass)
    assert await client.start_login(c.EMAIL, "enc", "mob") == "C1"
    assert client.account_id == c.ACCOUNT_ID
    assert await client.send_mfa_code("C1") == "C2"
    await client.complete_login(c.EMAIL, "enc", "mob", "C2", "1234")
    assert client.cognito_token == "tok2"
    assert aioclient_mock.mock_calls[3][2]["MfaAuthCode"] == "AUTH"


@pytest.mark.parametrize("code", [12000, 12002, 80001])
async def test_login_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, code: int
) -> None:
    """Any rejection at the login endpoint, 12002 included, is a login error."""
    aioclient_mock.post(LOGIN, status=400, json={"Code": code, "Message": BODY_MARKER})
    with pytest.raises(FurboLoginError) as err:
        await _client(hass).start_login(c.EMAIL, "enc", "mob")
    assert err.value.code == code
    assert BODY_MARKER not in str(err.value)


async def test_login_connection_error_is_not_a_login_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport failure during login stays a connection error."""
    aioclient_mock.post(LOGIN, exc=aiohttp.ClientError())
    with pytest.raises(FurboConnectionError):
        await _client(hass).start_login(c.EMAIL, "enc", "mob")


@pytest.mark.parametrize(
    "body",
    [
        {"Code": 12101, "AccountId": c.ACCOUNT_ID},
        {"Code": 12101, "MfaAuthCodeCandidate": "C1"},
        {"Code": 12101, "AccountId": "", "MfaAuthCodeCandidate": "C1"},
        {"Code": 12101, "AccountId": c.ACCOUNT_ID, "MfaAuthCodeCandidate": 5},
    ],
)
async def test_login_mfa_required_incomplete(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: dict[str, Any]
) -> None:
    """A 12101 without both validated MFA fields is a malformed response."""
    aioclient_mock.post(LOGIN, status=400, json=body)
    client = _client(hass)
    with pytest.raises(FurboConnectionError):
        await client.start_login(c.EMAIL, "enc", "mob")
    assert client.account_id is None


@pytest.mark.parametrize(
    "body",
    [
        {"AccountId": c.ACCOUNT_ID},
        {"CognitoToken": "tok"},
        {"AccountId": c.ACCOUNT_ID, "CognitoToken": ""},
        {"AccountId": 1, "CognitoToken": "tok"},
        [],
        "text",
    ],
)
async def test_login_200_malformed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: Any
) -> None:
    """A 200 without both login fields never stores a partial login."""
    aioclient_mock.post(LOGIN, json=body)
    client = _client(hass)
    with pytest.raises(FurboConnectionError):
        await client.start_login(c.EMAIL, "enc", "mob")
    assert client.account_id is None
    assert client.cognito_token is None


async def test_login_non_json(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-JSON body is a connection error and is not echoed back."""
    aioclient_mock.post(LOGIN, text=f"<html>{BODY_MARKER}</html>")
    with pytest.raises(FurboConnectionError) as err:
        await _client(hass).start_login(c.EMAIL, "enc", "mob")
    assert BODY_MARKER not in str(err.value)


async def test_send_mfa_code_without_candidate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """send_mfa_code returns the original candidate when none is echoed back."""
    aioclient_mock.post(SEND_CODE, json={})
    assert await _client(hass).send_mfa_code("ORIG") == "ORIG"


async def test_send_mfa_code_bad_candidate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-string candidate is rejected rather than passed on."""
    aioclient_mock.post(SEND_CODE, json={"MfaAuthCodeCandidate": 42})
    with pytest.raises(FurboConnectionError):
        await _client(hass).send_mfa_code("ORIG")


@pytest.mark.parametrize("code", [12102, 12103, 12002, 12101, 80001])
async def test_mfa_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, code: int
) -> None:
    """Any rejection verifying the emailed code surfaces as FurboMfaError."""
    body: dict[str, Any] = {"Code": code, "Message": BODY_MARKER}
    if code == 12101:
        body |= {"AccountId": c.ACCOUNT_ID, "MfaAuthCodeCandidate": "C"}
    aioclient_mock.post(VERIFY, status=400, json=body)
    with pytest.raises(FurboMfaError) as err:
        await _client(hass).complete_login(c.EMAIL, "enc", "mob", "C", "0000")
    assert err.value.code == code
    assert BODY_MARKER not in str(err.value)


async def test_mfa_verify_malformed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A verify response without MfaAuthCode is a connection error."""
    aioclient_mock.post(VERIFY, json={"Something": "else"})
    with pytest.raises(FurboConnectionError):
        await _client(hass).complete_login(c.EMAIL, "enc", "mob", "C", "0000")
    assert aioclient_mock.call_count == 1


async def test_mfa_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport failure verifying the code is not reported as a bad code."""
    aioclient_mock.post(VERIFY, exc=TimeoutError())
    with pytest.raises(FurboConnectionError):
        await _client(hass).complete_login(c.EMAIL, "enc", "mob", "C", "0000")


# --- generic error mapping -----------------------------------------------------


async def test_token_invalid_raises_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 12002 on an authenticated call maps to FurboAuthError."""
    aioclient_mock.post(INFO, status=400, json={"Code": 12002, "Message": BODY_MARKER})
    with pytest.raises(FurboAuthError) as err:
        await _client(hass, authed=True).get_account_info()
    assert err.value.code == 12002
    assert BODY_MARKER not in str(err.value)
    assert "/v2/account/info" in str(err.value)
    assert "400" in str(err.value)


@pytest.mark.parametrize(
    ("status", "response", "expected_code"),
    [
        (400, {"json": {"Code": 12001, "Message": BODY_MARKER}}, 12001),
        (400, {"json": {"Message": BODY_MARKER}}, None),
        (400, {"json": {"Code": "12001"}}, None),
        (400, {"json": {"Code": True}}, None),
        (400, {"json": [BODY_MARKER]}, None),
        (500, {"json": BODY_MARKER}, None),
        (502, {"text": "null"}, None),
    ],
)
async def test_error_payloads(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    response: dict[str, Any],
    expected_code: int | None,
) -> None:
    """Incomplete or non-object error payloads still map to a clean error."""
    aioclient_mock.post(INFO, status=status, **response)
    with pytest.raises(FurboError) as err:
        await _client(hass, authed=True).get_account_info()
    assert type(err.value) is FurboError
    assert err.value.code == expected_code
    assert BODY_MARKER not in str(err.value)
    assert str(status) in str(err.value)


async def test_error_non_json(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-JSON error page is a connection error and is not echoed back."""
    aioclient_mock.post(INFO, status=503, text=f"<html>{BODY_MARKER}</html>")
    with pytest.raises(FurboConnectionError) as err:
        await _client(hass, authed=True).get_account_info()
    assert BODY_MARKER not in str(err.value)
    assert "503" in str(err.value)


async def test_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport failure raises FurboConnectionError."""
    aioclient_mock.post(INFO, exc=aiohttp.ClientError(BODY_MARKER))
    with pytest.raises(FurboConnectionError) as err:
        await _client(hass, authed=True).get_account_info()
    assert "ClientError" in str(err.value)
    assert BODY_MARKER not in str(err.value)


async def test_not_logged_in(hass: HomeAssistant) -> None:
    """Authenticated calls without a token raise FurboAuthError."""
    with pytest.raises(FurboAuthError):
        await _client(hass).get_devices()


# --- rate limiting ---------------------------------------------------------------


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

    aioclient_mock.post(SUMMARY, side_effect=summary)
    with patch(
        "custom_components.furbo.api.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        assert await _client(hass, authed=True).get_daily_summary("2026-09-05") == "ok"
    sleep.assert_awaited_once_with(10.0)
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("time_wait", "expected"),
    [
        (1, 10.0),
        (30, 30.0),
        ("45", 45.0),
        (10_000, 120.0),
        ("soon", 10.0),
        (True, 10.0),
        (None, 10.0),
        ([5], 10.0),
    ],
)
async def test_rate_limit_wait_is_bounded(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    time_wait: Any,
    expected: float,
) -> None:
    """TimeWait is validated and clamped before it is slept on."""
    body: dict[str, Any] = {"Code": 80002}
    if time_wait is not None:
        body["TimeWait"] = time_wait
    aioclient_mock.post(SUMMARY, status=400, json=body)
    with (
        patch(
            "custom_components.furbo.api.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        pytest.raises(FurboError) as err,
    ):
        await _client(hass, authed=True).get_daily_summary("2026-09-05")
    assert err.value.code == 80002
    assert sleep.await_count == 3
    assert all(call.args == (expected,) for call in sleep.await_args_list)


# --- reads ------------------------------------------------------------------------


async def test_reads(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The read endpoints decode and normalise their payloads."""
    aioclient_mock.post(INFO, json=c.ACCOUNT_INFO)
    aioclient_mock.post(DEVICES, json=c.DEVICE_LIST_RESPONSE)
    aioclient_mock.post(ALERTS, json=dict(c.ALERTS))
    aioclient_mock.post(LICENSE, json=c.LICENSE_RESPONSE)
    aioclient_mock.post(EVENTS, json={"Events": c.NOTABLE_EVENTS})
    aioclient_mock.post(SUMMARY, json={"Summary": c.DAILY_SUMMARY})
    aioclient_mock.post(ACTIVITY, json=c.ACTIVITY_RESPONSE)
    client = _client(hass, authed=True)
    assert (await client.get_account_info())["Timezone"] == "Europe/London"
    assert (await client.get_devices())[0]["Id"] == c.DEVICE_ID
    assert (await client.get_alert_settings(c.DEVICE_ID))["Barking"] == "1"
    assert (await client.get_license())[c.DEVICE_ID][0]["TimeLeftDays"] == 24
    assert len(await client.get_notable_events("2026-09-05")) == 2
    assert await client.get_daily_summary("2026-09-05") == c.DAILY_SUMMARY
    assert await client.get_activity_report(["2026-09-05"]) == c.ACTIVITY_TOTALS


async def test_reads_optional_fields_absent(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Optional fields may be missing without breaking the read."""
    aioclient_mock.post(INFO, json={})
    aioclient_mock.post(DEVICES, json={})
    aioclient_mock.post(LICENSE, json={})
    aioclient_mock.post(EVENTS, json={})
    aioclient_mock.post(SUMMARY, json={})
    aioclient_mock.post(ACTIVITY, json={"2026-09-05": {"Data": None, "Error": None}})
    client = _client(hass, authed=True)
    assert await client.get_account_info() == {}
    assert await client.get_devices() == []
    assert await client.get_license() == {}
    assert await client.get_notable_events("2026-09-05") == []
    assert await client.get_daily_summary("2026-09-05") == ""
    assert await client.get_activity_report(["2026-09-05"]) == {"2026-09-05": {}}


async def test_license_normalises_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """TimeLeftDays becomes an int or is dropped when it is not numeric."""
    aioclient_mock.post(
        LICENSE,
        json={
            "DevicesLicense": {
                "A": [{"TimeLeftDays": "12"}, {"TimeLeftDays": 3.9}],
                "B": [{"TimeLeftDays": "soon"}, {}],
            }
        },
    )
    licenses = await _client(hass, authed=True).get_license()
    assert licenses == {
        "A": [{"TimeLeftDays": 12}, {"TimeLeftDays": 3}],
        "B": [{}, {}],
    }


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (INFO, {"Timezone": 1}),
        (DEVICES, {"DeviceList": {}}),
        (DEVICES, {"DeviceList": ["x"]}),
        (DEVICES, {"DeviceList": [{"DeviceName": "No id"}]}),
        (DEVICES, {"DeviceList": [{"Id": ""}]}),
        (DEVICES, {"DeviceList": [{"Id": 123}]}),
        (DEVICES, {"DeviceList": [{"Id": "A", "DeviceName": ["x"]}]}),
        (ALERTS, {"Barking": 1}),
        (ALERTS, []),
        (LICENSE, {"DevicesLicense": []}),
        (LICENSE, {"DevicesLicense": {"A": {}}}),
        (LICENSE, {"DevicesLicense": {"A": [1]}}),
        (LICENSE, {"DevicesLicense": {"A": [{"SubscriptionStatus": 1}]}}),
        (EVENTS, {"Events": {}}),
        (EVENTS, {"Events": [1]}),
        (EVENTS, {"Events": [{"DeviceId": 1}]}),
        (EVENTS, {"Events": [{"LocalTime": 1}]}),
        (SUMMARY, {"Summary": ["x"]}),
        (SUMMARY, "just text"),
        (ACTIVITY, {"2026-09-05": []}),
        (ACTIVITY, {"2026-09-05": {"Data": []}}),
        (ACTIVITY, {"2026-09-05": {"Data": {"Barking": 3}}}),
        (ACTIVITY, {"2026-09-05": {"Data": {"Barking": [1, "x"]}}}),
        (ACTIVITY, {"2026-09-05": {"Data": {"Barking": [1, None]}}}),
    ],
)
async def test_reads_malformed_200(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, url: str, body: Any
) -> None:
    """A 200 whose shape does not match the contract is a connection error."""
    aioclient_mock.post(url, json=body)
    client = _client(hass, authed=True)
    calls: dict[str, Callable[[], Awaitable[Any]]] = {
        INFO: client.get_account_info,
        DEVICES: client.get_devices,
        ALERTS: lambda: client.get_alert_settings(c.DEVICE_ID),
        LICENSE: client.get_license,
        EVENTS: lambda: client.get_notable_events("2026-09-05"),
        SUMMARY: lambda: client.get_daily_summary("2026-09-05"),
        ACTIVITY: lambda: client.get_activity_report(["2026-09-05"]),
    }
    with pytest.raises(FurboConnectionError) as err:
        await calls[url]()
    assert "Malformed response" in str(err.value)


async def test_set_alert_setting(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A write posts to the update endpoint."""
    aioclient_mock.post(f"{ALERTS}/update", json={"PersonDetection": "1"})
    await _client(hass, authed=True).set_alert_setting(
        c.DEVICE_ID, "PersonDetection", True
    )
    assert aioclient_mock.call_count == 1
    assert aioclient_mock.mock_calls[0][2]["Value"] == "1"


# --- doggie diary ----------------------------------------------------------------

DIARY_MAIN = f"{MAIN_URL}/doggie_diary/report"
DIARY_PETGPT = f"{PETGPT_URL}/doggie_diary/report"

# A real-looking signed link. The test asserts none of it survives into the
# shape the client returns.
TIMELAPSE = (
    "https://d2xyz.cloudfront.net/diary/2026-09-10/ACCOUNT/timelapse.mp4"
    "?Expires=1789000000&Signature=abc123def&Key-Pair-Id=APKAEXAMPLE"
)
DIARY_BODY = {
    "ResultCode": 0,
    "Diaries": [
        {
            "DiaryDate": "2026-09-10",
            "Weekday": "Thursday",
            "IsValid": True,
            "JoyRemark": "a good day",
            "TimeLapseUrl": TIMELAPSE,
            "SnapshotUrl": "",
            "SurveyUrl": None,
        }
    ],
}


async def test_diary_describes_without_disclosing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The shape names the host, type and signing parameters, never the URL."""
    aioclient_mock.post(DIARY_MAIN, json=DIARY_BODY)
    shape = await _client(hass, authed=True).get_diary()

    assert shape["host"] == "product.furbo.co"
    assert shape["count"] == 1
    assert shape["fields"] == ["Diaries", "ResultCode"]
    day = shape["days"][0]
    assert day["date"] == "2026-09-10"
    assert day["weekday"] == "Thursday"
    assert day["valid"] is True
    assert "JoyRemark" in day["fields"]
    assert day["links"]["TimeLapseUrl"] == {
        "host": "d2xyz.cloudfront.net",
        "type": "mp4",
        "query": ["Expires", "Key-Pair-Id", "Signature"],
    }
    # An empty string and a missing value both read as "no link", not a crash.
    assert day["links"]["SnapshotUrl"] is None
    assert day["links"]["SurveyUrl"] is None

    # The signed URL, and every part of it that identifies the account, is gone.
    rendered = repr(shape)
    assert "timelapse.mp4" not in rendered
    assert "abc123def" not in rendered
    assert "APKAEXAMPLE" not in rendered


async def test_diary_falls_back_to_the_other_host(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The host that serves the diary is found by trying, then remembered."""
    aioclient_mock.post(DIARY_MAIN, status=404, json={"Code": 404})
    aioclient_mock.post(DIARY_PETGPT, json=DIARY_BODY)
    client = _client(hass, authed=True)

    assert (await client.get_diary())["host"] == "pet-gpt.furbo.co"
    calls_after_first = len(aioclient_mock.mock_calls)

    # The second call goes straight to the host that answered.
    assert (await client.get_diary())["host"] == "pet-gpt.furbo.co"
    assert len(aioclient_mock.mock_calls) == calls_after_first + 1


async def test_diary_raises_when_no_host_serves_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Both hosts refusing surfaces the refusal, not a silent empty diary."""
    aioclient_mock.post(DIARY_MAIN, status=400, json={"Code": 12345})
    aioclient_mock.post(DIARY_PETGPT, status=400, json={"Code": 12345})
    with pytest.raises(FurboError):
        await _client(hass, authed=True).get_diary()


async def test_diary_does_not_retry_a_dead_token_on_the_second_host(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A rejected token fails everywhere, so only one host is asked."""
    aioclient_mock.post(DIARY_MAIN, status=400, json={"Code": 12002})
    aioclient_mock.post(DIARY_PETGPT, json=DIARY_BODY)
    with pytest.raises(FurboAuthError):
        await _client(hass, authed=True).get_diary()
    assert len(aioclient_mock.mock_calls) == 1


async def test_diary_caps_the_days_it_describes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The count is the whole report; the detail stays small enough to store."""
    body = {"Diaries": [dict(DIARY_BODY["Diaries"][0]) for _ in range(10)]}
    aioclient_mock.post(DIARY_MAIN, json=body)
    shape = await _client(hass, authed=True).get_diary()
    assert shape["count"] == 10
    assert len(shape["days"]) == 3


async def test_diary_rejects_a_malformed_report(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Diaries that is not a list of objects is a malformed response."""
    aioclient_mock.post(DIARY_MAIN, json={"Diaries": ["not-an-object"]})
    with pytest.raises(FurboConnectionError):
        await _client(hass, authed=True).get_diary()
