"""Live camera entity for the Intelbras Allo wT7 video doorbell.

Exposes the doorbell's live view as an HA ``Camera``:
  - still images via :meth:`async_camera_image` (used by the camera card,
    snapshots, and automations), and
  - a live H.264 stream via a local RTSP server (:mod:`.video`) handed to HA's
    ``stream`` component.

Both go over the direct LAN TCP channel using credentials fetched from the
cloud by the coordinator; nothing here touches the cloud directly.
"""

from __future__ import annotations

import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    RTSP_BASE_PORT,
    VIDEO_CHANNEL,
    VIDEO_STREAM,
)
from .coordinator import AlloWT7Coordinator
from .video import WT7VideoBridge, grab_snapshot_bytes

_LOGGER = logging.getLogger(__name__)

# Don't re-open a full LAN session for every still request; reuse a recent
# snapshot for this long (the live stream is the path for smooth video).
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
    _attr_name = "Câmera"
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
        # Deterministic per-entry RTSP port so multiple wT7 don't collide.
        self._rtsp_port = RTSP_BASE_PORT + (
            abs(hash(entry.entry_id)) % 100
        )
        self._bridge: WT7VideoBridge | None = None
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
        creds = await self._coordinator.async_get_stream_credentials()
        data = await self.hass.async_add_executor_job(
            grab_snapshot_bytes,
            self._coordinator.monitor_ip,
            creds,
        )
        self._snapshot = data
        self._snapshot_at = now
        return data

    async def async_stream_source(self) -> str | None:
        """Return the local RTSP URL, starting the LAN->RTSP bridge if needed."""
        creds = await self._coordinator.async_get_stream_credentials()
        if self._bridge is None:
            self._bridge = WT7VideoBridge(
                self._coordinator.monitor_ip,
                self._rtsp_port,
                channel=VIDEO_CHANNEL,
                stream=VIDEO_STREAM,
            )
        bridge = self._bridge
        return await self.hass.async_add_executor_job(bridge.start, creds)

    async def async_will_remove_from_hass(self) -> None:
        """Tear down the RTSP bridge thread on unload."""
        if self._bridge is not None:
            await self.hass.async_add_executor_job(self._bridge.stop)
            self._bridge = None
