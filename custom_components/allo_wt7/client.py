"""Intelbras Allo wT7 protocol client (aiohttp, async).

Talks to the Allo Plus cloud to obtain a per-device session credential,
then issues open-door commands directly to the monitor over the LAN.
The session credential is cached on disk; after the first successful
fetch, every unlock is pure LAN.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import ssl
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp

from .const import (
    APP_ID,
    CLIENT_TYPE,
    CLOUD_HOST,
    CLOUD_PORT,
    CLOUD_RESULT_ACCOUNT_LOCKED,
    CLOUD_RESULT_ACCOUNT_NOT_FOUND,
    CLOUD_RESULT_BAD_CREDENTIALS,
    CLOUD_RESULT_OK,
    ENVELOPE_FLAG,
    ENVELOPE_VERSION,
    LAN_CGI_PATH,
    LAN_ERROR_AUTH_INVALID,
    LAN_ERROR_COMMAND_UNKNOWN,
    LAN_ERROR_NO_STORAGE,
    LAN_ERROR_NOT_SUPPORTED,
    LAN_ERROR_OK,
    LAN_ERROR_WRONG_PIN,
    LAN_PASSWORDENCODE,
    LAN_SECURITY,
    LAN_USERNAME,
    OEM_ID,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# SSL context created once at import time (ssl.create_default_context() is a
# blocking I/O call that reads system certs — it must NOT run inside the async
# event loop). All LAN and cloud calls reuse this single context instance.
_SSL_CTX_NO_VERIFY: ssl.SSLContext = ssl.create_default_context()
_SSL_CTX_NO_VERIFY.check_hostname = False
_SSL_CTX_NO_VERIFY.verify_mode = ssl.CERT_NONE


class AlloWT7Error(Exception):
    """Base error for the Allo wT7 client."""


class AlloWT7AuthError(AlloWT7Error):
    """Cloud login or device-list authentication failed."""


class AlloWT7ConnectionError(AlloWT7Error):
    """Could not reach the cloud or the monitor."""


class AlloWT7WrongPinError(AlloWT7Error):
    """The unlock PIN configured in HA does not match the device."""


class AlloWT7UnsupportedError(AlloWT7Error):
    """The device rejected a command as unknown/unsupported (LAN error -1/-10).

    Seen on protocol-dialect variants (e.g. IDS9478W) whose firmware does not
    implement the commands this integration was reverse-engineered against.
    """


class AlloWT7RateLimitError(AlloWT7Error):
    """Local rate limiter rejected the request."""


@dataclass
class DeviceInfo:
    umid: str
    model: str
    name: str


def _ssl_no_verify() -> ssl.SSLContext:
    """Return the shared no-verify TLS context (created once at import time)."""
    return _SSL_CTX_NO_VERIFY


def _encode_device_password(pwd: str) -> str:
    """Encode the body password the way the monitor expects it.

    If the input is already 64 hex chars (i.e. an OAC), passes through;
    otherwise applies SHA-256 hex. Used for both check_password and
    opendoor commands.
    """
    if len(pwd) >= 64:
        return pwd
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()


def _build_cloud_envelope(
    command: str, seq: int, content_xml: str, client_id: str, session_id: str = ""
) -> bytes:
    env = ET.Element("envelope")
    h = ET.SubElement(env, "header")
    for k, v in (
        ("flag", ENVELOPE_FLAG),
        ("version", ENVELOPE_VERSION),
        ("command", command),
        ("seq", str(seq)),
        ("session", session_id),
        ("user-data", ""),
    ):
        ET.SubElement(h, k).text = v
    cl = ET.SubElement(h, "client")
    for k, v in (
        ("id", client_id),
        ("type", str(CLIENT_TYPE)),
        ("oem", OEM_ID),
        ("app", APP_ID),
    ):
        ET.SubElement(cl, k).text = v
    raw = ET.tostring(env, encoding="utf-8")
    raw = raw.replace(b"</envelope>", content_xml.encode("utf-8") + b"</envelope>")
    return b'<?xml version="1.0" encoding="UTF-8"?>' + raw


def _build_lan_envelope(oac: str, command: str, content_inner: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<envelope>"
        "<header>"
        f"<password>{oac}</password>"
        f"<passwordencode>{LAN_PASSWORDENCODE}</passwordencode>"
        f"<security>{LAN_SECURITY}</security>"
        f"<username>{LAN_USERNAME}</username>"
        "</header>"
        "<body>"
        f"<command>{command}</command>"
        f"<content>{content_inner}</content>"
        "</body>"
        "</envelope>"
    )


_COMMAND_RE = re.compile(r"<command>([^<]*)</command>")
_SECRET_RE = re.compile(r"<(password|out-auth-code|auth-code)>[^<]*</\1>")


def _redact_for_log(raw: bytes, limit: int = 300) -> str:
    """Decode a LAN response for DEBUG logs, masking credential elements."""
    text = raw[:limit * 2].decode("utf-8", errors="replace")
    return _SECRET_RE.sub(r"<\1>***</\1>", text)[:limit]


def _parse_envelope(buf: bytes) -> ET.Element | None:
    if not buf:
        return None
    idx = buf.find(b"<envelope")
    if idx < 0:
        return None
    end = buf.find(b"</envelope>", idx)
    if end < 0:
        return None
    try:
        return ET.fromstring(buf[idx : end + len(b"</envelope>")])
    except ET.ParseError:
        return None


class AlloWT7Client:
    """Pure protocol client. Stateless across calls (caller owns the OAC cache)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        client_id: str,
        request_timeout_s: float = 10.0,
        monitor_retry_delay_s: float = 10.0,
        max_unlocks_per_hour: int = 12,
        min_seconds_between_unlocks: int = 3,
    ) -> None:
        self._session_hint = session  # not used directly; we create our own per OAC fetch
        self._client_id = client_id
        self._request_timeout_s = request_timeout_s
        self._monitor_retry_delay_s = monitor_retry_delay_s
        self._max_unlocks_per_hour = max_unlocks_per_hour
        self._min_seconds_between_unlocks = min_seconds_between_unlocks
        self._unlock_history: deque[float] = deque(maxlen=256)
        self._lock = asyncio.Lock()

    # --- Public API -----------------------------------------------------------

    async def fetch_oac(
        self, email: str, password: str
    ) -> tuple[str, DeviceInfo]:
        """Log in to the cloud and retrieve the OAC + device info."""
        return await self._cloud_login_and_get_oac(email, password)

    async def open_door(
        self,
        monitor_ip: str,
        oac: str,
        unlock_pin: str | None,
        *,
        lock_number: int = 1,
        scheme: str = "http",
    ) -> None:
        """Open a door on the monitor. Raises on failure.

        Args:
            monitor_ip: Local IP of the wT7 monitor.
            oac: Out-auth-code obtained from fetch_oac.
            unlock_pin: The numeric PIN the user set in the app on first
                use. Will be SHA-256 hex'd before being sent. Pass None (or
                empty) for devices configured without an unlock PIN — the
                password element is then omitted and the door opens on the OAC.
            lock_number: 1 for the social door (default), 2 for the gate, ...
            scheme: "http" or "https". We try http first then fall back.
        """
        async with self._lock:
            self._enforce_rate_limit()
            await self._send_opendoor(monitor_ip, oac, unlock_pin, lock_number, scheme)
            self._unlock_history.append(time.monotonic())

    async def check_pin(
        self,
        monitor_ip: str,
        oac: str,
        unlock_pin: str,
        *,
        scheme: str = "http",
    ) -> bool:
        """Validates the PIN without opening any door. Returns True if accepted."""
        encoded = _encode_device_password(unlock_pin)
        body = _build_lan_envelope(
            oac, "set.opendoor.checkpassword", f"<password>{encoded}</password>"
        )
        err = await self._post_lan(monitor_ip, body, scheme)
        if err == LAN_ERROR_OK:
            return True
        if err == LAN_ERROR_WRONG_PIN:
            return False
        raise AlloWT7Error(f"check_pin: unexpected error code {err!r}")

    async def poll_last_picture(
        self,
        session: aiohttp.ClientSession,
        monitor_ip: str,
        oac: str,
        *,
        session_id: str | None = None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Return the (filename, starttime) of the most-recent picture stored.

        Uses the two-phase protocol: ``get.record.session`` to open a search
        session, then ``get.record.message`` in a loop until the datalist is
        empty.  The device ignores the time filter server-side so we send
        today's window as a hint and do no client-side date filtering (we only
        care about the *latest* record, not which day it belongs to).

        IMPORTANT: the device intermittently returns a *truncated* record list
        — a prefix of the full set, missing the newest entries (observed: 40 of
        57 records on a cold read).  Records come back oldest-first, so a
        truncated read yields an *older* max starttime.  Callers MUST therefore
        treat ``starttime`` as a high-water mark and only act on a *strictly
        newer* value; never on a mere change of filename, which oscillates as
        reads alternate between truncated and complete.

        Bandwidth: opening a fresh ``get.record.session`` forces the device to
        redo a full listing of every picture recorded today, which is the
        dominant cost of polling every few seconds — easily tens of GB/day on
        LAN traffic. Pass in the ``session_id`` returned by a previous call to
        reuse the same open search instead of reopening it (skips straight to
        ``get.record.message``). The caller should persist the returned
        session_id and pass it back in; pass None to force a fresh session
        (e.g. a periodic refresh) or when the previous call returned None as
        its session_id (the device no longer accepts the reused one).

        Returns:
            (filename, starttime, session_id, None) — success (session_id is
                None if the device has no storage; otherwise pass it back in
                on the next call to reuse the session).
            (None, None, None, err_str) — protocol/network error; back off.
        """
        url = f"https://{monitor_ip}{LAN_CGI_PATH}"
        ctx = _ssl_no_verify()
        headers = {
            "Content-Type": "application/xml;charset=utf-8",
            "User-Agent": USER_AGENT,
        }
        timeout = aiohttp.ClientTimeout(total=self._request_timeout_s)
        total_bytes = 0

        sess_id = session_id
        if sess_id is None:
            today_0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = today_0 + timedelta(days=1)
            t0 = today_0.strftime("%Y-%m-%dT%H:%M:%S")
            t1 = tomorrow.strftime("%Y-%m-%dT%H:%M:%S")

            session_body = _build_lan_envelope(
                oac,
                "get.record.session",
                (
                    "<record>"
                    "<filetype>picture</filetype>"
                    "<occurtype>all</occurtype>"
                    "<channels>1</channels>"
                    f"<starttime>{t0}</starttime>"
                    f"<endtime>{t1}</endtime>"
                    "<stream>all</stream>"
                    "</record>"
                ),
            )

            try:
                async with session.post(
                    url,
                    data=session_body.encode("utf-8"),
                    headers=headers,
                    ssl=ctx,
                    timeout=timeout,
                ) as resp:
                    raw = await resp.read()
            except Exception as exc:  # noqa: BLE001
                return None, None, None, f"session request failed: {exc}"
            total_bytes += len(raw)

            env = _parse_envelope(raw)
            if env is None:
                return None, None, None, f"session: no envelope (raw={raw[:80]!r})"

            err = env.findtext(".//error", "?")
            if err == LAN_ERROR_NO_STORAGE:
                return None, None, None, None  # no SD card / flash — not an error
            if err in (LAN_ERROR_COMMAND_UNKNOWN, LAN_ERROR_NOT_SUPPORTED):
                # Permanent: this firmware doesn't implement get.record.session.
                # Signal the caller to stop polling rather than retry forever.
                raise AlloWT7UnsupportedError(
                    f"device does not support 'get.record.session' (LAN error {err})"
                )
            if err != LAN_ERROR_OK:
                return None, None, None, f"session error={err}"

            sess_id = env.findtext(".//record/id", "")
            if not sess_id:
                return None, None, None, "session returned no id"

        # Read pages until empty datalist
        last_filename: str | None = None
        last_starttime: str = ""
        # Becomes False if the device rejects the reused session outright —
        # signals the caller to open a fresh one next time instead of
        # silently retrying the same (dead) session_id forever.
        session_still_valid = True

        for _ in range(20):  # hard cap — device typically needs ≤3 pages
            msg_body = _build_lan_envelope(
                oac,
                "get.record.message",
                f"<record><id>{sess_id}</id></record>",
            )
            try:
                async with session.post(
                    url,
                    data=msg_body.encode("utf-8"),
                    headers=headers,
                    ssl=ctx,
                    timeout=timeout,
                ) as resp:
                    raw = await resp.read()
            except Exception:  # noqa: BLE001
                break  # use whatever we collected so far
            total_bytes += len(raw)

            env = _parse_envelope(raw)
            if env is None:
                break

            msg_err = env.findtext(".//error")
            if msg_err is not None and msg_err != LAN_ERROR_OK:
                session_still_valid = False
                break

            datas = env.findall(".//data")
            if not datas:
                break  # empty datalist = no more pages (or nothing new, if reused)

            for d in datas:
                st = (d.findtext("starttime") or "").strip()
                fn = (d.findtext("filename") or "").strip()
                # Skip records with no starttime explicitly — otherwise an empty
                # "" would lose to the seeded "" under the comparison below and a
                # real record could vanish (the whole bug is a device that emits
                # malformed/truncated data, so empty fields are plausible).
                # Strict ">" (plus the last_filename-is-None seed) makes the
                # *first* record at the max starttime win, so the returned
                # filename is deterministic when several pictures share one ring
                # timestamp (the device stores a short burst per ring, differing
                # only by trailing sequence number).
                if not fn or not st:
                    continue
                if last_filename is None or st > last_starttime:
                    last_starttime = st
                    last_filename = fn

        _LOGGER.debug(
            "Doorbell poll: %d bytes over the wire (session %s, reused=%s, valid=%s)",
            total_bytes,
            sess_id,
            session_id is not None,
            session_still_valid,
        )

        returned_session_id = sess_id if session_still_valid else None
        return last_filename, (last_starttime or None), returned_session_id, None

    # --- Internals ------------------------------------------------------------

    def _enforce_rate_limit(self) -> None:
        now = time.monotonic()
        if self._unlock_history:
            since_last = now - self._unlock_history[-1]
            if since_last < self._min_seconds_between_unlocks:
                raise AlloWT7RateLimitError(
                    f"only {since_last:.1f}s since last unlock"
                )
        window_start = now - 3600
        recent = sum(1 for t in self._unlock_history if t >= window_start)
        if recent >= self._max_unlocks_per_hour:
            raise AlloWT7RateLimitError(
                f"{recent} unlocks in last hour (limit {self._max_unlocks_per_hour})"
            )

    async def _cloud_login_and_get_oac(
        self, email: str, password: str
    ) -> tuple[str, DeviceInfo]:
        ctx = _ssl_no_verify()
        base = f"https://{CLOUD_HOST}:{CLOUD_PORT}"
        p_sha = hashlib.sha256(password.encode("utf-8")).hexdigest()
        jar = aiohttp.CookieJar(unsafe=True)

        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            try:
                await self._post(session, f"{base}/auth/user;jus_duplex=down", b"", ctx)

                login_content = (
                    f"<content><account>{email}</account>"
                    f"<password>{p_sha}</password>"
                    "<auth-type>0</auth-type><auth-code></auth-code>"
                    "<ip-region-id>0</ip-region-id></content>"
                )
                login_body = _build_cloud_envelope(
                    "login", 1, login_content, self._client_id
                )
                await self._post(
                    session, f"{base}/auth/user;jus_duplex=up", login_body, ctx
                )
                _, raw = await self._post(
                    session,
                    f"{base}/auth/user;jus_duplex=down",
                    b"",
                    ctx,
                    timeout=15,
                )
                env = _parse_envelope(raw)
                if env is None:
                    raise AlloWT7AuthError("login: no envelope in response")
                result = env.findtext(".//result", "")
                session_id = env.findtext(".//session/id") or ""
                if result not in CLOUD_RESULT_OK:
                    msg = {
                        CLOUD_RESULT_BAD_CREDENTIALS: "invalid credentials",
                        CLOUD_RESULT_ACCOUNT_LOCKED: "account locked",
                        CLOUD_RESULT_ACCOUNT_NOT_FOUND: "account does not exist",
                    }.get(result, f"unknown error (result={result})")
                    raise AlloWT7AuthError(f"login failed: {msg}")

                dl_body = _build_cloud_envelope(
                    "get-device-list",
                    2,
                    "<content><filter></filter><order>0</order>"
                    "<count>0</count><page>0</page><owner></owner></content>",
                    self._client_id,
                    session_id,
                )
                await self._post(
                    session, f"{base}/auth/user;jus_duplex=up", dl_body, ctx
                )
                _, raw = await self._post(
                    session,
                    f"{base}/auth/user;jus_duplex=down",
                    b"",
                    ctx,
                    timeout=15,
                )
                env = _parse_envelope(raw)
                if env is None:
                    raise AlloWT7Error("device-list: no envelope")
                dev = env.find(".//device")
                if dev is None:
                    raise AlloWT7Error(
                        "device-list: no <device> (account has no wT7 registered?)"
                    )
                oac = dev.findtext("out-auth-code") or dev.findtext("auth-code")
                if not oac:
                    raise AlloWT7Error("device returned without out-auth-code")
                info = DeviceInfo(
                    umid=dev.findtext("id", ""),
                    model=dev.findtext("model", ""),
                    name=dev.findtext("name", ""),
                )
                _LOGGER.info(
                    "cloud OAC obtained (%d chars) for device %s (%s)",
                    len(oac),
                    info.name,
                    info.model,
                )
                return oac, info
            except aiohttp.ClientConnectorError as e:
                raise AlloWT7ConnectionError(f"cloud unreachable: {e}") from e
            except asyncio.TimeoutError as e:
                raise AlloWT7ConnectionError("cloud timeout") from e

    async def _send_opendoor(
        self,
        monitor_ip: str,
        oac: str,
        unlock_pin: str | None,
        lock_number: int,
        scheme: str,
    ) -> None:
        content = f"<door>1</door><locknumber>{lock_number}</locknumber>"
        # Only include the password element when a PIN is configured. Devices
        # set up with "device requires no unlock PIN" open with the OAC alone;
        # sending an empty/garbage hash would otherwise be rejected. When a PIN
        # *is* present the emitted bytes are identical to before.
        if unlock_pin:
            content += f"<password>{_encode_device_password(unlock_pin)}</password>"
        body = _build_lan_envelope(oac, "set.device.opendoor", content)
        err = await self._post_lan(monitor_ip, body, scheme)
        if err == LAN_ERROR_OK:
            return
        if err == LAN_ERROR_WRONG_PIN:
            raise AlloWT7WrongPinError("unlock PIN mismatch")
        if err == LAN_ERROR_AUTH_INVALID:
            raise AlloWT7AuthError("OAC expired/invalid (refresh from cloud)")
        if err in (LAN_ERROR_COMMAND_UNKNOWN, LAN_ERROR_NOT_SUPPORTED):
            raise AlloWT7UnsupportedError(
                f"device does not support 'set.device.opendoor' (LAN error {err}); "
                "this wT7 model likely speaks a different protocol dialect"
            )
        raise AlloWT7Error(f"unexpected LAN error code {err!r}")

    async def _post_lan(self, monitor_ip: str, body: str, scheme: str) -> str:
        """POST body to /tdkcgi. Returns the <error> field as string.

        Tries http first (default); falls back to https on connect refused.
        The monitor's HTTP port is closed when idle and reopens on activity;
        we retry once after a short delay.
        """
        # https-first: many wT7 units (e.g. IDS94B1W) are https-only and *filter*
        # port 80 (SYN dropped -> full-timeout hang) rather than refusing it, which
        # tar-pits an http-first probe for request_timeout_s on every LAN call.
        schemes = ["https"] if scheme == "https" else ["https", scheme]
        cmd_match = _COMMAND_RE.search(body)
        command = cmd_match.group(1) if cmd_match else "?"
        last_exc: Exception | None = None

        for attempt in range(2):
            for sch in schemes:
                url = f"{sch}://{monitor_ip}{LAN_CGI_PATH}"
                ctx = _ssl_no_verify() if sch == "https" else None
                async with aiohttp.ClientSession() as session:
                    t = aiohttp.ClientTimeout(total=self._request_timeout_s)
                    try:
                        async with session.post(
                            url,
                            data=body.encode("utf-8"),
                            headers={
                                "Content-Type": "application/xml;charset=utf-8",
                                "User-Agent": USER_AGENT,
                            },
                            ssl=ctx,
                            timeout=t,
                        ) as resp:
                            raw = await resp.read()
                            _LOGGER.debug(
                                "LAN %s -> HTTP %s, command=%s, body=%s",
                                url,
                                resp.status,
                                command,
                                _redact_for_log(raw),
                            )
                            if resp.status != 200:
                                _LOGGER.warning(
                                    "LAN %s HTTP %s", url, resp.status
                                )
                                continue
                            try:
                                root = ET.fromstring(raw)
                            except ET.ParseError:
                                _LOGGER.warning("LAN unparseable: %r", raw[:200])
                                return "?"
                            return root.findtext(".//error") or "?"
                    except aiohttp.ClientConnectorError as e:
                        last_exc = e
                        continue
            if attempt == 0:
                _LOGGER.info(
                    "monitor possibly idle, waiting %.1fs and retrying",
                    self._monitor_retry_delay_s,
                )
                await asyncio.sleep(self._monitor_retry_delay_s)

        raise AlloWT7ConnectionError(
            f"monitor unreachable at {monitor_ip}{LAN_CGI_PATH}: {last_exc}"
        )

    async def _post(
        self,
        session: aiohttp.ClientSession,
        url: str,
        body: bytes,
        ctx: ssl.SSLContext,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        t = aiohttp.ClientTimeout(total=timeout or self._request_timeout_s)
        async with session.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/xml;charset=utf-8",
                "User-Agent": USER_AGENT,
            },
            ssl=ctx,
            timeout=t,
        ) as resp:
            return resp.status, await resp.read()


def generate_client_id() -> str:
    """Stable per-installation client ID."""
    return f"003-{APP_ID}-{uuid.uuid4().hex[:16]}"
