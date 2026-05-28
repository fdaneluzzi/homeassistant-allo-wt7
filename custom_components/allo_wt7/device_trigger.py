"""Device triggers for the Intelbras Allo wT7.

Registers a "Doorbell pressed" trigger so automations can be created
via the HA UI (Settings → Automations → Add Trigger → Device → Allo wT7).
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo

from .const import DOMAIN, EVENT_DOORBELL_RING

TRIGGER_TYPE_DOORBELL = "doorbell_ring"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required("type"): TRIGGER_TYPE_DOORBELL,
    }
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict]:
    """Return the list of triggers available for the wT7 device."""
    return [
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": device_id,
            "type": TRIGGER_TYPE_DOORBELL,
            "metadata": {"secondary": False},
        }
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: dict,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach the device trigger to the HA event bus."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            "platform": "event",
            "event_type": EVENT_DOORBELL_RING,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: dict
) -> dict:
    """Return extra capabilities (none needed for a simple trigger)."""
    return {}
