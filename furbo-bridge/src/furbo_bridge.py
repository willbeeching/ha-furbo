#!/usr/bin/env python3
"""HTTP bridge in front of the Furbo P2P session, for Home Assistant.

Runs one long-lived TUTK session to a single camera on a worker thread and
exposes its state and controls over a small JSON API. The Home Assistant
integration on branch ``claude/furbo-integration`` talks to this API; it
never loads the TUTK library itself.

Video is served from that same session over ``GET /api/stream``. That matters:
the camera's ``P2PAccountKey`` is reissued by the cloud on every
``/v5/device/p2p_connection/get`` call, so a second process opening its own
session invalidates this one's credential and both then fail to authenticate
(``avClientStartEx`` times out). One session, one credential, no race.

    furbo_p2p.py serve --port 8791 --token <secret>

API (all JSON; ``Authorization: Bearer <token>`` required when --token is set):

    GET  /api/status
        -> 200 {"connected": bool, "updated_at": float | null,
                "last_error": str | null, "device": {"id", "name", "product"},
                "state": {"camera_on": bool, "volume": int, "muted": bool,
                          "night_mode": "auto"|"on"|"off",
                          "bark_sensitivity": "off"|"low"|"medium"|"high",
                          "auto_tracking": bool, "auto_zoom": bool,
                          "firmware": str | null}}
        Served from the cache the worker refreshes every --interval seconds;
        ``state`` keys are present only once the camera has reported them.
    POST /api/settings   body: any subset of camera_on, volume, night_mode,
                         bark_sensitivity, auto_tracking, auto_zoom
        -> 200 status document after reading the settings back
    POST /api/pan        body: {"direction": "left"|"right", "degrees": 1..180}
    POST /api/toss       body: {}      (dispenses a real treat)
    POST /api/treat-sound body: {}
        -> 200 {"ok": true}
    GET  /api/stream
        -> 200 a chunked H.264 elementary stream from the live session, read
           at the quality in the quality file. One viewer at a time; a second
           request gets 409. go2rtc consumes this via stream.sh.

Errors: 400 {"error": "bad_request", "detail": "..."}, 401 {"error":
"unauthorized"}, 503 {"error": "p2p_unavailable", "detail": "..."} when the
session cannot be (re)established. The detail is a short reason, never a
response body from the camera or the cloud.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hmac
import json
import logging
import os
from pathlib import Path
import signal
import struct
import threading
import time
from typing import Any

from aiohttp import web

import furbo_p2p as fp

_LOGGER = logging.getLogger("furbo_bridge")

DEFAULT_PORT = 8791
DEFAULT_INTERVAL = 30.0
# Every settings value the camera holds is written through this bridge, so the
# only reason to re-read them all is to catch a change made in the Furbo app.
# A full sweep is 13 round trips, so do it rarely and keep the frequent poll to
# a single command that also proves the session is alive.
FULL_REFRESH_INTERVAL = 300.0
# A refresh slower than this means a degraded (usually relayed) session. Worth
# a log line, because it is the difference between working controls and not.
SLOW_REFRESH_SECONDS = 10.0
# Frames are handed to the HTTP writer through this queue. Bounded so a stalled
# viewer cannot grow the heap; overflow drops frames and ffmpeg resyncs.
STREAM_QUEUE_MAX = 512
FRAME_BUFFER_BYTES = 2 * 1024 * 1024
# How long a reconnect waits for the frame reader to leave the SDK.
READER_EXIT_TIMEOUT = 3.0
# How long to wait for the first frame after asking the camera to start video.
# Without this the reader spins on "no data" for as long as the viewer waits,
# holding the single stream slot, so every retry gets a busy response and the
# failure looks like a hang rather than a refusal.
FIRST_FRAME_TIMEOUT = 8.0
# A moment between telling the camera to stop video and asking it to start
# again, so it has released the old stream before the new request lands.
STREAM_RESTART_SETTLE = 0.3
# Backoff after the cloud refuses to log us in. Furbo rate-limits repeated
# attempts (80001, 80002), so retrying on the poll interval makes recovery
# slower rather than faster.
LOGIN_BACKOFF_START = 60.0
LOGIN_BACKOFF_MAX = 900.0
NIGHT_MODES = ("auto", "on", "off")
BARK_LEVELS = ("off", "low", "medium", "high")
PAN_DIRECTIONS = ("left", "right")
TREAT_SIZES = ("large", "small")
SNACK_MODES = ("default", "custom", "mute")
VIDEO_QUALITIES = ("1080p", "720p", "360p")
# go2rtc's on-demand stream reads the chosen quality from this file when a
# viewer connects, so a change takes effect on the next view.
QUALITY_FILE = Path(os.environ.get("FURBO_QUALITY_FILE", "/data/quality"))


def _quality_file(device_id: str) -> Path:
    """Where one camera's chosen quality lives."""
    return QUALITY_FILE.with_name(f"{QUALITY_FILE.name}.{device_id}")


def read_quality(device_id: str) -> str:
    """Return the camera's stream quality, defaulting to 1080p.

    The unsuffixed file is read as a fallback so a bridge that served one
    camera before keeps the quality its user chose.
    """
    for path in (_quality_file(device_id), QUALITY_FILE):
        try:
            value = path.read_text().strip()
        except OSError:
            continue
        if value in VIDEO_QUALITIES:
            return value
    return "1080p"


def write_quality(device_id: str, quality: str) -> None:
    """Persist one camera's stream quality for its next go2rtc stream start."""
    _quality_file(device_id).write_text(quality + "\n")


class StreamBusy(Exception):
    """Another viewer already holds the single video stream."""


class BridgeUnavailable(Exception):
    """The P2P session is not available; the reason is safe to show."""


class UnknownCamera(Exception):
    """The request named a camera this bridge does not serve."""


def normalise_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the decoder's state dict into the documented API shape."""
    out: dict[str, Any] = {}
    if "camera_on" in raw:
        out["camera_on"] = bool(raw["camera_on"])
    if isinstance(raw.get("volume"), int):
        out["volume"] = max(0, min(100, raw["volume"]))
    if "muted" in raw:
        out["muted"] = bool(raw["muted"])
    if raw.get("night_mode") in NIGHT_MODES:
        out["night_mode"] = raw["night_mode"]
    if raw.get("bark_sensitivity") in BARK_LEVELS:
        out["bark_sensitivity"] = raw["bark_sensitivity"]
    tracking = raw.get("auto_tracking")
    if isinstance(tracking, dict):
        out["auto_tracking"] = bool(tracking.get("live"))
    zoom = raw.get("auto_zoom")
    if isinstance(zoom, dict):
        out["auto_zoom"] = bool(zoom.get("live"))
    firmware = raw.get("firmware")
    if isinstance(firmware, dict) and firmware.get("current"):
        out["firmware"] = str(firmware["current"])
    if "voice_control" in raw:
        out["voice_control"] = bool(raw["voice_control"])
    if raw.get("treat_size") in TREAT_SIZES:
        out["treat_size"] = raw["treat_size"]
    if raw.get("snack_call") in SNACK_MODES:
        out["snack_call"] = raw["snack_call"]
    schedule = raw.get("schedule")
    if isinstance(schedule, dict) and isinstance(schedule.get("enable"), bool):
        out["schedule_enabled"] = schedule["enable"]
    calm = raw.get("auto_calm")
    if isinstance(calm, dict) and calm.get("enable") is not None:
        out["calm_enabled"] = bool(calm.get("enable"))
    return out


class P2PWorker:
    """Owns one camera's P2P session. Every method runs on the worker thread.

    One per camera: a session, a lock and a single video slot each, so a camera
    that is slow, wedged or logged out cannot hold up the others.
    """

    def __init__(self, args: argparse.Namespace, device_id: str | None = None) -> None:
        self._args = args
        # None means "the account's only camera", which is what a bridge
        # serving a single camera has always passed to the cloud.
        self.device_id = device_id
        self._p2p: fp.FurboP2P | None = None
        self._lock = threading.Lock()
        # Set while a viewer is reading frames. The reader runs on its own
        # thread and does not take _lock: TUTK allows avRecvFrameData2 to run
        # alongside avSendIOCtrl on one channel, which is what lets the
        # controls keep working while video is streaming.
        self._streaming = False
        self._stream_stop = threading.Event()
        # Set while the reader is inside the SDK. Closing the channel under it
        # would crash the process, so a reconnect waits for this to clear.
        self._reader_active = threading.Event()
        self.device: dict[str, str] = {}
        self.state: dict[str, Any] = {}
        self.updated_at: float | None = None
        self.last_error: str | None = None
        self.last_full_refresh: float = 0.0
        # Set when the cloud wants a person: a verification code, or credentials
        # it will not accept. Retrying on the usual interval only burns rate
        # limit, so attempts back off until someone fixes the options.
        self.needs_login = False
        self._login_retry_at = 0.0
        self._login_backoff = LOGIN_BACKOFF_START

    @property
    def key(self) -> str:
        """A stable name for this camera's own files and go2rtc stream.

        The cloud device id once connected, the configured one before that, and
        "default" for a bridge that was never told which camera it serves.
        """
        return self.device.get("device_id") or self.device_id or "default"

    # -- session -----------------------------------------------------------

    def _ensure(self) -> fp.FurboP2P:
        if self._p2p is not None and self._p2p.alive():
            return self._p2p
        if self.needs_login and time.monotonic() < self._login_retry_at:
            raise BridgeUnavailable(self.last_error or "waiting for a new login")
        self._drop()
        try:
            creds = asyncio.run(fp.fetch_p2p_credentials(self.device_id))
            p2p = fp.FurboP2P(
                self._args.lib, self._args.region, self._args.tutk_log, self._args.tcp_relay
            )
            p2p.initialize()
            try:
                p2p.proto = creds.get("proto", "v2")
                p2p.connect(creds, self._args.timeout)
                p2p.start_av(creds)
                p2p.register(creds)
            except SystemExit:
                p2p.close()
                raise
        except SystemExit as exc:
            self.last_error = str(exc)
            if "log in" in str(exc) or "login" in str(exc):
                self._hold_off_login()
            raise BridgeUnavailable(str(exc)) from None
        self.device = {
            "id": creds["uid"],
            "device_id": creds["device_id"],
            "name": creds["name"],
            "product": creds["product"],
        }
        self._p2p = p2p
        self.last_error = None
        self.needs_login = False
        self._login_backoff = LOGIN_BACKOFF_START
        self.last_full_refresh = 0.0
        _LOGGER.info("P2P session established to %s over %s", creds["name"], p2p.mode or "unknown")
        if p2p.mode == "relay":
            _LOGGER.warning(
                "session is relayed, not local: commands will be slow and video may fail to start"
            )
        return p2p

    def _drop(self) -> None:
        # Stop the frame reader and let it leave the SDK before the channel is
        # closed; tearing it down mid-call would take the process with it.
        self._stream_stop.set()
        was_streaming = self._streaming
        self._streaming = False
        deadline = time.monotonic() + READER_EXIT_TIMEOUT
        while self._reader_active.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._reader_active.is_set():
            _LOGGER.warning("frame reader did not stop in time; closing anyway")
        if self._p2p is not None:
            if was_streaming:
                # Tell the camera video is over before this client disappears.
                # Without it the camera holds the stream for a client that has
                # gone and refuses to start one for the next, silently, until
                # its own timeout expires.
                with contextlib.suppress(Exception):
                    self._p2p.send(fp.IPCAM_STOP, struct.pack("<i", 0))
            # Closing an already-dead session can raise from the SDK; ignore it.
            with contextlib.suppress(Exception):
                self._p2p.close()
            self._p2p = None

    @property
    def connected(self) -> bool:
        return self._p2p is not None

    @property
    def session_mode(self) -> str | None:
        """LAN, P2P or relay for the current session, None when not connected."""
        return self._p2p.mode if self._p2p is not None else None

    @property
    def streaming(self) -> bool:
        return self._streaming

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _hold_off_login(self) -> None:
        """Stop retrying a login the cloud is refusing, and say so once."""
        if not self.needs_login:
            _LOGGER.error(
                "cloud login needed: %s (retrying in %.0fs)", self.last_error, self._login_backoff
            )
        self.needs_login = True
        self._login_retry_at = time.monotonic() + self._login_backoff
        self._login_backoff = min(self._login_backoff * 2, LOGIN_BACKOFF_MAX)

    # -- operations ----------------------------------------------------------

    def _readback(self, p2p: fp.FurboP2P, wait: float) -> dict[str, Any]:
        p2p.state.pop("rejected", None)
        self.state = normalise_state(p2p.query_state(wait=wait))
        self.updated_at = time.time()
        return self.state

    def refresh(self) -> dict[str, Any]:
        """Poll the camera. Reads everything only when the full sweep is due.

        The frequent path is a single command, which is enough to prove the
        session is alive without holding the lock for a minute at a time on a
        slow link. Writes update the cache directly, so a full sweep only
        catches changes made outside Home Assistant.
        """
        with self._lock:
            started = time.monotonic()
            p2p = self._ensure()
            due = time.monotonic() - self.last_full_refresh >= FULL_REFRESH_INTERVAL
            if due or not self.state:
                state = self._readback(p2p, 2.0)
                self.last_full_refresh = time.monotonic()
            else:
                state = self._light_refresh(p2p)
            elapsed = time.monotonic() - started
            if elapsed > SLOW_REFRESH_SECONDS:
                _LOGGER.warning(
                    "camera poll took %.1fs over %s; controls may time out",
                    elapsed,
                    p2p.mode or "unknown",
                )
            return state

    def _light_refresh(self, p2p: fp.FurboP2P) -> dict[str, Any]:
        """One round trip that both refreshes power state and proves liveness."""
        key = "GET_CAMERA_ON" if p2p.proto == "v3" else "GET_FURBO_POWER"
        cmd = fp.CMD3 if p2p.proto == "v3" else fp.CMD
        p2p.send(cmd[key], b"\0\0\0\0")
        p2p.drain(1.0)
        merged = normalise_state(p2p.state)
        if merged:
            self.state = {**self.state, **merged}
        self.updated_at = time.time()
        return self.state

    def apply(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "quality" in settings:
                # Stream quality is a bridge/go2rtc concern, not a P2P command.
                write_quality(self.key, settings.pop("quality"))
            p2p = self._ensure()
            cmd = fp.CMD3 if p2p.proto == "v3" else fp.CMD
            # Setting name -> the opcode written for it, so a refusal can be
            # matched back to the setting it belongs to.
            sent: dict[str, int] = {}
            if "camera_on" in settings:
                key = "SET_CAMERA_ON" if p2p.proto == "v3" else "SET_FURBO_POWER"
                sent["camera_on"] = cmd[key]
                p2p.send(cmd[key], bytes([1 if settings["camera_on"] else 0, 0, 0, 0]))
            if "volume" in settings:
                sent["volume"] = cmd["SET_VOLUME"]
                p2p.send(cmd["SET_VOLUME"], bytes([settings["volume"], 0, 0, 0]))
            if "night_mode" in settings:
                code = {v: k for k, v in fp.NIGHT_MODES.items()}[settings["night_mode"]]
                sent["night_mode"] = cmd["SET_NIGHT_VISION"]
                p2p.send(cmd["SET_NIGHT_VISION"], bytes([code, 0, 0, 0]))
            if "bark_sensitivity" in settings:
                level = settings["bark_sensitivity"]
                if p2p.proto == "v3":
                    code = fp.BARK_V3[level]
                else:
                    code = {v: k for k, v in fp.SENSITIVITY.items()}.get(level, 0)
                sent["bark_sensitivity"] = cmd["SET_BARKING"]
                p2p.send(cmd["SET_BARKING"], bytes([code, 0, 0, 0]))
            if p2p.proto == "v3" and "auto_tracking" in settings:
                on = 1 if settings["auto_tracking"] else 0
                sent["auto_tracking"] = fp.CMD3["SET_AUTO_TRACKING"]
                p2p.send(fp.CMD3["SET_AUTO_TRACKING"], bytes([on, on, on, 0]))
            if p2p.proto == "v3" and "auto_zoom" in settings:
                on = 1 if settings["auto_zoom"] else 0
                sent["auto_zoom"] = fp.CMD3["SET_AUTO_ZOOM"]
                p2p.send(fp.CMD3["SET_AUTO_ZOOM"], bytes([on, on, 0, 0]))
            if p2p.proto == "v3" and "voice_control" in settings:
                on = 1 if settings["voice_control"] else 0
                sent["voice_control"] = fp.CMD3["SET_VOICE_CONTROL"]
                p2p.send(fp.CMD3["SET_VOICE_CONTROL"], bytes([on, 0, 0, 0]))
            if p2p.proto == "v3" and "treat_size" in settings:
                code = fp.TREAT_SIZE[settings["treat_size"]]
                sent["treat_size"] = fp.CMD3["SET_TOSS_PROFILE"]
                p2p.send(fp.CMD3["SET_TOSS_PROFILE"], bytes([code, 0, 0, 0]))
            if p2p.proto == "v3" and ("schedule_enabled" in settings or "calm_enabled" in settings):
                sent.update(self._apply_json_toggles(p2p, settings))
            p2p.drain(2.0)
            return self._confirm(p2p, settings, sent)

    def _confirm(
        self, p2p: fp.FurboP2P, settings: dict[str, Any], sent: dict[str, int]
    ) -> dict[str, Any]:
        """Fold the values just written into the cache, without re-reading.

        Reading everything back costs one round trip per setting, which is over
        ten seconds on a relayed session - long enough for Home Assistant to
        give up on the write even though the camera applied it. The camera
        answers each SET with a status, so a refused write is dropped here
        instead, and the periodic full sweep still reconciles anything changed
        from the Furbo app.
        """
        refused = {
            entry.get("opcode")
            for entry in p2p.state.pop("rejected", None) or []
            if isinstance(entry, dict)
        }
        applied = {k: v for k, v in settings.items() if k in sent and sent[k] not in refused}
        for name in sorted(set(sent) - set(applied)):
            _LOGGER.warning("camera refused %s; the cached value is unchanged", name)
        self.state = {**self.state, **applied}
        self.updated_at = time.time()
        return self.state

    def _apply_json_toggles(self, p2p: fp.FurboP2P, settings: dict[str, Any]) -> dict[str, int]:
        """Flip the enable flag on the schedule or auto-calm config, keeping the
        rest of the config the camera already holds. Both are JSON payloads.

        Returns the opcode written per setting, empty for one the camera has
        not reported a usable config for.
        """
        sent: dict[str, int] = {}
        if "schedule" not in p2p.state or "auto_calm" not in p2p.state:
            p2p.send(fp.CMD3["GET_CAMERA_SCHEDULE"], b"\0\0\0\0")
            p2p.send(fp.CMD3["GET_AUTO_CALM"], b"\0\0\0\0")
            p2p.drain(1.5)
        if "schedule_enabled" in settings:
            sched = p2p.state.get("schedule")
            if isinstance(sched, dict) and "enabledDay" in sched and "schedule" in sched:
                body = {
                    "enable": bool(settings["schedule_enabled"]),
                    "enabledDay": sched["enabledDay"],
                    "schedule": sched["schedule"],
                }
                sent["schedule_enabled"] = fp.CMD3["SET_CAMERA_SCHEDULE"]
                p2p.send(fp.CMD3["SET_CAMERA_SCHEDULE"], json.dumps(body).encode() + b"\0")
        if "calm_enabled" in settings:
            calm = p2p.state.get("auto_calm")
            if isinstance(calm, dict):
                body = {
                    "enable": 1 if settings["calm_enabled"] else 0,
                    "startAudio": int(calm.get("startAudio", 0)),
                    "treatToss": int(calm.get("treatToss", 0)),
                }
                buf = bytearray(1024)
                payload = json.dumps(body).encode()
                buf[: len(payload)] = payload
                sent["calm_enabled"] = fp.CMD3["SET_AUTO_CALM"]
                p2p.send(fp.CMD3["SET_AUTO_CALM"], bytes(buf))
        return sent

    def pan(self, direction: str, degrees: int) -> None:
        with self._lock:
            p2p = self._ensure()
            if p2p.proto != "v3":
                raise BridgeUnavailable("pan is only available on V3 cameras")
            p2p.send(fp.CMD3["PAN"], bytes([fp.PAN_DIR[direction], degrees, 0, 0, 0, 0]))
            p2p.drain(1.0)

    def toss(self) -> None:
        with self._lock:
            p2p = self._ensure()
            cmd = fp.CMD3 if p2p.proto == "v3" else fp.CMD
            p2p.send(cmd["TOSS"])
            p2p.drain(1.0)

    def treat_sound(self) -> None:
        with self._lock:
            p2p = self._ensure()
            cmd = fp.CMD3 if p2p.proto == "v3" else fp.CMD
            p2p.send(cmd["PLAY_TREAT_SOUND"])
            p2p.drain(1.0)

    # -- video ---------------------------------------------------------------

    def open_stream(self, quality: str) -> None:
        """Ask the camera to start video on the session we already hold."""
        with self._lock:
            if self._streaming:
                raise StreamBusy("a viewer is already streaming")
            p2p = self._ensure()
            number = (fp.QUALITY_V3 if p2p.proto == "v3" else fp.QUALITY)[quality]
            # Clear any stream the camera still believes it is serving. After an
            # unclean exit it holds one for the client that went away, and a
            # start request arriving on top of that returns no data at all.
            p2p.send(fp.IPCAM_STOP, struct.pack("<i", 0))
            time.sleep(STREAM_RESTART_SETTLE)
            p2p.send(fp.IPCAM_START, struct.pack("<i", number))
            self._stream_stop.clear()
            self._streaming = True
            _LOGGER.info("video started at %s over %s", quality, p2p.mode or "unknown")

    def iter_frames(self) -> Iterator[bytes]:
        """Yield H.264 frames until the viewer leaves or the session drops.

        Deliberately does not take _lock, so control commands keep working
        while video runs. Ends quietly on any SDK error; go2rtc reconnects.
        """
        p2p = self._p2p
        if p2p is None:
            return
        buf = fp.create_string_buffer(FRAME_BUFFER_BYTES)
        info = fp.FrameInfo()
        self._reader_active.set()
        started = time.monotonic()
        seen_frame = False
        # What the SDK kept telling us, so a stream that yields nothing can say
        # why instead of spinning in silence.
        outcomes: dict[int, int] = {}
        biggest_expected = 0
        try:
            while not self._stream_stop.is_set():
                ret, size, expected = p2p.recv_frame(buf, info)
                if ret >= 0 and size > 0:
                    seen_frame = True
                    yield buf.raw[:size]
                    continue
                outcomes[ret] = outcomes.get(ret, 0) + 1
                biggest_expected = max(biggest_expected, expected)
                # The give-up covers every way of not getting a frame, not just
                # "no data": a camera whose frames all arrive lost or
                # incomplete never reaches that branch.
                if not seen_frame and time.monotonic() - started > FIRST_FRAME_TIMEOUT:
                    _LOGGER.warning(
                        "no usable video %.0fs after the start command "
                        "(%s, largest frame expected %d bytes, buffer %d). "
                        "Giving up so the slot is free for the next attempt",
                        FIRST_FRAME_TIMEOUT,
                        ", ".join(f"{fp.err(c)} x{n}" for c, n in sorted(outcomes.items())),
                        biggest_expected,
                        FRAME_BUFFER_BYTES,
                    )
                    return
                if ret >= 0 or ret in (fp.AV_ER_DATA_NOREADY, fp.AV_ER_LOSED_THIS_FRAME):
                    # Sleeping matters: without it this loop hammers the SDK on
                    # the same channel the controls use, and they crawl.
                    time.sleep(0.005)
                elif ret != fp.AV_ER_INCOMPLETE_FRAME:
                    _LOGGER.info("video ended: %s", fp.err(ret))
                    return
        finally:
            self._reader_active.clear()
            if outcomes and not seen_frame:
                _LOGGER.info(
                    "video produced no frames: %s",
                    ", ".join(f"{fp.err(c)} x{n}" for c, n in sorted(outcomes.items())),
                )

    def close_stream(self) -> None:
        """Stop video, leaving the session up for the controls."""
        self._stream_stop.set()
        with self._lock:
            if not self._streaming:
                return
            self._streaming = False
            p2p = self._p2p
            if p2p is not None:
                with contextlib.suppress(Exception):
                    p2p.send(fp.IPCAM_STOP, struct.pack("<i", 0))
            _LOGGER.info("video stopped")


class CameraRegistry:
    """The cameras this bridge serves, each with its own worker.

    The first camera is the primary: it answers the unscoped API paths that a
    bridge serving one camera has always exposed, so an integration that has
    not learned about the scoped paths keeps working.
    """

    def __init__(self, workers: list[P2PWorker]) -> None:
        if not workers:
            raise SystemExit("no cameras to serve")
        self._workers = {w.key: w for w in workers}
        self.primary = workers[0]

    def __iter__(self) -> Iterator[P2PWorker]:
        return iter(self._workers.values())

    def __len__(self) -> int:
        return len(self._workers)

    def get(self, device_id: str | None) -> P2PWorker:
        """The worker for a camera, or the primary when none is named."""
        if device_id is None:
            return self.primary
        worker = self._workers.get(device_id)
        if worker is None:
            raise UnknownCamera(device_id)
        return worker


def build_workers(args: argparse.Namespace) -> list[P2PWorker]:
    """One worker per camera this bridge should serve.

    With no --device the account's cameras are all served, which is what an
    unconfigured add-on now does instead of refusing to guess between them.
    """
    # --device stays a single string for the other subcommands; here it also
    # accepts a comma-separated list, so the add-on's device_id option can pin
    # a subset without a new option.
    wanted = [d.strip() for d in (args.device or "").split(",") if d.strip()]
    known = {d["device_id"]: d for d in fp.session_devices()}
    if wanted:
        missing = [d for d in wanted if d not in known]
        if missing:
            available = ", ".join(known) or "none"
            raise SystemExit(
                f"device_id not found on this account: {', '.join(missing)}; available: {available}"
            )
        chosen = wanted
    else:
        chosen = list(known)
    if not chosen:
        raise SystemExit("the account has no cameras")
    return [P2PWorker(args, device_id) for device_id in chosen]


# --- HTTP ------------------------------------------------------------------


def _bad_request(detail: str) -> web.Response:
    return web.json_response({"error": "bad_request", "detail": detail}, status=400)


def _parse_settings(body: Any) -> dict[str, Any]:
    """Validate a settings body. Returns the accepted subset or raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    out: dict[str, Any] = {}
    for key in (
        "camera_on",
        "auto_tracking",
        "auto_zoom",
        "voice_control",
        "schedule_enabled",
        "calm_enabled",
    ):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = body[key]
    if "volume" in body:
        volume = body["volume"]
        if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
            raise ValueError("volume must be an integer from 0 to 100")
        out["volume"] = volume
    if "night_mode" in body:
        if body["night_mode"] not in NIGHT_MODES:
            raise ValueError(f"night_mode must be one of {', '.join(NIGHT_MODES)}")
        out["night_mode"] = body["night_mode"]
    if "bark_sensitivity" in body:
        if body["bark_sensitivity"] not in BARK_LEVELS:
            raise ValueError(f"bark_sensitivity must be one of {', '.join(BARK_LEVELS)}")
        out["bark_sensitivity"] = body["bark_sensitivity"]
    if "treat_size" in body:
        if body["treat_size"] not in TREAT_SIZES:
            raise ValueError(f"treat_size must be one of {', '.join(TREAT_SIZES)}")
        out["treat_size"] = body["treat_size"]
    if "quality" in body:
        if body["quality"] not in VIDEO_QUALITIES:
            raise ValueError(f"quality must be one of {', '.join(VIDEO_QUALITIES)}")
        out["quality"] = body["quality"]
    unknown = set(body) - set(out)
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
    if not out:
        raise ValueError("no settings given")
    return out


def create_app(
    cameras: CameraRegistry,
    token: str | None,
    executors: dict[str, ThreadPoolExecutor],
) -> web.Application:
    """Build the aiohttp application around the cameras this bridge serves.

    Every operation exists twice: under /api/cameras/{device_id}/... naming a
    camera, and unscoped at /api/... for the primary one. The unscoped paths
    are what a bridge serving a single camera has always exposed, so an
    integration that predates multiple cameras keeps working unchanged.
    """

    def status_document(worker: Any) -> dict[str, Any]:
        return {
            "connected": worker.connected,
            "updated_at": worker.updated_at,
            "last_error": worker.last_error,
            "device": worker.device,
            "session_mode": worker.session_mode,
            "streaming": worker.streaming,
            "needs_login": worker.needs_login,
            "state": {**worker.state, "quality": read_quality(worker.key)},
        }

    def target(request: web.Request) -> Any:
        """The camera this request is about: named in the path, or the primary."""
        return cameras.get(request.match_info.get("device_id"))

    async def run(worker: Any, fn: Any, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executors[worker.key], fn, *args)

    async def read_json(request: web.Request) -> Any:
        if request.content_length in (None, 0):
            return {}
        try:
            return await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("body is not valid JSON") from exc

    expected_header = f"Bearer {token}" if token else None

    @web.middleware
    async def auth_and_errors(request: web.Request, handler: Any) -> web.StreamResponse:
        if expected_header is not None:
            # Constant-time compare so a wrong token cannot be recovered by
            # timing. A missing header is rejected the same as a wrong one.
            presented = request.headers.get("Authorization", "")
            if not hmac.compare_digest(presented, expected_header):
                return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except ValueError as exc:
            return _bad_request(str(exc))
        except UnknownCamera as exc:
            return web.json_response(
                {"error": "unknown_camera", "detail": f"no camera {exc}"}, status=404
            )
        except StreamBusy as exc:
            return web.json_response({"error": "stream_busy", "detail": str(exc)}, status=409)
        except BridgeUnavailable as exc:
            return web.json_response({"error": "p2p_unavailable", "detail": str(exc)}, status=503)

    async def get_cameras(request: web.Request) -> web.Response:
        """Every camera this bridge serves, so a client can find the rest."""
        return web.json_response(
            {
                "primary": cameras.primary.key,
                "cameras": [
                    {
                        "device_id": w.key,
                        "name": w.device.get("name") or w.key,
                        "product": w.device.get("product") or "",
                        "stream": stream_name(w.key),
                        "connected": w.connected,
                    }
                    for w in cameras
                ],
            }
        )

    async def get_status(request: web.Request) -> web.Response:
        return web.json_response(status_document(target(request)))

    async def post_settings(request: web.Request) -> web.Response:
        worker = target(request)
        settings = _parse_settings(await read_json(request))
        await run(worker, worker.apply, settings)
        return web.json_response(status_document(worker))

    async def post_pan(request: web.Request) -> web.Response:
        worker = target(request)
        body = await read_json(request)
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        direction = body.get("direction")
        if direction not in PAN_DIRECTIONS:
            raise ValueError("direction must be left or right")
        degrees = body.get("degrees", 60)
        if isinstance(degrees, bool) or not isinstance(degrees, int) or not 1 <= degrees <= 180:
            raise ValueError("degrees must be an integer from 1 to 180")
        await run(worker, worker.pan, direction, degrees)
        return web.json_response({"ok": True})

    async def post_toss(request: web.Request) -> web.Response:
        worker = target(request)
        await run(worker, worker.toss)
        return web.json_response({"ok": True})

    async def post_treat_sound(request: web.Request) -> web.Response:
        worker = target(request)
        await run(worker, worker.treat_sound)
        return web.json_response({"ok": True})

    async def get_stream(request: web.Request) -> web.StreamResponse:
        """Serve H.264 from the live session for as long as the viewer stays."""
        worker = target(request)
        quality = read_quality(worker.key)
        await run(worker, worker.open_stream, quality)
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "video/H264", "Cache-Control": "no-store"}
        )
        await response.prepare(request)

        loop = asyncio.get_running_loop()
        frames = FrameQueue()

        def pump() -> None:
            # Runs on its own thread so the single control executor stays free.
            try:
                for frame in worker.iter_frames():
                    loop.call_soon_threadsafe(frames.offer, frame)
            finally:
                loop.call_soon_threadsafe(frames.offer, None)

        reader = threading.Thread(target=pump, name=f"furbo-video-{worker.key}", daemon=True)
        reader.start()
        try:
            while True:
                frame = await frames.get()
                if frame is None:
                    break
                await response.write(frame)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await run(worker, worker.close_stream)
            reader.join(timeout=5)
            if frames.dropped:
                _LOGGER.warning("dropped %d frames: the viewer could not keep up", frames.dropped)
        return response

    app = web.Application(middlewares=[auth_and_errors])
    routes = [web.get("/api/cameras", get_cameras)]
    # Each operation twice: unscoped for the primary camera (what a
    # single-camera bridge has always served) and scoped by device id.
    for path, method, handler in (
        ("status", web.get, get_status),
        ("stream", web.get, get_stream),
        ("settings", web.post, post_settings),
        ("pan", web.post, post_pan),
        ("toss", web.post, post_toss),
        ("treat-sound", web.post, post_treat_sound),
    ):
        routes.append(method(f"/api/{path}", handler))
        routes.append(method(f"/api/cameras/{{device_id}}/{path}", handler))
    app.add_routes(routes)
    return app


class FrameQueue:
    """Frames on their way from the reader thread to one viewer's response.

    Bounded, and drops frames when the viewer cannot keep up. The end-of-stream
    marker is never dropped: losing it leaves the response waiting for a frame
    that will never come, holding the camera's single video slot until the
    viewer disconnects. A viewer too slow to keep up is exactly when the reader
    gives up, so the full queue is the case that has to deliver it.
    """

    def __init__(self, maxsize: int = STREAM_QUEUE_MAX) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, frame: bytes | None) -> None:
        """Add a frame, or the end-of-stream marker. Never raises."""
        if frame is None:
            while self._queue.full():
                self._queue.get_nowait()
                self.dropped += 1
            self._queue.put_nowait(None)
            return
        if self._queue.full():
            self.dropped += 1
            return
        self._queue.put_nowait(frame)

    async def get(self) -> bytes | None:
        return await self._queue.get()


def stream_name(device_id: str) -> str:
    """The go2rtc stream for one camera."""
    return f"furbo_{device_id}"


# The primary camera also answers on the plain name a single-camera bridge has
# always published, so a stream URL already saved in someone's options keeps
# working after they gain a second camera.
LEGACY_STREAM = "furbo"


def render_go2rtc_config(template: str, device_ids: list[str]) -> str:
    """The go2rtc config for these cameras, built from the shipped template.

    The template holds everything that does not depend on which cameras exist
    (listeners, credentials, logging) and a `streams:` line to append to.

    No talkback stream is written. Talkback opened a P2P session of its own,
    competing with the one the bridge holds for video and controls, which is
    the failure this add-on exists to avoid. talk.sh stays in the image, ready
    to be wired back up once it reads from the shared session.
    """
    if not device_ids:
        raise SystemExit("no cameras to write a go2rtc config for")
    lines = [template.rstrip("\n"), ""]
    for index, device_id in enumerate(device_ids):
        names = [stream_name(device_id)]
        if index == 0:
            names.append(LEGACY_STREAM)
        for name in names:
            lines.append(f"  {name}:")
            lines.append(
                f'    - "exec:/app/stream.sh {{output}} {device_id}#killsignal=15#killtimeout=8"'
            )
    return "\n".join(lines) + "\n"


def write_go2rtc_config(args: argparse.Namespace) -> int:
    """Write the go2rtc config for the cameras this bridge will serve."""
    device_ids = [w.device_id or "default" for w in build_workers(args)]
    template = Path(args.template).read_text()
    Path(args.output).write_text(render_go2rtc_config(template, device_ids))
    _LOGGER.info("wrote %s for %d camera(s)", args.output, len(device_ids))
    return 0


async def _refresh_loop(worker: P2PWorker, executor: ThreadPoolExecutor, interval: float) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(executor, worker.refresh)
        except BridgeUnavailable as exc:
            _LOGGER.warning("P2P unavailable: %s", exc)
        except Exception:
            _LOGGER.exception("state refresh failed")
        await asyncio.sleep(interval)


async def serve(args: argparse.Namespace) -> None:
    """Run the HTTP bridge until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not args.token:
        # The API exposes camera video and physical controls; never run it
        # unauthenticated on the network.
        raise SystemExit("a --token is required; refusing to serve without one")
    cameras = CameraRegistry(build_workers(args))
    # One thread per camera. The worker serialises its own session with a lock,
    # so a shared pool would only queue one camera's slow poll behind another's.
    executors = {
        w.key: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"p2p-{w.key}") for w in cameras
    }
    app = create_app(cameras, args.token, executors)
    refreshers = [
        asyncio.create_task(_refresh_loop(w, executors[w.key], args.interval)) for w in cameras
    ]
    _LOGGER.info("serving %d camera(s): %s", len(cameras), ", ".join(w.key for w in cameras))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    _LOGGER.info("listening on http://%s:%s (bearer auth required)", args.host, args.port)

    # Supervisor stops the add-on with SIGTERM. Handle it so the P2P session is
    # closed on the camera instead of being left orphaned until it times out,
    # and so the container exits 0 rather than 143.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
        _LOGGER.info("stopping: closing %d P2P session(s)", len(cameras))
    finally:
        for task in refreshers:
            task.cancel()
        await runner.cleanup()
        loop = asyncio.get_running_loop()
        # Close every camera, even if one of them fails on the way out.
        await asyncio.gather(
            *(loop.run_in_executor(executors[w.key], w.close) for w in cameras),
            return_exceptions=True,
        )
        for executor in executors.values():
            executor.shutdown(wait=False)
        # Every session is closed; the SDK itself is process-wide, so it comes
        # down here rather than when any one camera closes.
        fp.deinitialize_sdk()


def add_arguments(sp: argparse.ArgumentParser) -> None:
    """Options for the serve subcommand (P2P options are added by the caller)."""
    sp.add_argument("--host", default="0.0.0.0", help="bind address, default all interfaces")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--token", default=None, help="bearer token clients must send (required)")
    sp.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="seconds between state refreshes",
    )
