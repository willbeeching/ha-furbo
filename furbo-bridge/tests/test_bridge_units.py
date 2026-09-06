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
    assert fb.read_quality() == "1080p"
    fb.write_quality("360p")
    assert qfile.read_text().strip() == "360p"
    assert fb.read_quality() == "360p"
    # An unexpected value on disk falls back to the default.
    qfile.write_text("garbage")
    assert fb.read_quality() == "1080p"
