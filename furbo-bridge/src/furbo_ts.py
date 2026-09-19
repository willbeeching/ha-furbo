"""A minimal MPEG-TS muxer, just enough for H.264 video and ADTS AAC audio.

Why this exists. The camera sends video and audio as two separate queues of
bare frames, neither carrying a container timestamp. Handing those to ffmpeg as
two pipes puts the tracks three seconds apart, because each input is opened and
probed at a different moment and nothing tells ffmpeg how they relate. Measured
across every combination of its timestamp flags, the gap never moved: two
clockless streams have no common zero, and no flag can invent one.

The camera does stamp both, on one clock, and that is the only honest source of
alignment there is. Carrying those stamps to ffmpeg needs a container that has
somewhere to put them, so the bridge writes one. MPEG-TS is the smallest such
container that ffmpeg reads without being told anything, and writing it takes
less code than any of the alternatives take to get wrong.

Deliberately not a general muxer: one program, two streams, no PCR discipline
beyond what a live viewer needs, no B-frames (the camera sends none, so PTS and
DTS are equal and there is nothing to reorder).
"""

from __future__ import annotations

from collections.abc import Iterator

PACKET_SIZE = 188
SYNC_BYTE = 0x47

PAT_PID = 0x0000
PMT_PID = 0x1000
VIDEO_PID = 0x0100
AUDIO_PID = 0x0101
PROGRAM_NUMBER = 1

STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_AAC_ADTS = 0x0F

# PES stream ids: the first video and the first audio elementary stream.
VIDEO_STREAM_ID = 0xE0
AUDIO_STREAM_ID = 0xC0

# MPEG timestamps run at 90 kHz, whatever the media's own rate.
CLOCK_HZ = 90_000
# How often to repeat the tables. A viewer joining mid-stream cannot decode
# anything until it has seen them, so they go out far more often than the
# standard's minimum; they cost 376 bytes.
TABLE_INTERVAL = 0.1


def _crc32_mpeg(data: bytes) -> int:
    """The MPEG-2 systems CRC: big-endian, polynomial 0x04C11DB7, no final xor."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            shifted = (crc << 1) & 0xFFFFFFFF
            crc = shifted ^ 0x04C11DB7 if crc & 0x80000000 else shifted
    return crc


def _section(table_id: int, body: bytes) -> bytes:
    """Wrap a table body in its header and CRC, then point to its start."""
    # syntax indicator set, reserved bits high, length covers body + CRC.
    header = bytes([table_id]) + ((0xB000 | (len(body) + 4)).to_bytes(2, "big"))
    section = header + body
    return b"\x00" + section + _crc32_mpeg(section).to_bytes(4, "big")


def _pat() -> bytes:
    body = (
        PROGRAM_NUMBER.to_bytes(2, "big")  # transport stream id
        + b"\xc1\x00\x00"  # version 0, current, section 0 of 0
        + PROGRAM_NUMBER.to_bytes(2, "big")
        + (0xE000 | PMT_PID).to_bytes(2, "big")
    )
    return _section(0x00, body)


def _pmt() -> bytes:
    streams = (
        bytes([STREAM_TYPE_H264])
        + (0xE000 | VIDEO_PID).to_bytes(2, "big")
        + b"\xf0\x00"  # no descriptors
        + bytes([STREAM_TYPE_AAC_ADTS])
        + (0xE000 | AUDIO_PID).to_bytes(2, "big")
        + b"\xf0\x00"
    )
    body = (
        PROGRAM_NUMBER.to_bytes(2, "big")
        + b"\xc1\x00\x00"
        + (0xE000 | VIDEO_PID).to_bytes(2, "big")  # PCR rides on the video PID
        + b"\xf0\x00"  # no program descriptors
        + streams
    )
    return _section(0x02, body)


def _timestamp(marker: int, value: int) -> bytes:
    """One 33-bit timestamp in the five-byte shape PES uses for PTS and DTS."""
    value &= (1 << 33) - 1
    return bytes(
        [
            (marker << 4) | ((value >> 29) & 0x0E) | 1,
            (value >> 22) & 0xFF,
            ((value >> 14) & 0xFE) | 1,
            (value >> 7) & 0xFF,
            ((value << 1) & 0xFE) | 1,
        ]
    )


def _pcr(value: int) -> bytes:
    """The six-byte program clock reference an adaptation field carries."""
    base = value & ((1 << 33) - 1)
    return (
        ((base >> 25) & 0xFF).to_bytes(1, "big")
        + ((base >> 17) & 0xFF).to_bytes(1, "big")
        + ((base >> 9) & 0xFF).to_bytes(1, "big")
        + ((base >> 1) & 0xFF).to_bytes(1, "big")
        + bytes([((base & 1) << 7) | 0x7E, 0x00])
    )


class TSMuxer:
    """Turn timestamped frames into a transport stream, one program, two PIDs.

    Timestamps arrive as seconds on whatever clock the caller has; the muxer
    only needs them to share an origin with each other, which is the whole
    point of doing this here rather than in ffmpeg.
    """

    def __init__(self) -> None:
        self._continuity: dict[int, int] = {VIDEO_PID: 0, AUDIO_PID: 0, PAT_PID: 0, PMT_PID: 0}
        self._tables_at = -TABLE_INTERVAL

    def _next_continuity(self, pid: int) -> int:
        counter = self._continuity[pid]
        self._continuity[pid] = (counter + 1) & 0x0F
        return counter

    def _packets(self, pid: int, payload: bytes, start: bool, pcr: int | None) -> Iterator[bytes]:
        """Split one PES packet or section across 188-byte transport packets."""
        first = True
        while payload or first:
            header = bytearray([SYNC_BYTE])
            header.append(((0x40 if (first and start) else 0x00) | (pid >> 8)) & 0xFF)
            header.append(pid & 0xFF)
            adaptation = b""
            if first and pcr is not None:
                # Adaptation field carrying the clock, ahead of the payload.
                adaptation = bytes([7, 0x10]) + _pcr(pcr)
            room = PACKET_SIZE - len(header) - 1 - len(adaptation)
            chunk = payload[:room]
            payload = payload[len(chunk) :]
            padding = room - len(chunk)
            if padding:
                # Stuffing goes in the adaptation field, never in the payload:
                # a short PES would otherwise be indistinguishable from data.
                if adaptation:
                    adaptation = (
                        bytes([adaptation[0] + padding]) + adaptation[1:] + b"\xff" * padding
                    )
                else:
                    tail = b"\x00" + b"\xff" * (padding - 2) if padding >= 2 else b""
                    adaptation = bytes([padding - 1]) + tail
            flags = 0x30 if adaptation else 0x10
            header.append((flags | self._next_continuity(pid)) & 0xFF)
            yield bytes(header) + adaptation + chunk
            first = False

    def _tables(self, now: float) -> Iterator[bytes]:
        if now - self._tables_at < TABLE_INTERVAL:
            return
        self._tables_at = now
        yield from self._packets(PAT_PID, _pat(), start=True, pcr=None)
        yield from self._packets(PMT_PID, _pmt(), start=True, pcr=None)

    def frame(self, pid: int, payload: bytes, seconds: float) -> Iterator[bytes]:
        """Emit one frame as transport packets, tables first when due."""
        yield from self._tables(seconds)
        ticks = int(seconds * CLOCK_HZ)
        stream_id = VIDEO_STREAM_ID if pid == VIDEO_PID else AUDIO_STREAM_ID
        # PTS only: the camera sends no B-frames, so there is nothing to
        # reorder and a DTS would only repeat the PTS.
        pes_header = bytes([0x80, 0x80, 5]) + _timestamp(0b0010, ticks)
        body = pes_header + payload
        # A video PES may exceed 65535 bytes, which the length field cannot
        # express; zero means "until the next one", which is legal for video.
        length = len(body) if len(body) <= 0xFFFF and pid == AUDIO_PID else 0
        pes = b"\x00\x00\x01" + bytes([stream_id]) + length.to_bytes(2, "big") + body
        yield from self._packets(pid, pes, start=True, pcr=ticks if pid == VIDEO_PID else None)

    def video(self, payload: bytes, seconds: float) -> Iterator[bytes]:
        yield from self.frame(VIDEO_PID, payload, seconds)

    def audio(self, payload: bytes, seconds: float) -> Iterator[bytes]:
        yield from self.frame(AUDIO_PID, payload, seconds)
