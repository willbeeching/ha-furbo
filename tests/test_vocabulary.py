"""The alert and event vocabularies against the files that describe them.

Every one of these was a real bug on a cat camera. The alert list was built
from a single dog camera and shipped as everyone's vocabulary, so an FBC0030
owner got four switches out of nineteen alerts; the action's selector held a
second hand-maintained copy of a third list, which left CatSelfie unaskable
from the UI even once the schema allowed it. Copies drift. These fail when
they do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from custom_components.furbo.const import (
    ALERT_KEYS,
    DEFAULT_ENABLED_ALERTS,
    EVENT_NAMES,
    FREQUENCY_ALERTS,
)
from custom_components.furbo.switch import _snake

COMPONENT = Path(__file__).parent.parent / "custom_components" / "furbo"


def _load(name: str) -> Any:
    text = (COMPONENT / name).read_text()
    return yaml.safe_load(text) if name.endswith(".yaml") else json.loads(text)


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_every_alert_has_a_name(filename: str) -> None:
    """An alert without one ships as a switch called "alert_cat_selfie"."""
    switches = _load(filename)["entity"]["switch"]
    missing = [key for key in ALERT_KEYS if f"alert_{_snake(key)}" not in switches]
    assert not missing


def test_every_alert_has_an_icon() -> None:
    """Without one it falls back to the domain's, unlike its neighbours."""
    switches = _load("icons.json")["entity"]["switch"]
    missing = [key for key in ALERT_KEYS if f"alert_{_snake(key)}" not in switches]
    assert not missing


def test_the_action_offers_exactly_the_event_names_it_accepts() -> None:
    """The selector is a second copy of the list the schema validates against.

    It is also `custom_value: false`, so a name missing here cannot be typed
    around: the UI simply will not offer it.
    """
    fields = _load("services.yaml")["get_events"]["fields"]
    options = fields["event_names"]["selector"]["select"]["options"]
    assert options == list(EVENT_NAMES)


def test_the_defaults_name_alerts_that_exist() -> None:
    """A typo here silently disables nothing and enables nothing."""
    assert set(ALERT_KEYS) >= DEFAULT_ENABLED_ALERTS
    assert set(ALERT_KEYS) >= set(FREQUENCY_ALERTS)


def test_both_kinds_of_camera_have_everyday_defaults() -> None:
    """A cat camera reports none of the dog keys, so it needs its own.

    Without this the four alerts enabled by default were all dog ones, and a
    cat camera arrived with every switch disabled.
    """
    assert DEFAULT_ENABLED_ALERTS.issuperset({"Barking", "Crying", "DogMoveAbove10Sec"})
    assert DEFAULT_ENABLED_ALERTS.issuperset({"Meowing", "CatCrying", "CatActivity"})
