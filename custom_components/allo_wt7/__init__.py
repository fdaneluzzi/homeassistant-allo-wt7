"""The Intelbras Allo wT7 integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import ATTR_LOCK_NUMBER, DOMAIN, PLATFORMS, SERVICE_OPEN_DOOR
from .coordinator import AlloWT7Coordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_OPEN_DOOR_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional(ATTR_LOCK_NUMBER, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=4)
        ),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Allo wT7 from a config entry."""
    coordinator = AlloWT7Coordinator(hass, entry)
    await coordinator.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register the service on first entry only
    if not hass.services.has_service(DOMAIN, SERVICE_OPEN_DOOR):
        async def _handle_open_door(call: ServiceCall) -> None:
            entries = hass.data.get(DOMAIN, {})
            target_entry_id = call.data.get("entry_id")
            if target_entry_id:
                coord = entries.get(target_entry_id)
                if coord is None:
                    raise HomeAssistantError(
                        f"Allo wT7: entry_id {target_entry_id!r} not found"
                    )
            else:
                if len(entries) != 1:
                    raise HomeAssistantError(
                        "Allo wT7: multiple wT7 configured — pass entry_id"
                    )
                coord = next(iter(entries.values()))
            lock_number = call.data.get(ATTR_LOCK_NUMBER, 1)
            await coord.async_open_door(lock_number)

        hass.services.async_register(
            DOMAIN, SERVICE_OPEN_DOOR, _handle_open_door, schema=SERVICE_OPEN_DOOR_SCHEMA
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator is not None:
        coordinator.async_cancel_doorbell_poll()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Remove the service when no entries left
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_OPEN_DOOR)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
