"""Async client for the Furbo cloud API (add-on copy).

The API was reverse engineered from the Furbo Android app. Every request is a
JSON POST to product.furbo.co carrying the account id and a CognitoToken that
is issued at login. Logins on accounts with two step verification enabled go
through an email code flow (MFA), so the client exposes that as three steps:
start_login, send_mfa_code and complete_login.

This mirrors the hardening of the integration's client
(custom_components/furbo/api.py): responses are validated at this boundary, and
exception messages and log lines carry only the endpoint, HTTP status and
numeric result code - never the response body, which may contain tokens or
personal data.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, cast
import uuid

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://product.furbo.co"
PETGPT_URL = "https://pet-gpt.furbo.co"  # calendar: events, summaries, activity
BASIC_AUTH = "Basic dG9tb2Z1bnJkOmhhcHB5UGV0MTIz"
USER_AGENT = "Furbo/7.65.0 (Linux; Android 14)"
TIMEOUT = aiohttp.ClientTimeout(total=20)

# Public key embedded in the app, used to encrypt the password at login.
RSA_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDwg8rbwkNIBmtGaS9GBURspabtzLdKCl24iZL9FFLsxexYqm2rb79wmVHptJg5JdrxufmIx9aZgnE/I9CiI7EWHzAKdupsya6LEQUg++rY18T8zvVaZnLzAqZC4+93SX5BGljDQ/khc0KH/ZvaJBDuHZGtI4J08wIK/8lLmDZ6lwIDAQAB
-----END PUBLIC KEY-----"""

CODE_MFA_REQUIRED = 12101
CODE_MFA_INVALID = 12102
CODE_MFA_EXPIRED = 12103
CODE_TOKEN_INVALID = 12002
CODE_TOO_MANY_ATTEMPTS = 80001
CODE_RATE_LIMITED = 80002  # body carries TimeWait in seconds

# The calendar host rejects a repeat call to the same endpoint inside roughly
# ten seconds; TimeWait understates it, so wait at least this long on 80002 and
# never sleep longer than the upper bound whatever TimeWait claims.
_RATE_LIMIT_MIN_WAIT = 10.0
_RATE_LIMIT_MAX_WAIT = 120.0

ACTION_TOSS_TREAT = "TossTreat"
ACTION_PLAY_TREAT_SOUND = "PlayTreatTossingSound"


class FurboError(Exception):
    """Base error for the Furbo API.

    The message names the endpoint, HTTP status and numeric result code only;
    it never carries the response body.
    """

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class FurboConnectionError(FurboError):
    """The API could not be reached or returned an unusable response."""


class FurboAuthError(FurboError):
    """The token was rejected. A new login is needed."""


class FurboLoginError(FurboError):
    """Wrong email or password."""


class FurboMfaError(FurboError):
    """The MFA code was wrong or expired."""


class FurboMfaRequired(FurboError):
    """Login needs an emailed code. Carries only the two validated ids."""

    def __init__(self, account_id: str, candidate: str) -> None:
        super().__init__("Furbo login requires an MFA code", CODE_MFA_REQUIRED)
        self.account_id = account_id
        self.candidate = candidate


def encrypt_password(password: str) -> str:
    """RSA encrypt the password the way the app does."""
    public_key = serialization.load_pem_public_key(RSA_PUBLIC_KEY)
    encrypted = public_key.encrypt(password.encode(), padding.PKCS1v15())  # type: ignore[union-attr]
    return base64.b64encode(encrypted).decode()


def new_mobile_id() -> str:
    """Generate an id that identifies this client to Furbo."""
    return str(uuid.uuid4()).upper()


# --- response validation ---------------------------------------------------


def _malformed(path: str, what: str) -> FurboConnectionError:
    """Build the error raised for a response that does not fit the contract."""
    return FurboConnectionError(f"Malformed response from {path}: {what}")


def _as_dict(value: Any, path: str, what: str = "body") -> dict[str, Any]:
    """Return value if it is a JSON object, else raise."""
    if not isinstance(value, dict):
        raise _malformed(path, f"{what} is not an object")
    return cast("dict[str, Any]", value)


def _as_dict_list(value: Any, path: str, what: str) -> list[dict[str, Any]]:
    """Return value if it is a list of JSON objects, else raise."""
    if not isinstance(value, list) or not all(isinstance(i, dict) for i in value):
        raise _malformed(path, f"{what} is not a list of objects")
    return cast("list[dict[str, Any]]", value)


def _required_str(data: dict[str, Any], key: str, path: str) -> str:
    """Return a non-empty string field or raise."""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise _malformed(path, f"missing {key}")
    return value


def _optional_str(data: dict[str, Any], key: str, path: str) -> str | None:
    """Return a string field, None when absent, and raise on any other type."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _malformed(path, f"{key} is not a string")
    return value


def _as_number(value: Any) -> float | None:
    """Coerce an int, float or numeric string to float; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _result_code(data: Any) -> int | None:
    """Extract the numeric Furbo result code from an error body, if any."""
    if not isinstance(data, dict):
        return None
    code = data.get("Code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _rate_limit_wait(data: dict[str, Any]) -> float:
    """Return how long to sleep after an 80002, bounded on both sides."""
    wait = _as_number(data.get("TimeWait"))
    if wait is None:
        wait = _RATE_LIMIT_MIN_WAIT
    return min(max(wait, _RATE_LIMIT_MIN_WAIT), _RATE_LIMIT_MAX_WAIT)


class FurboClient:
    """Furbo cloud API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        account_id: str | None = None,
        cognito_token: str | None = None,
    ) -> None:
        self._session = session
        self.account_id = account_id
        self.cognito_token = cognito_token
        self._headers = {
            "User-Agent": USER_AGENT,
            "Authorization": BASIC_AUTH,
        }

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        base: str = BASE_URL,
        form: bool = False,
        retry: int = 3,
    ) -> dict[str, Any]:
        """POST JSON (or form) and return the decoded object, mapping errors.

        Only the HTTP status, endpoint and numeric result code are surfaced in
        exceptions; the body itself never leaves this method.
        """
        headers = dict(self._headers)
        if form:
            kwargs: dict[str, Any] = {"data": payload}
        else:
            headers["Content-Type"] = "application/json"
            kwargs = {"json": payload}
        try:
            async with self._session.post(
                f"{base}{path}", headers=headers, timeout=TIMEOUT, **kwargs
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except ValueError as err:
                    raise _malformed(path, f"status {status} body is not JSON") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FurboConnectionError(
                f"Error talking to Furbo on {path}: {type(err).__name__}"
            ) from err

        if status == 200:
            return _as_dict(data, path)

        code = _result_code(data)
        if code == CODE_RATE_LIMITED and retry > 0:
            await asyncio.sleep(_rate_limit_wait(_as_dict(data, path)))
            return await self._post(path, payload, base=base, form=form, retry=retry - 1)
        if code == CODE_MFA_REQUIRED:
            body = _as_dict(data, path)
            raise FurboMfaRequired(
                _required_str(body, "AccountId", path),
                _required_str(body, "MfaAuthCodeCandidate", path),
            )
        message = f"Furbo API error {status} (code {code}) on {path}"
        if code == CODE_TOKEN_INVALID:
            raise FurboAuthError(message, code)
        raise FurboError(message, code)

    def _base(self) -> dict[str, Any]:
        if not self.account_id or not self.cognito_token:
            raise FurboAuthError("Not logged in")
        return {"AccountId": self.account_id, "CognitoToken": self.cognito_token}

    # --- login -------------------------------------------------------------

    async def start_login(self, email: str, enc_password: str, mobile_id: str) -> str | None:
        """Try to log in.

        Returns None when login completed without MFA, otherwise the
        MfaAuthCodeCandidate needed for the next step.
        """
        path = "/v5/account/read/login"
        payload = {"Email": email, "EncPassword": enc_password, "MobileId": mobile_id}
        try:
            data = await self._post(path, payload)
        except FurboMfaRequired as err:
            self.account_id = err.account_id
            return err.candidate
        except FurboConnectionError:
            raise
        except FurboError as err:
            # The login endpoint answers a bad email or password with 12002
            # ("wrong account or wrong password"), the same code the other
            # endpoints use for a dead token.
            raise FurboLoginError(str(err), err.code) from err
        self._store_login(data, path)
        return None

    async def send_mfa_code(self, candidate: str) -> str:
        """Ask Furbo to email the MFA code. Returns the refreshed candidate."""
        path = "/v4/account/mfa/login/send-code"
        data = await self._post(path, {"MfaAuthCodeCandidate": candidate, "Model": "Android"})
        return _optional_str(data, "MfaAuthCodeCandidate", path) or candidate

    async def complete_login(
        self, email: str, enc_password: str, mobile_id: str, candidate: str, code: str
    ) -> None:
        """Verify the emailed code and finish logging in."""
        verify_path = "/v4/account/mfa/verify"
        try:
            verify = await self._post(
                verify_path, {"MfaAuthCodeCandidate": candidate, "Code": code}
            )
        except FurboConnectionError:
            raise
        except FurboError as err:
            raise FurboMfaError(str(err), err.code) from err
        login_path = "/v2/account/login"
        data = await self._post(
            login_path,
            {
                "Email": email,
                "EncPassword": enc_password,
                "MobileId": mobile_id,
                "MfaAuthCode": _required_str(verify, "MfaAuthCode", verify_path),
            },
        )
        self._store_login(data, login_path)

    def _store_login(self, data: dict[str, Any], path: str) -> None:
        self.account_id = _required_str(data, "AccountId", path)
        self.cognito_token = _required_str(data, "CognitoToken", path)

    # --- account and devices ---------------------------------------------

    async def get_account_info(self) -> dict[str, Any]:
        """Return the account profile."""
        return await self._post("/v2/account/info", self._base())

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return the bound devices; each carries a non-empty string Id."""
        base = self._base()
        path = f"/v2/account/{base['AccountId']}/device"
        data = await self._post(path, {"CognitoToken": base["CognitoToken"]})
        devices = _as_dict_list(data.get("DeviceList", []), path, "DeviceList")
        for device in devices:
            _required_str(device, "Id", path)
        return devices

    async def get_alert_settings(self, device_id: str) -> dict[str, str]:
        """Return the smart-alert flags/cooldowns (all string-valued)."""
        path = "/v5/device/alert-setting"
        data = await self._post(path, {**self._base(), "DeviceId": device_id})
        if not all(isinstance(v, str) for v in data.values()):
            raise _malformed(path, "alert values are not strings")
        return cast("dict[str, str]", data)

    async def set_alert_setting(self, device_id: str, name: str, enabled: bool) -> None:
        """Enable or disable one named smart alert."""
        await self._post(
            "/v5/device/alert-setting/update",
            {
                **self._base(),
                "DeviceId": device_id,
                "Name": name,
                "Value": "1" if enabled else "0",
            },
        )

    async def get_license(self) -> dict[str, Any]:
        """Subscription (Furbo Nanny) state; DevicesLicense is validated."""
        path = "/v3/service/license"
        data = await self._post(path, self._base())
        _as_dict(data.get("DevicesLicense", {}), path, "DevicesLicense")
        return data

    async def get_pet_profiles(self) -> list[dict[str, Any]]:
        """Return the account's pet profiles."""
        path = "/v3/pet/profile/get"
        data = await self._post(path, {**self._base(), "LastUpdatedTime": 0})
        return _as_dict_list(data.get("PetProfiles", []), path, "PetProfiles")

    # --- calendar (pet-gpt host) -------------------------------------------

    async def get_notable_events(self, date: str) -> list[dict[str, Any]]:
        """Detected pet events for one day (YYYY-MM-DD, account local time)."""
        path = "/v1/calendar/notable-events/get"
        data = await self._post(path, {**self._base(), "Date": date}, base=PETGPT_URL, form=True)
        return _as_dict_list(data.get("Events", []), path, "Events")

    async def get_daily_summary(self, date: str) -> str:
        """Return the written summary of one day, or '' when there is none."""
        path = "/v1/calendar/daily-summary/get"
        data = await self._post(path, {**self._base(), "Date": date}, base=PETGPT_URL, form=True)
        return _optional_str(data, "Summary", path) or ""

    async def get_activity_report(self, dates: list[str]) -> dict[str, Any]:
        """Hourly counts per alert type (Barking, DogMoveAbove10Sec) per day."""
        return await self._post(
            "/v2/calendar/activity-report/get",
            {**self._base(), "Dates": dates},
            base=PETGPT_URL,
        )

    async def get_p2p_connection(self, device_id: str) -> dict[str, str]:
        """AuthKey, P2PAccountId and a freshly issued P2PAccountKey for TUTK."""
        path = "/v5/device/p2p_connection/get"
        data = await self._post(path, {**self._base(), "DeviceId": device_id})
        _required_str(data, "AuthKey", path)
        _required_str(data, "P2PAccountKey", path)
        return cast("dict[str, str]", data)

    async def control_device(self, device_id: str, action: str) -> None:
        """Send a fire-and-forget command to the camera."""
        path = "/v3/account/control_device"
        data = await self._post(path, {**self._base(), "DeviceId": device_id, "Action": action})
        if not data.get("Success"):
            raise FurboError(f"Action failed on {path}")

    async def toss_treat(self, device_id: str) -> None:
        """Dispense a treat."""
        await self.control_device(device_id, ACTION_TOSS_TREAT)

    async def play_treat_sound(self, device_id: str) -> None:
        """Play the treat-tossing sound."""
        await self.control_device(device_id, ACTION_PLAY_TREAT_SOUND)
