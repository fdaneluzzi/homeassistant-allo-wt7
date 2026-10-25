# Intelbras Allo wT7 — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-fdaneluzzi-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/fdaneluzzi)

Open the doors of your **Intelbras Allo wT7** video intercom from Home Assistant — and see who's at the door — over the local network, without keeping the official Allo Plus app running.

> **Status: 0.5.1.** Door opening, doorbell ring detection, **live camera** and **ring snapshots** are working and validated against a real device (model `IDS9478AW`).  Two-way audio is out of scope for now.

## Features

- 🔔 **Doorbell ring detection** as a Home Assistant `event` entity — trigger any automation the moment someone rings the bell (typ. 2–3 s latency, pure LAN polling)
- 📹 **Live camera** as a Home Assistant `camera` entity — still image and live H.264 stream, served over the direct LAN video channel
- 🖼️ **Last-ring snapshot** as an `image` entity — automatically captured when the doorbell is pressed, so a notification can show who rang
- 🔘 **Open door(s)** as Home Assistant `button` entities (one press → one open pulse)
- 🚪 **Dual-door support**: a wT7 typically controls a social door + a garage gate
- 🛠️ **Service action** `allo_wt7.open_door` for automations that need to pass `lock_number` dynamically
- 🌐 **Local-first**: the cloud is contacted only on startup and every 12 h to refresh credentials; all doorbell polls and door-opens are direct LAN
- 🔒 **Your password and PIN are stored encrypted by Home Assistant**, never transmitted in plaintext

## Requirements

- Home Assistant 2023.8 or newer (for the `event` entity platform used by doorbell detection)
- Intelbras Allo wT7 (or wT7 Lite) on your LAN, paired and working with the Allo Plus app
- Your Allo Plus account email and password
- The numeric unlock PIN that you set in the app the first time you opened a door
- **FFmpeg on the host** for camera/snapshot JPEG rendering — bundled with Home Assistant OS and the official container, so no extra step on a standard install
- The camera/image entities pull in the [`quii-helper`](https://pypi.org/project/quii-helper/) library automatically (declared in the manifest); no manual install needed

## Unsupported models

The integration was tested against the **`IDS9478AW`**. Some wT7
variants ship firmware that speaks a **different LAN protocol dialect** and are
**not currently supported**:

- **`IDS9478W`** — cloud login works, but the device rejects the door-open
  (`set.device.opendoor`) and doorbell (`get.record.session`) commands with LAN
  error `-1` ("command unknown"). The integration now reports a clean error and
  disables doorbell polling on such devices instead of retrying.

If your model returns `-1`/`-10` errors, supporting it requires capturing the
official Allo Plus app's LAN traffic (e.g. with mitmproxy) while opening a door,
so the real command names/format can be mapped. Contributions welcome — please
[open an issue](https://github.com/fdaneluzzi/homeassistant-allo-wt7/issues)
with the capture (redact your PIN/OAC/credentials first).

## Install via HACS

1. In HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/fdaneluzzi/homeassistant-allo-wt7` as an Integration
3. Install **Intelbras Allo wT7**
4. Restart Home Assistant
5. Settings → Devices & Services → Add Integration → search "Allo wT7"

## Manual install

```bash
cd /config/custom_components/
git clone https://github.com/fdaneluzzi/homeassistant-allo-wt7.git tmp
mv tmp/custom_components/allo_wt7 .
rm -rf tmp
```

Restart Home Assistant, then add the integration via the UI.

## Configuration

The integration is fully configured through the UI:

| Field | What it is |
|---|---|
| Email | Your Allo Plus account email |
| Password | Your Allo Plus account password |
| Monitor IP | Local IP of the wT7 on your LAN (e.g. `192.168.1.42`) |
| Unlock PIN | The numeric PIN you set in the app the first time |
| Door 1 / Door 2 names | Friendly names; `locknumber=1` and `locknumber=2` map to your device's outputs |
| Door 2 enabled | Uncheck if your installation only has one door |

After save, you get:
- `event.campainha` — fires `ring` every time someone presses the doorbell
- `camera.camera` — live view of the doorbell (still image + stream)
- `image.ultimo_toque` — snapshot of the most recent ring
- `button.<door1_name>`
- `button.<door2_name>` (if enabled)

Each press fires one open-door pulse on the relay:
- **Social door** (`locknumber=1` by default): the relay momentarily unlocks the door; spring closes it back.
- **Garage gate** (`locknumber=2` by default): the relay sends a pulse to the gate motor. Most gate motors **toggle** — pressing once opens, pressing again closes.

### Using the service in automations

```yaml
service: allo_wt7.open_door
data:
  lock_number: 1        # or 2 for the garage gate
```

If you have multiple wT7 monitors configured, also pass `entry_id`.

## Doorbell ring detection

The wT7 saves one JPEG snapshot to its internal flash every time someone presses the bell.  The integration polls the device every **2 seconds** over the LAN and fires a Home Assistant `event` when a new picture appears.

### The `event.campainha` entity

- **Entity type:** `event` (HA 2023.8+)
- **Event type:** `ring`
- **Typical latency:** 2–3 s from physical ring to HA trigger (median ~800 ms poll round-trip on LAN)

### Automation example

```yaml
automation:
  - alias: "Notify on doorbell ring"
    trigger:
      - platform: event
        event_type: allo_wt7_doorbell_ring
    action:
      - service: notify.mobile_app_my_phone
        data:
          title: "Campainha!"
          message: "Alguém tocou a campainha."
```

Or use the entity trigger in the UI: **Automations → + → Trigger → Event entity** → select `event.campainha` → Event type `ring`.

### Adaptive polling

To avoid spamming the device right after a ring (when the Allo Plus app also connects), the integration automatically throttles to **10 s intervals for 30 s** after each detected ring, then returns to 2 s.

### Configuring the poll interval

In Settings → Devices & Services → Allo wT7 → **Configure**, you can:
- Enable / disable doorbell detection entirely
- Adjust the poll interval (1–30 s)

Changes take effect after HA reloads the entry (done automatically when you save).

## Camera & snapshots

The wT7's live video travels over a direct **LAN TCP channel** (port `34567`,
AES-256) using the device's proprietary Quvii protocol. This integration uses
the MIT-licensed [`quii-helper`](https://github.com/xaer981/quii-helper) library
(tested against this device family) to decode it, and fetches the short-lived
video **stream key** from the same Intelbras cloud the integration already uses
for login.

- `camera.camera` — a `camera` entity with a still image and a live H.264
  stream. The stream is served to Home Assistant through a local RTSP endpoint
  and played by HA's built-in `stream` component.
- `image.ultimo_toque` — an `image` entity that captures a fresh JPEG the moment
  a ring is detected. Point a mobile notification at it to see who rang.

**All video is LAN.** The cloud is contacted only to refresh the stream key
(about every 12 h, a small HTTPS call — no video ever goes through the cloud).
Video is on demand: the doorbell only encodes and sends frames while the camera
or stream is actually open, so there's no continuous traffic.

> ⚠️ **Use the camera sparingly.** The doorbell supports only **one video
> session at a time**. While the stream is open in Home Assistant, you
> **cannot watch the camera in the Allo Plus app**, and the **physical video
> intercom (indoor monitor) is disabled**. Do **not** leave the live stream open
> permanently (dashboards with an always-on stream card) and do **not** use it
> for continuous recording or an NVR. The integration releases the session
> shortly after the last viewer disconnects (and caps a session at 10 minutes),
> but anything that keeps re-opening the stream will keep the intercom locked.
> Snapshots are brief and safe to use in automations.

Snapshot example in an automation:

```yaml
automation:
  - alias: "Ring snapshot to phone"
    trigger:
      - platform: event
        event_type: allo_wt7_doorbell_ring
    action:
      - delay: "00:00:02"   # let the image entity capture the frame
      - service: notify.mobile_app_my_phone
        data:
          message: "Alguém tocou a campainha."
          data:
            image: /api/image_proxy/image.ultimo_toque
```

> **Known limitation — live aspect ratio.** The doorbell encodes anamorphic
> video (960x1080 with a 2:1 pixel aspect ratio, i.e. 16:9). Snapshots are
> corrected to 16:9, but the live stream is passed through unchanged, so some
> players may show it narrow/tall. Forcing a 16:9 aspect ratio on your
> dashboard camera card works around it.

> **Two-way audio** (intercom) is not implemented. The current stream is
> video-only; adding audio — especially the talk-back path — is tracked on the
> roadmap.

## Why `button` and not `lock`?

The wT7 has no state sensor — it cannot tell HA whether a door is currently open or closed. Earlier versions exposed each door as a `lock` entity with fake "unlocked → re-locked after 5s" state, which was misleading (especially for the garage gate). A `button` is honest with the hardware: one press = one pulse. If you need state, build a template `binary_sensor` on top of an external sensor you trust.

## Migrating to 0.2.0

If you used 0.1.0, your dashboards/automations referenced `lock.<door_name>` with `unlock` actions. To migrate:

1. **Automations / scripts:** change `service: lock.unlock` + `entity_id: lock.<door>` to `service: button.press` + `entity_id: button.<door>`. The entity names are preserved (only the domain changes from `lock` to `button`).
2. **Lovelace cards:** replace any `entity: lock.<door>` with `entity: button.<door>`. Default `entities` cards render `button` correctly with a "Press" button.
3. **Voice assistants** (Alexa / Google): since `button` is not exposed to voice by default, create a HA script wrapping `allo_wt7.open_door` and expose the script instead.

After updating to 0.2.0 in HACS, **remove and re-add the integration** in Settings → Devices & Services to drop the old `lock.*` entities cleanly.

## How it works

```
┌────────────────────┐  1× every 12h   ┌─────────────────────────────┐
│ Home Assistant     │ ── HTTPS:443 ──▶│ intelbras-4.qvcloud.net     │
│ allo_wt7 component │  login → OAC +  │ /auth/user;jus_duplex=up|dn │
└──────────┬─────────┘  stream key     └─────────────────────────────┘
           │
           │  every 2 s: get.record.session + get.record.message (HTTPS, LAN)
           │  every door-open: set.device.opendoor (HTTPS, LAN)
           │  live view / snapshot: direct TCP :34567 (AES-256 video, LAN)
           ▼
   ┌──────────────┐
   │ wT7 monitor  │ ─── saves JPEG snapshot on each ring ───▶ internal flash
   │ (your LAN)   │ ─── H.264 live video on demand ─────────▶ TCP :34567
   └──────────────┘
```

The cloud is contacted only to fetch two short-lived per-device credentials — the `out-auth-code` (OAC) for door/doorbell and the video stream key — each lasting ~12 h.  Once cached, all doorbell polls, door-opens and video are pure LAN.

**Doorbell detection mechanism:** the wT7 records one JPEG snapshot per ring.  HA polls the picture list every 2 s; when a new filename appears, it fires the `allo_wt7_doorbell_ring` bus event.  No cloud, no push notifications, no network sniffer required.

## Privacy & safety

- Your password and PIN are stored in Home Assistant's encrypted store (`.storage/`).
- The integration **never sends** anything to anyone other than Intelbras' own cloud (the same servers the official app uses).
- A local rate-limit prevents runaway loops (default: max 12 unlocks/hour, 3s between).

## Acknowledgments

- [pablopr/homeassistant-golmar](https://github.com/pablopr/homeassistant-golmar) — companion integration for a sibling device.
- The Intelbras community forum, which has been asking for this integration since 2020.

## Roadmap

- [x] 0.1.0 — Open doors (as `lock` entities — deprecated)
- [x] 0.2.0 — Switch to `button` entities + service action
- [x] 0.3.0 — Doorbell ring detection via `event` entity
- [x] 0.5.0 — Live `camera` entity + last-ring `image` snapshot
- [x] 0.5.1 — Single-session video manager, 16:9 corrected snapshots (current)
- [ ] Square-pixel (16:9) live stream
- [ ] Two-way audio (intercom)
- [ ] Push to HACS default repositories

## Reporting issues

Open an issue on [GitHub](https://github.com/fdaneluzzi/homeassistant-allo-wt7/issues) with:
- Your wT7 model (visible in the device QR code, e.g. `IDS9478AW`, `IDS9478SW`, `IDS9478AW Lite`)
- Home Assistant version
- A copy of the entry's debug log (Settings → Logs, filter by `custom_components.allo_wt7`)
- **Never include** your PIN, password, OAC, or full UMID — redact them first.

## Support this project

If this integration saved you time or money, consider supporting its development:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-fdaneluzzi-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/fdaneluzzi)
[![GitHub Sponsors](https://img.shields.io/badge/GitHub%20Sponsors-fdaneluzzi-EA4AAA?style=for-the-badge&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/fdaneluzzi)

<details>
<summary>PIX (Brasil)</summary>

Chave aleatória: `aae9a1b1-11e2-4282-a16d-7df8f48ae903`

</details>

## License

[MIT](LICENSE).
