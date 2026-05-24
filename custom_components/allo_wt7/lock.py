"""Lock entities for the Intelbras Allo wT7."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DOOR1_NAME,
    CONF_DOOR2_ENABLED,
    CONF_DOOR2_NAME,
    DEFAULT_DOOR1_NAME,
    DEFAULT_DOOR2_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import AlloWT7Coordinator

_LOGGER = logging.getLogger(__name__)

# How long to show the lock as "unlocked" in HA after a successful open.
# The physical relay pulse is short; the door auto-closes by spring.
_UNLOCKED_FEEDBACK_SECONDS = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN][entry.entry_id]

    door1_name = entry.data.get(CONF_DOOR1_NAME, DEFAULT_DOOR1_NAME)
    entities = [AlloWT7Lock(coordinator, entry, 1, door1_name)]

    if entry.data.get(CONF_DOOR2_ENABLED, True):
        door2_name = entry.data.get(CONF_DOOR2_NAME, DEFAULT_DOOR2_NAME)
        entities.append(AlloWT7Lock(coordinator, entry, 2, door2_name))

    async_add_entities(entities)


class AlloWT7Lock(LockEntity):
    """A door controlled by the Allo wT7 monitor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: AlloWT7Coordinator,
        entry: ConfigEntry,
        lock_number: int,
        name: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._lock_number = lock_number
        self._attr_name = name
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_lock_{lock_number}"
        self._is_locked = True
        self._reset_task: asyncio.Task | None = None

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

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    async def async_unlock(self, **kwargs) -> None:
        """Pulse the door open."""
        await self._coordinator.async_open_door(self._lock_number)
        self._is_locked = False
        self.async_write_ha_state()
        if self._reset_task and not self._reset_task.done():
            self._reset_task.cancel()
        self._reset_task = self.hass.async_create_task(self._reset_lock_state())

    async def async_lock(self, **kwargs) -> None:
        """The monitor has no close command; doors auto-close by spring.
        Locking via UI just resets the feedback state."""
        self._is_locked = True
        self.async_write_ha_state()

    async def _reset_lock_state(self) -> None:
        try:
            await asyncio.sleep(_UNLOCKED_FEEDBACK_SECONDS)
            self._is_locked = True
            self.async_write_ha_state()
        except asyncio.CancelledError:
            pass
