"""Standalone async client for the Furbo cloud API.

This module has no Home Assistant imports so it can be exercised on its own
and unit-tested without a running Home Assistant. It talks to two hosts:

* ``product.furbo.co`` - account, devices, alert settings, subscription.
* ``pet-gpt.furbo.co`` - the pet calendar: notable events, daily summary and
  the hourly activity report.

Every endpoint, header and payload here was captured live against a real
account (Furbo 360, product id FB0030, firmware 108) on 2026-09-05 and
cross-checked against the Android app's API definitions. Values that could
not be proven are not asserted here; in particular the
``/v3/account/control_device`` endpoint returns ``{"Success": true}`` for any
action string, so it is not exposed as a capability by this client.

Responses are validated at this boundary. Anything that does not have the
shape the caller relies on raises :class:`FurboConnectionError`, so callers
never see a ``KeyError`` or ``TypeError`` from a malformed body. Response
bodies are never included in exception messages or log lines.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
import uuid

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

_LOGGER = logging.getLogger(__name__)

MAIN_URL = "https://product.furbo.co"
PETGPT_URL = "https://pet-gpt.furbo.co"
# The diary, the event list and saved videos: the app's unversioned endpoints.
# All three hosts are named in the app's own runtime config, which it reads
# from https://dh1mqkcjivi9n.cloudfront.net/config/TF_FQDN.json rather than
# building in. Should one of them ever move, that file says where to.
EVENT_URL = "https://event-handler.furbo.co"

DIARY_PATH = "/doggie_diary/report"
DIARY_LINKS = ("TimeLapseUrl", "SnapshotUrl", "SurveyUrl")
# Enough days to see the pattern, few enough to sit in an entity attribute.
DIARY_DAYS = 3

# Static basic-auth header shipped in the app (client id, not a user secret).
BASIC_AUTH = "Basic dG9tb2Z1bnJkOmhhcHB5UGV0MTIz"
USER_AGENT = "Furbo/7.82.1 (Linux; Android 14)"
DEFAULT_TIMEOUT = 20

# Public key embedded in the app, used to RSA-encrypt the password at login.
RSA_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDwg8rbwkNIBmtGaS9GBURspabtzLdKCl24iZL9FFLsxexYqm2rb79wmVHptJg5JdrxufmIx9aZgnE/I9CiI7EWHzAKdupsya6LEQUg++rY18T8zvVaZnLzAqZC4+93SX5BGljDQ/khc0KH/ZvaJBDuHZGtI4J08wIK/8lLmDZ6lwIDAQAB
-----END PUBLIC KEY-----"""

# API result codes, from the app and observed responses.
CODE_MFA_REQUIRED = 12101
CODE_MFA_INVALID = 12102
CODE_MFA_EXPIRED = 12103
CODE_TOKEN_INVALID = 12002
CODE_TOO_MANY_ATTEMPTS = 80001
CODE_RATE_LIMITED = 80002  # body carries "TimeWait" in seconds

# The calendar host rejects a repeat call to the same endpoint inside roughly
# ten seconds; TimeWait understates it, so wait at least this long on 80002.
_RATE_LIMIT_MIN_WAIT = 10.0
# Upper bound on how long a single rate-limit back-off may sleep, whatever
# TimeWait claims.
_RATE_LIMIT_MAX_WAIT = 120.0


class FurboError(Exception):
    """Base error for the Furbo API.

    The message names the endpoint, HTTP status and Furbo result code only.
    It never carries the response body.
    """

    def __init__(self, message: str, code: int | None = None) -> None:
        """Store the numeric API result code alongside the message."""
        super().__init__(message)
        self.code = code


class FurboConnectionError(FurboError):
    """The API could not be reached or returned an unusable response."""


class FurboAuthError(FurboError):
    """The stored token was rejected. A fresh login is required."""


class FurboLoginError(FurboError):
    """The email or password was rejected."""


class FurboMfaError(FurboError):
    """The multi-factor code was wrong or expired."""


class FurboMfaRequired(FurboError):
    """Login needs an emailed code. Carries only the two validated ids."""

    def __init__(self, account_id: str, candidate: str) -> None:
        """Keep the account id and MFA candidate the next step needs."""
        super().__init__("Furbo login requires an MFA code", CODE_MFA_REQUIRED)
        self.account_id = account_id
        self.candidate = candidate


def encrypt_password(password: str) -> str:
    """RSA-encrypt a password the way the app does before login."""
    public_key = serialization.load_pem_public_key(RSA_PUBLIC_KEY)
    encrypted = public_key.encrypt(password.encode(), padding.PKCS1v15())  # type: ignore[union-attr]
    return base64.b64encode(encrypted).decode()


def new_mobile_id() -> str:
    """Generate a stable client identifier for this config entry."""
    return str(uuid.uuid4()).upper()


# --- response validation ---------------------------------------------------


def _link_shape(value: Any) -> dict[str, Any] | None:
    """Describe a URL without disclosing it, or None when there is not one.

    A diary link is a signed URL to video of the inside of someone's home. It
    ends up in an entity attribute, a diagnostics download and an issue
    report, so what is kept is the shape that answers whether it can simply be
    fetched: the host, the file type and which query parameters sign it. The
    URL itself never leaves the response.
    """
    if not isinstance(value, str) or not value:
        return None
    parts = urlsplit(value)
    name = parts.path.rsplit("/", 1)[-1]
    return {
        "host": parts.netloc,
        "type": name.rsplit(".", 1)[-1].lower() if "." in name else "",
        "query": sorted(parse_qs(parts.query)),
    }


def _diary_shape(data: dict[str, Any], host: str, path: str) -> dict[str, Any]:
    """Summarise a diary report: what it holds, not what it links to."""
    days = _as_dict_list(data.get("Diaries", []), path, "Diaries")
    return {
        "host": urlsplit(host).netloc,
        "fields": sorted(data),
        "count": len(days),
        "days": [
            {
                # Passed through as they come. This describes what the
                # report holds, so a field of an unexpected type is the
                # answer, not a malformed response: Weekday is an int.
                "date": day.get("DiaryDate"),
                "weekday": day.get("Weekday"),
                "valid": day.get("IsValid"),
                "fields": sorted(day),
                "links": {key: _link_shape(day.get(key)) for key in DIARY_LINKS},
            }
            for day in days[:DIARY_DAYS]
        ],
    }


def _malformed(path: str, what: str) -> FurboConnectionError:
    """Build the error raised for a response that does not fit the contract."""
    return FurboConnectionError(f"Malformed response from {path}: {what}")


def _as_dict(value: Any, path: str, what: str = "body") -> dict[str, Any]:
    """Return ``value`` if it is a JSON object, else raise."""
    if not isinstance(value, dict):
        raise _malformed(path, f"{what} is not an object")
    return cast("dict[str, Any]", value)


def _as_dict_list(value: Any, path: str, what: str) -> list[dict[str, Any]]:
    """Return ``value`` if it is a list of JSON objects, else raise."""
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
    """Furbo cloud API client bound to an aiohttp session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        account_id: str | None = None,
        cognito_token: str | None = None,
        *,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialise the client, optionally with an existing session token."""
        self._session = session
        self.account_id = account_id
        self.cognito_token = cognito_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = {
            "User-Agent": USER_AGENT,
            "Authorization": BASIC_AUTH,
        }

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        base: str = MAIN_URL,
        form: bool = False,
        retry: int = 3,
    ) -> dict[str, Any]:
        """POST JSON (or form) and return the decoded object, mapping errors.

        Only the HTTP status, endpoint and numeric result code are surfaced
        in exceptions; the body itself stays inside this method.
        """
        headers = dict(self._headers)
        if form:
            kwargs: dict[str, Any] = {"data": payload}
        else:
            headers["Content-Type"] = "application/json"
            kwargs = {"json": payload}
        try:
            async with self._session.post(
                f"{base}{path}", headers=headers, timeout=self._timeout, **kwargs
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
            await asyncio.sleep(_rate_limit_wait(data))
            return await self._post(
                path, payload, base=base, form=form, retry=retry - 1
            )
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
        """Return the account id and token every authenticated call needs."""
        if not self.account_id or not self.cognito_token:
            raise FurboAuthError("Not logged in")
        return {"AccountId": self.account_id, "CognitoToken": self.cognito_token}

    # --- login -------------------------------------------------------------

    async def start_login(
        self, email: str, enc_password: str, mobile_id: str
    ) -> str | None:
        """Begin login. Returns None if no MFA is needed, else the candidate."""
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
            # Every rejection at the login endpoint - including 12002, which
            # Furbo also returns for a wrong email or password here - is a
            # login failure, not a stale token.
            raise FurboLoginError(str(err), err.code) from err
        self._store_login(data, path)
        return None

    async def send_mfa_code(self, candidate: str) -> str:
        """Ask Furbo to email the MFA code. Returns the refreshed candidate."""
        path = "/v4/account/mfa/login/send-code"
        data = await self._post(
            path, {"MfaAuthCodeCandidate": candidate, "Model": "Android"}
        )
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
            # Any rejection verifying the emailed code is an MFA failure.
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
        """Persist the identifiers a successful login returns."""
        # Field names only, never values. Two fields are kept and the rest
        # discarded, and "the cloud gives us nothing to refresh with" has only
        # ever been a description of this parser -- nobody has looked at what
        # the response actually carries. If there is a refresh token in here,
        # renewing the way the app does beats logging in again.
        _LOGGER.debug(
            "Login response from %s carried: %s", path, ", ".join(sorted(data))
        )
        account_id = _required_str(data, "AccountId", path)
        cognito_token = _required_str(data, "CognitoToken", path)
        self.account_id = account_id
        self.cognito_token = cognito_token

    # --- account and devices ----------------------------------------------

    async def get_account_info(self) -> dict[str, Any]:
        """Return account profile including Timezone and ServiceRegion."""
        path = "/v2/account/info"
        data = await self._post(path, self._base())
        _optional_str(data, "Timezone", path)
        return data

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return the bound devices with their metadata.

        Each device is guaranteed to carry a non-empty string ``Id`` and, when
        present, a string ``DeviceName``.
        """
        base = self._base()
        path = f"/v2/account/{base['AccountId']}/device"
        data = await self._post(path, {"CognitoToken": base["CognitoToken"]})
        devices = _as_dict_list(data.get("DeviceList", []), path, "DeviceList")
        for device in devices:
            _required_str(device, "Id", path)
            _optional_str(device, "DeviceName", path)
        return devices

    async def get_alert_settings(self, device_id: str) -> dict[str, str]:
        """Return the smart-alert flags and cooldowns for one device.

        The API returns every value as a string ("0"/"1" for flags, seconds
        for the ``Frequency:*`` cooldowns); anything else is rejected.
        """
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

    async def set_alert_frequency(
        self, device_id: str, alert: str, seconds: str
    ) -> None:
        """Set how often one alert may notify, as a ``Frequency:<alert>`` value."""
        await self._post(
            "/v5/device/alert-setting/update",
            {
                **self._base(),
                "DeviceId": device_id,
                "Name": f"Frequency:{alert}",
                "Value": seconds,
            },
        )

    async def get_license(self) -> dict[str, list[dict[str, Any]]]:
        """Return subscription (Furbo Nanny) entries keyed by device id.

        ``TimeLeftDays`` is normalised to an int, or removed when it is not
        numeric, so sensors can use it without further checks.
        """
        path = "/v3/service/license"
        data = await self._post(path, self._base())
        licenses = _as_dict(data.get("DevicesLicense", {}), path, "DevicesLicense")
        result: dict[str, list[dict[str, Any]]] = {}
        for device_id, entries in licenses.items():
            validated = _as_dict_list(entries, path, f"DevicesLicense[{device_id}]")
            for entry in validated:
                days = _as_number(entry.get("TimeLeftDays"))
                if days is None:
                    entry.pop("TimeLeftDays", None)
                else:
                    entry["TimeLeftDays"] = int(days)
                _optional_str(entry, "SubscriptionStatus", path)
            result[str(device_id)] = validated
        return result

    # --- calendar (pet-gpt host) ------------------------------------------

    async def get_notable_events(self, date: str) -> list[dict[str, Any]]:
        """Return detected events for one day (YYYY-MM-DD, account local)."""
        path = "/v1/calendar/notable-events/get"
        data = await self._post(
            path, {**self._base(), "Date": date}, base=PETGPT_URL, form=True
        )
        events = _as_dict_list(data.get("Events", []), path, "Events")
        for event in events:
            _optional_str(event, "DeviceId", path)
            _optional_str(event, "LocalTime", path)
        return events

    async def get_daily_summary(self, date: str) -> str:
        """Return the written summary of one day, or "" when there is none."""
        path = "/v1/calendar/daily-summary/get"
        data = await self._post(
            path, {**self._base(), "Date": date}, base=PETGPT_URL, form=True
        )
        return _optional_str(data, "Summary", path) or ""

    async def get_diary_report(self, language: str = "en") -> list[dict[str, Any]]:
        """Return the account's diary days as the cloud sends them.

        One entry per day for a rolling week, oldest first, each carrying a
        TimeLapseUrl: the daily video that otherwise only the phone app will
        show you. The links are presigned and time limited, so this is for a
        caller about to use them right now. Anything that keeps a result
        should keep :meth:`get_diary` instead.
        """
        data = await self._post(
            DIARY_PATH,
            {**self._base(), "Language": language},
            base=EVENT_URL,
            form=True,
        )
        return _as_dict_list(data.get("Diaries", []), DIARY_PATH, "Diaries")

    async def get_diary(self, language: str = "en") -> dict[str, Any]:
        """Describe the account's Doggie Diary, without its links.

        The shape of the report rather than the report itself, because
        nothing needs a URL until something downloads it, and whatever does
        that reads it where it is fetched rather than carrying it through
        Home Assistant's state.
        """
        data = await self._post(
            DIARY_PATH,
            {**self._base(), "Language": language},
            base=EVENT_URL,
            form=True,
        )
        return _diary_shape(data, EVENT_URL, DIARY_PATH)

    async def get_activity_report(self, dates: list[str]) -> dict[str, dict[str, int]]:
        """Return the total count per alert type for each requested day.

        The API answers with hourly buckets per type; they are summed here
        after checking every bucket is a number.
        """
        path = "/v2/calendar/activity-report/get"
        data = await self._post(path, {**self._base(), "Dates": dates}, base=PETGPT_URL)
        report: dict[str, dict[str, int]] = {}
        for day, day_data in data.items():
            buckets = _as_dict(day_data, path, f"report for {day}").get("Data")
            totals: dict[str, int] = {}
            if buckets is None:
                buckets = {}
            for key, values in _as_dict(buckets, path, f"Data for {day}").items():
                if not isinstance(values, list):
                    raise _malformed(path, f"{key} counts are not a list")
                numbers = [_as_number(v) for v in values]
                if any(n is None for n in numbers):
                    raise _malformed(path, f"{key} counts are not numeric")
                totals[str(key)] = int(sum(n for n in numbers if n is not None))
            report[str(day)] = totals
        return report
