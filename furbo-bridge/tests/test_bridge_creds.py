"""Tests for device selection in fetch_p2p_credentials.

The bridge must never silently serve the wrong camera: with more than one
camera it requires a device_id, and it reports the resolved cloud device id so
the integration can verify identity.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import furbo_p2p as fp

DEV1 = {"Id": 111, "DeviceName": "Kitchen", "P2PUuid": "UID1", "ProductId": "FB0030"}
DEV2 = {"Id": 222, "DeviceName": "Hallway", "P2PUuid": "UID2", "ProductId": "FB0030"}

P2P = {"AuthKey": "a", "P2PAccountId": "acc", "P2PAccountKey": "k"}


def _patch(monkeypatch: pytest.MonkeyPatch, devices: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(
        fp, "_load_session", lambda: {"devices": devices, "account_id": "a", "cognito_token": "t"}
    )

    class _Client:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def get_p2p_connection(self, _device_id: str) -> dict[str, str]:
            return dict(P2P)

    monkeypatch.setattr(fp, "FurboClient", _Client)


def test_single_camera_no_device_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [dict(DEV1)])
    creds = asyncio.run(fp.fetch_p2p_credentials(None))
    assert creds["device_id"] == "111"
    assert creds["uid"] == "UID1"


def test_multi_camera_requires_device_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [dict(DEV1), dict(DEV2)])
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(fp.fetch_p2p_credentials(None))
    # The error lists the ids to choose from.
    assert "111" in str(excinfo.value) and "222" in str(excinfo.value)


def test_multi_camera_selects_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [dict(DEV1), dict(DEV2)])
    creds = asyncio.run(fp.fetch_p2p_credentials("222"))
    assert creds["device_id"] == "222"
    assert creds["uid"] == "UID2"


def test_unknown_device_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [dict(DEV1), dict(DEV2)])
    with pytest.raises(SystemExit):
        asyncio.run(fp.fetch_p2p_credentials("333"))
