"""Config flow for Intelbras Allo wT7."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult

from .client import (
    AlloWT7AuthError,
    AlloWT7Client,
    AlloWT7ConnectionError,
    AlloWT7Error,
    AlloWT7WrongPinError,
    generate_client_id,
)
from .const import (
    CONF_DOOR1_NAME,
    CONF_DOOR2_ENABLED,
    CONF_DOOR2_NAME,
    CONF_MONITOR_IP,
    CONF_UNLOCK_PIN,
    DEFAULT_DOOR1_NAME,
    DEFAULT_DOOR2_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_MONITOR_IP): str,
        vol.Required(CONF_UNLOCK_PIN): str,
        vol.Optional(CONF_DOOR1_NAME, default=DEFAULT_DOOR1_NAME): str,
        vol.Optional(CONF_DOOR2_ENABLED, default=True): bool,
        vol.Optional(CONF_DOOR2_NAME, default=DEFAULT_DOOR2_NAME): str,
    }
)


class AlloWT7ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Allo wT7."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                from homeassistant.helpers.aiohttp_client import (
                    async_get_clientsession,
                )

                session = async_get_clientsession(self.hass)
                client = AlloWT7Client(session, client_id=generate_client_id())

                # Test cloud login + retrieve OAC
                oac, info = await client.fetch_oac(
                    user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
                # Verify PIN against device (read-only)
                ok = await client.check_pin(
                    monitor_ip=user_input[CONF_MONITOR_IP],
                    oac=oac,
                    unlock_pin=user_input[CONF_UNLOCK_PIN],
                )
                if not ok:
                    errors[CONF_UNLOCK_PIN] = "invalid_pin"
                else:
                    await self.async_set_unique_id(info.umid)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=info.name or f"Allo wT7 ({info.umid[:8]})",
                        data=user_input,
                    )
            except AlloWT7AuthError:
                errors["base"] = "invalid_auth"
            except AlloWT7ConnectionError:
                errors["base"] = "cannot_connect"
            except AlloWT7WrongPinError:
                errors[CONF_UNLOCK_PIN] = "invalid_pin"
            except AlloWT7Error as err:
                _LOGGER.exception("Unexpected error during setup: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=SCHEMA, errors=errors
        )
