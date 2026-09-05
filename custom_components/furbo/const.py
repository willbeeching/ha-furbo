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

# Flow field key for the emailed code.
CONF_MFA_CODE: Final = "mfa_code"

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
