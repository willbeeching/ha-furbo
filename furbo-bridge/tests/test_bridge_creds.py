"""Tests for device selection in fetch_p2p_credentials.

The bridge must never silently serve the wrong camera: with more than one
camera it requires a device_id, and it reports the resolved cloud device id so
the integration can verify identity.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from furbo_cloud import FurboConnectionError, FurboLoginError
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


class _Refused(fp.FurboError):
    """The cloud's 'this token is no good' answer."""

    def __init__(self) -> None:
        super().__init__("token invalid")
        self.code = 12002


def _cloud(monkeypatch: pytest.MonkeyPatch, account_info: Any) -> list[str]:
    """Patch the cloud client; return the tokens it was constructed with."""
    seen: list[str] = []

    class _Client:
        def __init__(self, _http: Any, _account: str = "", token: str = "") -> None:
            seen.append(token)

        async def get_account_info(self) -> dict[str, Any]:
            if isinstance(account_info, Exception):
                raise account_info
            return account_info

    monkeypatch.setattr(fp, "FurboClient", _Client)
    return seen


def test_current_credentials_checks_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token the cloud still accepts is handed over unchanged."""
    monkeypatch.setattr(fp, "_load_session", lambda: {"account_id": "A1", "cognito_token": "T1"})
    seen = _cloud(monkeypatch, {"Timezone": "Europe/London"})
    monkeypatch.setattr(fp, "silent_relogin", None)  # must not be needed

    assert asyncio.run(fp.current_credentials()) == {"account_id": "A1", "cognito_token": "T1"}
    assert seen == ["T1"]


def test_current_credentials_renews_a_dead_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stored token the cloud refuses is replaced before it is handed over.

    Nothing else renews it on a schedule, so a camera that stays connected can
    leave the session file holding a token as dead as the caller's own.
    """
    monkeypatch.setattr(fp, "_load_session", lambda: {"account_id": "A1", "cognito_token": "OLD"})
    _cloud(monkeypatch, _Refused())

    async def _relogin(stale: str | None = None) -> dict[str, Any]:
        assert stale == "OLD"
        return {"account_id": "A1", "cognito_token": "NEW"}

    monkeypatch.setattr(fp, "silent_relogin", _relogin)
    assert asyncio.run(fp.current_credentials()) == {"account_id": "A1", "cognito_token": "NEW"}


def test_current_credentials_without_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before the first login there is nothing to hand over."""
    monkeypatch.setattr(fp, "_load_session", lambda: {})
    with pytest.raises(SystemExit):
        asyncio.run(fp.current_credentials())


def test_current_credentials_when_the_cloud_wants_a_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login that needs an emailed code says how to supply one."""
    monkeypatch.setattr(fp, "_load_session", lambda: {"account_id": "A1", "cognito_token": "OLD"})
    _cloud(monkeypatch, _Refused())

    async def _relogin(stale: str | None = None) -> dict[str, Any]:
        raise fp.MfaRequired("the cloud wants a verification code")

    monkeypatch.setattr(fp, "silent_relogin", _relogin)
    with pytest.raises(fp.LoginRequired) as excinfo:
        asyncio.run(fp.current_credentials())
    assert "mfa_code" in str(excinfo.value)


def test_current_credentials_passes_other_errors_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cloud outage is not treated as a dead token."""
    monkeypatch.setattr(fp, "_load_session", lambda: {"account_id": "A1", "cognito_token": "T1"})
    _cloud(monkeypatch, fp.FurboError("service unavailable"))
    with pytest.raises(SystemExit):
        asyncio.run(fp.current_credentials())


def test_relogin_uses_a_token_someone_else_already_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Two callers noticing the same dead token cause one login, not two.

    Each camera reconnects in its own thread and the HTTP API in the event
    loop, so they see a dead token at the same moment; a burst of logins is
    what the cloud's rate limiter answers with a lockout.
    """
    session = tmp_path / "furbo_session.json"
    session.write_text(json.dumps({"account_id": "A1", "cognito_token": "NEW"}))
    monkeypatch.setattr(fp, "SESSION_FILE", session)

    async def _fail() -> dict[str, Any]:
        raise AssertionError("logged in again when the session was already fresh")

    monkeypatch.setattr(fp, "_login_again", _fail)
    assert asyncio.run(fp.silent_relogin("OLD"))["cognito_token"] == "NEW"


def test_relogin_happens_when_the_token_is_still_the_dead_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Nobody else got there first, so this caller does log in."""
    session = tmp_path / "furbo_session.json"
    session.write_text(json.dumps({"account_id": "A1", "cognito_token": "OLD"}))
    monkeypatch.setattr(fp, "SESSION_FILE", session)

    async def _login() -> dict[str, Any]:
        return {"account_id": "A1", "cognito_token": "NEW"}

    monkeypatch.setattr(fp, "_login_again", _login)
    assert asyncio.run(fp.silent_relogin("OLD"))["cognito_token"] == "NEW"


def test_relogin_gives_up_rather_than_waiting_forever(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A login stuck in front of this one is answered with 'come back later'.

    The HTTP API waits on the same lock as the cameras, and a caller held
    there indefinitely is a request that never answers.
    """
    session = tmp_path / "furbo_session.json"
    session.write_text(json.dumps({"account_id": "A1", "cognito_token": "OLD"}))
    monkeypatch.setattr(fp, "SESSION_FILE", session)
    monkeypatch.setattr(fp, "_LOGIN_WAIT", 0.05)

    fp._LOGIN_LOCK.acquire()
    try:
        with pytest.raises(SystemExit) as excinfo:
            asyncio.run(fp.silent_relogin("OLD"))
    finally:
        fp._LOGIN_LOCK.release()
    assert "already running" in str(excinfo.value)
    # And the lock is free again for the next caller.
    assert fp._LOGIN_LOCK.acquire(timeout=0.1)
    fp._LOGIN_LOCK.release()


def test_a_login_that_cannot_reach_the_cloud_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network failure while logging in is an outage, not a bad password.

    It used to escape as a 500, which a client reads as a definite refusal and
    answers by asking someone to sign in, for something that would have
    worked a minute later.
    """
    monkeypatch.setattr(fp, "_load_session", lambda: {"account_id": "A1", "cognito_token": "OLD"})
    _cloud(monkeypatch, _Refused())

    async def _relogin(stale: str | None = None) -> dict[str, Any]:
        raise FurboConnectionError("cannot reach the cloud")

    monkeypatch.setattr(fp, "silent_relogin", _relogin)
    with pytest.raises(SystemExit):
        asyncio.run(fp.current_credentials())


def test_credentials_the_cloud_refuses_need_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong password is not something to keep retrying quietly."""
    monkeypatch.setattr(fp, "_load_session", lambda: {"account_id": "A1", "cognito_token": "OLD"})
    _cloud(monkeypatch, _Refused())

    async def _relogin(stale: str | None = None) -> dict[str, Any]:
        raise FurboLoginError("wrong email or password")

    monkeypatch.setattr(fp, "silent_relogin", _relogin)
    with pytest.raises(fp.LoginRequired) as excinfo:
        asyncio.run(fp.current_credentials())
    assert "email and password" in str(excinfo.value)
