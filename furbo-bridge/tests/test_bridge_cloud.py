"""Tests for the bridge's hardened Furbo cloud client.

These lock the security-relevant behaviour the review asked to port from the
integration's client: responses are validated, exception messages never carry
the response body, and the rate-limit back-off is bounded.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import furbo_cloud as fc


class FakeResponse:
    """Minimal async stand-in for an aiohttp response."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type: Any = None) -> Any:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Returns queued FakeResponses and records the URLs it was called with."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def post(self, url: str, **_kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        return self._responses.pop(0)


def _client(responses: list[FakeResponse]) -> fc.FurboClient:
    client = fc.FurboClient(FakeSession(responses), account_id="acc", cognito_token="tok")  # type: ignore[arg-type]
    return client


# --- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 10.0), (10, 10.0), (60, 60.0), (99999, 120.0), ("30", 30.0), ("x", 10.0)],
)
def test_rate_limit_wait_is_bounded(value: Any, expected: float) -> None:
    assert fc._rate_limit_wait({"TimeWait": value}) == expected


def test_rate_limit_wait_missing_field() -> None:
    assert fc._rate_limit_wait({}) == 10.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1.0), (1.5, 1.5), ("2", 2.0), (True, None), ("no", None), (None, None)],
)
def test_as_number(value: Any, expected: float | None) -> None:
    assert fc._as_number(value) == expected


def test_result_code_ignores_non_int() -> None:
    assert fc._result_code({"Code": 12002}) == 12002
    assert fc._result_code({"Code": True}) is None
    assert fc._result_code({"Code": "12002"}) is None
    assert fc._result_code("nope") is None


# --- redaction: bodies never appear in exceptions ---------------------------


def test_error_message_excludes_body() -> None:
    secret = "SUPER_SECRET_TOKEN_VALUE"
    resp = FakeResponse(400, {"Code": 55555, "Detail": secret, "Token": secret})
    client = _client([resp])

    async def go() -> None:
        with pytest.raises(fc.FurboError) as excinfo:
            await client.get_account_info()
        text = str(excinfo.value)
        assert secret not in text
        assert "55555" in text  # the numeric code is fine to surface
        assert "/v2/account/info" in text

    asyncio.run(go())


def test_token_invalid_maps_to_auth_error() -> None:
    resp = FakeResponse(401, {"Code": fc.CODE_TOKEN_INVALID, "Body": "secret"})
    client = _client([resp])

    async def go() -> None:
        with pytest.raises(fc.FurboAuthError) as excinfo:
            await client.get_account_info()
        assert "secret" not in str(excinfo.value)

    asyncio.run(go())


# --- response-shape validation ----------------------------------------------


def test_non_json_body_is_connection_error() -> None:
    resp = FakeResponse(200, ValueError("not json"))
    client = _client([resp])

    async def go() -> None:
        with pytest.raises(fc.FurboConnectionError):
            await client.get_account_info()

    asyncio.run(go())


def test_non_object_200_is_malformed() -> None:
    resp = FakeResponse(200, ["not", "an", "object"])
    client = _client([resp])

    async def go() -> None:
        with pytest.raises(fc.FurboConnectionError):
            await client.get_account_info()

    asyncio.run(go())


def test_alert_settings_rejects_non_string_values() -> None:
    resp = FakeResponse(200, {"Barking": "1", "Volume": 5})
    client = _client([resp])

    async def go() -> None:
        with pytest.raises(fc.FurboConnectionError):
            await client.get_alert_settings("dev")

    asyncio.run(go())


def test_p2p_connection_requires_keys() -> None:
    resp = FakeResponse(200, {"P2PAccountId": "x"})  # missing AuthKey/P2PAccountKey
    client = _client([resp])

    async def go() -> None:
        with pytest.raises(fc.FurboConnectionError):
            await client.get_p2p_connection("dev")

    asyncio.run(go())


def test_p2p_connection_ok() -> None:
    resp = FakeResponse(200, {"AuthKey": "a", "P2PAccountId": "1", "P2PAccountKey": "k"})
    client = _client([resp])

    async def go() -> None:
        creds = await client.get_p2p_connection("dev")
        assert creds["AuthKey"] == "a"

    asyncio.run(go())


# --- login / MFA flow -------------------------------------------------------


def test_start_login_requires_mfa() -> None:
    resp = FakeResponse(
        403,
        {"Code": fc.CODE_MFA_REQUIRED, "AccountId": "acc9", "MfaAuthCodeCandidate": "cand"},
    )
    client = fc.FurboClient(FakeSession([resp]))  # type: ignore[arg-type]

    async def go() -> None:
        candidate = await client.start_login("e", "enc", "mob")
        assert candidate == "cand"
        assert client.account_id == "acc9"

    asyncio.run(go())


def test_start_login_bad_password_is_login_error() -> None:
    resp = FakeResponse(401, {"Code": fc.CODE_TOKEN_INVALID, "Msg": "wrong"})
    client = fc.FurboClient(FakeSession([resp]))  # type: ignore[arg-type]

    async def go() -> None:
        with pytest.raises(fc.FurboLoginError):
            await client.start_login("e", "enc", "mob")

    asyncio.run(go())


def test_start_login_success_stores_token() -> None:
    resp = FakeResponse(200, {"AccountId": "acc", "CognitoToken": "newtok"})
    client = fc.FurboClient(FakeSession([resp]))  # type: ignore[arg-type]

    async def go() -> None:
        assert await client.start_login("e", "enc", "mob") is None
        assert client.cognito_token == "newtok"

    asyncio.run(go())


def test_send_and_complete_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """send_mfa_code refreshes the candidate; complete_login stores the token."""
    responses = [
        FakeResponse(200, {"MfaAuthCodeCandidate": "cand2"}),
        FakeResponse(200, {"MfaAuthCode": "authcode"}),
        FakeResponse(200, {"AccountId": "acc", "CognitoToken": "sess"}),
    ]
    client = fc.FurboClient(FakeSession(responses))  # type: ignore[arg-type]

    async def go() -> None:
        assert await client.send_mfa_code("cand") == "cand2"
        await client.complete_login("e", "enc", "mob", "cand2", "123456")
        assert client.cognito_token == "sess"

    asyncio.run(go())


def test_complete_login_bad_code_is_mfa_error() -> None:
    resp = FakeResponse(400, {"Code": fc.CODE_MFA_INVALID, "Msg": "nope"})
    client = fc.FurboClient(FakeSession([resp]))  # type: ignore[arg-type]

    async def go() -> None:
        with pytest.raises(fc.FurboMfaError):
            await client.complete_login("e", "enc", "mob", "cand", "000000")

    asyncio.run(go())


def test_get_devices_validates_ids() -> None:
    good = FakeResponse(200, {"DeviceList": [{"Id": "d1", "DeviceName": "Cam"}]})
    bad = FakeResponse(200, {"DeviceList": [{"DeviceName": "no id"}]})

    async def go() -> None:
        assert (await _client([good]).get_devices())[0]["Id"] == "d1"
        with pytest.raises(fc.FurboConnectionError):
            await _client([bad]).get_devices()

    asyncio.run(go())


def test_get_license_validates_container() -> None:
    good = FakeResponse(200, {"DevicesLicense": {"d1": []}})
    bad = FakeResponse(200, {"DevicesLicense": "not-a-dict"})

    async def go() -> None:
        assert "DevicesLicense" in await _client([good]).get_license()
        with pytest.raises(fc.FurboConnectionError):
            await _client([bad]).get_license()

    asyncio.run(go())


def test_notable_events_and_activity() -> None:
    events = FakeResponse(200, {"Events": [{"DeviceId": "d1"}]})
    activity = FakeResponse(200, {"2026-09-06": {"Data": {}}})

    async def go() -> None:
        assert (await _client([events]).get_notable_events("2026-09-06"))[0]["DeviceId"] == "d1"
        report = await _client([activity]).get_activity_report(["2026-09-06"])
        assert "2026-09-06" in report

    asyncio.run(go())


def test_encrypt_password_and_mobile_id() -> None:
    assert fc.encrypt_password("hunter2")  # non-empty base64
    mid = fc.new_mobile_id()
    assert len(mid) == 36 and mid == mid.upper()


def test_base_requires_login() -> None:
    client = fc.FurboClient(FakeSession([]))  # type: ignore[arg-type]

    async def go() -> None:
        with pytest.raises(fc.FurboAuthError):
            await client.get_account_info()

    asyncio.run(go())


def test_control_device_failure_has_no_body() -> None:
    resp = FakeResponse(200, {"Success": False, "Reason": "leak-me"})
    client = _client([resp])

    async def go() -> None:
        with pytest.raises(fc.FurboError) as excinfo:
            await client.toss_treat("dev")
        assert "leak-me" not in str(excinfo.value)

    asyncio.run(go())


# --- bounded rate-limit retry (no real sleeping) ----------------------------


def test_rate_limit_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(fc.asyncio, "sleep", fake_sleep)
    responses = [
        FakeResponse(429, {"Code": fc.CODE_RATE_LIMITED, "TimeWait": 99999}),
        FakeResponse(200, {"Summary": "all calm"}),
    ]
    client = _client(responses)

    async def go() -> None:
        summary = await client.get_daily_summary("2026-09-06")
        assert summary == "all calm"
        # The single back-off was clamped to the 120s ceiling, not 99999s.
        assert slept == [120.0]

    asyncio.run(go())
