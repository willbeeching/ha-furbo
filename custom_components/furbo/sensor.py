"""Sensor platform for Furbo."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FurboConfigEntry
from .bridge import SNACK_MODES, BridgeState
from .coordinator import (
    FurboBridgeCoordinator,
    FurboCoordinator,
    FurboData,
    FurboDeviceData,
)
from .entity import FurboAccountEntity, FurboBridgeEntity, FurboDeviceEntity

# Reads only; no writes to serialize.
PARALLEL_UPDATES = 0

EVENTS_UNIT = "events"


@dataclass(frozen=True, kw_only=True)
class FurboDeviceSensorDescription(SensorEntityDescription):
    """Describes a camera-level sensor."""

    value_fn: Callable[[FurboDeviceData], Any]
    attributes_fn: Callable[[FurboDeviceData], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class FurboAccountSensorDescription(SensorEntityDescription):
    """Describes an account-level sensor."""

    value_fn: Callable[[FurboData], Any]
    attributes_fn: Callable[[FurboData], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class FurboBridgeSensorDescription(SensorEntityDescription):
    """Describes a read-only camera value served by the bridge."""

    value_fn: Callable[[BridgeState], Any]


def _subscription_days(device: FurboDeviceData) -> int | None:
    if device.subscription is None:
        return None
    value = device.subscription.get("TimeLeftDays")
    return int(value) if value is not None else None


def _subscription_status(device: FurboDeviceData) -> str | None:
    if device.subscription is None:
        return None
    return device.subscription.get("SubscriptionStatus")


def _last_event_attrs(device: FurboDeviceData) -> dict[str, Any]:
    event = device.last_event or {}
    return {
        "caption": event.get("Caption"),
        "action": event.get("ActionCaption"),
        "location": event.get("LocationCaption"),
    }


DEVICE_SENSORS: tuple[FurboDeviceSensorDescription, ...] = (
    FurboDeviceSensorDescription(
        key="last_event",
        translation_key="last_event",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda device: device.last_event_at,
        attributes_fn=_last_event_attrs,
    ),
    FurboDeviceSensorDescription(
        key="subscription_days_left",
        translation_key="subscription_days_left",
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_subscription_days,
    ),
    FurboDeviceSensorDescription(
        key="subscription_status",
        translation_key="subscription_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_subscription_status,
    ),
)

ACCOUNT_SENSORS: tuple[FurboAccountSensorDescription, ...] = (
    FurboAccountSensorDescription(
        key="barking_events_today",
        translation_key="barking_events_today",
        native_unit_of_measurement=EVENTS_UNIT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        # The activity report omits a type with no events today, so a missing
        # key means zero, not "unknown".
        value_fn=lambda data: data.activity_today.get("Barking", 0),
    ),
    FurboAccountSensorDescription(
        key="activity_events_today",
        translation_key="activity_events_today",
        native_unit_of_measurement=EVENTS_UNIT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.activity_today.get("DogMoveAbove10Sec", 0),
    ),
    FurboAccountSensorDescription(
        key="notable_events_today",
        translation_key="notable_events_today",
        native_unit_of_measurement=EVENTS_UNIT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.notable_events_today,
        attributes_fn=lambda data: {"summary": data.daily_summary or None},
    ),
)

# The treat-tossing sound is a preset the app picks from a list of sound files;
# the camera only reports which kind is set, so it is surfaced read-only.
BRIDGE_SENSORS: tuple[FurboBridgeSensorDescription, ...] = (
    FurboBridgeSensorDescription(
        key="treat_toss_sound",
        translation_key="treat_toss_sound",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=list(SNACK_MODES),
        value_fn=lambda state: state.snack_call,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FurboConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Furbo sensors."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        FurboDeviceSensor(coordinator, device_id, description)
        for device_id in coordinator.data.devices
        for description in DEVICE_SENSORS
    ]
    if coordinator.events_enabled:
        entities.extend(
            FurboAccountSensor(coordinator, description)
            for description in ACCOUNT_SENSORS
        )
    entities.extend(
        FurboBridgeSensor(bridge, description)
        for bridge in entry.runtime_data.bridges.values()
        for description in BRIDGE_SENSORS
    )
    async_add_entities(entities)


class FurboDeviceSensor(FurboDeviceEntity, SensorEntity):
    """A camera-level sensor."""

    entity_description: FurboDeviceSensorDescription

    def __init__(
        self,
        coordinator: FurboCoordinator,
        device_id: str,
        description: FurboDeviceSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.device)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes when the description provides them."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.device)


class FurboAccountSensor(FurboAccountEntity, SensorEntity):
    """An account-level sensor."""

    entity_description: FurboAccountSensorDescription

    def __init__(
        self,
        coordinator: FurboCoordinator,
        description: FurboAccountSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        account_id = (
            coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        )
        self._attr_unique_id = f"account_{account_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes when the description provides them."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)


class FurboBridgeSensor(FurboBridgeEntity, SensorEntity):
    """A read-only camera value served by the bridge."""

    entity_description: FurboBridgeSensorDescription

    def __init__(
        self,
        coordinator: FurboBridgeCoordinator,
        description: FurboBridgeSensorDescription,
    ) -> None:
        """Initialise the bridge sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the value, or None while the camera has not reported it."""
        return self.entity_description.value_fn(self.coordinator.data)
