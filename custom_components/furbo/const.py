"""Constants for the Furbo integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "furbo"

# Config-entry schema version. Bump with a tested migration when data changes.
CONFIG_ENTRY_VERSION: Final = 2

# Config-entry data keys.
CONF_ACCOUNT_ID: Final = "account_id"
CONF_COGNITO_TOKEN: Final = "cognito_token"
CONF_MOBILE_ID: Final = "mobile_id"

# Options keys.
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_EVENTS_ENABLED: Final = "events_enabled"
# Per-camera live-stream URLs, keyed by device id. The camera entity for a
# device exists only while a URL is configured for it.
CONF_STREAM_URLS: Final = "stream_urls"
# Per-camera HTTP bridge, keyed by device id: {"url": str, "token": str}.
# The bridge (furbo_p2p.py serve) holds the P2P session on the camera's LAN.
CONF_BRIDGES: Final = "bridges"

# Flow field key for the emailed code.
CONF_MFA_CODE: Final = "mfa_code"
# Options-flow field keys on the per-camera page.
CONF_STREAM_URL: Final = "stream_url"
CONF_BRIDGE_URL: Final = "bridge_url"
CONF_BRIDGE_TOKEN: Final = "bridge_token"

# Schemes accepted for a stream URL. These are what Home Assistant's stream
# component and go2rtc can consume directly.
STREAM_URL_SCHEMES: Final = ("rtsp://", "rtsps://", "http://", "https://")
BRIDGE_URL_SCHEMES: Final = ("http://", "https://")

# The bridge answers from a local cache, so polling it often is cheap.
BRIDGE_SCAN_INTERVAL: Final = timedelta(seconds=30)
# Relative rotation per pan button press, matching one press in the app.
PAN_DEGREES: Final = 60

DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)
MIN_SCAN_INTERVAL_SECONDS: Final = 60
DEFAULT_EVENTS_ENABLED: Final = True

MANUFACTURER: Final = "Furbo"

# Product id -> friendly model name. Only ids seen on real hardware are listed;
# an unknown id falls back to the raw id rather than a guessed name.
MODEL_NAMES: Final = {
    "FB0030": "Furbo 360",
}

# Smart-alert flags exposed as switches. Keys are the exact names the
# /v5/device/alert-setting payload uses (verified live, FB0030 firmware 108).
# "Frequency:*" cooldown keys are intentionally not exposed. Only alerts we
# have named and translated are surfaced; unknown keys are ignored.
ALERT_KEYS: Final = (
    "Barking",
    "ContinuousBarking",
    "Crying",
    "ContinuousCrying",
    "Howling",
    "ContinuousHowling",
    "PersonDetection",
    "DogMoveAbove10Sec",
    "Run",
    "Selfie",
    "EatDrink",
    "Chew",
    "PeePoo",
    "ContinuousPeePoo",
    "Vomit",
    "ContinuousVomit",
    "Seizure",
    "ContinuousSeizure",
    "GlassBreaking",
    "HomeEmergency",
    "FurboOnOff",
)

# The everyday alerts, enabled by default. The rest (continuous variants,
# seizure, vomit, glass-breaking, home-emergency, selfie, and so on) are
# specialised or noisy, so their switches are created disabled by default and
# the user enables the ones they want.
DEFAULT_ENABLED_ALERTS: Final = frozenset(
    {"Barking", "Crying", "PersonDetection", "DogMoveAbove10Sec"}
)


# Smart alerts that also expose a notification-frequency select. Kept to the
# everyday alerts to avoid a select per alert. Only created when the device
# reports a "Frequency:<alert>" value for the alert.
FREQUENCY_ALERTS: Final = ("Barking", "PersonDetection", "DogMoveAbove10Sec")

# Frequency value (seconds, as the API stores it) -> option key. From the app:
# ALWAYS "1", EVERY_30_MINS "1800", EVERY_1_HR "3600".
ALERT_FREQUENCIES: Final = {
    "1": "always",
    "1800": "every_30_minutes",
    "3600": "every_hour",
}
