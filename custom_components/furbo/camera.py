"""Camera platform for Furbo.

Furbo cameras stream over ThroughTek's proprietary TUTK P2P protocol, which
needs the vendor SDK on a machine on the camera's LAN and cannot run inside
Home Assistant. The live view therefore comes from a stream URL the user
configures in the options flow: typically an RTSP URL that go2rtc publishes
from the Furbo P2P bridge. A camera entity exists only for devices with a URL.
"""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FurboConfigEntry
from .const import MANUFACTURER, MODEL_NAMES
from .coordinator import FurboCoordinator
from .entity import FurboDeviceEntity

# Reads only; the stream itself is handled by Home Assistant's stream component.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FurboConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one camera per device that has a stream URL (configured or discovered)."""
    coordinator = entry.runtime_data.coordinator
    stream_urls = entry.runtime_data.stream_urls
    async_add_entities(
        FurboCamera(coordinator, device_id, url)
        for device_id in coordinator.data.devices
        if (url := stream_urls.get(device_id))
    )


class FurboCamera(FurboDeviceEntity, Camera):
    """Live view of one Furbo, fed by a user-supplied stream URL."""

    # The camera is the device's primary entity, so it takes the device name.
    _attr_name = None
    _attr_brand = MANUFACTURER
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self, coordinator: FurboCoordinator, device_id: str, stream_url: str
    ) -> None:
        """Initialise the camera entity."""
        super().__init__(coordinator, device_id)
        Camera.__init__(self)
        self._stream_url = stream_url
        self._attr_unique_id = f"{device_id}_camera"
        product_id: str | None = self.device.info.get("ProductId")
        if product_id:
            self._attr_model = MODEL_NAMES.get(product_id, product_id)

    @property
    def use_stream_for_stills(self) -> bool:
        """Snapshots are taken from the stream; there is no still-image URL."""
        return True

    async def stream_source(self) -> str | None:
        """Return the configured stream URL."""
        return self._stream_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return None: stills come from the stream (use_stream_for_stills)."""
        return None
