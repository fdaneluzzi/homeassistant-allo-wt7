"""Media player entity for the wT7 door speaker (talk-back).

Exposes the door speaker as a write-only ``media_player`` so that the standard
Home Assistant tools work with it: ``tts.speak``, ``media_player.play_media``,
the media browser and dashboard media controls.  Audio is played through the
doorbell's single video session (see ``talk.py``).
"""

from __future__ import annotations

import threading
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import AlloWT7Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the door speaker entity."""
    coordinator: AlloWT7Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AlloWT7Speaker(coordinator, entry)])


class AlloWT7Speaker(MediaPlayerEntity):
    """The wT7 door speaker: play a clip or a TTS phrase to the visitor."""

    _attr_has_entity_name = True
    _attr_name = "Alto-falante"
    _attr_icon = "mdi:bullhorn"
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    )
    _attr_state = MediaPlayerState.IDLE

    def __init__(self, coordinator: AlloWT7Coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_speaker"
        self._cancel: threading.Event | None = None

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

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play audio through the door speaker (blocks until it finishes)."""
        if self._cancel is not None:
            self._cancel.set()  # newer request replaces the one in progress
        cancel = self._cancel = threading.Event()
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()
        try:
            await self._coordinator.async_send_audio(media_id, cancel)
        finally:
            if self._cancel is cancel:
                self._cancel = None
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Stop the clip that is playing."""
        if self._cancel is not None:
            self._cancel.set()

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Let the user pick audio from the media browser (local files, TTS)."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )
