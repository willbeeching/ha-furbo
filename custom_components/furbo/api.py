"""Standalone async client for the Furbo cloud API.

This module has no Home Assistant imports so it can be exercised on its own
and unit-tested without a running Home Assistant. It talks to two hosts:

* ``product.furbo.co`` - account, devices, alert settings, subscription.
* ``pet-gpt.furbo.co`` - the pet calendar: notable events, daily summary and
  the hourly activity report.

Provenance: every endpoint, header and payload here was captured live against
a real account (Furbo 360, product id FB0030, firmware 108) on 2026-09-05 and
cross-checked against the decompiled Android app 7.82.1 (see
``apk/decompiled/sources/j6/`` for the Retrofit interfaces and
``com/tomofun/furbo/device/p2p/`` for the P2P layer, which this cloud client
deliberately does not use). Values that could not be proven are not asserted
here; in particular the ``/v3/account/control_device`` endpoint returns
``{"Success": true}`` for any action string, so it is not exposed as a
capability by this client.
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

MAIN_URL = "https://product.furbo.co"
PETGPT_URL = "https://pet-gpt.furbo.co"

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


class FurboError(Exception):
    """Base error for the Furbo API."""

    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        """Store the API result code and body alongside the message."""
        super().__init__(message)
        self.code = code
        self.data = data


class FurboConnectionError(FurboError):
    """The API could not be reached or returned an unusable response."""


class FurboAuthError(FurboError):
    """The stored token was rejected. A fresh login is required."""


class FurboLoginError(FurboError):
    """The email or password was rejected."""


class FurboMfaError(FurboError):
    """The multi-factor code was wrong or expired."""


def encrypt_password(password: str) -> str:
    """RSA-encrypt a password the way the app does before login."""
    public_key = serialization.load_pem_public_key(RSA_PUBLIC_KEY)
    encrypted = public_key.encrypt(password.encode(), padding.PKCS1v15())  # type: ignore[union-attr]
    return base64.b64encode(encrypted).decode()


def new_mobile_id() -> str:
    """Generate a stable client identifier for this config entry."""
    return str(uuid.uuid4()).upper()


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
    ) -> Any:
        """POST JSON (or form) and return the decoded body, mapping errors."""
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
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise FurboConnectionError(f"Error talking to Furbo: {err}") from err

        if status == 200:
            return data

        code = data.get("Code") if isinstance(data, dict) else None
        if code == CODE_RATE_LIMITED and retry > 0:
            await asyncio.sleep(
                max(float(data.get("TimeWait", 1)), _RATE_LIMIT_MIN_WAIT)
            )
            return await self._post(
                path, payload, base=base, form=form, retry=retry - 1
            )
        if code == CODE_TOKEN_INVALID:
            raise FurboAuthError("Token rejected", code, data)
        raise FurboError(f"Furbo API error {status} on {path}: {data}", code, data)

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
        payload = {"Email": email, "EncPassword": enc_password, "MobileId": mobile_id}
        try:
            data = await self._post("/v5/account/read/login", payload)
        except FurboError as err:
            # 12101 asks for an MFA code. Every other rejection at the login
            # endpoint - including 12002, which Furbo also returns for a wrong
            # email or password here - is a login failure, not a stale token.
            if err.code == CODE_MFA_REQUIRED and isinstance(err.data, dict):
                self.account_id = err.data.get("AccountId")
                return cast("str", err.data["MfaAuthCodeCandidate"])
            raise FurboLoginError(str(err), err.code, err.data) from err
        self._store_login(data)
        return None

    async def send_mfa_code(self, candidate: str) -> str:
        """Ask Furbo to email the MFA code. Returns the refreshed candidate."""
        data = await self._post(
            "/v4/account/mfa/login/send-code",
            {"MfaAuthCodeCandidate": candidate, "Model": "Android"},
        )
        return cast("str", data.get("MfaAuthCodeCandidate", candidate))

    async def complete_login(
        self, email: str, enc_password: str, mobile_id: str, candidate: str, code: str
    ) -> None:
        """Verify the emailed code and finish logging in."""
        try:
            verify = await self._post(
                "/v4/account/mfa/verify",
                {"MfaAuthCodeCandidate": candidate, "Code": code},
            )
        except FurboError as err:
            # Any rejection verifying the emailed code is an MFA failure.
            raise FurboMfaError(str(err), err.code, err.data) from err
        data = await self._post(
            "/v2/account/login",
            {
                "Email": email,
                "EncPassword": enc_password,
                "MobileId": mobile_id,
                "MfaAuthCode": verify["MfaAuthCode"],
            },
        )
        self._store_login(data)

    def _store_login(self, data: dict[str, Any]) -> None:
        """Persist the identifiers a successful login returns."""
        self.account_id = data["AccountId"]
        self.cognito_token = data["CognitoToken"]

    # --- account and devices ----------------------------------------------

    async def get_account_info(self) -> dict[str, Any]:
        """Return account profile including Timezone and ServiceRegion."""
        return cast(
            "dict[str, Any]", await self._post("/v2/account/info", self._base())
        )

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return the bound devices with their metadata."""
        base = self._base()
        data = await self._post(
            f"/v2/account/{base['AccountId']}/device",
            {"CognitoToken": base["CognitoToken"]},
        )
        return cast("list[dict[str, Any]]", data.get("DeviceList", []))

    async def get_alert_settings(self, device_id: str) -> dict[str, str]:
        """Return the smart-alert flags and cooldowns for one device."""
        return cast(
            "dict[str, str]",
            await self._post(
                "/v5/device/alert-setting", {**self._base(), "DeviceId": device_id}
            ),
        )

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
        """Return subscription (Furbo Nanny) state per device."""
        return cast(
            "dict[str, Any]", await self._post("/v3/service/license", self._base())
        )

    # --- calendar (pet-gpt host) ------------------------------------------

    async def get_notable_events(self, date: str) -> list[dict[str, Any]]:
        """Return detected events for one day (YYYY-MM-DD, account local)."""
        data = await self._post(
            "/v1/calendar/notable-events/get",
            {**self._base(), "Date": date},
            base=PETGPT_URL,
            form=True,
        )
        return cast("list[dict[str, Any]]", data.get("Events", []))

    async def get_daily_summary(self, date: str) -> str:
        """Return the written summary of one day."""
        data = await self._post(
            "/v1/calendar/daily-summary/get",
            {**self._base(), "Date": date},
            base=PETGPT_URL,
            form=True,
        )
        return cast("str", data.get("Summary", ""))

    async def get_activity_report(self, dates: list[str]) -> dict[str, Any]:
        """Return hourly counts per alert type for the given days."""
        return cast(
            "dict[str, Any]",
            await self._post(
                "/v2/calendar/activity-report/get",
                {**self._base(), "Dates": dates},
                base=PETGPT_URL,
            ),
        )
