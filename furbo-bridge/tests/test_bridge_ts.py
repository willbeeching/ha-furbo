"""The transport stream the bridge writes so video and audio stay together.

Every one of these covers something that, got wrong, produces a file ffmpeg
either refuses or silently mis-times, which is exactly the failure this muxer
exists to end.
"""

from __future__ import annotations

from itertools import pairwise

import furbo_ts as ts


def _packets(data: bytes) -> list[bytes]:
    return [data[i : i + ts.PACKET_SIZE] for i in range(0, len(data), ts.PACKET_SIZE)]


def _mux(
    frames: list[tuple[str, bytes, float]],
    audio_type: int | None = ts.STREAM_TYPE_AAC_ADTS,
) -> bytes:
    mux = ts.TSMuxer(audio_type)
    out = b""
    for kind, payload, seconds in frames:
        emit = mux.video if kind == "video" else mux.audio
        out += b"".join(emit(payload, seconds))
    return out


def test_every_packet_is_the_right_size_and_starts_with_the_sync_byte() -> None:
    """A demuxer finds packet boundaries by the sync byte at a fixed stride.

    One short packet and everything after it is unreadable, so this is the
    property the whole format rests on.
    """
    data = _mux([("video", b"\x00\x00\x00\x01\x65" + b"\xaa" * 400, 0.0)])
    packets = _packets(data)
    assert packets, "the muxer produced nothing"
    for packet in packets:
        assert len(packet) == ts.PACKET_SIZE
        assert packet[0] == ts.SYNC_BYTE


def test_the_tables_come_before_any_media() -> None:
    """A viewer cannot decode a thing until it has seen the PAT and PMT."""
    data = _mux([("video", b"\x00\x00\x00\x01\x65" + b"\xbb" * 100, 0.0)])
    first_two = _packets(data)[:2]
    pids = [((p[1] & 0x1F) << 8) | p[2] for p in first_two]
    assert pids == [ts.PAT_PID, ts.PMT_PID]


def test_video_and_audio_land_on_their_own_pids() -> None:
    """Two tracks means two PIDs; one would splice them into each other."""
    data = _mux(
        [
            ("video", b"\x00\x00\x00\x01\x65" + b"\xcc" * 50, 0.0),
            ("audio", b"\xff\xf1" + b"\xdd" * 40, 0.0),
        ]
    )
    pids = {((p[1] & 0x1F) << 8) | p[2] for p in _packets(data)}
    assert ts.VIDEO_PID in pids
    assert ts.AUDIO_PID in pids


def test_the_continuity_counter_advances_per_pid() -> None:
    """A demuxer treats a gap in this counter as loss and drops the frame."""
    payload = b"\x00\x00\x00\x01\x65" + b"\xee" * 1000
    data = _mux([("video", payload, 0.0), ("video", payload, 0.1)])
    seen = [p[3] & 0x0F for p in _packets(data) if (((p[1] & 0x1F) << 8) | p[2]) == ts.VIDEO_PID]
    assert len(seen) > 1
    for before, after in pairwise(seen):
        assert after == (before + 1) & 0x0F


def test_the_timestamp_written_is_the_timestamp_asked_for() -> None:
    """The whole point: the caller's clock survives into the stream.

    Decoded straight back out of the PES header rather than trusting a
    round-trip through a demuxer, so a failure here points at the encoding.
    """
    data = _mux([("audio", b"\xff\xf1" + b"\x11" * 20, 1.5)])
    for packet in _packets(data):
        if (((packet[1] & 0x1F) << 8) | packet[2]) != ts.AUDIO_PID:
            continue
        body = packet[4:]
        start = body.index(b"\x00\x00\x01")
        pts_bytes = body[start + 9 : start + 14]
        value = (
            ((pts_bytes[0] >> 1) & 0x07) << 30
            | pts_bytes[1] << 22
            | (pts_bytes[2] >> 1) << 15
            | pts_bytes[3] << 7
            | pts_bytes[4] >> 1
        )
        assert value == int(1.5 * ts.CLOCK_HZ)
        return
    raise AssertionError("no audio packet was written")


def test_the_crc_matches_what_a_demuxer_computes() -> None:
    """A table with a bad CRC is discarded, and nothing decodes after it."""
    section = ts._pat()[1:]  # drop the pointer field
    body, crc = section[:-4], int.from_bytes(section[-4:], "big")
    assert ts._crc32_mpeg(body) == crc


def test_an_unknown_codec_gets_no_audio_track_at_all() -> None:
    """A track declared wrongly takes the video down with it.

    The tables are a promise about the payload. When ffmpeg was told the audio
    was AAC and it was not, it failed to parse it, the RTSP header failed with
    it, and the picture went too. Sound the bridge cannot name is not carried.
    """
    data = _mux(
        [
            ("video", b"\x00\x00\x00\x01\x65" + b"\xaa" * 50, 0.0),
            ("audio", b"\x01\x02" + b"\xbb" * 40, 0.0),
        ],
        audio_type=None,
    )
    pids = {((p[1] & 0x1F) << 8) | p[2] for p in _packets(data)}
    assert ts.VIDEO_PID in pids, "the picture must survive unknown audio"
    assert ts.AUDIO_PID not in pids, "an unnameable track was carried anyway"


def test_the_tables_only_name_the_tracks_that_are_there() -> None:
    """A viewer reads the PMT to know what to expect; it must not overpromise."""
    with_audio = ts._pmt(ts.STREAM_TYPE_AAC_ADTS)
    without = ts._pmt(None)
    assert bytes([ts.STREAM_TYPE_H264]) in with_audio
    assert bytes([ts.STREAM_TYPE_H264]) in without
    assert (0xE000 | ts.AUDIO_PID).to_bytes(2, "big") in with_audio
    assert (0xE000 | ts.AUDIO_PID).to_bytes(2, "big") not in without
    # And the section stays valid, since its length and CRC both moved.
    section = without[1:]
    assert ts._crc32_mpeg(section[:-4]) == int.from_bytes(section[-4:], "big")
