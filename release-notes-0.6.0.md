## What's new
- **Door audio in the live stream:** the camera now carries the door's microphone audio (G.711), so you can hear the visitor while watching the video. Falls back to video-only if the device sends no audio.
- **Talk-back:** a new `media_player` entity for the door speaker works with `tts.speak`, `media_player.play_media`, the media browser and `media_player.media_stop`. The `allo_wt7.send_audio` service does the same without an entity. Audio is converted to G.711 A-law (8 kHz mono) and capped at 60 s.
- **Translations** (English and Brazilian Portuguese) for entities and services, plus hassfest and HACS validation workflows.
- Requires `quii-helper` 0.3.0 (installed automatically).

## Important
Both the live camera and talk-back use the doorbell's single video session. While they are active, the Allo Plus app and the physical indoor monitor cannot use the camera, and talk-back fails with a "busy" error if the app is already showing it. Do not use it for continuous streaming or recording.

## Known limitations
- The live stream is not corrected to 16:9 (snapshots are); forcing a 16:9 aspect ratio on the dashboard card works around it.
- No microphone input from your computer yet.

Tested on an IDS9478AW.
