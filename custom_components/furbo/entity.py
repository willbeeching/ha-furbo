"""Shared entity base classes for Furbo."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .bridge import FurboBridgeError
from .const import DOMAIN, MANUFACTURER, MODEL_NAMES
from .coordinator import FurboBridgeCoordinator, FurboCoordinator, FurboDeviceData

# Home Assistant 2026.9 renamed the DeviceInfo parent key from ``via_device``
# (an identifier tuple) to ``via_device_id`` (the parent's registry id). Detect
# which one this Home Assistant expects so the integration links the camera to
# the account hub correctly on both the minimum and the latest supported lane.
_SUPPORTS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


def _account_identifier(coordinator: FurboCoordinator) -> tuple[str, str]:
    """Return the stable identifier of the account hub device."""
    account_id = coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
    return (DOMAIN, f"account_{account_id}")


class FurboAccountEntity(CoordinatorEntity[FurboCoordinator]):
    """Entity attached to the account-level hub device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FurboCoordinator) -> None:
        """Bind to the coordinator and the account hub device."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={_account_identifier(coordinator)},
            manufacturer=MANUFACTURER,
            model="Cloud account",
            name="Furbo account",
        )


def build_device_info(coordinator: FurboCoordinator, device_id: str) -> DeviceInfo:
    """Return the registry description of one camera, linked to the hub."""
    device = coordinator.data.devices[device_id]
    info = device.info
    product_id: str | None = info.get("ProductId")
    model = MODEL_NAMES.get(product_id, product_id) if product_id else None
    device_info = DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        manufacturer=MANUFACTURER,
        model=model,
        name=device.name,
        sw_version=info.get("FirmwareVersion"),
        serial_number=device_id,
    )
    _link_to_hub(device_info, coordinator)
    return device_info


class FurboDeviceEntity(CoordinatorEntity[FurboCoordinator]):
    """Entity attached to one Furbo camera device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FurboCoordinator, device_id: str) -> None:
        """Bind to the coordinator and build the camera's device info."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_device_info = build_device_info(coordinator, device_id)

    @property
    def device(self) -> FurboDeviceData:
        """Return the current data for this camera."""
        return self.coordinator.data.devices[self._device_id]

    @property
    def available(self) -> bool:
        """Available only while the camera is present in the last refresh."""
        return super().available and self._device_id in self.coordinator.data.devices


def _link_to_hub(device_info: DeviceInfo, coordinator: FurboCoordinator) -> None:
    """Point the camera device at the account hub, across HA versions.

    The key is set through an ``Any`` view so a single code path type-checks on
    both the ``via_device`` (<= 2026.2) and ``via_device_id`` (>= 2026.9) forms.
    """
    data = cast("dict[str, Any]", device_info)
    if _SUPPORTS_VIA_DEVICE_ID and coordinator.hub_device_id is not None:
        data["via_device_id"] = coordinator.hub_device_id
    else:
        data["via_device"] = _account_identifier(coordinator)


class FurboBridgeEntity(CoordinatorEntity[FurboBridgeCoordinator]):
    """Entity backed by the camera's HTTP bridge.

    It describes the same device as the cloud entities (same identifier and
    metadata), so the registry shows one Furbo device whichever platform
    happens to register first.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: FurboBridgeCoordinator) -> None:
        """Bind to the bridge coordinator and the camera's device."""
        super().__init__(coordinator)
        self._attr_device_info = build_device_info(
            coordinator.cloud, coordinator.device_id
        )

    @property
    def available(self) -> bool:
        """Available only while the bridge answers and holds a P2P session."""
        return super().available and self.coordinator.data.connected

    async def _async_apply(self, **settings: Any) -> None:
        """Write settings through the bridge and publish the state read back."""
        try:
            state = await self.coordinator.client.async_set(**settings)
        except FurboBridgeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="bridge_command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_set_updated_data(state)

    async def _async_action(self, action: Awaitable[None]) -> None:
        """Run a one-shot bridge action (pan, toss), one at a time per camera."""
        try:
            async with self.coordinator.action_lock:
                await action
        except FurboBridgeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="bridge_command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
