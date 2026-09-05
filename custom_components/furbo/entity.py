"""Shared entity base classes for Furbo."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL_NAMES
from .coordinator import FurboCoordinator, FurboDeviceData


class FurboAccountEntity(CoordinatorEntity[FurboCoordinator]):
    """Entity attached to the account-level hub device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FurboCoordinator) -> None:
        """Bind to the coordinator and the account hub device."""
        super().__init__(coordinator)
        account_id = (
            coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"account_{account_id}")},
            manufacturer=MANUFACTURER,
            model="Cloud account",
            name="Furbo account",
            entry_type=None,
        )


class FurboDeviceEntity(CoordinatorEntity[FurboCoordinator]):
    """Entity attached to one Furbo camera device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FurboCoordinator, device_id: str) -> None:
        """Bind to the coordinator and build the camera's device info."""
        super().__init__(coordinator)
        self._device_id = device_id
        info = self.device.info
        account_id = (
            coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        )
        product_id: str | None = info.get("ProductId")
        model = MODEL_NAMES.get(product_id, product_id) if product_id else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer=MANUFACTURER,
            model=model,
            name=self.device.name,
            sw_version=info.get("FirmwareVersion"),
            serial_number=device_id,
            via_device=(DOMAIN, f"account_{account_id}"),
        )

    @property
    def device(self) -> FurboDeviceData:
        """Return the current data for this camera."""
        return self.coordinator.data.devices[self._device_id]

    @property
    def available(self) -> bool:
        """Available only while the camera is present in the last refresh."""
        return super().available and self._device_id in self.coordinator.data.devices
