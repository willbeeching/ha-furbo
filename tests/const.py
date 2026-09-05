"""Shared test data.

The dicts below are faithful samples of live captures taken on 2026-09-05 from
a Furbo 360 (product id FB0030, firmware 108). Real identifiers and all P2P
secrets (AuthKey, P2PAccountId, P2PAccountKey, P2PUuid) have been replaced with
placeholders; field names, types and shapes are unchanged.
"""

from __future__ import annotations

from typing import Any

ACCOUNT_ID = "ACC0000000001"
DEVICE_ID = "AA11BB22CC33"
EMAIL = "furbo-user@example.com"
PASSWORD = "correct horse battery staple"
COGNITO_TOKEN = ".sample-cognito-token."

ACCOUNT_INFO: dict[str, Any] = {
    "AccountId": ACCOUNT_ID,
    "Email": EMAIL,
    "Locale": "en_gb",
    "ServiceRegion": "UK",
    "Timezone": "Europe/London",
    "TwoStepAuthEnabled": True,
    "Type": "FURBO_V2",
}

DEVICE: dict[str, Any] = {
    "Id": DEVICE_ID,
    "DeviceName": "Test Camera",
    "ProductId": "FB0030",
    "FirmwareVersion": "108",
    "LibraryVersion": "002.063",
    "DeviceStatus": "ACTIVE",
    "DeviceType": "NORMAL",
    "BindingRole": "ADMIN",
    "BindingStatus": "ACTIVATED",
    "ServiceStatus": "ACTIVE",
    "ActiveFeatureSets": ["SmartAlerts:Barking", "LiveViewTracking"],
    "P2PUuid": "PLACEHOLDERUID000000",
    "P2PAccountId": DEVICE_ID,
    "P2PAccountKey": "PLACEHOLDERKEY",
    "AuthKey": "PLCEHLDR",
    "P2PVendor": "TUTKV2",
}

DEVICE_LIST_RESPONSE: dict[str, Any] = {
    "AccountLicenseOn": True,
    "DeviceList": [DEVICE],
}

ALERTS: dict[str, str] = {
    "Barking": "1",
    "ContinuousBarking": "1",
    "Crying": "1",
    "PersonDetection": "0",
    "DogMoveAbove10Sec": "1",
    "Selfie": "1",
    "Frequency:Barking": "1800",
    "Frequency:PersonDetection": "1800",
}

LICENSE_RESPONSE: dict[str, Any] = {
    "AccountLicense": [],
    "DevicesLicense": {
        DEVICE_ID: [
            {
                "ServiceName": "NST Standard",
                "ServicePlanName": "NST Standard Monthly Plan",
                "SubscriptionStatus": "Active",
                "TimeLeftDays": 24,
                "IsAutoRenew": True,
                "LicenseType": "SUBSCRIPTION",
                "NextBillingAttempt": "2026-09-30T12:54:00",
            }
        ]
    },
}

ACTIVITY_RESPONSE: dict[str, Any] = {
    "2026-09-05": {
        "Data": {
            "Barking": [0, 0, 2, 3, 0, 1],
            "DogMoveAbove10Sec": [0, 1, 8, 26, 13, 7],
        },
        "DataTimeList": [
            "2026-09-05 00:00:00",
            "2026-09-05 01:00:00",
            "2026-09-05 02:00:00",
            "2026-09-05 03:00:00",
            "2026-09-05 04:00:00",
            "2026-09-05 05:00:00",
        ],
        "Error": None,
    }
}

# What FurboClient.get_activity_report returns for ACTIVITY_RESPONSE.
ACTIVITY_TOTALS: dict[str, dict[str, int]] = {
    "2026-09-05": {"Barking": 6, "DogMoveAbove10Sec": 55}
}

NOTABLE_EVENTS: list[dict[str, Any]] = [
    {
        "ActionCaption": "walking",
        "Caption": "walking around the door",
        "LocationCaption": "around the door",
        "DeviceId": DEVICE_ID,
        "Duration": 1350000,
        "Id": 1788515097686840,
        "Labels": [],
        "LocalTime": "2026-09-05 10:44:57",
        "Thumbnail": "https://example.invalid/thumb.jpg?sig=placeholder",
    },
    {
        "ActionCaption": "standing",
        "Caption": "standing in the doorway",
        "LocationCaption": "in the doorway",
        "DeviceId": DEVICE_ID,
        "Duration": 1350000,
        "Id": 1788515097686999,
        "Labels": [],
        "LocalTime": "2026-09-05 11:31:14",
        "Thumbnail": "https://example.invalid/thumb2.jpg?sig=placeholder",
    },
]

DAILY_SUMMARY = "Your furbabies had a quiet day with a little wandering by the door."
