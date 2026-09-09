"""Unit tests for the bridge's pure helpers (no network, no aiohttp server)."""

from __future__ import annotations

from pathlib import Path

import pytest

import furbo_bridge as fb


def test_normalise_state_maps_nested_shapes() -> None:
    raw = {
        "camera_on": 1,
        "volume": 250,  # clamped to 100
        "muted": 0,
        "night_mode": "auto",
        "bark_sensitivity": "high",
        "auto_tracking": {"live": 1},
        "auto_zoom": {"live": 0},
        "firmware": {"current": "1.2.3"},
        "voice_control": 1,
        "treat_size": "small",
        "snack_call": "mute",
        "schedule": {"enable": True},
        "auto_calm": {"enable": 1},
    }
    out = fb.normalise_state(raw)
    assert out["camera_on"] is True
    assert out["volume"] == 100
    assert out["muted"] is False
    assert out["night_mode"] == "auto"
    assert out["bark_sensitivity"] == "high"
    assert out["auto_tracking"] is True
    assert out["auto_zoom"] is False
    assert out["firmware"] == "1.2.3"
    assert out["voice_control"] is True
    assert out["treat_size"] == "small"
    assert out["snack_call"] == "mute"
    assert out["schedule_enabled"] is True
    assert out["calm_enabled"] is True


def test_normalise_state_ignores_unknown_and_missing() -> None:
    assert fb.normalise_state({}) == {}
    # An out-of-range enum value is dropped, not passed through.
    assert "night_mode" not in fb.normalise_state({"night_mode": "purple"})
    assert "volume" not in fb.normalise_state({"volume": "loud"})


def test_parse_settings_accepts_known_keys() -> None:
    body = {
        "camera_on": True,
        "volume": 40,
        "night_mode": "off",
        "bark_sensitivity": "low",
        "treat_size": "large",
        "quality": "720p",
    }
    assert fb._parse_settings(body) == body


@pytest.mark.parametrize(
    "body",
    [
        {},  # nothing to do
        {"volume": 101},  # out of range
        {"volume": True},  # bool is not an int here
        {"night_mode": "dim"},  # bad enum
        {"bark_sensitivity": "max"},
        {"treat_size": "medium"},
        {"quality": "4k"},
        {"nonsense": 1},  # unknown key
        "not-a-dict",
    ],
)
def test_parse_settings_rejects_bad_bodies(body: object) -> None:
    with pytest.raises(ValueError):
        fb._parse_settings(body)


def test_quality_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    qfile = tmp_path / "quality"
    monkeypatch.setattr(fb, "QUALITY_FILE", qfile)
    # Unset / missing file falls back to 1080p.
    assert fb.read_quality("cam1") == "1080p"
    fb.write_quality("cam1", "360p")
    assert fb.read_quality("cam1") == "360p"
    # Each camera keeps its own choice.
    fb.write_quality("cam2", "720p")
    assert fb.read_quality("cam1") == "360p"
    assert fb.read_quality("cam2") == "720p"
    # An unexpected value on disk falls back to the default.
    (tmp_path / "quality.cam1").write_text("garbage")
    assert fb.read_quality("cam1") == "1080p"


def test_quality_falls_back_to_the_single_camera_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bridge that served one camera keeps the quality its user chose."""
    qfile = tmp_path / "quality"
    monkeypatch.setattr(fb, "QUALITY_FILE", qfile)
    qfile.write_text("720p\n")
    assert fb.read_quality("cam1") == "720p"
    # Once that camera has its own choice, the shared file is no longer used.
    fb.write_quality("cam1", "360p")
    assert fb.read_quality("cam1") == "360p"


# --- go2rtc config -----------------------------------------------------------

TEMPLATE = "rtsp:\n  listen: ':8554'\n\nstreams:\n"


def test_go2rtc_config_has_a_stream_per_camera() -> None:
    """Each camera gets its own video and talkback stream, told which it is."""
    config = fb.render_go2rtc_config(TEMPLATE, ["111", "222"])
    assert "  furbo_111:" in config
    assert "  furbo_222:" in config
    assert "/app/stream.sh {output} 222#" in config
    # The template's own settings survive.
    assert "listen: ':8554'" in config


def test_go2rtc_config_keeps_the_plain_stream_for_the_primary() -> None:
    """A stream URL saved before the account had two cameras still resolves."""
    config = fb.render_go2rtc_config(TEMPLATE, ["111", "222"])
    assert "  furbo:" in config
    # It points at the primary, not the second camera.
    primary = config.split("  furbo:")[1]
    assert "/app/stream.sh {output} 111#" in primary.split("  furbo_222:")[0]


def test_go2rtc_config_does_not_wire_up_talkback() -> None:
    """Talkback opened a P2P session of its own, competing with the bridge's.

    That is the failure this add-on exists to avoid, so it stays unwired until
    it reads from the shared session.
    """
    config = fb.render_go2rtc_config(TEMPLATE, ["111"])
    assert "talk.sh" not in config
    assert "backchannel" not in config


def test_go2rtc_config_parses_with_the_shipped_template() -> None:
    """The real template plus generated streams must be valid, correct YAML.

    Substring checks pass happily on a config whose streams are nested in the
    wrong place, which go2rtc would read as no streams at all.
    """
    import yaml

    template = (Path(__file__).resolve().parent.parent / "go2rtc.yaml").read_text()
    config = yaml.safe_load(fb.render_go2rtc_config(template, ["111", "222"]))
    assert list(config["streams"]) == ["furbo_111", "furbo", "furbo_222"]
    # The primary is reachable under both names, pointing at the same camera.
    assert config["streams"]["furbo"] == config["streams"]["furbo_111"]
    # Video only: talkback is not wired up in this version.
    assert len(config["streams"]["furbo_111"]) == 1
    # Everything the template configures survives generation.
    assert config["rtsp"]["listen"] == ":8554"
    assert config["api"]["listen"] == "127.0.0.1:1984"


def test_go2rtc_config_needs_a_camera() -> None:
    """Writing a config with no cameras would leave go2rtc serving nothing."""
    with pytest.raises(SystemExit):
        fb.render_go2rtc_config(TEMPLATE, [])


# --- the frame queue ---------------------------------------------------------


def test_end_of_stream_survives_a_full_queue() -> None:
    """The marker must land even when the viewer has fallen behind.

    Dropping it left the response waiting for a frame that would never come,
    holding the camera's single video slot until the viewer disconnected. A
    viewer too slow to keep up is exactly when the reader gives up, so a full
    queue is the case that has to deliver it.
    """
    frames = fb.FrameQueue(maxsize=2)
    frames.offer(b"a")
    frames.offer(b"b")
    frames.offer(b"c")  # no room: dropped
    frames.offer(None)  # must arrive anyway
    drained = []
    while True:
        item = frames._queue.get_nowait()
        drained.append(item)
        if item is None:
            break
    assert drained[-1] is None
    assert frames.dropped >= 1


def test_frames_are_dropped_rather_than_queued_without_limit() -> None:
    """A viewer that cannot keep up loses frames, it does not grow the queue."""
    frames = fb.FrameQueue(maxsize=2)
    for _ in range(10):
        frames.offer(b"x")
    assert frames.dropped == 8
    assert frames._queue.qsize() == 2
