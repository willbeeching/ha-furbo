#!/usr/bin/env python3
"""HTTP bridge in front of the Furbo P2P session, for Home Assistant.

Runs one long-lived TUTK session to a single camera on a worker thread and
exposes its state and controls over a small JSON API. The Home Assistant
integration on branch ``claude/furbo-integration`` talks to this API; it
never loads the TUTK library itself. Video is not served here: go2rtc runs
``furbo_p2p.py stream`` as an exec source for that.

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

Errors: 400 {"error": "bad_request", "detail": "..."}, 401 {"error":
"unauthorized"}, 503 {"error": "p2p_unavailable", "detail": "..."} when the
session cannot be (re)established. The detail is a short reason, never a
response body from the camera or the cloud.
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import threading
import time
from typing import Any

from aiohttp import web

import furbo_p2p as fp

_LOGGER = logging.getLogger("furbo_bridge")

DEFAULT_PORT = 8791
DEFAULT_INTERVAL = 30.0
NIGHT_MODES = ("auto", "on", "off")
BARK_LEVELS = ("off", "low", "medium", "high")
PAN_DIRECTIONS = ("left", "right")


class BridgeUnavailable(Exception):
    """The P2P session is not available; the reason is safe to show."""


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
    return out


class P2PWorker:
    """Owns the one P2P session. Every method runs on the worker thread."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._p2p: fp.FurboP2P | None = None
        self._lock = threading.Lock()
        self.device: dict[str, str] = {}
        self.state: dict[str, Any] = {}
        self.updated_at: float | None = None
        self.last_error: str | None = None

    # -- session -----------------------------------------------------------

    def _ensure(self) -> fp.FurboP2P:
        if self._p2p is not None and self._p2p.alive():
            return self._p2p
        self._drop()
        try:
            creds = asyncio.run(fp.fetch_p2p_credentials(self._args.device))
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
            raise BridgeUnavailable(str(exc)) from None
        self.device = {"id": creds["uid"], "name": creds["name"], "product": creds["product"]}
        self._p2p = p2p
        self.last_error = None
        return p2p

    def _drop(self) -> None:
        if self._p2p is not None:
            try:
                self._p2p.close()
            except Exception:  # noqa: BLE001 - closing a dead session may fail
                pass
            self._p2p = None

    @property
    def connected(self) -> bool:
        return self._p2p is not None

    def close(self) -> None:
        with self._lock:
            self._drop()

    # -- operations ----------------------------------------------------------

    def _readback(self, p2p: fp.FurboP2P, wait: float) -> dict[str, Any]:
        p2p.state.pop("rejected", None)
        self.state = normalise_state(p2p.query_state(wait=wait))
        self.updated_at = time.time()
        return self.state

    def refresh(self) -> dict[str, Any]:
        with self._lock:
            p2p = self._ensure()
            return self._readback(p2p, 2.0)

    def apply(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            p2p = self._ensure()
            cmd = fp.CMD3 if p2p.proto == "v3" else fp.CMD
            if "camera_on" in settings:
                key = "SET_CAMERA_ON" if p2p.proto == "v3" else "SET_FURBO_POWER"
                p2p.send(cmd[key], bytes([1 if settings["camera_on"] else 0, 0, 0, 0]))
            if "volume" in settings:
                p2p.send(cmd["SET_VOLUME"], bytes([settings["volume"], 0, 0, 0]))
            if "night_mode" in settings:
                code = {v: k for k, v in fp.NIGHT_MODES.items()}[settings["night_mode"]]
                p2p.send(cmd["SET_NIGHT_VISION"], bytes([code, 0, 0, 0]))
            if "bark_sensitivity" in settings:
                level = settings["bark_sensitivity"]
                if p2p.proto == "v3":
                    code = fp.BARK_V3[level]
                else:
                    code = {v: k for k, v in fp.SENSITIVITY.items()}.get(level, 0)
                p2p.send(cmd["SET_BARKING"], bytes([code, 0, 0, 0]))
            if p2p.proto == "v3" and "auto_tracking" in settings:
                on = 1 if settings["auto_tracking"] else 0
                p2p.send(fp.CMD3["SET_AUTO_TRACKING"], bytes([on, on, on, 0]))
            if p2p.proto == "v3" and "auto_zoom" in settings:
                on = 1 if settings["auto_zoom"] else 0
                p2p.send(fp.CMD3["SET_AUTO_ZOOM"], bytes([on, on, 0, 0]))
            p2p.drain(2.0)
            return self._readback(p2p, 1.5)

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


# --- HTTP ------------------------------------------------------------------


def _bad_request(detail: str) -> web.Response:
    return web.json_response({"error": "bad_request", "detail": detail}, status=400)


def _parse_settings(body: Any) -> dict[str, Any]:
    """Validate a settings body. Returns the accepted subset or raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    out: dict[str, Any] = {}
    for key in ("camera_on", "auto_tracking", "auto_zoom"):
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
    unknown = set(body) - set(out)
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
    if not out:
        raise ValueError("no settings given")
    return out


def create_app(worker: Any, token: str | None, executor: ThreadPoolExecutor) -> web.Application:
    """Build the aiohttp application around a worker (real or fake)."""

    def status_document() -> dict[str, Any]:
        return {
            "connected": worker.connected,
            "updated_at": worker.updated_at,
            "last_error": worker.last_error,
            "device": worker.device,
            "state": worker.state,
        }

    async def run(fn: Any, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, fn, *args)

    async def read_json(request: web.Request) -> Any:
        if request.content_length in (None, 0):
            return {}
        try:
            return await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("body is not valid JSON") from exc

    @web.middleware
    async def auth_and_errors(request: web.Request, handler: Any) -> web.StreamResponse:
        if token and request.headers.get("Authorization") != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except ValueError as exc:
            return _bad_request(str(exc))
        except BridgeUnavailable as exc:
            return web.json_response(
                {"error": "p2p_unavailable", "detail": str(exc)}, status=503
            )

    async def get_status(request: web.Request) -> web.Response:
        return web.json_response(status_document())

    async def post_settings(request: web.Request) -> web.Response:
        settings = _parse_settings(await read_json(request))
        await run(worker.apply, settings)
        return web.json_response(status_document())

    async def post_pan(request: web.Request) -> web.Response:
        body = await read_json(request)
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        direction = body.get("direction")
        if direction not in PAN_DIRECTIONS:
            raise ValueError("direction must be left or right")
        degrees = body.get("degrees", 60)
        if isinstance(degrees, bool) or not isinstance(degrees, int) or not 1 <= degrees <= 180:
            raise ValueError("degrees must be an integer from 1 to 180")
        await run(worker.pan, direction, degrees)
        return web.json_response({"ok": True})

    async def post_toss(request: web.Request) -> web.Response:
        await run(worker.toss)
        return web.json_response({"ok": True})

    async def post_treat_sound(request: web.Request) -> web.Response:
        await run(worker.treat_sound)
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[auth_and_errors])
    app.add_routes(
        [
            web.get("/api/status", get_status),
            web.post("/api/settings", post_settings),
            web.post("/api/pan", post_pan),
            web.post("/api/toss", post_toss),
            web.post("/api/treat-sound", post_treat_sound),
        ]
    )
    return app


async def _refresh_loop(worker: P2PWorker, executor: ThreadPoolExecutor, interval: float) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(executor, worker.refresh)
        except BridgeUnavailable as exc:
            _LOGGER.warning("P2P unavailable: %s", exc)
        except Exception:  # noqa: BLE001 - keep the loop alive whatever the SDK does
            _LOGGER.exception("state refresh failed")
        await asyncio.sleep(interval)


async def serve(args: argparse.Namespace) -> None:
    """Run the HTTP bridge until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="p2p")
    worker = P2PWorker(args)
    app = create_app(worker, args.token, executor)
    refresher = asyncio.create_task(_refresh_loop(worker, executor, args.interval))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    _LOGGER.info("listening on http://%s:%s (auth %s)", args.host, args.port, "on" if args.token else "off")
    try:
        await asyncio.Event().wait()
    finally:
        refresher.cancel()
        await runner.cleanup()
        await asyncio.get_running_loop().run_in_executor(executor, worker.close)
        executor.shutdown(wait=False)


def add_arguments(sp: argparse.ArgumentParser) -> None:
    """Options for the serve subcommand (P2P options are added by the caller)."""
    sp.add_argument("--host", default="0.0.0.0", help="bind address, default all interfaces")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--token", default=None, help="bearer token clients must send; strongly recommended")
    sp.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="seconds between state refreshes")
