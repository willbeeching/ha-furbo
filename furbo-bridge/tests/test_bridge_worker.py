"""Tests for P2PWorker: how API settings map to P2P commands.

A fake FurboP2P records the opcodes and payloads the worker sends, so the
command-building logic is exercised without the TUTK library or a camera.
"""

from __future__ import annotations

import argparse
import struct
from typing import Any

import pytest

import furbo_bridge as fb
import furbo_p2p as fp


class FakeP2P:
    """Records send()/drain() calls and answers query_state from a fixture."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.proto = "v3"
        self.mode = "LAN"
        self.state: dict[str, Any] = state or {}
        self.sends: list[tuple[int, bytes]] = []
        self.closed = False
        self.frames: list[bytes] = []
        self.query_calls = 0

    def recv_frame(self, buf: Any, _info: Any) -> tuple[int, int, int]:
        if self.frames:
            data = self.frames.pop(0)
            buf[0 : len(data)] = data
            return len(data), len(data), 0
        # Nothing left: report the session going away so the loop ends.
        return -20015, 0, 0

    def alive(self) -> bool:
        return True

    def send(self, opcode: int, payload: bytes = b"") -> None:
        self.sends.append((opcode, payload))

    def drain(self, _timeout: float) -> None:
        pass

    def query_state(self, wait: float = 0.0) -> dict[str, Any]:
        self.query_calls += 1
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


# --- polling cadence ---------------------------------------------------------


def test_refresh_sweeps_once_then_polls_lightly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full 13-command sweep is rare; the frequent poll is one command."""
    fake = FakeP2P({"camera_on": True})
    worker = _worker_with(monkeypatch, fake)

    worker.refresh()
    assert fake.query_calls == 1, "first refresh reads everything"

    fake.sends.clear()
    worker.refresh()
    assert fake.query_calls == 1, "second refresh must not sweep again"
    assert _opcodes(fake) == [fp.CMD3["GET_CAMERA_ON"]]


def test_full_sweep_returns_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the full interval passes, everything is re-read to catch app changes."""
    fake = FakeP2P({"camera_on": True})
    worker = _worker_with(monkeypatch, fake)
    worker.refresh()
    worker.last_full_refresh -= fb.FULL_REFRESH_INTERVAL + 1
    worker.refresh()
    assert fake.query_calls == 2


# --- video on the shared session ---------------------------------------------


def test_open_stream_starts_video_at_the_requested_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Video starts on the session already held, with no second connection."""
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker.open_stream("720p")
    assert fake.sends == [(fp.IPCAM_START, struct.pack("<i", fp.QUALITY_V3["720p"]))]
    assert worker.streaming is True


def test_second_stream_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """One viewer at a time: the camera serves a single video channel."""
    worker = _worker_with(monkeypatch, FakeP2P())
    worker.open_stream("1080p")
    with pytest.raises(fb.StreamBusy):
        worker.open_stream("1080p")


def test_iter_frames_yields_then_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frames come through, and an SDK error ends the stream quietly."""
    fake = FakeP2P()
    fake.frames = [b"aaa", b"bbbb"]
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    assert list(worker.iter_frames()) == [b"aaa", b"bbbb"]


def test_close_stream_stops_video_but_keeps_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping video must not drop the session the controls are using."""
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    fake.sends.clear()
    worker.close_stream()
    assert fake.sends == [(fp.IPCAM_STOP, struct.pack("<i", 0))]
    assert worker.streaming is False
    assert fake.closed is False


def test_dropping_the_session_stops_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reconnect must not leave a reader on a channel that has gone away."""
    fake = FakeP2P()
    fake.frames = [b"a"] * 100
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    worker._drop()
    assert worker.streaming is False
    assert list(worker.iter_frames()) == []


def test_session_mode_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The path is visible in status, so a relayed session can be spotted."""
    fake = FakeP2P()
    fake.mode = "relay"
    worker = _worker_with(monkeypatch, fake)
    assert worker.session_mode is None
    worker._p2p = fake
    assert worker.session_mode == "relay"


def test_reconnect_waits_for_the_reader_to_leave_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the channel while the reader is inside it would crash the SDK."""
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    worker._reader_active.set()  # pretend the reader is mid-call

    monkeypatch.setattr(fb, "READER_EXIT_TIMEOUT", 0.1)
    worker._drop()
    # It gives up rather than hanging, but only after waiting.
    assert fake.closed is True

    # With the reader clear, the session closes without the wait.
    fake2 = FakeP2P()
    worker._p2p = fake2
    worker._reader_active.clear()
    worker._drop()
    assert fake2.closed is True


def test_apply_does_not_read_everything_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write updates the cache from what it wrote, not from a full sweep.

    The full sweep is one round trip per setting, which on a relayed session
    takes longer than Home Assistant waits for the write.
    """
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker.state = {"volume": 10, "camera_on": False}
    state = worker.apply({"volume": 42})
    assert fake.query_calls == 0
    # The written value lands, and the rest of the cache is left alone.
    assert state == {"volume": 42, "camera_on": False}
    assert worker.updated_at > 0


def test_apply_drops_a_refused_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A setting the camera refuses is not reported as applied."""
    fake = FakeP2P()
    worker = _worker_with(monkeypatch, fake)
    worker.state = {"volume": 10, "night_mode": "off"}

    def send(opcode: int, payload: bytes = b"") -> None:
        fake.sends.append((opcode, payload))
        if opcode == fp.CMD3["SET_VOLUME"]:
            fake.state.setdefault("rejected", []).append({"opcode": opcode, "status": 5})

    monkeypatch.setattr(fake, "send", send)
    state = worker.apply({"volume": 42, "night_mode": "on"})
    assert state == {"volume": 10, "night_mode": "on"}
    # The rejection is consumed, so it cannot leak into the next write.
    assert "rejected" not in fake.state


def test_apply_skips_settings_the_camera_cannot_take(monkeypatch: pytest.MonkeyPatch) -> None:
    """A V3-only setting sent to a V2 camera is not cached as applied."""
    fake = FakeP2P()
    fake.proto = "v2"
    worker = _worker_with(monkeypatch, fake)
    state = worker.apply({"auto_zoom": True, "volume": 20})
    assert state == {"volume": 20}


def test_apply_caches_the_json_toggles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The schedule and calm toggles report back through the same path."""
    fake = FakeP2P(
        state={
            "schedule": {"enable": False, "enabledDay": [1], "schedule": [[0, 1]]},
            "auto_calm": {"enable": 0, "startAudio": 0, "treatToss": 0},
        }
    )
    worker = _worker_with(monkeypatch, fake)
    state = worker.apply({"schedule_enabled": True, "calm_enabled": True})
    assert state == {"schedule_enabled": True, "calm_enabled": True}


def test_apply_ignores_a_toggle_with_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no schedule config to amend, nothing is written and nothing cached."""
    fake = FakeP2P(state={"schedule": "not-a-config", "auto_calm": "not-a-config"})
    worker = _worker_with(monkeypatch, fake)
    assert worker.apply({"schedule_enabled": True}) == {}


# --- cloud login recovery ---------------------------------------------------


def _unavailable_worker(monkeypatch: pytest.MonkeyPatch, message: str) -> fb.P2PWorker:
    """A worker whose session cannot be built, for the given reason."""
    args = argparse.Namespace(
        device=None, lib=None, region="us", tutk_log=False, tcp_relay=False, timeout=10
    )
    worker = fb.P2PWorker(args)

    def boom(_device: Any) -> dict[str, Any]:
        raise SystemExit(message)

    monkeypatch.setattr(fb.fp, "fetch_p2p_credentials", boom)
    monkeypatch.setattr(fb.asyncio, "run", lambda coro: coro)
    return worker


def test_login_failure_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused login is not retried on the poll interval."""
    worker = _unavailable_worker(monkeypatch, "Cloud rejected the token. Run: login")
    with pytest.raises(fb.BridgeUnavailable):
        worker.refresh()
    assert worker.needs_login is True
    first_deadline = worker._login_retry_at
    assert first_deadline > 0

    # The next poll is refused locally: the cloud is not asked again.
    calls: list[int] = []
    monkeypatch.setattr(fb.fp, "fetch_p2p_credentials", lambda _d: calls.append(1) or {})
    with pytest.raises(fb.BridgeUnavailable):
        worker.refresh()
    assert calls == []
    # And the wait doubles rather than staying put.
    assert worker._login_backoff > fb.LOGIN_BACKOFF_START


def test_other_failures_do_not_set_needs_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection problem is not a login problem, and keeps retrying."""
    worker = _unavailable_worker(monkeypatch, "IOTC_Connect_ByUIDEx failed: -13")
    with pytest.raises(fb.BridgeUnavailable):
        worker.refresh()
    assert worker.needs_login is False
    assert worker._login_retry_at == 0.0


def test_reader_gives_up_when_no_first_frame_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A camera that accepts the start command and sends nothing is a failure.

    Spinning on 'no data' holds the single stream slot, so every retry gets a
    busy response and it reads as a hang rather than a refusal.
    """
    monkeypatch.setattr(fb, "FIRST_FRAME_TIMEOUT", 0.05)
    fake = FakeP2P()
    # Always "no data": the camera never sends a frame.
    monkeypatch.setattr(fake, "recv_frame", lambda _buf, _info: (fb.fp.AV_ER_DATA_NOREADY, 0, 0))
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    assert list(worker.iter_frames()) == []
    # The slot is released, so the next viewer is not turned away.
    worker.close_stream()
    assert worker.streaming is False


def test_reader_keeps_waiting_once_frames_are_flowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The give-up only applies before the first frame, not to a gap after it."""
    monkeypatch.setattr(fb, "FIRST_FRAME_TIMEOUT", 0.05)
    fake = FakeP2P()
    replies = [(4, 4, 0), (fb.fp.AV_ER_DATA_NOREADY, 0, 0), (4, 4, 0), (-20015, 0, 0)]

    def recv(buf: Any, _info: Any) -> tuple[int, int, int]:
        ret = replies.pop(0)
        if ret[0] > 0:
            buf[0:4] = b"abcd"
        return ret

    monkeypatch.setattr(fake, "recv_frame", recv)
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    # Both frames arrive; the pause in the middle does not end the stream.
    assert list(worker.iter_frames()) == [b"abcd", b"abcd"]


def test_reader_gives_up_on_frames_that_never_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frames that all arrive lost or incomplete are a failure too.

    beta.8 only gave up on 'no data', so a camera sending nothing but
    incomplete frames spun in that branch forever, silently.
    """
    monkeypatch.setattr(fb, "FIRST_FRAME_TIMEOUT", 0.05)
    fake = FakeP2P()
    monkeypatch.setattr(
        fake, "recv_frame", lambda _buf, _info: (fb.fp.AV_ER_INCOMPLETE_FRAME, 0, 4_000_000)
    )
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    assert list(worker.iter_frames()) == []


def test_reader_reports_a_lost_frame_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream of lost frames ends rather than running until the viewer leaves."""
    monkeypatch.setattr(fb, "FIRST_FRAME_TIMEOUT", 0.05)
    fake = FakeP2P()
    monkeypatch.setattr(
        fake, "recv_frame", lambda _buf, _info: (fb.fp.AV_ER_LOSED_THIS_FRAME, 0, 0)
    )
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    assert list(worker.iter_frames()) == []


def test_frame_size_comes_from_the_sdk_not_the_return_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build that returns 0 on success still yields the whole frame.

    Slicing by the return value gave empty frames: the reader counted them as
    real, so the give-up never fired and the viewer got a stream carrying no
    bytes with nothing logged.
    """
    fake = FakeP2P()
    replies = [(0, 4, 0), (-20015, 0, 0)]

    def recv(buf: Any, _info: Any) -> tuple[int, int, int]:
        entry = replies.pop(0)
        if entry[1]:
            buf[0:4] = b"abcd"
        return entry

    monkeypatch.setattr(fake, "recv_frame", recv)
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    assert list(worker.iter_frames()) == [b"abcd"]


def test_a_successful_read_of_nothing_is_not_a_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-length reads must not suppress the give-up."""
    monkeypatch.setattr(fb, "FIRST_FRAME_TIMEOUT", 0.05)
    fake = FakeP2P()
    monkeypatch.setattr(fake, "recv_frame", lambda _buf, _info: (0, 0, 0))
    worker = _worker_with(monkeypatch, fake)
    worker._p2p = fake
    worker.open_stream("1080p")
    assert list(worker.iter_frames()) == []
