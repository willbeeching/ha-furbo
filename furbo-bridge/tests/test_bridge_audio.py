"""The audio format is proved, never guessed.

Version 1.3.3 declared the camera's audio AAC because the phone app decodes
AAC, and on a camera that sends something else the whole output header failed:
ffmpeg could not parse the track it had been promised, so the video went down
with the sound. These tests are about the rule that replaced the guess. A
format counts as known only when a decoder accepts the bytes, and the frames
used here are real AAC produced by ffmpeg rather than plausible-looking
constants, because a constant cannot tell you whether a decoder agrees.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import furbo_audio as fa
import furbo_ts as ts

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg, as the add-on image has")

RATE = 16000
CHANNELS = 1


def adts_frames(seconds: float = 1.0) -> list[bytes]:
    """Real AAC-LC frames in ADTS framing, from a tone ffmpeg encodes for us.

    Generated rather than committed: the point of these tests is that a
    decoder agrees with us, so the sample has to be something a decoder made.
    """
    assert FFMPEG is not None
    done = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={RATE}:duration={seconds}",
            "-ac",
            str(CHANNELS),
            "-c:a",
            "aac",
            "-f",
            "adts",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return split_adts(done.stdout)


def split_adts(stream: bytes) -> list[bytes]:
    """One ADTS frame per item, headers included."""
    frames, at = [], 0
    while at + fa.ADTS_HEADER_BYTES <= len(stream):
        assert fa.looks_like_adts(stream[at:]), "not an ADTS stream"
        length = ((stream[at + 3] & 0x03) << 11) | (stream[at + 4] << 3) | (stream[at + 5] >> 5)
        assert length > fa.ADTS_HEADER_BYTES
        frames.append(stream[at : at + length])
        at += length
    return frames


def strip(frame: bytes) -> bytes:
    """The raw AAC inside one ADTS frame, which is what a camera may send."""
    return frame[fa.ADTS_HEADER_BYTES :]


def test_the_header_we_build_says_what_the_frame_is() -> None:
    """Every field a decoder reads back out of it has to be the one we meant."""
    header = fa.adts_header(111, 16000, 1)
    assert len(header) == fa.ADTS_HEADER_BYTES
    assert header[0] == 0xFF and (header[1] & 0xF0) == 0xF0, "no sync word"
    assert (header[1] & 0x01) == 1, "CRC absent, so no CRC bytes follow"
    assert (header[2] >> 6) == 1, "profile is AAC-LC"
    assert fa.ADTS_RATES[(header[2] >> 2) & 0x0F] == 16000
    assert (((header[2] & 0x01) << 2) | (header[3] >> 6)) == 1, "one channel"
    length = ((header[3] & 0x03) << 11) | (header[4] << 3) | (header[5] >> 5)
    assert length == 111 + fa.ADTS_HEADER_BYTES, "length counts the header too"
    assert (header[6] & 0x03) == 0, "one raw data block"


@needs_ffmpeg
def test_frames_that_carry_their_own_header_are_passed_through() -> None:
    """ADTS says what it is, so there is nothing to add and nothing to guess."""
    frames = adts_frames()
    carries = fa.identify(frames, RATE, CHANNELS)
    assert carries is not None
    assert carries.stream_type == ts.STREAM_TYPE_AAC_ADTS
    assert carries.framed is True
    assert carries.package(frames[0]) == frames[0], "a framed payload must not be touched"


@needs_ffmpeg
def test_raw_frames_are_carried_once_the_header_we_build_decodes() -> None:
    """The case this is all for: AAC with the header left off.

    The SDK carries frame boundaries itself, so a camera has no reason to send
    ADTS. Putting the header back is what makes those frames playable, and the
    proof is that a decoder then accepts them.
    """
    raw = [strip(frame) for frame in adts_frames()]
    assert not any(fa.looks_like_adts(frame) for frame in raw)

    carries = fa.identify(raw, RATE, CHANNELS)
    assert carries is not None, "real AAC frames were not recognised"
    assert carries.name == "raw AAC"
    assert carries.stream_type == ts.STREAM_TYPE_AAC_ADTS
    assert carries.framed is False
    packaged = carries.package(raw[0])
    assert fa.looks_like_adts(packaged)
    assert packaged[fa.ADTS_HEADER_BYTES :] == raw[0], "the payload must survive intact"
    assert fa.decodes_as_aac(b"".join(carries.package(frame) for frame in raw))


@needs_ffmpeg
@pytest.mark.parametrize(
    "blob",
    [
        pytest.param(b"\x00" * 4000, id="silence"),
        pytest.param(bytes((i * 7) & 0xFF for i in range(4000)), id="a-companded-looking-ramp"),
        pytest.param(bytes((i * 131 + 17) % 251 for i in range(4000)), id="noise"),
    ],
)
def test_nothing_that_is_not_aac_is_ever_claimed(blob: bytes) -> None:
    """The check has to have teeth, or it is the 1.3.3 guess with extra steps."""
    frames = [blob[at : at + 111] for at in range(0, len(blob), 111)]
    assert fa.identify(frames, RATE, CHANNELS) is None, "this was accepted as AAC"


@needs_ffmpeg
def test_a_sample_rate_no_header_can_express_is_not_carried() -> None:
    """ADTS has thirteen rates. A camera reporting anything else cannot be
    described by a header we build, and a track we cannot describe is one we
    must not declare."""
    raw = [strip(frame) for frame in adts_frames()]
    assert fa.identify(raw, 37000, CHANNELS) is None
    assert fa.identify(raw, None, CHANNELS) is None
    assert fa.identify(raw, RATE, None) is None
    assert fa.identify(raw, RATE, 7) is None


@needs_ffmpeg
def test_a_sync_word_on_its_own_proves_nothing() -> None:
    """The 1.3.3 rule was "it starts with 0xFFF, so it is AAC". Eleven bits of
    coincidence are cheap, and the frames behind them still have to decode."""
    pretending = [b"\xff\xf1\x50\x80\x01\x7f\xfc" + bytes(range(60)) for _ in range(10)]
    assert all(fa.looks_like_adts(frame) for frame in pretending)
    assert fa.identify(pretending, RATE, CHANNELS) is None


def test_without_a_decoder_there_is_no_answer_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a no. A no is about the camera; this is about us, and a caller that
    cannot tell them apart will remember the wrong one."""
    monkeypatch.setattr(fa.shutil, "which", lambda _name: None)
    with pytest.raises(fa.Undecided):
        fa.decodes_as_aac(b"\xff\xf1" + b"\x40" * 100)
    with pytest.raises(fa.Undecided):
        fa.identify([b"\x01\x02\x03" + b"\x00" * 100], RATE, CHANNELS)


def test_a_decoder_that_will_not_run_is_not_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr(fa.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(fa.subprocess, "run", explode)
    with pytest.raises(fa.Undecided):
        fa.decodes_as_aac(b"anything")


def test_a_decoder_that_had_to_be_killed_is_not_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case that reached main: a timeout is a SubprocessError, so it was
    indistinguishable from the decoder looking at the bytes and refusing."""

    def hang(*_args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=fa.PROBE_TIMEOUT)

    monkeypatch.setattr(fa.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(fa.subprocess, "run", hang)
    with pytest.raises(fa.Undecided, match="did not finish"):
        fa.decodes_as_aac(b"anything")


def test_the_decoder_is_not_given_long_with_a_picture_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit is spent with a viewer's first frames held, so it is short."""
    assert fa.PROBE_TIMEOUT <= 5.0, "a wedged decoder would stall the picture this long"


def test_nothing_at_all_is_not_a_format() -> None:
    assert fa.identify([], RATE, CHANNELS) is None
    assert fa.identify([b"", b""], RATE, CHANNELS) is None


def test_the_sample_dump_is_bounded_and_readable() -> None:
    """It goes into a log a person has to read and then quote back to us."""
    lines = list(fa.dump([b"\xab" * 9000]))
    assert lines, "no sample to report"
    assert all(len(line.split(" ", 1)[1]) <= fa.DUMP_LINE for line in lines)
    assert lines[0].startswith(f"1/{len(lines)} ")
    import base64

    joined = "".join(line.split(" ", 1)[1] for line in lines)
    assert base64.b64decode(joined) == b"\xab" * fa.DUMP_BYTES
