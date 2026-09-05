"""Data update coordinator for Furbo."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FurboAuthError, FurboClient, FurboError
from .const import (
    CONF_EVENTS_ENABLED,
    CONF_SCAN_INTERVAL,
    DEFAULT_EVENTS_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from . import FurboConfigEntry

_LOGGER = logging.getLogger(__name__)


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
        self._tzinfo: ZoneInfo | None = None
        self._timezone_name: str | None = None

    @property
    def events_enabled(self) -> bool:
        """Whether the pet calendar is polled."""
        return bool(
            self.config_entry.options.get(CONF_EVENTS_ENABLED, DEFAULT_EVENTS_ENABLED)
        )

    async def _async_setup(self) -> None:
        """One-off setup: learn the account timezone for event timestamps."""
        try:
            info = await self.client.get_account_info()
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

    async def _async_update_data(self) -> FurboData:
        """Fetch devices, alerts, subscription and (optionally) the calendar."""
        try:
            device_list = await self.client.get_devices()
            licenses = (await self.client.get_license()).get("DevicesLicense", {})
            devices: dict[str, FurboDeviceData] = {}
            for info in device_list:
                device = FurboDeviceData(info=info)
                device.alerts = await self.client.get_alert_settings(device.device_id)
                subs = licenses.get(device.device_id) or []
                device.subscription = subs[0] if subs else None
                devices[device.device_id] = device

            summary = ""
            activity: dict[str, int] = {}
            event_count = 0
            if self.events_enabled:
                today = self._today().isoformat()
                report = await self.client.get_activity_report([today])
                data = (report.get(today) or {}).get("Data") or {}
                activity = {key: sum(values) for key, values in data.items()}
                summary = await self.client.get_daily_summary(today)
                events = await self.client.get_notable_events(today)
                event_count = len(events)
                self._apply_events(devices, events)
        except FurboAuthError as err:
            raise ConfigEntryAuthFailed from err
        except FurboError as err:
            raise UpdateFailed(str(err)) from err

        return FurboData(
            devices=devices,
            daily_summary=summary,
            activity_today=activity,
            notable_events_today=event_count,
            account_timezone=self._timezone_name,
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
