"""Image entity: the snapshot of the latest doorbell ring.

When the coordinator detects a new ring (``allo_wt7_doorbell_ring``), this
entity grabs a fresh JPEG from the doorbell camera over the LAN and exposes it
as an HA ``image`` — so a notification or dashboard can show *who rang*.
"""

from __future__ import annotations

import logging

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_DOORBELL_RING, MANUFACTURER, MODEL
from .coordinator import AlloWT7Coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the last-ring image entity."""
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AlloWT7LastRingImage(hass, coordinator, entry)])


class AlloWT7LastRingImage(ImageEntity):
    """Holds the JPEG captured on the most recent doorbell ring."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_ring"
    _attr_icon = "mdi:image-frame"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: AlloWT7Coordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(hass)
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_last_ring"
        self._image: bytes | None = None

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

    async def async_added_to_hass(self) -> None:
        """Subscribe to doorbell ring events."""
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_DOORBELL_RING, self._handle_ring_event
            )
        )

    @callback
    def _handle_ring_event(self, event: Event) -> None:
        """On a ring, kick off a background snapshot capture."""
        self.hass.async_create_task(self._capture())

    async def _capture(self) -> None:
        if self._coordinator.video is None:
            return
        try:
            creds = await self._coordinator.async_get_stream_credentials()
            data = await self.hass.async_add_executor_job(
                self._coordinator.video.snapshot_bytes,
                creds,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not capture last-ring snapshot: %s", exc)
            return
        self._image = data
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Return the cached last-ring JPEG bytes."""
        return self._image
