"""Data update coordinator for Furbo."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
import logging
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .api import FurboAuthError, FurboClient, FurboError
from .bridge import (
    BridgeState,
    FurboBridgeClient,
    FurboBridgeError,
    FurboBridgeUnavailable,
)
from .const import (
    BRIDGE_SCAN_INTERVAL,
    CONF_COGNITO_TOKEN,
    CONF_EVENTS_ENABLED,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN_ISSUED_AT,
    DEFAULT_EVENTS_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from . import FurboConfigEntry

_LOGGER = logging.getLogger(__name__)

# How many polls in a row may end with the bridge unable to supply a token
# before the user is asked to sign in. Renewal is slow (the bridge may log in
# to the cloud first) and the add-on restarts on its own account, so a few
# missed attempts say nothing about whether recovery will work; a bridge that
# is gone for good must not leave the entry stuck without a way back.
MAX_RENEWAL_ATTEMPTS = 3
# Where that count lives. It cannot live on the coordinator: a setup that
# fails is retried with a new one, so a budget kept there starts again every
# time and an entry whose token has died with no bridge to renew it retries
# for ever instead of asking for a sign-in.
RENEWALS_MISSED: HassKey[dict[str, int]] = HassKey(f"{DOMAIN}_renewals_missed")


def clear_renewal_budget(hass: HomeAssistant, entry_id: str) -> None:
    """Forget any renewals this entry missed, restoring its full allowance."""
    hass.data.get(RENEWALS_MISSED, {}).pop(entry_id, None)


class _Renewal(Enum):
    """What came of asking the bridge for a current cloud token."""

    TAKEN = "taken"
    # The bridge cannot help: there is none, it is signed in elsewhere, or it
    # offered the token the cloud had just refused.
    REFUSED = "refused"
    # It might help, but could not answer this time.
    UNAVAILABLE = "unavailable"


# The diary changes once a day and the host that serves it rate-limits
# repeat calls, so it is asked for at most this often however fast the rest
# of the account is polled. A failed attempt waits the same hour: an account
# without the subscription would otherwise be refused on every cycle.
DIARY_INTERVAL = timedelta(hours=1)


@dataclass(slots=True)
class FurboDeviceData:
    """State for one Furbo camera."""

    info: dict[str, Any]
    alerts: dict[str, str] = field(default_factory=dict)
    subscription: dict[str, Any] | None = None
    last_event: dict[str, Any] | None = None
    last_event_at: datetime | None = None

    @property
    def device_id(self) -> str:
        """Return the stable device id (also the P2P account id)."""
        return str(self.info["Id"])

    @property
    def name(self) -> str:
        """Return the user-assigned device name."""
        return self.info.get("DeviceName") or self.device_id


@dataclass(slots=True)
class FurboData:
    """Everything one coordinator refresh produces."""

    devices: dict[str, FurboDeviceData]
    daily_summary: str = ""
    activity_today: dict[str, int] = field(default_factory=dict)
    notable_events_today: int = 0
    account_timezone: str | None = None
    # Shape of the account's Doggie Diary, never its links. None until the
    # cloud has answered once; see FurboClient.get_diary.
    diary: dict[str, Any] | None = None


class FurboCoordinator(DataUpdateCoordinator[FurboData]):
    """Polls the Furbo cloud and shares one response across all entities."""

    config_entry: FurboConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: FurboConfigEntry, client: FurboClient
    ) -> None:
        """Set up the coordinator with the per-entry poll interval."""
        interval = DEFAULT_SCAN_INTERVAL
        if seconds := entry.options.get(CONF_SCAN_INTERVAL):
            interval = max(
                timedelta(seconds=seconds), timedelta(seconds=MIN_SCAN_INTERVAL_SECONDS)
            )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=interval,
        )
        self.client = client
        # Set once the bridges are built: the add-on holds the account
        # password and logs in again by itself, so it can hand over a current
        # cloud token when this one is rejected.
        self.token_source: Callable[[], Awaitable[tuple[str, str]]] | None = None
        self.hub_device_id: str | None = None
        self._tzinfo: ZoneInfo | None = None
        self._timezone_name: str | None = None
        self._diary: dict[str, Any] | None = None
        self._diary_at: float | None = None
        # One diary download at a time for this account. Two presses -- a
        # person and an automation, say -- otherwise fetch the same day at
        # once and write the same partial file, and the first to finish moves
        # it out from under the second. Separate from the per-camera action
        # lock: a download has nothing to do with any camera, and waits far
        # longer than a pan should.
        self.diary_lock = asyncio.Lock()

    @property
    def events_enabled(self) -> bool:
        """Whether the pet calendar is polled."""
        return bool(
            self.config_entry.options.get(CONF_EVENTS_ENABLED, DEFAULT_EVENTS_ENABLED)
        )

    async def _async_setup(self) -> None:
        """One-off setup: learn the account timezone for event timestamps."""
        try:
            info = await self._async_account_info()
        except FurboAuthError as err:
            raise ConfigEntryAuthFailed from err
        except FurboError as err:
            raise UpdateFailed(str(err)) from err
        self._timezone_name = info.get("Timezone")
        if self._timezone_name:
            try:
                self._tzinfo = ZoneInfo(self._timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                self._tzinfo = None

    async def _async_account_info(self) -> dict[str, Any]:
        """Read the account, replacing a refused token once if we can.

        A restart after the token has died overnight lands here first.
        """
        try:
            return await self.client.get_account_info()
        except FurboAuthError:
            if not await self._async_recover_token():
                raise
        return await self.client.get_account_info()

    def _today(self) -> date:
        """Return today's date in the account's timezone."""
        tz = self._tzinfo or dt_util.get_default_time_zone()
        return datetime.now(tz).date()

    def _parse_event_time(self, local_time: str) -> datetime | None:
        """Parse a 'YYYY-MM-DD HH:MM:SS' local timestamp to an aware datetime."""
        try:
            naive = datetime.strptime(local_time, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None
        tz = self._tzinfo or dt_util.get_default_time_zone()
        return naive.replace(tzinfo=tz)

    def _token_age(self) -> str:
        """How old the stored cloud token is, for the log when it is refused."""
        issued = self.config_entry.data.get(CONF_TOKEN_ISSUED_AT)
        if not isinstance(issued, (int, float)):
            return "age unknown"
        return f"issued {(time.time() - issued) / 3600:.1f}h ago"

    async def _async_recover_token(self) -> bool:
        """Whether the refused call is worth trying again with a new token.

        A bridge that could not answer raises UpdateFailed instead, for a few
        polls: renewal takes as long as a cloud login, and a sign-in prompt
        put in front of someone whose add-on was merely slow is one they
        cannot tell from a real one.
        """
        outcome = await self._async_replace_token()
        if outcome is _Renewal.TAKEN:
            return True
        missed = self.hass.data.setdefault(RENEWALS_MISSED, {})
        entry_id = self.config_entry.entry_id
        if outcome is _Renewal.UNAVAILABLE and missed.get(entry_id, 0) < (
            MAX_RENEWAL_ATTEMPTS
        ):
            missed[entry_id] = missed.get(entry_id, 0) + 1
            raise UpdateFailed(
                "the Furbo Bridge add-on could not supply a cloud token "
                f"(attempt {missed[entry_id]} of {MAX_RENEWAL_ATTEMPTS})"
            )
        return False

    async def _async_replace_token(self) -> _Renewal:
        """Take a current cloud token from the bridge.

        The cloud issues a short-lived token and nothing to refresh it with,
        and this integration does not store the account password, so without
        the add-on the only way back is a person entering an emailed code.

        The bridge's token is only taken when it belongs to the account this
        entry was set up with; a bridge signed in elsewhere is refused rather
        than quietly repointing the entry at another account.
        """
        _LOGGER.debug("Cloud rejected the token (%s)", self._token_age())
        if self.token_source is None:
            return _Renewal.REFUSED
        try:
            account_id, token = await self.token_source()
        except FurboBridgeUnavailable as err:
            # Unreachable, still starting, or renewing and slow about it.
            _LOGGER.debug("The bridge could not supply a cloud token yet: %s", err)
            return _Renewal.UNAVAILABLE
        except FurboBridgeError as err:
            _LOGGER.debug("No cloud token from the bridge: %s", err)
            return _Renewal.REFUSED
        if account_id != self.client.account_id:
            # A bridge signed in to a different Furbo account. Its token would
            # authenticate, and every poll after it would read another
            # account's cameras through entities, a unique id and a device
            # registry that all belong to this one.
            _LOGGER.warning(
                "The Furbo Bridge add-on is signed in to a different Furbo "
                "account, so its cloud token was not used. Point the bridge at "
                "this account, or sign in to Furbo again to restore this one"
            )
            return _Renewal.REFUSED
        if token == self.client.cognito_token:
            # The bridge is offering the same token that was just refused.
            return _Renewal.REFUSED
        self.client.cognito_token = token
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_COGNITO_TOKEN: token,
                CONF_TOKEN_ISSUED_AT: time.time(),
            },
        )
        clear_renewal_budget(self.hass, self.config_entry.entry_id)
        _LOGGER.info("Took a fresh cloud token from the Furbo Bridge add-on")
        return _Renewal.TAKEN

    async def _async_update_data(self) -> FurboData:
        """Fetch the account, replacing a refused token once if we can."""
        try:
            return await self._async_fetch()
        except ConfigEntryAuthFailed:
            if not await self._async_recover_token():
                raise
        return await self._async_fetch()

    async def _async_fetch(self) -> FurboData:
        """Fetch devices, alerts, subscription and (optionally) the calendar."""
        try:
            device_list = await self.client.get_devices()
            licenses = await self.client.get_license()
            devices: dict[str, FurboDeviceData] = {}
            for info in device_list:
                device = FurboDeviceData(info=info)
                device.alerts = await self.client.get_alert_settings(device.device_id)
                subs = licenses.get(device.device_id, [])
                device.subscription = subs[0] if subs else None
                devices[device.device_id] = device
        except FurboAuthError as err:
            raise ConfigEntryAuthFailed from err
        except FurboError as err:
            raise UpdateFailed(str(err)) from err

        summary = ""
        activity: dict[str, int] = {}
        event_count = 0
        if self.events_enabled:
            # The calendar host (pet-gpt) rate-limits repeat calls hard (code
            # 80002) and is not needed for the camera, alert switches or device
            # sensors. Treat it as best-effort: on a non-auth failure, keep the
            # previous cycle's values and try again next time, rather than
            # failing the whole config entry's setup.
            try:
                today = self._today().isoformat()
                report = await self.client.get_activity_report([today])
                activity = report.get(today, {})
                summary = await self.client.get_daily_summary(today)
                events = await self.client.get_notable_events(today)
                event_count = len(events)
                self._apply_events(devices, events)
            except FurboAuthError as err:
                raise ConfigEntryAuthFailed from err
            except FurboError as err:
                _LOGGER.warning("Calendar data unavailable this cycle: %s", err)
                summary, activity, event_count = self._carry_over_calendar(devices)

        return FurboData(
            devices=devices,
            daily_summary=summary,
            activity_today=activity,
            notable_events_today=event_count,
            account_timezone=self._timezone_name,
            diary=await self._async_diary() if self.events_enabled else None,
        )

    async def _async_diary(self) -> dict[str, Any] | None:
        """Return the diary's shape, asking the cloud at most hourly.

        Gated on the same option as the calendar: both are extras on the
        rate-limiting host that the camera itself does not need.
        """
        now = time.monotonic()
        waited = None if self._diary_at is None else now - self._diary_at
        if waited is not None and waited < DIARY_INTERVAL.total_seconds():
            return self._diary
        # Set before the call, so a refusal backs off for the same hour.
        self._diary_at = now
        try:
            self._diary = await self.client.get_diary()
        except FurboAuthError as err:
            raise ConfigEntryAuthFailed from err
        except FurboError as err:
            _LOGGER.debug("Doggie Diary unavailable this cycle: %s", err)
        return self._diary

    def _carry_over_calendar(
        self, devices: dict[str, FurboDeviceData]
    ) -> tuple[str, dict[str, int], int]:
        """Reuse the previous cycle's calendar data when this one is unavailable."""
        previous = self.data
        if previous is None:
            return "", {}, 0
        for device_id, device in devices.items():
            prior = previous.devices.get(device_id)
            if prior is not None:
                device.last_event = prior.last_event
                device.last_event_at = prior.last_event_at
        return (
            previous.daily_summary,
            previous.activity_today,
            previous.notable_events_today,
        )

    def _apply_events(
        self, devices: dict[str, FurboDeviceData], events: list[dict[str, Any]]
    ) -> None:
        """Attach the most recent event to each device it belongs to."""
        latest: dict[str, dict[str, Any]] = {}
        for event in events:
            device_id = event.get("DeviceId")
            when = self._parse_event_time(event.get("LocalTime", ""))
            if device_id is None or when is None or device_id not in devices:
                continue
            current = latest.get(device_id)
            if current is None or when > current["_when"]:
                latest[device_id] = {**event, "_when": when}
        for device_id, device in devices.items():
            found = latest.get(device_id)
            if found is not None:
                device.last_event = {k: v for k, v in found.items() if k != "_when"}
                device.last_event_at = found["_when"]


class FurboBridgeCoordinator(DataUpdateCoordinator[BridgeState]):
    """Polls one camera's HTTP bridge for P2P state."""

    config_entry: FurboConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: FurboConfigEntry,
        device_id: str,
        client: FurboBridgeClient,
        cloud: FurboCoordinator,
    ) -> None:
        """Set up polling of the bridge that fronts one camera."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} bridge {device_id}",
            update_interval=BRIDGE_SCAN_INTERVAL,
        )
        self.device_id = device_id
        self.client = client
        # The cloud coordinator owns the device metadata (name, model, firmware).
        self.cloud = cloud
        # One-shot actions (pan, toss) share this camera's single P2P session,
        # so they go one at a time. Held here rather than as the button
        # platform's PARALLEL_UPDATES because that is per platform, not per
        # camera: it made one camera's pan wait on another camera's, and it
        # made both wait on a diary download that has nothing to do with any
        # camera and can run for minutes.
        self.action_lock = asyncio.Lock()

    async def _async_update_data(self) -> BridgeState:
        """Fetch the bridge's cached camera state, verifying its identity."""
        try:
            state = await self.client.async_get_status()
        except FurboBridgeError as err:
            raise UpdateFailed(str(err)) from err
        # Guard against a bridge wired to the wrong camera: if it reports a cloud
        # device id, it must match the one this coordinator serves, or its state
        # and controls would belong to another camera.
        if state.device_id is not None and state.device_id != self.device_id:
            raise UpdateFailed(
                f"bridge is serving camera {state.device_id}, not {self.device_id}; "
                "check the add-on's device_id option"
            )
        return state
