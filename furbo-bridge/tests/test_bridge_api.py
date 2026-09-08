"""Tests for the Furbo Bridge HTTP API.

The focus is the security boundary: without a valid bearer token, no status,
control, or settings request may reach the camera. These run the real aiohttp
app against a fake P2P worker, so no TUTK library or network is needed.

The tests drive aiohttp with ``asyncio.run`` directly rather than a pytest
plugin, so the suite needs only pytest + aiohttp.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from aiohttp.test_utils import TestClient, TestServer
import pytest

import furbo_bridge as fb

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeWorker:
    """A stand-in P2P worker that records control calls instead of acting."""

    def __init__(self) -> None:
        self.connected = True
        self.updated_at = 123.0
        self.last_error: str | None = None
        self.device = {
            "id": "UID",
            "device_id": "12345",
            "name": "Furbo",
            "product": "FB0030",
        }
        self.state: dict[str, Any] = {"camera_on": True, "volume": 50}
        self.session_mode: str | None = "LAN"
        self.streaming = False
        self.needs_login = False
        self.frames: list[bytes] = []
        self.busy = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    # -- video ---------------------------------------------------------------

    def open_stream(self, quality: str) -> None:
        if self.busy:
            raise fb.StreamBusy("a viewer is already streaming")
        self.calls.append(("open_stream", (quality,)))
        self.streaming = True

    def iter_frames(self):
        yield from self.frames
        self.streaming = False

    def close_stream(self) -> None:
        self.calls.append(("close_stream", ()))
        self.streaming = False

    def apply(self, settings: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("apply", (settings,)))
        return self.state

    def pan(self, direction: str, degrees: int) -> None:
        self.calls.append(("pan", (direction, degrees)))

    def toss(self) -> None:
        self.calls.append(("toss", ()))

    def treat_sound(self) -> None:
        self.calls.append(("treat_sound", ()))


@asynccontextmanager
async def _client(token: str | None = TOKEN):
    """Yield a TestClient wrapping the real app around a FakeWorker."""
    worker = FakeWorker()
    executor = ThreadPoolExecutor(max_workers=1)
    app = fb.create_app(worker, token, executor)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, worker
    finally:
        await client.close()
        executor.shutdown(wait=False)


def _run(coro_fn: Callable[[Any, Any], Awaitable[None]], token: str | None = TOKEN) -> None:
    async def inner() -> None:
        async with _client(token) as (client, worker):
            await coro_fn(client, worker)

    asyncio.run(inner())


# --- authentication: the security boundary ---------------------------------

ENDPOINTS = [
    ("get", "/api/status"),
    ("post", "/api/settings"),
    ("post", "/api/pan"),
    ("post", "/api/toss"),
    ("post", "/api/treat-sound"),
]


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_no_token_is_rejected(method: str, path: str) -> None:
    """Every endpoint returns 401 with no Authorization header."""

    async def check(client: TestClient, worker: FakeWorker) -> None:
        resp = await client.request(method.upper(), path)
        assert resp.status == 401
        assert (await resp.json())["error"] == "unauthorized"
        # Nothing reached the camera.
        assert worker.calls == []

    _run(check)


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_wrong_token_is_rejected(method: str, path: str) -> None:
    """A wrong bearer token returns 401 and never reaches the worker."""

    async def check(client: TestClient, worker: FakeWorker) -> None:
        resp = await client.request(method.upper(), path, headers={"Authorization": "Bearer nope"})
        assert resp.status == 401
        assert worker.calls == []

    _run(check)


def test_malformed_auth_header_is_rejected() -> None:
    """A non-bearer Authorization value is rejected like a missing one."""

    async def check(client: TestClient, worker: FakeWorker) -> None:
        for value in ("", "Basic abc", TOKEN, f"bearer {TOKEN}"):
            resp = await client.get("/api/status", headers={"Authorization": value})
            assert resp.status == 401
        assert worker.calls == []

    _run(check)


# --- authorized happy paths -------------------------------------------------


def test_status_with_token() -> None:
    async def check(client: TestClient, worker: FakeWorker) -> None:
        resp = await client.get("/api/status", headers=AUTH)
        assert resp.status == 200
        body = await resp.json()
        assert body["connected"] is True
        assert body["device"]["product"] == "FB0030"
        assert body["device"]["device_id"] == "12345"
        assert body["state"]["camera_on"] is True
        assert "quality" in body["state"]

    _run(check)


def test_toss_with_token_reaches_worker() -> None:
    async def check(client: TestClient, worker: FakeWorker) -> None:
        resp = await client.post("/api/toss", headers=AUTH)
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True}
        assert ("toss", ()) in worker.calls

    _run(check)


def test_settings_with_token_validates_and_applies() -> None:
    async def check(client: TestClient, worker: FakeWorker) -> None:
        resp = await client.post(
            "/api/settings", headers=AUTH, json={"volume": 30, "night_mode": "on"}
        )
        assert resp.status == 200
        assert worker.calls == [("apply", ({"volume": 30, "night_mode": "on"},))]

    _run(check)


def test_settings_bad_body_is_400() -> None:
    async def check(client: TestClient, worker: FakeWorker) -> None:
        resp = await client.post("/api/settings", headers=AUTH, json={"volume": 999})
        assert resp.status == 400
        assert (await resp.json())["error"] == "bad_request"
        assert worker.calls == []

    _run(check)


def test_pan_validates_direction() -> None:
    async def check(client: TestClient, worker: FakeWorker) -> None:
        bad = await client.post("/api/pan", headers=AUTH, json={"direction": "up"})
        assert bad.status == 400
        ok = await client.post("/api/pan", headers=AUTH, json={"direction": "left", "degrees": 45})
        assert ok.status == 200
        assert ("pan", ("left", 45)) in worker.calls

    _run(check)


def test_unavailable_worker_is_503() -> None:
    """A worker that cannot reach the camera surfaces as 503, not 500."""

    async def check(client: TestClient, worker: FakeWorker) -> None:
        def boom() -> None:
            raise fb.BridgeUnavailable("camera offline")

        worker.toss = boom  # type: ignore[method-assign]
        resp = await client.post("/api/toss", headers=AUTH)
        assert resp.status == 503
        assert (await resp.json())["error"] == "p2p_unavailable"

    _run(check)


# --- serve() refuses to run without a token --------------------------------


def test_serve_refuses_without_token() -> None:
    """serve() exits rather than exposing an unauthenticated API."""
    args = argparse.Namespace(token=None, host="0.0.0.0", port=8791, interval=30.0)
    with pytest.raises(SystemExit):
        asyncio.run(fb.serve(args))


def test_serve_refuses_empty_token() -> None:
    args = argparse.Namespace(token="", host="0.0.0.0", port=8791, interval=30.0)
    with pytest.raises(SystemExit):
        asyncio.run(fb.serve(args))


# --- video ------------------------------------------------------------------


def test_stream_serves_frames_from_the_single_session() -> None:
    """The endpoint starts video on the live session and streams the frames."""
    frames = [b"\x00\x00\x00\x01frame-one", b"\x00\x00\x00\x01frame-two"]
    seen: dict[str, Any] = {}

    async def scenario(client: Any, worker: Any) -> None:
        worker.frames = list(frames)
        resp = await client.get("/api/stream", headers=AUTH)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "video/H264"
        seen["body"] = await resp.read()
        seen["calls"] = [name for name, _ in worker.calls]

    _run(scenario)
    assert seen["body"] == b"".join(frames)
    assert "open_stream" in seen["calls"]
    # The session is always released, so the controls keep working after.
    assert seen["calls"][-1] == "close_stream"


def test_stream_uses_the_configured_quality() -> None:
    """Video starts at whatever the quality file currently holds."""
    fb.write_quality("720p")
    try:
        calls: dict[str, Any] = {}

        async def scenario(client: Any, worker: Any) -> None:
            worker.frames = [b"x"]
            await (await client.get("/api/stream", headers=AUTH)).read()
            calls["all"] = worker.calls

        _run(scenario)
        assert ("open_stream", ("720p",)) in calls["all"]
    finally:
        fb.write_quality("1080p")


def test_stream_requires_the_token() -> None:
    """Video is behind the same bearer token as the controls."""

    async def scenario(client: Any, worker: Any) -> None:
        assert (await client.get("/api/stream")).status == 401
        assert not worker.calls

    _run(scenario)


def test_second_viewer_is_refused() -> None:
    """One viewer at a time; a second gets 409 rather than a broken session."""

    async def scenario(client: Any, worker: Any) -> None:
        worker.busy = True
        resp = await client.get("/api/stream", headers=AUTH)
        assert resp.status == 409
        assert (await resp.json())["error"] == "stream_busy"

    _run(scenario)


def test_status_reports_the_session_path() -> None:
    """The status document exposes the path, so a relayed session is visible."""

    async def scenario(client: Any, worker: Any) -> None:
        worker.session_mode = "relay"
        body = await (await client.get("/api/status", headers=AUTH)).json()
        assert body["session_mode"] == "relay"
        assert body["streaming"] is False
        assert body["needs_login"] is False

    _run(scenario)
