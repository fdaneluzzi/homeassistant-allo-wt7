# Intelbras Allo wT7 — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Open the doors of your **Intelbras Allo wT7** video intercom from Home Assistant — over the local network, without keeping the official Allo Plus app running.

> **Status: 0.3.0.** Door opening and doorbell ring detection are working and validated against a real device (model `IDS9478AW`).  Two-way audio and camera stream are out of scope for now.

## Features

- 🔔 **Doorbell ring detection** as a Home Assistant `event` entity — trigger any automation the moment someone rings the bell (typ. 2–3 s latency, pure LAN polling)
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
┌────────────────────┐  1× every 12h  ┌─────────────────────────────┐
│ Home Assistant     │ ── HTTPS:443 ─▶│ intelbras-4.qvcloud.net     │
│ allo_wt7 component │                │ /auth/user;jus_duplex=up|dn │
└──────────┬─────────┘  (login → OAC) └─────────────────────────────┘
           │
           │  every 2 s: get.record.session + get.record.message (HTTPS)
           │  every door-open: set.device.opendoor (HTTPS)
           ▼
   ┌──────────────┐
   │ wT7 monitor  │ ─── saves JPEG snapshot on each ring ───▶ internal flash
   │ (your LAN)   │
   └──────────────┘
```

The cloud is contacted only to fetch the `out-auth-code` (OAC) — a per-device session credential that lasts ~12 h.  Once cached on disk, all doorbell polls and door-opens are pure LAN.

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
- [x] 0.3.0 — Doorbell ring detection via `event` entity (current)
- [ ] Camera snapshot
- [ ] Two-way audio
- [ ] Push to HACS default repositories

## Reporting issues

Open an issue on [GitHub](https://github.com/fdaneluzzi/homeassistant-allo-wt7/issues) with:
- Your wT7 model (visible in the device QR code, e.g. `IDS9478AW`, `IDS9478SW`, `IDS9478AW Lite`)
- Home Assistant version
- A copy of the entry's debug log (Settings → Logs, filter by `custom_components.allo_wt7`)
- **Never include** your PIN, password, OAC, or full UMID — redact them first.

## License

[MIT](LICENSE).
