"""Tests for the pure decoding/lifecycle logic in furbo_p2p.

These do not load the TUTK library: they exercise the module-level helpers and
the V3 reply decoder (which only touches ``self.state``), so the protocol
parsing that turns camera replies into state is covered without hardware.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

import furbo_p2p as fp


def _decoder(proto: str = "v3") -> fp.FurboP2P:
    """A FurboP2P with just the attributes decode() needs (no SDK loaded)."""
    obj = fp.FurboP2P.__new__(fp.FurboP2P)
    obj.proto = proto
    obj.state = {}
    return obj


# --- module helpers ---------------------------------------------------------


def test_proto_by_product() -> None:
    assert fp.PROTO_BY_PRODUCT["FB0030"] == "v3"
    assert fp.PROTO_BY_PRODUCT["FB001"] == "v2"


def test_err_formats_known_and_unknown() -> None:
    assert fp.err(0).startswith("0")
    assert "20012" in fp.err(fp.AV_ER_DATA_NOREADY)


def test_cloud_fail_maps_token_error() -> None:
    token = fp._cloud_fail(fp.FurboError("boom", 12002))
    other = fp._cloud_fail(fp.FurboError("boom", 55))
    assert isinstance(token, SystemExit) and "login" in str(token)
    assert isinstance(other, SystemExit) and "failed" in str(other)


def test_ascii_fields_splits_padded_versions() -> None:
    data = b"1.2.3\x00\x00\x004.5.6\x00\x00\x00"
    assert fp._ascii_fields(data, 2) == ["1.2.3", "4.5.6"]
    assert fp._ascii_fields(b"", 3) == ["", "", ""]


def test_json_helper_parses_or_passes_through() -> None:
    assert fp._json(b'{"enable": true}\x00') == {"enable": True}
    assert fp._json(b"not json\x00") == "not json"


def test_decode_hex_ascii_recovers_ssid() -> None:
    # "Garden" -> ascii-hex "47617264656e" -> the struct hex-encodes that again.
    inner = b"Garden".hex()  # "47617264656e"
    doubled = inner.encode().hex()  # hex of the ascii of the hex
    assert fp._decode_hex_ascii(doubled.encode()) == "Garden"


# --- V3 reply decoding ------------------------------------------------------


def _reply(get_key: str) -> int:
    """The reply opcode for a GET command is the request opcode + 1."""
    return fp.CMD3[get_key] + 1


def test_decode_volume_and_mute() -> None:
    d = _decoder()
    d.decode(_reply("GET_VOLUME"), bytes([0, 55, 1]))
    assert d.state["volume"] == 55
    assert d.state["muted"] is True


def test_decode_camera_on_and_night_mode() -> None:
    d = _decoder()
    d.decode(_reply("GET_CAMERA_ON"), bytes([0, 1]))
    d.decode(_reply("GET_NIGHT_VISION"), bytes([0, 2]))
    assert d.state["camera_on"] is True
    assert d.state["night_mode"] == "off"


def test_decode_bark_tracking_treat() -> None:
    d = _decoder()
    d.decode(_reply("GET_BARKING"), bytes([0, 3]))
    d.decode(_reply("GET_AUTO_TRACKING"), bytes([0, 1, 1, 0]))
    d.decode(_reply("GET_TOSS_PROFILE"), bytes([0, 1]))
    assert d.state["bark_sensitivity"] == "high"
    assert d.state["auto_tracking"] == {"cruise": True, "live": True}
    assert d.state["treat_size"] == "small"


def test_decode_zoom_voice_snackcall() -> None:
    d = _decoder()
    d.decode(_reply("GET_AUTO_ZOOM"), bytes([0, 0, 1]))
    d.decode(_reply("GET_VOICE_CONTROL"), bytes([0, 1]))
    d.decode(_reply("GET_SNACKCALL"), bytes([0, 2]))
    assert d.state["auto_zoom"] == {"cruise": False, "live": True}
    assert d.state["voice_control"] is True
    assert d.state["snack_call"] == "mute"


def test_decode_device_info_struct() -> None:
    d = _decoder()
    # [0]=status, [1]=CAM on, [5]=VOL, then four nul-padded ascii version fields.
    head = bytes([0, 1, 0, 0, 0, 65]) + bytes(3)
    versions = b"1.0\x00" + b"2.0\x00" + b"libA\x00" + b"libB\x00"
    d.decode(_reply("GET_DEVICE_INFO"), head + versions)
    assert d.state["camera_on"] is True
    assert d.state["volume"] == 65
    assert d.state["firmware"]["current"] == "1.0"


def test_decode_auto_calm_and_upgrade() -> None:
    d = _decoder()
    d.decode(_reply("GET_AUTO_CALM"), b'\x00{"enable": 1}')
    d.decode(
        _reply("GET_UPGRADE_INFO"),
        bytes([0, 0]) + b"9.9\x00" + b"1.0\x00" + b"nl\x00\x00" + b"ol\x00\x00",
    )
    assert d.state["auto_calm"] == {"enable": 1}
    assert d.state["firmware"]["new"] == "9.9"


def test_decode_schedule_json() -> None:
    d = _decoder()
    d.decode(_reply("GET_CAMERA_SCHEDULE"), b'\x00{"enable": true}')
    assert d.state["schedule"] == {"enable": True}


def test_decode_rejection_is_recorded() -> None:
    d = _decoder()
    opcode = fp.CMD3["SET_VOLUME"]
    payload = bytes([5, opcode & 0xFF, (opcode >> 8) & 0xFF])
    d.decode(fp.CMD_REJECTED, payload)
    # The reject payload carries only the low 16 bits of the request opcode.
    assert d.state["rejected"] == [{"opcode": opcode & 0xFFFF, "status": 5}]


def test_decode_ignores_short_payload() -> None:
    d = _decoder()
    # A truncated GET_VOLUME reply (status only) leaves state untouched.
    d.decode(_reply("GET_VOLUME"), bytes([0]))
    assert "volume" not in d.state


@pytest.mark.parametrize("proto", ["v2", "v3"])
def test_decode_dispatches_by_proto(proto: str, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _decoder(proto)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(d, "decode_v3", lambda o, x: seen.setdefault("v3", True))
    monkeypatch.setattr(d, "decode_v2", lambda o, x: seen.setdefault("v2", True))
    d.decode(1, b"\x00")
    assert seen == {proto: True}


# --- waiting for replies ----------------------------------------------------


def _waiter(replies: list[tuple[int, bytes] | None]) -> fp.FurboP2P:
    """A FurboP2P whose poll() serves a canned sequence, then None forever."""
    obj = _decoder()
    queue = list(replies)

    def poll(_timeout_ms: int = 10) -> tuple[int, bytes] | None:
        return queue.pop(0) if queue else None

    obj.poll = poll  # type: ignore[method-assign]
    return obj


def test_await_reply_returns_as_soon_as_the_reply_lands() -> None:
    """The wait ends on the reply, not on the timeout."""
    op = fp.CMD3["GET_VOLUME"]
    p2p = _waiter([None, (_reply("GET_VOLUME"), bytes([0, 40]))])
    start = time.monotonic()
    assert p2p.await_reply(op, 5.0) is True
    assert time.monotonic() - start < 1.0


def test_await_reply_accepts_a_refusal() -> None:
    """A refusal answers the command; the sweep should not sit out the ceiling."""
    op = fp.CMD3["GET_VOLUME"]
    payload = bytes([5, op & 0xFF, (op >> 8) & 0xFF])
    p2p = _waiter([(fp.CMD_REJECTED, payload)])
    assert p2p.await_reply(op, 5.0) is True


def test_await_reply_ignores_another_commands_reply() -> None:
    """Someone else's reply does not end this wait, and does not confuse it."""
    op = fp.CMD3["GET_VOLUME"]
    other = bytes([9, 0xFF, 0xFF])  # a refusal for a different opcode
    p2p = _waiter([(_reply("GET_BARKING"), bytes([0, 2])), (fp.CMD_REJECTED, other)])
    assert p2p.await_reply(op, 0.3) is False


def test_await_reply_gives_up_at_the_ceiling() -> None:
    """A command the camera never answers costs the ceiling and no more."""
    p2p = _waiter([])
    start = time.monotonic()
    assert p2p.await_reply(fp.CMD3["GET_VOLUME"], 0.2) is False
    assert 0.2 <= time.monotonic() - start < 1.0


def test_query_state_v3_waits_per_reply_not_per_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each get is awaited individually; no fixed sleep between commands."""
    p2p = _decoder()
    sent: list[int] = []
    awaited: list[tuple[int, float]] = []
    drains: list[float] = []
    monkeypatch.setattr(p2p, "send", lambda op, data=b"": sent.append(op))
    monkeypatch.setattr(p2p, "await_reply", lambda op, t: (awaited.append((op, t)), True)[1])
    monkeypatch.setattr(p2p, "drain", lambda s: drains.append(s))
    p2p.query_state(wait=3.0)
    assert sent == [op for op, _ in awaited]
    assert len(sent) == 13
    assert {t for _, t in awaited} == {fp.REPLY_TIMEOUT}
    # One short catch-all at the end, not the caller's wait.
    assert drains == [fp.TRAILING_DRAIN]


def test_await_reply_accepts_the_request_opcode_as_the_reply() -> None:
    """Some V3 replies come back on the request opcode, not the request + 1.

    decode() has always looked the name up both ways; matching only the +1
    form made every one of those commands sit out the full ceiling.
    """
    op = fp.CMD3["GET_TOSS_PROFILE"]
    p2p = _waiter([(op, bytes([0, 0]))])
    assert p2p.await_reply(op, 5.0) is True


# --- device identity --------------------------------------------------------


def test_login_reuses_the_stored_device_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A second login keeps the MobileId the first one used.

    Furbo identifies a client by MobileId, so minting a new one per login makes
    every login look like a new phone and earns a verification code every time.
    """
    session = tmp_path / "furbo_session.json"
    session.write_text('{"account_id": "A", "cognito_token": "T", "mobile_id": "keep-me"}')
    monkeypatch.setattr(fp, "SESSION_FILE", session)
    assert fp._stored_mobile_id() == "keep-me"


def test_stored_device_id_absent_or_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """No session, or a corrupt one, means a fresh id rather than a crash."""
    missing = tmp_path / "none.json"
    monkeypatch.setattr(fp, "SESSION_FILE", missing)
    assert fp._stored_mobile_id() is None
    missing.write_text("{not json")
    assert fp._stored_mobile_id() is None


def test_device_id_survives_a_session_reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Clearing the session must not change who the bridge claims to be.

    'reset_session' deletes the session file, so an id kept only in there is
    lost and the next login registers as a new phone, which earns a code.
    """
    session = tmp_path / "furbo_session.json"
    monkeypatch.setattr(fp, "SESSION_FILE", session)
    fp._remember_mobile_id("stable-id")
    session.write_text('{"account_id": "A", "cognito_token": "T"}')
    session.unlink()  # what reset_session does
    assert fp._stored_mobile_id() == "stable-id"


def test_device_id_falls_back_to_an_older_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A bridge that stored the id in the session keeps the id it has."""
    session = tmp_path / "furbo_session.json"
    session.write_text('{"mobile_id": "from-session"}')
    monkeypatch.setattr(fp, "SESSION_FILE", session)
    assert not fp._device_file().exists()
    assert fp._stored_mobile_id() == "from-session"


def test_reply_ceiling_is_no_worse_than_the_wait_it_replaced() -> None:
    """An unmatched reply must not cost more than the old fixed sleep."""
    assert fp.REPLY_TIMEOUT <= 0.4
