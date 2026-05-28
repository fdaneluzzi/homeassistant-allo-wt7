"""Data update coordinator for the Allo wT7 integration.

This coordinator owns the OAC cache (on top of HA's storage helper) so
the cloud is hit at most every `oac_cache_ttl_s` seconds (12h default).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    AlloWT7AuthError,
    AlloWT7Client,
    AlloWT7Error,
    DeviceInfo,
    generate_client_id,
)
from .const import (
    CONF_EMAIL,
    CONF_MAX_UNLOCKS_PER_HOUR,
    CONF_MIN_SECONDS_BETWEEN_UNLOCKS,
    CONF_MONITOR_IP,
    CONF_MONITOR_RETRY_DELAY,
    CONF_OAC_CACHE_TTL,
    CONF_PASSWORD,
    CONF_REQUEST_TIMEOUT,
    CONF_UNLOCK_PIN,
    DEFAULT_MAX_UNLOCKS_PER_HOUR,
    DEFAULT_MIN_SECONDS_BETWEEN_UNLOCKS,
    DEFAULT_MONITOR_RETRY_DELAY,
    DEFAULT_OAC_CACHE_TTL,
    DEFAULT_REQUEST_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1


class AlloWT7Coordinator(DataUpdateCoordinator):
    """Owns the OAC cache and exposes open_door()."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_{entry.entry_id}")
        self._hass = hass
        self._entry = entry
        self._data = entry.data
        self._options = entry.options

        self._store: Store = Store(
            hass, STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_oac"
        )
        self._client_id: str | None = None
        self._oac: str | None = None
        self._oac_expires_at: float = 0.0
        self.device_info: DeviceInfo | None = None

        session = async_get_clientsession(hass)
        self._client = AlloWT7Client(
            session,
            client_id="",  # filled after storage load
            request_timeout_s=self._opt(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT),
            monitor_retry_delay_s=self._opt(
                CONF_MONITOR_RETRY_DELAY, DEFAULT_MONITOR_RETRY_DELAY
            ),
            max_unlocks_per_hour=self._opt(
                CONF_MAX_UNLOCKS_PER_HOUR, DEFAULT_MAX_UNLOCKS_PER_HOUR
            ),
            min_seconds_between_unlocks=self._opt(
                CONF_MIN_SECONDS_BETWEEN_UNLOCKS,
                DEFAULT_MIN_SECONDS_BETWEEN_UNLOCKS,
            ),
        )

    def _opt(self, key: str, default: Any) -> Any:
        return self._options.get(key, self._data.get(key, default))

    async def async_setup(self) -> None:
        """Load persisted OAC + client_id from disk."""
        stored = await self._store.async_load() or {}
        self._client_id = stored.get("client_id") or generate_client_id()
        self._oac = stored.get("oac")
        self._oac_expires_at = stored.get("expires_at", 0.0)
        device_info = stored.get("device_info") or {}
        if device_info:
            self.device_info = DeviceInfo(
                umid=device_info.get("umid", ""),
                model=device_info.get("model", ""),
                name=device_info.get("name", ""),
            )
        self._client._client_id = self._client_id  # patch in client_id
        await self._save()

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "client_id": self._client_id,
                "oac": self._oac,
                "expires_at": self._oac_expires_at,
                "device_info": (
                    {
                        "umid": self.device_info.umid,
                        "model": self.device_info.model,
                        "name": self.device_info.name,
                    }
                    if self.device_info
                    else None
                ),
            }
        )

    async def _ensure_oac(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._oac and time.time() < self._oac_expires_at:
            return self._oac
        try:
            oac, info = await self._client.fetch_oac(
                self._data[CONF_EMAIL], self._data[CONF_PASSWORD]
            )
        except AlloWT7AuthError as err:
            raise UpdateFailed(f"cloud auth failed: {err}") from err
        except AlloWT7Error as err:
            raise UpdateFailed(f"cloud error: {err}") from err
        self._oac = oac
        self.device_info = info
        ttl = self._opt(CONF_OAC_CACHE_TTL, DEFAULT_OAC_CACHE_TTL)
        self._oac_expires_at = time.time() + ttl
        await self._save()
        return oac

    async def _async_update_data(self) -> dict[str, Any]:
        """Called by DataUpdateCoordinator. We do nothing periodic — only
        refresh OAC on demand or when explicitly forced.
        """
        return {
            "umid": self.device_info.umid if self.device_info else None,
            "model": self.device_info.model if self.device_info else None,
            "name": self.device_info.name if self.device_info else None,
        }

    async def async_open_door(self, lock_number: int) -> None:
        """Open the given door (1 = social, 2 = gate, ...)."""
        for attempt in range(2):
            oac = await self._ensure_oac(force_refresh=(attempt == 1))
            try:
                await self._client.open_door(
                    monitor_ip=self._data[CONF_MONITOR_IP],
                    oac=oac,
                    unlock_pin=self._data[CONF_UNLOCK_PIN],
                    lock_number=lock_number,
                )
                return
            except AlloWT7AuthError as err:
                if attempt == 0:
                    _LOGGER.warning(
                        "OAC may be expired (%s) — refreshing once", err
                    )
                    continue
                raise
        # unreachable, but keep type-checkers happy
        raise RuntimeError("open_door retry loop fell through")

    async def async_validate(self) -> None:
        """Used by config flow: fetch OAC and verify PIN against the device."""
        oac = await self._ensure_oac(force_refresh=True)
        try:
            ok = await self._client.check_pin(
                monitor_ip=self._data[CONF_MONITOR_IP],
                oac=oac,
                unlock_pin=self._data[CONF_UNLOCK_PIN],
            )
        except AlloWT7Error as err:
            raise UpdateFailed(f"check_pin failed: {err}") from err
        if not ok:
            raise UpdateFailed("unlock PIN is incorrect")
