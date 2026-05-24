# Anti-detection notes

Q: **Can Intelbras detect or block my account for using this integration?**

A: It's **very unlikely**, but not theoretically impossible. This document describes what the integration does to look identical to the official app, and what residual risks exist.

## What the integration mimics from the official app

| Aspect | Official app | This integration |
|---|---|---|
| HTTP User-Agent | `okhttp/3.12.13` | identical |
| `<client>.id` format | `003-4077-{hex16}` | identical |
| `<client>.id` stability | persisted per app install | persisted per HA install |
| OEM / App ID / envelope version | constants from `AppConfig` | identical constants |
| Auth flow | duplex /up + /down with jsessionid cookie | identical |
| Password format | `sha256(plaintext).hex()` | identical |
| TLS | self-signed `eziotest` cert, no verify | identical |
| Frequency of cloud login | ad-hoc + token refresh | once every 12h (cache) |
| LAN auth | `adminapp2` + OAC | identical |

## What's different (and could in theory be detected)

- **TLS fingerprint** (JA3/JA4): Python's aiohttp produces a different TLS Client Hello signature than Android's okhttp. A sophisticated server-side fingerprinting system could in principle tell them apart. We have no evidence that Quvii/Qualvision does this. The Golmar HA integration uses the same Python stack and has been running against the same backend for over a year without issue.
- **Timing / connection patterns**: the official app maintains long-lived MQTT and P2P UDP connections (for push notifications and live video). This integration only does periodic HTTP to fetch the OAC. The cloud sees less traffic from this integration than from the app, not more — which is a hard signal to construct an abuse rule on top of.
- **Local rate limit caps**: the integration default is 12 unlocks/hour, 3s between. If you intentionally raise this and then automate a flood, you'll create an unnatural traffic pattern. Don't do that.

## What would NOT count as "abuse"

- Opening your door 5–20 times a day. Same as a normal household.
- The integration silently retrying once after `OAC expired`. Same as the app refreshing its token.
- HA reload triggering a fresh OAC fetch. Same as the app cold-starting.

## What WOULD constitute abuse (and you should avoid)

- Running the integration against accounts that aren't yours.
- Scraping other people's devices via the cloud (impossible by construction — you only see your own devices in `get-device-list`).
- Brute-forcing the unlock PIN (irrelevant — you set the PIN yourself).
- Reselling access to devices through this integration. Don't.

## Worst case

If, hypothetically, Intelbras decided to block accounts using non-official clients:

- Your **physical device keeps working** — touchscreen, RFID, the doorbell button, all the wiring, all of it. The wT7 is a fully self-contained device.
- You can create a fresh Allo Plus account on a new email, re-register the device, and resume using the official app.
- You lose the HA integration until the integration is updated to whatever the new posture requires.

This is the same "blast radius" as e.g. losing access to a Tuya account, and is recoverable.

## Reference: similar precedents

The Tuya, Aqara, Hikvision/EZVIZ, Xiongmai/XMeye, and Sofia/DVRIP ecosystems all have widely-used open-source clients that pre-date this one by years. Account-level enforcement against unofficial clients is **vanishingly rare** in the IoT space.
