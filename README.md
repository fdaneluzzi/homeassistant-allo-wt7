# Intelbras Allo wT7 — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Open the doors of your **Intelbras Allo wT7** video intercom from Home Assistant — over the local network, without keeping the official Allo Plus app running.

> ⚠️ **Status: 0.1.0 (initial release).** Door opening is working and validated against a real device (model `IDS9478AW`). Doorbell ring detection, two-way audio, and camera stream are **out of scope** for this version (see [Roadmap](#roadmap)).

## Features

- 🔓 **Open door(s)** as Home Assistant `lock` entities
- 🚪 **Dual-door support**: a wT7 typically controls a social door + a garage gate
- 🌐 **Local-first**: the cloud is hit once every 12h (configurable) to refresh credentials; every door-open is direct LAN
- 🛡️ **Anti-detection hardened**: stable client ID, OAC cache, exponential backoff on auth errors, local rate-limit, User-Agent matching the official app
- 🔒 **Your password and PIN are stored encrypted by Home Assistant**, never transmitted in plaintext

## Requirements

- Home Assistant 2024.6 or newer
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
- `lock.<door1_name>`
- `lock.<door2_name>` (if enabled)

Each `unlock` action pulses the relay; the door auto-closes by spring (the monitor has no close command).

## How it works

```
┌────────────────────┐  1× every 12h  ┌─────────────────────────────┐
│ Home Assistant     │ ── HTTPS:443 ─▶│ intelbras-4.qvcloud.net     │
│ allo_wt7 component │                │ /auth/user;jus_duplex=up|dn │
└──────────┬─────────┘  (login → OAC) └─────────────────────────────┘
           │
           │ HTTP(S) /tdkcgi with OAC + sha256(PIN)
           ▼          (every lock.unlock call)
   ┌──────────────┐
   │ wT7 monitor  │
   │ (your LAN)   │
   └──────────────┘
```

The cloud is contacted only to fetch the `out-auth-code` (OAC) — a per-device session credential. Once cached on disk, all opendoor calls are pure LAN.

## Privacy & safety

- Your password and PIN are stored in Home Assistant's encrypted store (`.storage/`).
- The integration **never sends** anything to anyone other than Intelbras' own cloud (the same servers the official app uses).
- The TLS client identifies itself with the same `okhttp/3.12.13` User-Agent and `client_id` format as the official app, so the cloud sees indistinguishable traffic.
- A local rate-limit prevents runaway loops (default: max 12 unlocks/hour, 3s between).
- See [Anti-detection notes](docs/ANTI_DETECTION.md) for details.

## Acknowledgments

- [pablopr/homeassistant-golmar](https://github.com/pablopr/homeassistant-golmar) — companion device using the same Quvii/Qualvision SDK; the duplex protocol scaffolding was inspired by this excellent integration.
- [duhow/wibox](https://github.com/duhow/wibox) — invasive RE of the Fermax Wi-Box (same SDK), confirmed the `ASZENO.SEARCH` protocol family.
- The Intelbras community forum, which has been asking for this integration since 2020.

## Roadmap

- [x] 0.1.0 — Open doors (1 and 2)
- [ ] 0.2.0 — Doorbell ring detection (probably via the `mqttintelbras.qvcloud.net:1884` MQTT broker)
- [ ] 0.3.0 — Snapshot from the front camera
- [ ] 0.4.0 — Two-way audio (requires implementing the Quvii P2P/KCP UDP protocol — non-trivial)
- [ ] Push to HACS default repositories

## Reporting issues

Open an issue on [GitHub](https://github.com/fdaneluzzi/homeassistant-allo-wt7/issues) with:
- Your wT7 model (visible in the device QR code, e.g. `IDS9478AW`, `IDS9478SW`, `IDS9478AW Lite`)
- Home Assistant version
- A copy of the entry's debug log (Settings → Logs, filter by `custom_components.allo_wt7`)
- **Never include** your PIN, password, OAC, or full UMID — redact them first.

## License

[MIT](LICENSE).
