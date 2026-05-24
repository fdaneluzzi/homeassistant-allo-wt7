# Intelbras Allo wT7 — protocol notes

End-to-end summary of the protocol used to talk to the Intelbras Allo wT7
video intercom from Home Assistant. Full RE history is in the (private)
companion repo `fdaneluzzi/allo-plus-ha`.

The wT7 is an ODM device built around the **Qualvision/Quvii P2P SDK** (the
same SDK behind Fermax Wi-Box, Golmar ART 7W/G2Call+, Godrej Eve Nx, and
others). The cloud subdomain and a few constants (OEM ID, App ID, envelope
version, session mechanism) differ between brands; everything else is shared.

## Architecture

```
┌─────────────────────┐  1× every 12h        ┌────────────────────────────────┐
│ HA `allo_wt7`       │ ──── HTTPS:443 ────▶ │ intelbras-4.qvcloud.net        │
│ (this integration)  │                      │  · login                       │
│                     │                      │  · get-device-list → OAC       │
└──────────┬──────────┘                      └────────────────────────────────┘
           │
           │  HTTP(S) /tdkcgi (any time, every door-open)
           ▼
   ┌──────────────┐
   │ wT7 monitor  │  e.g. 192.168.1.42 — Intelbras Allo wT7 / IDS9478AW
   │ on the LAN   │
   └──────────────┘
```

## Cloud layer (`intelbras-4.qvcloud.net`, port 443)

- TLS with the Qualvision `eziotest` self-signed certificate — **TLS yes, hostname/cert verification no** (the SDK does the same).
- Duplex over two HTTP endpoints:
  - `POST /auth/user;jus_duplex=down` — long-poll, server → client response channel. Open it BEFORE any command and keep it open.
  - `POST /auth/user;jus_duplex=up` — client → server commands. The /up response body is always empty; the actual reply arrives over the /down stream.
- Session is tracked via the `jsessionid` HTTP cookie (note: differs from Golmar, which uses an XML `<session>` element).
- Each request body is an XML envelope:

  ```xml
  <envelope>
    <header>
      <flag>tdkcloud</flag>
      <version>v1.24</version>
      <command>login</command>     <!-- or get-device-list, ... -->
      <seq>1</seq>
      <session></session>
      <user-data></user-data>
      <client>
        <id>003-4077-{random hex 16}</id>
        <type>3</type>
        <oem>A0077,G0077</oem>
        <app>4077</app>
      </client>
    </header>
    <content>...</content>
  </envelope>
  ```

  - `version=v1.24` — server-defined; this is what Intelbras serves right now (Golmar still serves v1.13).
  - `oem=A0077,G0077` — `AppConfig.OEM_ID` from `com.intelbras.alloplus`.
  - `app=4077` — `AppConfig.APP_ID`.
  - `<client>.id` — stable per-installation (the integration stores it in HA's encrypted storage).

- Login content:
  - `account` = Allo Plus email
  - `password` = `sha256(plaintext_password).hex().lower()` (64 chars)
  - `auth-type=0`, `auth-code=""`, `ip-region-id=0`
- Login response (read over /down): the `<result>` element holds the status code; `0` or `100` = success. `100100003` = invalid credentials, `100100009` = account locked, `100100010` = account not found.
- `get-device-list` response (over /down) includes per device:
  - `id` (UMID)
  - `model`
  - **`out-auth-code`** (64-hex SHA-256 of the admin password — used as the LAN auth credential)
  - `dynamic-password`, `data-encode-key`, `transparent-basedata`, `password-expired`, `default-out-auth-code`, etc.

## Device LAN layer (`http(s)://<monitor-ip>/tdkcgi`)

- HTTP on port 80 may close when the device is idle. HTTPS on port 443 stays open. The integration tries HTTP first then falls back to HTTPS.
- All commands use the same XML envelope shape, with the OAC in the header and a per-command body:

  ```xml
  <envelope>
    <header>
      <password>{out-auth-code}</password>     <!-- session auth -->
      <passwordencode>1</passwordencode>
      <security>username</security>
      <username>adminapp2</username>            <!-- SDKConst.DEVICE_USER_NAME -->
    </header>
    <body>
      <command>set.device.opendoor</command>
      <content>
        <door>1</door>
        <locknumber>1</locknumber>              <!-- 1 = first lock, 2 = second -->
        <password>{sha256(UNLOCK_PIN)}</password>  <!-- IMPORTANT — see below -->
      </content>
    </body>
  </envelope>
  ```

- **The `<password>` field inside `<body>/<content>` is NOT the OAC.** It is the per-device **unlock PIN** that the user sets in the Allo Plus app the first time they open a door, transformed by the same `EncodeDevicePassword` helper as the cloud password: SHA-256 hex if the input is shorter than 64 chars, passthrough otherwise. This is the single detail that makes the Intelbras firmware reject the simpler Golmar-style envelope (where `<password>` in the body is the OAC).
- The HTTP response is `200 OK`; the actual outcome is the `<error>` element:
  - `0` — success (the relay pulses, the door opens)
  - `-1` — command not recognized or envelope malformed
  - `-3` — unlock PIN incorrect
  - `-10` — `QVERR_SUPPORT` — command not supported by this device type (some IPC-specific commands are rejected by VDPs like the wT7)
  - `401` — header auth invalid (OAC expired or wrong)

### Confirmed read-only commands

- `get.device.qrcode` → returns JSON with `umid`, `mac`, factory PIN, model
- `set.opendoor.checkpassword` → validates the PIN without pulsing the relay (the integration uses this on initial setup to verify the user typed the PIN correctly)

### Opendoor — two variants in the SDK source

The Quvii SDK exposes two content classes for the same command, used by different code paths:

- `DeviceUnlockContent`: `<door>` + `<locknumber>` + `<password>` — used by the "main" door API (`channelNum=1`, `lockNum=1|2|...`)
- `OpenLockContent`: `<door>` + `<password>` (no `<locknumber>`) — used by an alternate path

The Intelbras firmware accepts the `DeviceUnlockContent` shape on the wT7. We have not found a firmware that requires the `OpenLockContent` variant.

## What we do NOT do (out of scope for 0.1.0)

The wT7 has additional capabilities that go through different channels and that this integration does not currently implement:

- **Doorbell ring events** are pushed via the MQTT broker at `mqttintelbras.qvcloud.net:1884` (TLS) and/or FCM. Subscribing to the MQTT topics requires the device's MQTT credentials, which come from a separate Quvii API.
- **Live video** uses a proprietary P2P/KCP UDP protocol on port 50000 between phone and device, with payloads encrypted by a per-session key derived in `libqv-p2p-v2.so`. Implementing this from scratch is weeks of work.
- **Two-way audio** rides on the same P2P channel.

## Anti-ban posture

The integration aims to be indistinguishable from the official app to the cloud:

- `User-Agent: okhttp/3.12.13` (identical to the released app v2.4.77.10)
- Stable `client_id` per HA installation (matches app behavior)
- OAC cached for 12h — at most ~2 cloud logins per day per user
- Exponential backoff on `100100009` (account-locked)
- Local rate-limit on opendoor calls

In normal operation, the cloud sees one login + one `get-device-list` every ~12h, which is well within the noise floor of legitimate app usage.

## Where this came from

The full reverse-engineering history (~5 days of work across PCAPdroid captures, JADX of the Allo Plus APK, curl probing, and verification against a real wT7) lives in a private companion repo. The decisive insights were:

1. The cloud subdomain comes from `AppConfig.APP_INFO_URL` + a runtime location query — not hardcoded. PCAPdroid on a non-rooted Android (with the official APK, no MITM) was the cleanest way to discover `intelbras-4.qvcloud.net` and friends.
2. The `<password>` field inside the `<body>` of `set.device.opendoor` is the unlock PIN (SHA-256 hex), not the OAC. This is the difference vs. Golmar/Fermax firmwares that accept the OAC there.
3. `<locknumber>` 1 vs. 2 selects which of the device's two outputs to pulse.

Thanks to [@pablopr](https://github.com/pablopr) for documenting the Golmar dialect of this protocol publicly — that work made the Intelbras dialect tractable.
