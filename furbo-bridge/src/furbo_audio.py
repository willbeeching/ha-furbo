"""What the camera's microphone is sending, settled by proving it.

The frame header carries a codec id, and that id is useless here. It is
defined inside TUTK's native library, which is not something this project can
read: an FB0030 reports 135, the phone app configures its decoder at runtime
rather than from a table, and the only id established anywhere in this
repository is the 137 the camera's speaker accepts in the other direction.
Version 1.3.3 guessed AAC from that id and broke live video for it, because a
transport stream's tables are a promise about its payload and a wrong promise
costs the picture as well as the sound.

So nothing here guesses. A format is claimed only when a decoder accepts the
bytes, and ffmpeg is in the add-on image already, so it can be asked. Two
shapes are recognised:

  * ADTS AAC, which announces itself in its first eleven bits.
  * Raw AAC frames with no header at all, which is what a camera sends when
    the SDK already carries the frame boundaries. Those become playable by
    prepending the header the camera left out, built from the sample rate and
    channel count its own frame header reports.

Anything else gets no audio track and a log line with the bytes in it, which
is what identifying a new format needs. Video is never the price of sound.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import logging
import shutil
import subprocess
from typing import NamedTuple

import furbo_ts as ts

_LOGGER = logging.getLogger("furbo_bridge")

# Sample rates in the order an ADTS header's four bit index numbers them.
ADTS_RATES = (
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
)
ADTS_HEADER_BYTES = 7
# How long to let the decoder look at the sample. It reads a fraction of a
# second of audio from memory, so this is a guard against a wedged process
# rather than a budget.
PROBE_TIMEOUT = 10.0
# How much of an unrecognised sample to write to the log, in bytes of payload.
DUMP_BYTES = 4096
DUMP_LINE = 180


def looks_like_adts(payload: bytes) -> bool:
    """True when a frame opens with an ADTS sync word."""
    return len(payload) >= 2 and payload[0] == 0xFF and (payload[1] & 0xF0) == 0xF0


def adts_header(payload_len: int, sample_rate: int, channels: int) -> bytes:
    """The seven byte ADTS header for one AAC-LC frame of ``payload_len``.

    Profile AAC-LC, no CRC, variable bit rate, one raw data block. The camera
    sends a constant rate and channel count for the life of a stream, so the
    only field that moves frame to frame is the length.
    """
    index = ADTS_RATES.index(sample_rate)
    total = payload_len + ADTS_HEADER_BYTES
    return bytes(
        (
            0xFF,
            0xF1,  # sync, MPEG-4, layer 0, no CRC
            (1 << 6) | (index << 2) | (channels >> 2),
            ((channels & 0x03) << 6) | (total >> 11),
            (total >> 3) & 0xFF,
            ((total & 0x07) << 5) | 0x1F,  # length, then buffer fullness: VBR
            0xFC,
        )
    )


class Format(NamedTuple):
    """An audio format that a decoder has accepted, and how to package it.

    ``stream_type`` is what the transport stream's tables must say. ``package``
    turns one frame from the camera into one frame of that stream.
    """

    name: str
    stream_type: int
    sample_rate: int
    channels: int
    framed: bool

    def package(self, payload: bytes) -> bytes:
        if self.framed:
            return payload
        return adts_header(len(payload), self.sample_rate, self.channels) + payload


def decodes_as_aac(stream: bytes) -> bool:
    """True when ffmpeg decodes this ADTS stream without a single complaint.

    Strict on purpose. AAC is a structured bitstream, so bytes that are not
    AAC do not decode quietly: a run of silence, a ramp and random noise are
    all rejected, which is what makes an accepted sample evidence rather than
    an absence of evidence. A missing ffmpeg proves nothing, so it is a no.
    """
    binary = shutil.which("ffmpeg")
    if binary is None:
        _LOGGER.warning("no ffmpeg to check the audio format with, so audio is left out")
        return False
    try:
        done = subprocess.run(
            [binary, "-v", "error", "-f", "aac", "-i", "pipe:0", "-f", "null", "-"],
            input=stream,
            capture_output=True,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        _LOGGER.warning("could not run ffmpeg to check the audio format", exc_info=True)
        return False
    if done.returncode == 0 and not done.stderr.strip():
        return True
    _LOGGER.debug(
        "the decoder rejected this packaging: %s",
        done.stderr.decode("utf-8", "replace").strip()[:200] or f"exit {done.returncode}",
    )
    return False


def identify(frames: list[bytes], sample_rate: int | None, channels: int | None) -> Format | None:
    """The format of these frames, or None when nothing here can name it.

    ``sample_rate`` and ``channels`` are what the camera's own frame header
    reported. They are needed only for frames that carry no header of their
    own, where they are the one thing that says how to build one.
    """
    frames = [frame for frame in frames if frame]
    if not frames:
        return None
    if all(looks_like_adts(frame) for frame in frames):
        if decodes_as_aac(b"".join(frames)):
            # The header in the payload is authoritative here, so whatever the
            # camera reported alongside it does not come into it.
            return Format("AAC in ADTS framing", ts.STREAM_TYPE_AAC_ADTS, 0, 0, True)
        return None
    if sample_rate is None or channels is None:
        return None
    if sample_rate not in ADTS_RATES or channels not in (1, 2):
        return None
    candidate = Format("raw AAC", ts.STREAM_TYPE_AAC_ADTS, sample_rate, channels, False)
    if decodes_as_aac(b"".join(candidate.package(frame) for frame in frames)):
        return candidate
    return None


def dump(frames: list[bytes], limit: int = DUMP_BYTES) -> Iterator[str]:
    """The sample as base64 lines, so an unknown format can be reported.

    Bounded, because this goes to a log a person has to read, and split into
    short lines because a log viewer wraps one long one into uselessness.
    """
    blob = b"".join(frames)[:limit]
    text = base64.b64encode(blob).decode("ascii")
    lines = [text[i : i + DUMP_LINE] for i in range(0, len(text), DUMP_LINE)]
    for number, line in enumerate(lines, start=1):
        yield f"{number}/{len(lines)} {line}"
