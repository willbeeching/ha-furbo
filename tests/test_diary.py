"""Saving Doggie Diary videos: what is skipped, what is written, what fails."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.furbo.api import FurboError
from custom_components.furbo.diary import (
    DiaryError,
    async_save,
    diary_folder,
    missing,
)

from .conftest import setup_integration

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
        diary_folder(hass)


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

    assert _names(tmp_path / "furbo_diary") == ["2026-09-04.mp4", "2026-09-05.mp4"]
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
    folder = tmp_path / "furbo_diary"
    folder.mkdir()
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
