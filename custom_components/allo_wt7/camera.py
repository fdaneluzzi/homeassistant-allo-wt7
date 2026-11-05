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

import asyncio
import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_AUX_CAMERA_CHANNELS, DOMAIN, MANUFACTURER, MODEL
from .coordinator import AlloWT7Coordinator

_LOGGER = logging.getLogger(__name__)

# Reuse a recent still for this long so the camera card doesn't re-open a
# device session (or re-probe the stream) on every thumbnail refresh.
SNAPSHOT_CACHE_TTL = 5.0

# The device allows only one video session, so an aux grab can be briefly
# rejected as busy ("status 11") right after a config reload or while the main
# camera is mid-handoff. Retry a couple of times with a short pause before
# giving up and serving the last cached still.
AUX_SNAPSHOT_ATTEMPTS = 3
AUX_SNAPSHOT_RETRY_DELAY = 1.5
_BUSY_SESSION_MARKER = "status 11"


def _configured_aux_channels(entry: ConfigEntry) -> list[int]:
    """Auxiliary camera channels the user enabled in options.

    Empty by default, so a wT7 without extra cameras gets no aux entity. The
    option stores channel ids (as strings) chosen in the options flow.
    """
    channels: list[int] = []
    for value in entry.options.get(CONF_AUX_CAMERA_CHANNELS, []):
        try:
            channels.append(int(value))
        except (TypeError, ValueError):
            continue
    return channels


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the wT7 camera entities (doorbell + any enabled aux cameras)."""
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[Camera] = [AlloWT7Camera(coordinator, entry)]
    for channel in _configured_aux_channels(entry):
        entities.append(AlloWT7AuxCamera(coordinator, entry, channel))
    async_add_entities(entities)


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


class AlloWT7AuxCamera(Camera):
    """On-demand snapshot of an auxiliary camera wired to the wT7.

    One instance per channel the user enabled in options. Snapshot-only (no
    live RTSP stream): the still is fetched only while this entity's view is
    open and is cached briefly, so it never opens a device session in the
    background — put it behind a button/tap on the dashboard rather than a
    live camera card.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:cctv"

    def __init__(
        self,
        coordinator: AlloWT7Coordinator,
        entry: ConfigEntry,
        channel: int,
    ) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._entry = entry
        self._channel = channel
        self._attr_name = f"Camera {channel}"
        self._attr_unique_id = (
            f"{entry.unique_id or entry.entry_id}_camera_aux{channel}"
        )
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
        """Return a current JPEG still of the aux camera (briefly cached).

        The device's single video session may be momentarily busy, so a grab
        can be rejected as "status 11". Retry a few times; if it still fails,
        serve the last cached still (even if stale) rather than raising, so the
        card keeps showing the previous frame instead of breaking.
        """
        now = time.monotonic()
        if self._snapshot and (now - self._snapshot_at) < SNAPSHOT_CACHE_TTL:
            return self._snapshot
        if self._coordinator.video is None:
            return self._snapshot
        creds = await self._coordinator.async_get_stream_credentials()
        last_exc: Exception | None = None
        for attempt in range(AUX_SNAPSHOT_ATTEMPTS):
            try:
                data = await self.hass.async_add_executor_job(
                    self._coordinator.video.snapshot_bytes, creds, self._channel
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as cached fallback below
                last_exc = exc
                busy = _BUSY_SESSION_MARKER in str(exc)
                if busy and attempt + 1 < AUX_SNAPSHOT_ATTEMPTS:
                    await asyncio.sleep(AUX_SNAPSHOT_RETRY_DELAY)
                    continue
                break
            self._snapshot = data
            self._snapshot_at = now
            return data
        _LOGGER.debug(
            "Aux camera channel %s snapshot failed (%s); serving cached still",
            self._channel,
            last_exc,
        )
        return self._snapshot
