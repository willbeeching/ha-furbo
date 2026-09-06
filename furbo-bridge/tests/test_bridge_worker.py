"""Tests for P2PWorker: how API settings map to P2P commands.

A fake FurboP2P records the opcodes and payloads the worker sends, so the
command-building logic is exercised without the TUTK library or a camera.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

import furbo_bridge as fb
import furbo_p2p as fp


class FakeP2P:
    """Records send()/drain() calls and answers query_state from a fixture."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.proto = "v3"
        self.state: dict[str, Any] = state or {}
        self.sends: list[tuple[int, bytes]] = []
        self.closed = False

    def alive(self) -> bool:
        return True

    def send(self, opcode: int, payload: bytes = b"") -> None:
        self.sends.append((opcode, payload))

    def drain(self, _timeout: float) -> None:
        pass

    def query_state(self, wait: float = 0.0) -> dict[str, Any]:
        return dict(self.state)

    def close(self) -> None:
        self.closed = True


def _worker_with(monkeypatch: pytest.MonkeyPatch, fake: FakeP2P) -> fb.P2PWorker:
    """Build a worker whose session is the given fake, bypassing the cloud."""
    args = argparse.Namespace(
        device=None,
        lib=None,
        region="us",
        tutk_log=False,
        tcp_relay=False,
        timeout=10,
    )
    worker = fb.P2PWorker(args)
    monkeypatch.setattr(worker, "_ensure", lambda: fake)
    return worker


def _opcodes(fake: FakeP2P) -> list[int]:
    return [op for op, _ in fake.sends]


def test_apply_volume_and_night_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker.apply({"volume": 42, "night_mode": "on"})
    sends = dict(fake.sends)
    assert sends[fp.CMD3["SET_VOLUME"]] == bytes([42, 0, 0, 0])
    # night_mode "on" is code 1 in NIGHT_MODES.
    assert sends[fp.CMD3["SET_NIGHT_VISION"]] == bytes([1, 0, 0, 0])


def test_apply_camera_on_and_bark(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker.apply({"camera_on": True, "bark_sensitivity": "high"})
    sends = dict(fake.sends)
    assert sends[fp.CMD3["SET_CAMERA_ON"]] == bytes([1, 0, 0, 0])
    assert sends[fp.CMD3["SET_BARKING"]] == bytes([fp.BARK_V3["high"], 0, 0, 0])


def test_apply_tracking_zoom_voice_treat(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker.apply(
        {
            "auto_tracking": True,
            "auto_zoom": False,
            "voice_control": True,
            "treat_size": "small",
        }
    )
    ops = _opcodes(fake)
    assert fp.CMD3["SET_AUTO_TRACKING"] in ops
    assert fp.CMD3["SET_AUTO_ZOOM"] in ops
    assert fp.CMD3["SET_VOICE_CONTROL"] in ops
    assert dict(fake.sends)[fp.CMD3["SET_TOSS_PROFILE"]] == bytes([fp.TREAT_SIZE["small"], 0, 0, 0])


def test_apply_quality_is_not_a_p2p_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    qfile = tmp_path / "quality"
    monkeypatch.setattr(fb, "QUALITY_FILE", qfile)
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker.apply({"quality": "720p"})
    # Quality is written to the file, no opcode is sent for it.
    assert qfile.read_text().strip() == "720p"
    assert fake.sends == []


def test_apply_schedule_toggle_reuses_existing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeP2P(
        state={
            "schedule": {"enable": False, "enabledDay": [1], "schedule": [[0, 1]]},
            "auto_calm": {"enable": 0, "startAudio": 0, "treatToss": 0},
        }
    )
    worker = _worker_with(monkeypatch, fake)
    worker.apply({"schedule_enabled": True})
    # The SET_CAMERA_SCHEDULE payload keeps the days/schedule the camera holds.
    payloads = dict(fake.sends)
    assert fp.CMD3["SET_CAMERA_SCHEDULE"] in payloads
    assert b'"enable": true' in payloads[fp.CMD3["SET_CAMERA_SCHEDULE"]]


def test_pan_requires_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeP2P()
    fake.proto = "v2"
    worker = _worker_with(monkeypatch, fake)
    with pytest.raises(fb.BridgeUnavailable):
        worker.pan("left", 45)


def test_pan_toss_treat_sound(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker.pan("right", 30)
    worker.toss()
    worker.treat_sound()
    ops = _opcodes(fake)
    assert fp.CMD3["PAN"] in ops
    assert fp.CMD3["TOSS"] in ops
    assert fp.CMD3["PLAY_TREAT_SOUND"] in ops


def test_refresh_normalises_state(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeP2P(state={"camera_on": 1, "volume": 30})
    worker = _worker_with(monkeypatch, fake)
    state = worker.refresh()
    assert state["camera_on"] is True
    assert state["volume"] == 30
    assert worker.updated_at is not None


def test_apply_calm_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeP2P(
        state={
            "schedule": {"enable": False, "enabledDay": [1], "schedule": [[0, 1]]},
            "auto_calm": {"enable": 0, "startAudio": 1, "treatToss": 1},
        }
    )
    worker = _worker_with(monkeypatch, fake)
    worker.apply({"calm_enabled": True})
    payloads = dict(fake.sends)
    assert fp.CMD3["SET_AUTO_CALM"] in payloads
    assert b'"enable": 1' in payloads[fp.CMD3["SET_AUTO_CALM"]]


def test_ensure_failure_becomes_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cloud/connect failure surfaces as BridgeUnavailable, not a raw exit."""

    async def boom(_device: Any) -> dict[str, Any]:
        raise SystemExit("login expired")

    monkeypatch.setattr(fp, "fetch_p2p_credentials", boom)
    args = argparse.Namespace(
        device=None, lib=None, region="us", tutk_log=False, tcp_relay=False, timeout=10
    )
    worker = fb.P2PWorker(args)
    with pytest.raises(fb.BridgeUnavailable):
        worker.refresh()
    assert worker.last_error == "login expired"


def test_add_arguments_defaults() -> None:
    parser = argparse.ArgumentParser()
    fb.add_arguments(parser)
    ns = parser.parse_args([])
    assert ns.host == "0.0.0.0"
    assert ns.port == fb.DEFAULT_PORT
    assert ns.token is None


def test_connected_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeP2P()
    args = argparse.Namespace(
        device=None, lib=None, region="us", tutk_log=False, tcp_relay=False, timeout=10
    )
    worker = fb.P2PWorker(args)
    assert worker.connected is False
    worker._p2p = fake  # type: ignore[attr-defined]
    assert worker.connected is True
    worker.close()
    assert fake.closed is True
    assert worker.connected is False
