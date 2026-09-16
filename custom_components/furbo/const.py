"""Constants for the Furbo integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "furbo"

# Config-entry schema version. Bump with a tested migration when data changes.
CONFIG_ENTRY_VERSION: Final = 3

# Config-entry data keys.
CONF_ACCOUNT_ID: Final = "account_id"
CONF_COGNITO_TOKEN: Final = "cognito_token"
CONF_MOBILE_ID: Final = "mobile_id"
# When the stored cloud token was issued, so its age can be reported when the
# cloud rejects it. The token is short lived and carries no expiry of its own.
CONF_TOKEN_ISSUED_AT: Final = "token_issued_at"

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
    "FBC0030": "Furbo 360 Cat",
    "MC0030": "Furbo Mini Cat",
}

# Smart-alert flags exposed as switches. Keys are the exact names the
# /v5/device/alert-setting payload uses. "Frequency:*" cooldown keys are
# intentionally not exposed. Only alerts we have named and translated are
# surfaced; unknown keys are ignored.
#
# The cat keys are not variants of the dog ones, they are a separate
# vocabulary a cat camera reports instead: Meowing where a dog barks,
# CatActivity where a dog moves. This list was first built from one FB0030
# and so held only the dog half, which left an FBC0030 owner with four
# switches out of nineteen alerts and no way to name the rest.
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
    # Cat cameras (FBC0030, MC0030).
    "Meowing",
    "ContinuousMeowing",
    "CatCrying",
    "ContinuousCatCrying",
    "CatActivity",
    "CatRun",
    "CatSelfie",
    "CatEatDrink",
    "CatChew",
    "CatPeePoo",
    "ContinuousCatPeePoo",
    "CatVomit",
    "ContinuousCatVomit",
    "CatSeizure",
    "ContinuousCatSeizure",
    "CatFurboOnline",
)

# The everyday alerts, enabled by default. The rest (continuous variants,
# seizure, vomit, glass-breaking, home-emergency, selfie, and so on) are
# specialised or noisy, so their switches are created disabled by default and
# the user enables the ones they want.
DEFAULT_ENABLED_ALERTS: Final = frozenset(
    {
        "Barking",
        "Crying",
        "PersonDetection",
        "DogMoveAbove10Sec",
        # The same four everyday alerts as a cat camera names them.
        "Meowing",
        "CatCrying",
        "CatActivity",
    }
)


# Smart alerts that also expose a notification-frequency select. Kept to the
# everyday alerts to avoid a select per alert. Only created when the device
# reports a "Frequency:<alert>" value for the alert.
FREQUENCY_ALERTS: Final = (
    "Barking",
    "PersonDetection",
    "DogMoveAbove10Sec",
    "Meowing",
    "CatActivity",
)

# What the event list can be filtered by, which is not the same set as the
# alerts above. Read from the app, which builds this list from the account's
# active features and sends it on every event request, never omitting it.
# Run, EatDrink, Chew, PeePoo, Seizure and FurboOnOff are alerts you can turn
# on but cannot ask the event list for; Earthquake, AutoCalm and Fighting are
# the other way round.
EVENT_NAMES: Final = (
    "PersonDetection",
    "GlassBreaking",
    "Earthquake",
    "HomeEmergency",
    "AutoCalm",
    "Barking",
    "ContinuousBarking",
    "Crying",
    "ContinuousCrying",
    "Howling",
    "ContinuousHowling",
    "Selfie",
    "DogMoveAbove10Sec",
    "Vomit",
    "ContinuousVomit",
    "Meowing",
    "ContinuousMeowing",
    "CatCrying",
    "ContinuousCatCrying",
    "CatSelfie",
    "CatActivity",
    "CatVomit",
    "ContinuousCatVomit",
    "Fighting",
)


# Frequency value (seconds, as the API stores it) -> option key. From the app:
# ALWAYS "1", EVERY_30_MINS "1800", EVERY_1_HR "3600".
ALERT_FREQUENCIES: Final = {
    "1": "always",
    "1800": "every_30_minutes",
    "3600": "every_hour",
}
