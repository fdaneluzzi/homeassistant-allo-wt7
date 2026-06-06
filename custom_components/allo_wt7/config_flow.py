"""Config flow for Intelbras Allo wT7."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
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
    CONF_DOORBELL_ENABLED,
    CONF_DOORBELL_POLL_INTERVAL,
    CONF_MONITOR_IP,
    CONF_REQUIRE_PIN,
    CONF_UNLOCK_PIN,
    DEFAULT_DOOR1_NAME,
    DEFAULT_DOOR2_NAME,
    DEFAULT_DOORBELL_ENABLED,
    DEFAULT_DOORBELL_POLL_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_MONITOR_IP): str,
        vol.Required(CONF_REQUIRE_PIN, default=True): bool,
        vol.Optional(CONF_UNLOCK_PIN, default=""): str,
        vol.Optional(CONF_DOOR1_NAME, default=DEFAULT_DOOR1_NAME): str,
        vol.Optional(CONF_DOOR2_ENABLED, default=True): bool,
        vol.Optional(CONF_DOOR2_NAME, default=DEFAULT_DOOR2_NAME): str,
    }
)


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_DOORBELL_ENABLED, default=DEFAULT_DOORBELL_ENABLED): bool,
        vol.Optional(
            CONF_DOORBELL_POLL_INTERVAL, default=DEFAULT_DOORBELL_POLL_INTERVAL
        ): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=30.0)),
    }
)


class AlloWT7OptionsFlow(config_entries.OptionsFlow):
    """Options flow for Allo wT7 (doorbell settings)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {
            CONF_DOORBELL_ENABLED: self.config_entry.options.get(
                CONF_DOORBELL_ENABLED, DEFAULT_DOORBELL_ENABLED
            ),
            CONF_DOORBELL_POLL_INTERVAL: self.config_entry.options.get(
                CONF_DOORBELL_POLL_INTERVAL, DEFAULT_DOORBELL_POLL_INTERVAL
            ),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, current),
        )


class AlloWT7ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Allo wT7."""

    VERSION = 1

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AlloWT7OptionsFlow:
        """Return the options flow handler."""
        return AlloWT7OptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Trim stray whitespace the user (or a mobile keyboard/paste) may
            # have introduced. A trailing space silently changes the PIN's
            # SHA-256 and is a common cause of a spurious "invalid_pin".
            for key in (CONF_EMAIL, CONF_MONITOR_IP, CONF_UNLOCK_PIN):
                if isinstance(user_input.get(key), str):
                    user_input[key] = user_input[key].strip()

            require_pin = user_input.get(CONF_REQUIRE_PIN, True)
            raw_pin = user_input.get(CONF_UNLOCK_PIN, "")
            # Precedence: when the device is marked as not requiring a PIN, any
            # value typed in the PIN field is ignored and not persisted.
            if not require_pin:
                user_input[CONF_UNLOCK_PIN] = ""
            elif not raw_pin:
                # PIN required but left blank.
                errors[CONF_UNLOCK_PIN] = "pin_required"

            if not errors:
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

                    ok = True
                    if require_pin:
                        # Verify PIN against device (read-only)
                        ok = await client.check_pin(
                            monitor_ip=user_input[CONF_MONITOR_IP],
                            oac=oac,
                            unlock_pin=raw_pin,
                        )
                    if not ok:
                        # PII-safe diagnostics: never log the PIN itself, only
                        # its shape, so a user's debug log can distinguish
                        # "wrong PIN" from formatting issues without leaking it.
                        _LOGGER.debug(
                            "Device rejected unlock PIN: length=%d, digits_only=%s",
                            len(raw_pin),
                            raw_pin.isdigit(),
                        )
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
