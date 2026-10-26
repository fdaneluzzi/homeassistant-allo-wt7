"""Resolve a Home Assistant media reference into something FFmpeg can open."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError


async def async_resolve_audio_source(hass: HomeAssistant, media_id: str) -> str:
    """Turn a media_content_id into a URL or file path for FFmpeg."""
    from homeassistant.components import media_source  # noqa: PLC0415
    from homeassistant.components.media_player import (  # noqa: PLC0415
        async_process_play_media_url,
    )

    if media_source.is_media_source_id(media_id):
        try:
            play = await media_source.async_resolve_media(hass, media_id, None)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"Allo wT7: cannot resolve {media_id!r}: {err}"
            ) from err
        return async_process_play_media_url(hass, play.url)
    if media_id.startswith(("http://", "https://")):
        return media_id
    if media_id.startswith(("/api/", "/local/", "/media/")):
        # Relative HA URL (e.g. /local/beep.wav, TTS proxy) -> absolute.
        return async_process_play_media_url(hass, media_id)
    if not hass.config.is_allowed_path(media_id):
        raise HomeAssistantError(
            f"Allo wT7: {media_id!r} is not an allowed path "
            "(add it to allowlist_external_dirs, or use /config/www)"
        )
    return media_id
