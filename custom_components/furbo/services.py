"""Actions that hand back Furbo data the entities deliberately do not hold.

Two things an entity is the wrong shape for. The event list is a window of
clips, dozens a day, each with its own links; the insight report is a written
recap whose contents the app's own model does not describe. Neither belongs in
state: attributes are written to the recorder, included in diagnostics and
pasted into issue reports, and neither of these has a fixed shape to hold.

So they are actions that return their answer to the caller. An automation
decides what to do with it, what to keep, and for how long, which is the part
of "let me build my own recap" that does not belong in an integration.

Worth knowing before using them: a response goes into the automation trace
that asked for it, so links land there too. They expire, and the trace is
local, but it is the one place this data does persist.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final, cast

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import EVENTS_DEFAULT_LIMIT, FurboError
from .const import ALERT_KEYS, DOMAIN

if TYPE_CHECKING:
    from . import FurboConfigEntry

SERVICE_GET_EVENTS: Final = "get_events"
SERVICE_GET_INSIGHT_REPORT: Final = "get_insight_report"

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_START: Final = "start"
ATTR_END: Final = "end"
ATTR_DEVICE_IDS: Final = "device_ids"
ATTR_EVENT_NAMES: Final = "event_names"
ATTR_LIMIT: Final = "limit"
ATTR_DATES: Final = "dates"

GET_EVENTS_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_START): cv.datetime,
        vol.Required(ATTR_END): cv.datetime,
        vol.Optional(ATTR_DEVICE_IDS): vol.All(cv.ensure_list, [cv.string]),
        # Checked here rather than sent on, because the cloud's answer to a
        # name it does not know is the same bare 12001 it gives for a missing
        # field, with nothing to say which one was wrong.
        vol.Optional(ATTR_EVENT_NAMES): vol.All(cv.ensure_list, [vol.In(ALERT_KEYS)]),
        vol.Optional(ATTR_LIMIT, default=EVENTS_DEFAULT_LIMIT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1000)
        ),
    }
)

GET_INSIGHT_REPORT_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_DATES): vol.All(cv.ensure_list, [cv.date]),
    }
)


def _entry(hass: HomeAssistant, call: ServiceCall) -> FurboConfigEntry:
    """Return the loaded Furbo entry the call names, or explain why not."""
    entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"entry": entry.title},
        )
    return entry


def _failed(err: FurboError) -> ServiceValidationError:
    """Turn a cloud refusal into something an automation author can read."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="cloud_request_failed",
        translation_placeholders={"error": str(err)},
    )


async def _async_get_events(call: ServiceCall) -> ServiceResponse:
    """Return the events in a window, each with its own clip links."""
    entry = _entry(call.hass, call)
    # cv.datetime hands back whatever was written: a time with an offset keeps
    # it, a bare "2026-09-12 19:00:00" arrives with no timezone at all. Left
    # alone, the naive one is read in the timezone of whatever machine Home
    # Assistant happens to run on, which for a container is UTC and for the
    # person writing the automation is their own clock. as_utc reads a naive
    # time in the timezone Home Assistant is configured for, which is the one
    # the automation was written against, and normalises both so the
    # comparison below cannot raise on a mixed pair.
    start: datetime = dt_util.as_utc(call.data[ATTR_START])
    end: datetime = dt_util.as_utc(call.data[ATTR_END])
    if end <= start:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="end_before_start"
        )
    coordinator = entry.runtime_data.coordinator
    # Required by the cloud, which refuses a request without it as plainly
    # "missing parameters" and says no more. Left to the caller it was simply
    # omitted, so every call failed. Unset means the whole account.
    device_ids = call.data.get(ATTR_DEVICE_IDS) or list(coordinator.data.devices)
    if not device_ids:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_cameras"
        )
    try:
        events = await coordinator.client.get_events(
            start=int(start.timestamp()),
            end=int(end.timestamp()),
            device_ids=device_ids,
            event_names=call.data.get(ATTR_EVENT_NAMES),
            limit=call.data[ATTR_LIMIT],
        )
    except FurboError as err:
        raise _failed(err) from err
    return cast("ServiceResponse", {"events": events})


async def _async_get_insight_report(call: ServiceCall) -> ServiceResponse:
    """Return the written daily report for each date asked for."""
    entry = _entry(call.hass, call)
    dates = [day.isoformat() for day in call.data[ATTR_DATES]]
    try:
        report = await entry.runtime_data.coordinator.client.get_insight_report(dates)
    except FurboError as err:
        raise _failed(err) from err
    return cast("ServiceResponse", {"report": report})


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once, for the integration rather than an entry."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_EVENTS,
        _async_get_events,
        schema=GET_EVENTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_INSIGHT_REPORT,
        _async_get_insight_report,
        schema=GET_INSIGHT_REPORT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
