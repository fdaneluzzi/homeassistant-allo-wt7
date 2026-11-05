"""Diagnostic sensors for the Intelbras Allo wT7 (IP and MAC address).

The IP is the monitor address configured in the config flow.  The wT7 cloud
API does not expose the MAC, so it is resolved from the host's ARP/neighbour
table (``/proc/net/arp``).  That works whenever Home Assistant and the monitor
share a Layer-2 segment; otherwise the sensor is simply unknown.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import AlloWT7Coordinator

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)
_ARP_TABLE = Path("/proc/net/arp")
_EMPTY_MAC = "00:00:00:00:00:00"


def _lookup_mac(ip: str) -> str | None:
    """Return the MAC for ``ip`` from the ARP table, or None if not present."""
    try:
        lines = _ARP_TABLE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines[1:]:  # skip header
        # columns: IP address, HW type, Flags, HW address, Mask, Device
        parts = line.split()
        if len(parts) >= 4 and parts[0] == ip:
            mac = parts[3].lower()
            return None if mac == _EMPTY_MAC else mac
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            AlloWT7IpSensor(coordinator, entry),
            AlloWT7MacSensor(coordinator, entry),
        ],
        update_before_add=True,
    )


class _AlloWT7DiagnosticSensor(SensorEntity):
    """Base class: diagnostic sensor attached to the wT7 device."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator: AlloWT7Coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry

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


class AlloWT7IpSensor(_AlloWT7DiagnosticSensor):
    """Monitor IP address on the local network (from the config entry)."""

    _attr_translation_key = "ip_address"
    _attr_icon = "mdi:ip-network"

    def __init__(self, coordinator: AlloWT7Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_ip_address"

    @property
    def native_value(self) -> str:
        return self._coordinator.monitor_ip


class AlloWT7MacSensor(_AlloWT7DiagnosticSensor):
    """Monitor MAC address, resolved from the host ARP table."""

    _attr_translation_key = "mac_address"
    _attr_icon = "mdi:network-outline"
    _attr_should_poll = True

    def __init__(self, coordinator: AlloWT7Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_mac_address"
        self._attr_native_value: str | None = None

    async def async_update(self) -> None:
        mac = await self.hass.async_add_executor_job(
            _lookup_mac, self._coordinator.monitor_ip
        )
        # Keep the last known MAC if the ARP entry has just aged out.
        if mac:
            self._attr_native_value = mac
        elif self._attr_native_value is None:
            _LOGGER.debug(
                "MAC for %s not in ARP table (different subnet or no recent traffic)",
                self._coordinator.monitor_ip,
            )
