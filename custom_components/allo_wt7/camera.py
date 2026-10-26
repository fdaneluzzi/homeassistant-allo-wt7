"""Live camera entity for the Intelbras Allo wT7 video doorbell.

Exposes the doorbell's live view as an HA ``Camera``:
  - still images via :meth:`async_camera_image`, and
  - a live H.264 stream via a local RTSP server handed to HA's ``stream``
    component.

All device access goes through the coordinator's single :class:`WT7Video`
manager, which serializes the device's one video session and releases it when
idle so the official Allo Plus app stays usable.
"""

from __future__ import annotations

import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import AlloWT7Coordinator

_LOGGER = logging.getLogger(__name__)

# Reuse a recent still for this long so the camera card doesn't re-open a
# device session (or re-probe the stream) on every thumbnail refresh.
SNAPSHOT_CACHE_TTL = 5.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the wT7 camera entity."""
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AlloWT7Camera(coordinator, entry)])


class AlloWT7Camera(Camera):
    """Live view of the wT7 doorbell camera."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: AlloWT7Coordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_camera"
        self._snapshot: bytes | None = None
        self._snapshot_at: float = 0.0

    @property
    def device_info(self) -> DeviceInfo:
        dev = self._coordinator.device_info
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.unique_id or self._entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=(dev.name if dev else "Allo wT7"),
            serial_number=(dev.umid if dev else None),
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a current JPEG still (cached briefly to limit LAN sessions)."""
        now = time.monotonic()
        if self._snapshot and (now - self._snapshot_at) < SNAPSHOT_CACHE_TTL:
            return self._snapshot
        if self._coordinator.video is None:
            return None
        creds = await self._coordinator.async_get_stream_credentials()
        data = await self.hass.async_add_executor_job(
            self._coordinator.video.snapshot_bytes, creds
        )
        self._snapshot = data
        self._snapshot_at = now
        return data

    async def stream_source(self) -> str | None:
        """Return the local RTSP URL, starting the LAN->RTSP bridge if needed."""
        if self._coordinator.video is None:
            return None
        creds = await self._coordinator.async_get_stream_credentials()
        return await self.hass.async_add_executor_job(
            self._coordinator.video.stream_url, creds
        )
