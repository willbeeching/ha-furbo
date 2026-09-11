"""Saving the account's daily Doggie Diary videos into Home Assistant's media.

Furbo's diary report hands back a presigned link per day straight to an mp4,
so a day is fetched with one ordinary GET and no export job. The links are
short lived and signed with temporary credentials, which is why nothing here
stores one: a caller fetches the report and downloads in the same breath.

The report is a rolling week, so a run that was missed is not a day lost. Days
already on disk are skipped, which makes pressing the button twice, or running
it daily from an automation, harmless.

Every filesystem call goes through the executor: this is video, over someone's
home broadband, onto whatever storage their Home Assistant runs on.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

# Under Home Assistant's local media directory, so the videos appear in the
# media browser and play in the UI.
DIARY_DIR = "furbo_diary"
# Generous: these are day-long timelapses over a home connection.
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600)
CHUNK = 256 * 1024
# The date becomes a filename, and it arrives from the cloud. Nothing but a
# plain calendar date is accepted, so no value of DiaryDate can climb out of
# the diary folder or name a file it should not.
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The account id is a Furbo value and also becomes a path segment, so it is
# held to the same rule. Anything else falls back to the config entry id,
# which Home Assistant generates and is always safe.
ACCOUNT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DiaryError(HomeAssistantError):
    """A diary video could not be saved."""


def diary_folder(hass: HomeAssistant, account: str, fallback: str) -> Path:
    """Return the folder one account's diary videos belong in.

    Scoped per account, because two accounts produce a video for the same
    date. Sharing a folder would not merely mix them up: the second account's
    video is skipped as already saved, and quietly never arrives.

    Raises when the installation has no local media directory. That is worth
    saying plainly rather than inventing a path: somewhere outside the media
    tree would hide the videos from the media browser, which is the whole
    point of putting them there.
    """
    local = hass.config.media_dirs.get("local")
    if not local:
        raise DiaryError(
            "Home Assistant has no local media directory, so there is nowhere "
            "to save the diary. Add one under media_dirs in configuration.yaml."
        )
    name = account if ACCOUNT.match(account) else fallback
    return Path(local) / DIARY_DIR / name


def _video(day: dict[str, Any]) -> tuple[str, str] | None:
    """Return a day's date and video link, or None when it has neither."""
    date = day.get("DiaryDate")
    url = day.get("TimeLapseUrl")
    if not isinstance(date, str) or not DATE.match(date):
        return None
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return date, url


def missing(folder: Path, days: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return this report's (date, url) pairs that are not on disk yet.

    Blocking: call it from the executor.
    """
    folder.mkdir(parents=True, exist_ok=True)
    found = []
    for day in days:
        video = _video(day)
        if video is None:
            continue
        date, url = video
        if not (folder / f"{date}.mp4").exists():
            found.append((date, url))
    return found


async def async_save(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    folder: Path,
    date: str,
    url: str,
) -> Path:
    """Download one day's video and return where it landed.

    Written under a partial name and moved into place once complete, so an
    interrupted download cannot leave a half file that the next run then
    mistakes for a day already saved.
    """
    target = folder / f"{date}.mp4"
    partial = folder / f"{date}.mp4.part"
    try:
        async with session.get(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            if resp.status != 200:
                # These URLs are signed and expiring, so a refusal here is
                # usually a stale link rather than anything wrong with the
                # account. The response body is Amazon's, and not logged.
                raise DiaryError(f"Furbo returned {resp.status} for the {date} video")
            handle = await hass.async_add_executor_job(partial.open, "wb")
            try:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    await hass.async_add_executor_job(handle.write, chunk)
            finally:
                await hass.async_add_executor_job(handle.close)
    except (aiohttp.ClientError, TimeoutError) as err:
        await hass.async_add_executor_job(partial.unlink, True)
        raise DiaryError(
            f"Could not download the {date} video: {type(err).__name__}"
        ) from err
    await hass.async_add_executor_job(partial.replace, target)
    return target
