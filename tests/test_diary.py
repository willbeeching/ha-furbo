"""Saving Doggie Diary videos: what is skipped, what is written, what fails."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.furbo.api import FurboError
from custom_components.furbo.diary import (
    DiaryError,
    async_save,
    diary_folder,
    missing,
)

from . import const as c
from .conftest import setup_integration
from .test_bridge_entities import BRIDGE_OPTIONS

VIDEO = "https://time-lapse-prod-de.s3.amazonaws.com/a/b.mp4?X-Amz-Signature=abc"


def _day(date: str, url: str = VIDEO) -> dict[str, object]:
    return {"DiaryDate": date, "TimeLapseUrl": url, "IsValid": True}


# Filesystem reads live in sync helpers: the tests that need them are async,
# and blocking calls do not belong in an async body even in a test.
def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def _contents(path: Path) -> bytes:
    return path.read_bytes()


def test_folder_needs_a_media_directory(hass: HomeAssistant) -> None:
    """Without a local media directory there is nowhere sensible to write."""
    hass.config.media_dirs = {}
    with pytest.raises(DiaryError, match="no local media directory"):
        diary_folder(hass, "acct", "entry")


def test_folder_is_per_account(hass: HomeAssistant, tmp_path: Path) -> None:
    """Two accounts produce a video for the same date, so they cannot share.

    Sharing does not just mix them up: the second account's video is skipped
    as already saved and never arrives at all.
    """
    hass.config.media_dirs = {"local": str(tmp_path)}
    assert diary_folder(hass, "acct-a", "entry") != diary_folder(
        hass, "acct-b", "entry"
    )
    assert diary_folder(hass, "acct-a", "entry").name == "acct-a"


def test_folder_falls_back_when_the_account_is_not_path_safe(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """The account id is Furbo's value and becomes a path segment."""
    hass.config.media_dirs = {"local": str(tmp_path)}
    root = tmp_path / "furbo_diary"
    for account in ("../../etc", "a/b", "", "has space"):
        folder = diary_folder(hass, account, "entry123")
        assert folder == root / "entry123"


def test_missing_skips_days_already_saved(tmp_path: Path) -> None:
    """A day on disk is not fetched again, which makes a daily run harmless."""
    (tmp_path / "2026-09-04.mp4").write_bytes(b"old")
    found = missing(tmp_path, [_day("2026-09-04"), _day("2026-09-05")])
    assert [date for date, _ in found] == ["2026-09-05"]


def test_missing_refuses_a_date_that_is_not_one(tmp_path: Path) -> None:
    """DiaryDate builds a filename and comes from the cloud, so it is checked.

    A path in that field would otherwise write outside the diary folder.
    """
    days = [
        _day("../../../etc/passwd"),
        _day("2026-09-05/../.."),
        _day("not-a-date"),
        {"DiaryDate": 20260905, "TimeLapseUrl": VIDEO},
        _day("2026-09-06"),
    ]
    assert [date for date, _ in missing(tmp_path, days)] == ["2026-09-06"]


def test_missing_skips_a_day_with_no_video(tmp_path: Path) -> None:
    """A day whose link is empty, absent or not https is passed over."""
    days = [
        _day("2026-09-04", ""),
        {"DiaryDate": "2026-09-05"},
        _day("2026-09-06", "http://insecure.example/a.mp4"),
    ]
    assert missing(tmp_path, days) == []


def test_missing_creates_the_folder(tmp_path: Path) -> None:
    """The diary folder does not have to exist beforehand."""
    folder = tmp_path / "nested" / "furbo_diary"
    assert missing(folder, []) == []
    assert folder.is_dir()


async def test_save_writes_the_video(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    """A downloaded day lands under its date, with no partial file left over."""
    aioclient_mock.get(VIDEO, content=b"mp4-bytes")
    saved = await async_save(
        hass, async_get_clientsession(hass), tmp_path, "2026-09-04", VIDEO
    )
    assert saved == tmp_path / "2026-09-04.mp4"
    assert _contents(saved) == b"mp4-bytes"
    assert _names(tmp_path) == ["2026-09-04.mp4"]


async def test_save_reports_a_stale_link(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    """These URLs expire, so a refusal is named rather than written to disk."""
    aioclient_mock.get(VIDEO, status=403, text="<Error>expired</Error>")
    with pytest.raises(DiaryError, match="returned 403"):
        await async_save(
            hass, async_get_clientsession(hass), tmp_path, "2026-09-04", VIDEO
        )
    assert _names(tmp_path) == []


async def test_save_leaves_nothing_behind_when_the_download_breaks(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    """A failed transfer must not leave a part file the next run mistakes for a day."""
    aioclient_mock.get(VIDEO, exc=TimeoutError)
    with pytest.raises(DiaryError, match="Could not download"):
        await async_save(
            hass, async_get_clientsession(hass), tmp_path, "2026-09-04", VIDEO
        )
    assert _names(tmp_path) == []


async def test_button_downloads_the_missing_days(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    tmp_path: Path,
) -> None:
    """Pressing it fetches a fresh report and saves the days not already there."""
    hass.config.media_dirs = {"local": str(tmp_path)}
    mock_client.get_diary_report.return_value = [
        _day("2026-09-04"),
        _day("2026-09-05"),
    ]
    aioclient_mock.get(VIDEO, content=b"mp4-bytes")
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.furbo_account_download_doggie_diary"},
        blocking=True,
    )

    folder = tmp_path / "furbo_diary" / c.ACCOUNT_ID
    assert _names(folder) == ["2026-09-04.mp4", "2026-09-05.mp4"]
    # The report is read at press time, never reused: the links expire.
    assert mock_client.get_diary_report.await_count == 1


async def test_button_says_when_the_report_cannot_be_read(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    tmp_path: Path,
) -> None:
    """A cloud refusal surfaces as an error on the press, not a silent no-op."""
    hass.config.media_dirs = {"local": str(tmp_path)}
    mock_client.get_diary_report.side_effect = FurboError("rate limited", code=80002)
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(DiaryError, match="Could not read the Doggie Diary"):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.furbo_account_download_doggie_diary"},
            blocking=True,
        )


async def test_button_does_nothing_when_every_day_is_saved(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    tmp_path: Path,
) -> None:
    """Pressing twice, or running it daily, downloads nothing the second time."""
    hass.config.media_dirs = {"local": str(tmp_path)}
    folder = tmp_path / "furbo_diary" / c.ACCOUNT_ID
    folder.mkdir(parents=True)
    (folder / "2026-09-04.mp4").write_bytes(b"already here")
    mock_client.get_diary_report.return_value = [_day("2026-09-04")]
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.furbo_account_download_doggie_diary"},
        blocking=True,
    )

    assert _contents(folder / "2026-09-04.mp4") == b"already here"
    assert aioclient_mock.call_count == 0


async def test_a_download_does_not_hold_up_a_camera_action(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    tmp_path: Path,
) -> None:
    """Tossing a treat must not wait behind a video.

    The diary button and the camera buttons are one platform, and it used to
    declare PARALLEL_UPDATES = 1: a single semaphore shared by all five. A
    download runs for as long as the video takes, up to ten minutes, and the
    camera has nothing to do with it.

    Pressed together, because that is when the semaphore applies: Home
    Assistant's entity service call takes a fast path for a single entity that
    skips it entirely (helpers/service.py, "Single entity case avoids creating
    task"). One call naming both is what an area or label target does.
    """
    hass.config.media_dirs = {"local": str(tmp_path)}
    mock_client.get_diary_report.return_value = [_day("2026-09-04")]

    tossed = asyncio.Event()

    async def slow_video(method: str, url: URL, data: object) -> Any:
        # Holds the download open until the treat has gone out. If the two
        # share a semaphore this waits for ever and the timeout below fires.
        await tossed.wait()
        return AiohttpClientMockResponse(method, url, response=b"mp4-bytes")

    async def toss() -> None:
        tossed.set()

    aioclient_mock.get(VIDEO, side_effect=slow_video)
    mock_bridge.async_toss.side_effect = toss
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)

    async with asyncio.timeout(10):
        await hass.services.async_call(
            "button",
            "press",
            {
                "entity_id": [
                    "button.furbo_account_download_doggie_diary",
                    "button.test_camera_toss_treat",
                ]
            },
            blocking=True,
        )

    assert mock_bridge.async_toss.await_count == 1
    folder = tmp_path / "furbo_diary" / c.ACCOUNT_ID
    assert _names(folder) == ["2026-09-04.mp4"]


async def test_camera_actions_still_go_one_at_a_time(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_bridge: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Dropping the platform limit must not let two actions into one session.

    They share the camera's single P2P session, so the serialising moved to
    the bridge coordinator's lock rather than going away.
    """
    running = 0
    overlapped = False

    async def slow_action(*args: object) -> None:
        nonlocal running, overlapped
        running += 1
        overlapped = overlapped or running > 1
        await asyncio.sleep(0)
        running -= 1

    mock_bridge.async_toss.side_effect = slow_action
    mock_bridge.async_pan.side_effect = slow_action
    await setup_integration(hass, mock_config_entry, BRIDGE_OPTIONS)

    await asyncio.gather(
        hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.test_camera_toss_treat"},
            blocking=True,
        ),
        hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.test_camera_pan_left"},
            blocking=True,
        ),
    )

    assert not overlapped


async def test_two_presses_do_not_race_on_the_same_file(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    tmp_path: Path,
) -> None:
    """An automation and a person can press at once; they must not collide.

    Both presses want the same date, so both write the same .part file and one
    moves it away while the other is still using it. Nothing serialised them
    once the platform-wide limit went, so the diary does its own.
    """
    hass.config.media_dirs = {"local": str(tmp_path)}
    mock_client.get_diary_report.return_value = [_day("2026-09-04")]

    arrived = 0

    async def slow_video(method: str, url: URL, data: object) -> Any:
        nonlocal arrived
        arrived += 1
        # Yield repeatedly, so a second press has every chance to reach the
        # same partial file while this one still holds it open. Unserialised,
        # it does: both fetch, and the first to finish moves the file out from
        # under the second, which fails with FileNotFoundError.
        for _ in range(5):
            await asyncio.sleep(0)
        return AiohttpClientMockResponse(method, url, response=b"mp4-bytes")

    aioclient_mock.get(VIDEO, side_effect=slow_video)
    await setup_integration(hass, mock_config_entry)

    press = {"entity_id": "button.furbo_account_download_doggie_diary"}
    await asyncio.gather(
        hass.services.async_call("button", "press", press, blocking=True),
        hass.services.async_call("button", "press", press, blocking=True),
    )

    folder = tmp_path / "furbo_diary" / c.ACCOUNT_ID
    assert _names(folder) == ["2026-09-04.mp4"]
    # The second press waits, finds the day saved, and fetches nothing.
    assert arrived == 1
