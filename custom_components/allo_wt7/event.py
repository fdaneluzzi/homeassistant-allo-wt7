"""Event entity for the Intelbras Allo wT7 doorbell.

The wT7 saves one JPEG snapshot to its internal flash every time someone
presses the door bell.  The coordinator polls for new pictures every 2 s
(adaptive) and fires ``allo_wt7_doorbell_ring`` on the HA bus when a new
filename is detected.  This ``EventEntity`` listens to that bus event and
translates it into a proper HA event entity (available since HA 2023.8).

Usage examples:
  - Automation trigger: ``event_type: ring`` on this entity.
  - Logbook: shows "Doorbell ring detected" with timestamp.
  - Notifications via standard HA automations.
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    EVENT_DOORBELL_RING,
    MANUFACTURER,
    MODEL,
)
from .coordinator import AlloWT7Coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the doorbell event entity."""
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AlloWT7DoorbellEvent(coordinator, entry)])


class AlloWT7DoorbellEvent(EventEntity):
    """Fires an event whenever the wT7 doorbell is pressed.

    State is the last event type fired (``ring``).  The entity itself has no
    meaningful on/off state — it's a momentary event.
    """

    _attr_has_entity_name = True
    _attr_name = "Campainha"
    _attr_icon = "mdi:doorbell"
    _attr_event_types = ["ring"]

    def __init__(
        self,
        coordinator: AlloWT7Coordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_doorbell"

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
        """Subscribe to doorbell ring events when entity is registered."""
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_DOORBELL_RING, self._handle_ring_event
            )
        )

    @callback
    def _handle_ring_event(self, event: Event) -> None:
        """Handle the bus event and forward it as an HA event entity update."""
        _LOGGER.debug("Doorbell event entity received ring: %s", event.data)
        self._trigger_event("ring", event.data)
        self.async_write_ha_state()
