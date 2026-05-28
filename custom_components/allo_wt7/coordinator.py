"""Data update coordinator for the Allo wT7 integration.

This coordinator owns the OAC cache (on top of HA's storage helper) so
the cloud is hit at most every `oac_cache_ttl_s` seconds (12h default).

Doorbell detection: a background asyncio task polls ``get.record.session``
+ ``get.record.message`` on the LAN every 2 s (adaptive).  When a new
picture filename appears (the wT7 saves one JPEG per ring), the coordinator
fires the ``allo_wt7_doorbell_ring`` event on the HA bus.
"""

from __future__ import annotations

import asyncio
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
    CONF_DOORBELL_ENABLED,
    CONF_DOORBELL_POLL_INTERVAL,
    CONF_EMAIL,
    CONF_MAX_UNLOCKS_PER_HOUR,
    CONF_MIN_SECONDS_BETWEEN_UNLOCKS,
    CONF_MONITOR_IP,
    CONF_MONITOR_RETRY_DELAY,
    CONF_OAC_CACHE_TTL,
    CONF_PASSWORD,
    CONF_REQUEST_TIMEOUT,
    CONF_UNLOCK_PIN,
    DEFAULT_DOORBELL_ENABLED,
    DEFAULT_DOORBELL_POLL_INTERVAL,
    DEFAULT_MAX_UNLOCKS_PER_HOUR,
    DEFAULT_MIN_SECONDS_BETWEEN_UNLOCKS,
    DEFAULT_MONITOR_RETRY_DELAY,
    DEFAULT_OAC_CACHE_TTL,
    DEFAULT_REQUEST_TIMEOUT,
    DOMAIN,
    EVENT_DOORBELL_RING,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1


class AlloWT7Coordinator(DataUpdateCoordinator):
    """Owns the OAC cache, exposes open_door() and doorbell ring detection."""

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

        # Doorbell polling state
        self._doorbell_task: asyncio.Task | None = None
        self._last_picture_filename: str | None = None

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
        """Load persisted OAC + client_id from disk, then start doorbell polling."""
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

        if self._opt(CONF_DOORBELL_ENABLED, DEFAULT_DOORBELL_ENABLED):
            self._doorbell_task = self._hass.async_create_task(
                self._doorbell_poll_loop(),
                name=f"{DOMAIN}_doorbell_{self._entry.entry_id}",
            )
            _LOGGER.debug("Doorbell polling task created")

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

    async def async_stop_doorbell_polling(self) -> None:
        """Cancel the background doorbell polling task (called on entry unload)."""
        if self._doorbell_task and not self._doorbell_task.done():
            self._doorbell_task.cancel()
            try:
                await self._doorbell_task
            except asyncio.CancelledError:
                pass
        self._doorbell_task = None
        _LOGGER.debug("Doorbell polling stopped")

    async def _doorbell_poll_loop(self) -> None:
        """Background loop: poll get.record.session/message, fire event on new picture.

        Adaptive interval:
        - Normal: ``doorbell_poll_interval_s`` (default 2 s).
        - After a ring is detected: 10 s for the next 30 s (throttle window).
        - On transient error: 5 s back-off; after 3 consecutive errors the OAC
          is force-refreshed from the cloud.
        """
        normal_interval: float = self._opt(
            CONF_DOORBELL_POLL_INTERVAL, DEFAULT_DOORBELL_POLL_INTERVAL
        )
        throttle_interval = 10.0   # seconds between polls right after a ring
        throttle_window = 30.0     # how long to stay in throttle mode
        error_backoff = 5.0        # sleep after a transient error
        max_consecutive_errors = 3

        throttle_until: float = 0.0
        consecutive_errors: int = 0

        # Reuse the shared HA client session for all LAN polls
        session = async_get_clientsession(self._hass)

        _LOGGER.info(
            "Doorbell polling started (%.1f s normal, %.1f s throttled)",
            normal_interval,
            throttle_interval,
        )

        while True:
            try:
                now = time.monotonic()
                interval = throttle_interval if now < throttle_until else normal_interval
                await asyncio.sleep(interval)

                try:
                    oac = await self._ensure_oac()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Doorbell poll: cannot get OAC: %s", exc)
                    consecutive_errors += 1
                    continue

                filename, err = await self._client.poll_last_picture(
                    session,
                    self._data[CONF_MONITOR_IP],
                    oac,
                )

                if err is not None:
                    consecutive_errors += 1
                    _LOGGER.debug(
                        "Doorbell poll transient error (%d/%d): %s",
                        consecutive_errors,
                        max_consecutive_errors,
                        err,
                    )
                    if consecutive_errors >= max_consecutive_errors:
                        _LOGGER.info(
                            "Too many consecutive doorbell errors — forcing OAC refresh"
                        )
                        try:
                            await self._ensure_oac(force_refresh=True)
                        except Exception:  # noqa: BLE001
                            pass
                        consecutive_errors = 0
                    await asyncio.sleep(error_backoff)
                    continue

                consecutive_errors = 0

                if filename and filename != self._last_picture_filename:
                    prev = self._last_picture_filename
                    self._last_picture_filename = filename
                    if prev is None:
                        # First poll after startup — establish baseline, no event
                        _LOGGER.debug("Doorbell baseline picture: %s", filename)
                    else:
                        _LOGGER.info("Doorbell ring detected (new picture %s)", filename)
                        throttle_until = time.monotonic() + throttle_window
                        self._hass.bus.async_fire(
                            EVENT_DOORBELL_RING,
                            {"filename": filename},
                        )

            except asyncio.CancelledError:
                _LOGGER.debug("Doorbell poll loop cancelled")
                break
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error in doorbell poll loop")
                await asyncio.sleep(error_backoff)

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
