"""Async client for the Furbo cloud API.

The API was reverse engineered from the Furbo Android app. Every request is a
JSON POST to product.furbo.co carrying the account id and a CognitoToken that
is issued at login. Logins on accounts with two step verification enabled go
through an email code flow (MFA), so the client exposes that as three steps:
start_login, send_mfa_code and complete_login.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any
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

ACTION_TOSS_TREAT = "TossTreat"
ACTION_PLAY_TREAT_SOUND = "PlayTreatTossingSound"


class FurboError(Exception):
    """Base error for the Furbo API."""

    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class FurboAuthError(FurboError):
    """The token was rejected. A new login is needed."""


class FurboLoginError(FurboError):
    """Wrong email or password."""


class FurboMfaError(FurboError):
    """The MFA code was wrong or expired."""


class FurboConnectionError(FurboError):
    """The API could not be reached."""


def encrypt_password(password: str) -> str:
    """RSA encrypt the password the way the app does."""
    public_key = serialization.load_pem_public_key(RSA_PUBLIC_KEY)
    encrypted = public_key.encrypt(password.encode(), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def new_mobile_id() -> str:
    """Generate an id that identifies this client to Furbo."""
    return str(uuid.uuid4()).upper()


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
            "Content-Type": "application/json",
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
    ) -> Any:
        headers = dict(self._headers)
        kwargs: dict[str, Any] = {"json": payload}
        if form:
            headers.pop("Content-Type")
            kwargs = {"data": payload}
        try:
            async with self._session.post(
                f"{base}{path}", headers=headers, timeout=TIMEOUT, **kwargs
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200:
                    return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise FurboConnectionError(f"Error talking to Furbo: {err}") from err

        code = data.get("Code") if isinstance(data, dict) else None
        if code == CODE_RATE_LIMITED and retry > 0:
            # The calendar host rejects a repeat call to the same endpoint
            # inside roughly ten seconds. TimeWait understates it.
            await asyncio.sleep(max(float(data.get("TimeWait", 1)), 10.0))
            return await self._post(path, payload, base=base, form=form, retry=retry - 1)
        if code == CODE_TOKEN_INVALID:
            raise FurboAuthError("Token rejected", code, data)
        raise FurboError(f"Furbo API error {resp.status} on {path}: {data}", code, data)

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
        payload = {"Email": email, "EncPassword": enc_password, "MobileId": mobile_id}
        try:
            data = await self._post("/v5/account/read/login", payload)
        except FurboError as err:
            if err.code == CODE_MFA_REQUIRED:
                self.account_id = err.data.get("AccountId")
                return err.data["MfaAuthCodeCandidate"]
            # The login endpoint answers a bad email or password with 12002
            # ("wrong account or wrong password"), the same code the other
            # endpoints use for a dead token.
            raise FurboLoginError(str(err), err.code, err.data) from err
        self._store_login(data)
        return None

    async def send_mfa_code(self, candidate: str) -> str:
        """Ask Furbo to email the MFA code. Returns the refreshed candidate."""
        data = await self._post(
            "/v4/account/mfa/login/send-code",
            {"MfaAuthCodeCandidate": candidate, "Model": "Android"},
        )
        return data.get("MfaAuthCodeCandidate", candidate)

    async def complete_login(
        self, email: str, enc_password: str, mobile_id: str, candidate: str, code: str
    ) -> None:
        """Verify the emailed code and finish logging in."""
        try:
            verify = await self._post(
                "/v4/account/mfa/verify",
                {"MfaAuthCodeCandidate": candidate, "Code": code},
            )
        except FurboAuthError:
            raise
        except FurboError as err:
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
        self.account_id = data["AccountId"]
        self.cognito_token = data["CognitoToken"]

    # --- account and devices ---------------------------------------------

    async def get_account_info(self) -> dict[str, Any]:
        return await self._post("/v2/account/info", self._base())

    async def get_devices(self) -> list[dict[str, Any]]:
        base = self._base()
        data = await self._post(
            f"/v2/account/{base['AccountId']}/device",
            {"CognitoToken": base["CognitoToken"]},
        )
        return data.get("DeviceList", [])

    async def get_alert_settings(self, device_id: str) -> dict[str, str]:
        return await self._post("/v5/device/alert-setting", {**self._base(), "DeviceId": device_id})

    async def set_alert_setting(self, device_id: str, name: str, enabled: bool) -> None:
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
        """Subscription (Furbo Nanny) state per device."""
        return await self._post("/v3/service/license", self._base())

    async def get_pet_profiles(self) -> list[dict[str, Any]]:
        data = await self._post("/v3/pet/profile/get", {**self._base(), "LastUpdatedTime": 0})
        return data.get("PetProfiles", [])

    # --- calendar (pet-gpt host) -------------------------------------------

    async def get_notable_events(self, date: str) -> list[dict[str, Any]]:
        """Detected pet events for one day (YYYY-MM-DD, account local time).

        Each event has LocalTime, DeviceId, ActionCaption, Caption, Duration,
        a signed Thumbnail URL and, with cloud recording, Videos.
        """
        data = await self._post(
            "/v1/calendar/notable-events/get",
            {**self._base(), "Date": date},
            base=PETGPT_URL,
            form=True,
        )
        return data.get("Events", [])

    async def get_daily_summary(self, date: str) -> str:
        data = await self._post(
            "/v1/calendar/daily-summary/get",
            {**self._base(), "Date": date},
            base=PETGPT_URL,
            form=True,
        )
        return data.get("Summary", "")

    async def get_activity_report(self, dates: list[str]) -> dict[str, Any]:
        """Hourly counts per alert type (Barking, DogMoveAbove10Sec) per day."""
        return await self._post(
            "/v2/calendar/activity-report/get",
            {**self._base(), "Dates": dates},
            base=PETGPT_URL,
        )

    async def get_p2p_connection(self, device_id: str) -> dict[str, str]:
        """AuthKey, P2PAccountId and a freshly issued P2PAccountKey for TUTK."""
        return await self._post(
            "/v5/device/p2p_connection/get", {**self._base(), "DeviceId": device_id}
        )

    async def control_device(self, device_id: str, action: str) -> None:
        """Send a fire-and-forget command to the camera."""
        data = await self._post(
            "/v3/account/control_device",
            {**self._base(), "DeviceId": device_id, "Action": action},
        )
        if not data.get("Success"):
            raise FurboError(f"Action {action} failed: {data}")

    async def toss_treat(self, device_id: str) -> None:
        await self.control_device(device_id, ACTION_TOSS_TREAT)

    async def play_treat_sound(self, device_id: str) -> None:
        await self.control_device(device_id, ACTION_PLAY_TREAT_SOUND)
