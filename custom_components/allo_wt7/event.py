"""Event entity for the Intelbras Allo wT7 doorbell.

Fires a Home Assistant `event` entity whenever the doorbell button on the wT7
is pressed.  The event is detected by polling the Quvii alarm server
(intelbras-4.qvcloud.net:4443/UserAlarm) for ALARM_TYPE_CALL = 19 records.

Entity type `event` (EventEntity) was introduced in HA 2023.8 and is the
canonical way to represent momentary, stateless signals such as button presses
and doorbell rings.
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    EVENT_DOORBELL,
    EVENT_TYPE_RING,
    MANUFACTURER,
    MODEL,
)
from .coordinator import SIGNAL_DOORBELL_RING, AlloWT7Coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AlloWT7DoorbellEvent(coordinator, entry)])


class AlloWT7DoorbellEvent(EventEntity):
    """Represents the wT7 doorbell button as a HA event entity.

    Each time someone presses the physical doorbell button, HA fires an event
    of type 'ring'.  Automations can trigger on this entity directly using the
    'event' trigger type.
    """

    _attr_has_entity_name = True
    _attr_name = "Campainha"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_TYPE_RING]

    def __init__(self, coordinator: AlloWT7Coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{EVENT_DOORBELL}"

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
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_DOORBELL_RING}_{self._entry.entry_id}",
                self._handle_ring,
            )
        )

    @callback
    def _handle_ring(self) -> None:
        self._trigger_event(EVENT_TYPE_RING, {})
        self.async_write_ha_state()
