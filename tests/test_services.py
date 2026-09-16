"""The two actions: what they return, and what they refuse."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.furbo import async_setup
from custom_components.furbo.api import FurboError
from custom_components.furbo.const import DOMAIN

from . import const as c
from .conftest import setup_integration

WINDOW = {
    "start": datetime(2026, 9, 12, 19, 0, 0),
    "end": datetime(2026, 9, 13, 7, 0, 0),
}
# The same window as absolute moments, worked out by hand rather than by
# repeating the conversion under test. September, so London is on BST: the
# 19:00 an automation author writes is 18:00 UTC.
LONDON_START = 1789236000  # 2026-09-12T18:00:00Z
LONDON_END = 1789279200  # 2026-09-13T06:00:00Z
EVENT = {
    "Id": 91,
    "DeviceId": c.DEVICE_ID,
    "Caption": "standing in the doorway",
    "Videos": ["https://example.invalid/a.mp4"],
    "Thumbnail": "https://example.invalid/t.jpg",
}


def _london(hass: HomeAssistant) -> None:
    """Put Home Assistant on a timezone that is not the host's.

    Every assertion against LONDON_START and LONDON_END depends on this: with
    Home Assistant and the host agreeing, reading a naive time in the wrong
    one of them is invisible.
    """
    hass.config.time_zone = "Europe/London"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/London"))


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any]
) -> dict[str, Any] | None:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def test_get_events_returns_the_clips(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The point of the action: the caller gets the links, state does not."""
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = [dict(EVENT)]

    result = await _call(
        hass, "get_events", {"config_entry_id": mock_config_entry.entry_id, **WINDOW}
    )

    assert result == {"events": [EVENT]}
    # The window reaches the cloud as epoch seconds, overnight included.
    sent = mock_client.get_events.await_args.kwargs
    assert sent["start"] == LONDON_START
    assert sent["end"] == LONDON_END


async def test_a_bare_time_means_home_assistants_own_clock(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A window written without an offset is the user's local time.

    Asserted against fixed epoch seconds rather than against the same
    conversion the code does, which is what let this through: a test that
    calls .timestamp() on a naive datetime agrees with the bug.

    The trap is that a naive datetime is read in the timezone of the machine
    Home Assistant runs on, which for a container is UTC and has nothing to do
    with the person writing the automation. In September that is an hour of
    drift, so 19:00 fetched the clips from 20:00.
    """
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(
        hass, "get_events", {"config_entry_id": mock_config_entry.entry_id, **WINDOW}
    )

    sent = mock_client.get_events.await_args.kwargs
    assert sent["start"] == LONDON_START
    assert datetime.fromtimestamp(sent["start"], UTC).hour == 18


async def test_an_offset_is_honoured_not_overwritten(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A window that states its offset means exactly what it says."""
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    bst = timezone(timedelta(hours=1))
    await _call(
        hass,
        "get_events",
        {
            "config_entry_id": mock_config_entry.entry_id,
            "start": datetime(2026, 9, 12, 19, 0, 0, tzinfo=bst),
            "end": datetime(2026, 9, 13, 7, 0, 0, tzinfo=bst),
        },
    )

    sent = mock_client.get_events.await_args.kwargs
    assert sent["start"] == LONDON_START
    assert sent["end"] == LONDON_END


async def test_one_end_with_an_offset_and_one_without(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A mixed pair is a window, not a crash.

    Comparing a naive datetime with an aware one raises TypeError in Python,
    so before both ends were normalised this reached the user as an unhandled
    error rather than as clips.
    """
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(
        hass,
        "get_events",
        {
            "config_entry_id": mock_config_entry.entry_id,
            "start": datetime(2026, 9, 12, 19, 0, 0),
            "end": datetime(2026, 9, 13, 6, 0, 0, tzinfo=UTC),
        },
    )

    sent = mock_client.get_events.await_args.kwargs
    assert sent["start"] == LONDON_START
    assert sent["end"] == LONDON_END


async def test_get_events_refuses_a_backwards_window(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Caught here rather than sent to the cloud to be refused obscurely."""
    await setup_integration(hass, mock_config_entry)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "get_events",
            {
                "config_entry_id": mock_config_entry.entry_id,
                "start": WINDOW["end"],
                "end": WINDOW["start"],
            },
        )
    assert mock_client.get_events.await_count == 0


async def test_an_unknown_entry_is_named(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """An automation pointed at a deleted account should say so."""
    await setup_integration(hass, mock_config_entry)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "get_events", {"config_entry_id": "nope", **WINDOW})


async def test_a_cloud_refusal_reaches_the_caller(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Rate limiting and dead tokens are the automation author's to see."""
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.side_effect = FurboError("rate limited", code=80002)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "get_events",
            {"config_entry_id": mock_config_entry.entry_id, **WINDOW},
        )


async def test_get_insight_report_returns_a_day(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Dates go out as YYYY-MM-DD and the report comes back keyed by them."""
    await setup_integration(hass, mock_config_entry)
    mock_client.get_insight_report.return_value = {
        "2026-09-12": [{"Type": "Sleep", "Daily": {"hours": 9}}]
    }

    result = await _call(
        hass,
        "get_insight_report",
        {"config_entry_id": mock_config_entry.entry_id, "dates": ["2026-09-12"]},
    )

    assert result["report"]["2026-09-12"][0]["Type"] == "Sleep"
    assert mock_client.get_insight_report.await_args.args[0] == ["2026-09-12"]


async def test_the_actions_exist_without_a_loaded_entry(hass: HomeAssistant) -> None:
    """Registered in async_setup, so they exist with no entry set up at all.

    An automation calling an action that vanished with its entry is harder to
    diagnose than one told the entry is not loaded.
    """
    assert await async_setup(hass, {}) is True
    assert hass.services.has_service(DOMAIN, "get_events")
    assert hass.services.has_service(DOMAIN, "get_insight_report")


async def test_an_entry_that_is_not_loaded_says_so(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Signed out or failed to start: name the account rather than the symptom."""
    await setup_integration(hass, mock_config_entry)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "get_events",
            {"config_entry_id": mock_config_entry.entry_id, **WINDOW},
        )


async def test_a_refused_insight_report_reaches_the_caller(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The calendar host rate-limits hard; do not swallow that."""
    await setup_integration(hass, mock_config_entry)
    mock_client.get_insight_report.side_effect = FurboError("rate", code=80002)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "get_insight_report",
            {"config_entry_id": mock_config_entry.entry_id, "dates": ["2026-09-12"]},
        )


async def test_the_account_is_the_default_camera_list(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A call that names no camera still names every camera.

    The cloud requires DeviceIds and refuses a request without it as a bare
    12001, the same code it gives for any missing field. Leaving it to the
    caller meant every call made from the UI, where it is not obvious the
    field matters, failed with nothing to explain it.
    """
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(
        hass, "get_events", {"config_entry_id": mock_config_entry.entry_id, **WINDOW}
    )

    assert mock_client.get_events.await_args.kwargs["device_ids"] == [c.DEVICE_ID]


async def test_a_named_camera_wins_over_the_default(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Asking for one camera does not quietly widen to the whole account."""
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(
        hass,
        "get_events",
        {
            "config_entry_id": mock_config_entry.entry_id,
            "device_ids": ["a-named-camera"],
            **WINDOW,
        },
    )

    assert mock_client.get_events.await_args.kwargs["device_ids"] == ["a-named-camera"]


async def test_an_event_name_the_cloud_does_not_know_is_refused_here(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Caught locally, because the cloud's answer names nothing.

    An unknown event name comes back as the same bare 12001 as a missing
    field, so a typo would otherwise be indistinguishable from a bug.
    """
    _london(hass)
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(vol.Invalid):
        await _call(
            hass,
            "get_events",
            {
                "config_entry_id": mock_config_entry.entry_id,
                "event_names": ["NotARealEvent"],
                **WINDOW,
            },
        )
    assert mock_client.get_events.await_count == 0


async def test_a_call_with_no_window_asks_for_no_window(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Both ends may be left out, and then neither is sent.

    The cloud accepts a call with no window, and asking that way is the only
    test that separates a window we are sending wrongly from a week with
    nothing in it: both otherwise come back as no events at all.
    """
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(hass, "get_events", {"config_entry_id": mock_config_entry.entry_id})

    sent = mock_client.get_events.await_args.kwargs
    assert sent["start"] is None
    assert sent["end"] is None
    assert sent["device_ids"] == [c.DEVICE_ID]


async def test_one_end_of_the_window_may_be_given_alone(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Everything since a moment, with no end to it."""
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(
        hass,
        "get_events",
        {"config_entry_id": mock_config_entry.entry_id, "start": WINDOW["start"]},
    )

    sent = mock_client.get_events.await_args.kwargs
    assert sent["start"] == LONDON_START
    assert sent["end"] is None


async def test_the_enabled_alerts_are_sent_when_no_event_name_is_given(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The app never omits EventNames, and nor should we.

    Leaving the field out returns events on some accounts and nothing at all
    on others, a difference invisible from the outside. Disabled alerts are
    left out, and the "Frequency:*" cooldown keys are not event names at all.
    """
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(
        hass, "get_events", {"config_entry_id": mock_config_entry.entry_id, **WINDOW}
    )

    # PersonDetection is "0" in the fixture, so it is absent; the order is
    # EVENT_NAMES' own, so two identical calls send an identical request.
    assert mock_client.get_events.await_args.kwargs["event_names"] == [
        "Barking",
        "ContinuousBarking",
        "Crying",
        "Selfie",
        "DogMoveAbove10Sec",
    ]


async def test_a_named_event_wins_over_the_enabled_alerts(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Asking for one kind of event does not quietly widen to all of them."""
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(
        hass,
        "get_events",
        {
            "config_entry_id": mock_config_entry.entry_id,
            "event_names": ["Barking"],
            **WINDOW,
        },
    )

    assert mock_client.get_events.await_args.kwargs["event_names"] == ["Barking"]


async def test_a_cat_camera_can_be_asked_for_its_own_events(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A cat camera reports Meowing and CatSelfie, never Barking or Selfie.

    Validating against the alert switches' list rejected every name an
    FBC0030 actually uses, so the one question worth asking on those cameras
    was the one question the action refused.
    """
    _london(hass)
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = []

    await _call(
        hass,
        "get_events",
        {
            "config_entry_id": mock_config_entry.entry_id,
            "event_names": ["CatSelfie", "Meowing", "CatActivity"],
            **WINDOW,
        },
    )

    assert mock_client.get_events.await_args.kwargs["event_names"] == [
        "CatSelfie",
        "Meowing",
        "CatActivity",
    ]


async def test_an_alert_that_is_not_an_event_name_is_still_refused(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The two vocabularies overlap without either containing the other.

    Run is a real alert you can switch on, and the event list has never been
    able to filter by it, so widening the check to every alert key would have
    swapped one wrong answer for another.
    """
    _london(hass)
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(vol.Invalid):
        await _call(
            hass,
            "get_events",
            {
                "config_entry_id": mock_config_entry.entry_id,
                "event_names": ["Run"],
                **WINDOW,
            },
        )
    assert mock_client.get_events.await_count == 0
