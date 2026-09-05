"""Diagnostics for Furbo, built by allowlisting safe fields."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import FurboConfigEntry
from .const import CONF_STREAM_URLS

# Device metadata that is safe to share. Identifiers (Id, P2PUuid, P2PAccountId,
# P2PAccountKey, AuthKey, DeviceHashData, MAC) are deliberately excluded.
_SAFE_DEVICE_FIELDS = (
    "DeviceName",
    "ProductId",
    "FirmwareVersion",
    "LibraryVersion",
    "DeviceStatus",
    "DeviceType",
    "BindingRole",
    "BindingStatus",
    "ServiceStatus",
    "ActiveFeatureSets",
)
_SAFE_SUBSCRIPTION_FIELDS = (
    "ServiceName",
    "ServicePlanName",
    "SubscriptionStatus",
    "TimeLeftDays",
    "IsAutoRenew",
    "LicenseType",
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FurboConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry with identifiers withheld."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    # Stream URLs can embed hostnames and credentials; report presence only.
    stream_urls: dict[str, str] = entry.options.get(CONF_STREAM_URLS, {})
    options = {k: v for k, v in entry.options.items() if k != CONF_STREAM_URLS}

    devices = []
    for index, device in enumerate(data.devices.values()):
        info = device.info
        entry_out: dict[str, Any] = {
            # A report-local index keeps devices distinguishable without
            # exposing the real device id.
            "ref": index,
            **{key: info.get(key) for key in _SAFE_DEVICE_FIELDS},
            "alerts": {
                key: value
                for key, value in device.alerts.items()
                if not key.startswith("Frequency:")
            },
            "has_recent_event": device.last_event_at is not None,
            "has_stream_url": device.device_id in stream_urls,
        }
        if device.subscription is not None:
            entry_out["subscription"] = {
                key: device.subscription.get(key) for key in _SAFE_SUBSCRIPTION_FIELDS
            }
        devices.append(entry_out)

    return {
        "options": options,
        "account_timezone": data.account_timezone,
        "events_enabled": coordinator.events_enabled,
        "activity_today": data.activity_today,
        "notable_events_today": data.notable_events_today,
        "devices": devices,
    }
