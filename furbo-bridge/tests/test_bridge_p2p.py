"""Tests for the pure decoding/lifecycle logic in furbo_p2p.

These do not load the TUTK library: they exercise the module-level helpers and
the V3 reply decoder (which only touches ``self.state``), so the protocol
parsing that turns camera replies into state is covered without hardware.
"""

from __future__ import annotations

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
