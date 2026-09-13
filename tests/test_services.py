"""The two actions: what they return, and what they refuse."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.furbo import async_setup
from custom_components.furbo.api import FurboError
from custom_components.furbo.const import DOMAIN

from . import const as c
from .conftest import setup_integration

WINDOW = {
    "start": datetime(2026, 9, 12, 19, 0, 0),
    "end": datetime(2026, 9, 13, 7, 0, 0),
}
EVENT = {
    "Id": 91,
    "DeviceId": c.DEVICE_ID,
    "Caption": "standing in the doorway",
    "Videos": ["https://example.invalid/a.mp4"],
    "Thumbnail": "https://example.invalid/t.jpg",
}


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
    await setup_integration(hass, mock_config_entry)
    mock_client.get_events.return_value = [dict(EVENT)]

    result = await _call(
        hass, "get_events", {"config_entry_id": mock_config_entry.entry_id, **WINDOW}
    )

    assert result == {"events": [EVENT]}
    # The window reaches the cloud as epoch seconds, overnight included.
    sent = mock_client.get_events.await_args.kwargs
    assert sent["start"] == int(WINDOW["start"].timestamp())
    assert sent["end"] == int(WINDOW["end"].timestamp())


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
