"""Self-contained LAN download of the *saved* doorbell snapshot (SD/flash).

The wT7 writes a JPEG to its internal flash on every ring. The live-snapshot
path grabs a *fresh* frame instead (by then the visitor is gone), so this module
fetches the actual saved file over the LAN video channel (TCP 34567).

Reverse-engineered from the official Allo Plus / Quvii uBell app traffic:
  * file-channel auth is the cloud ``dynamic-password`` (NOT ``auth_code``),
    formatted ``adminapp&&<dynamic-password>.<OEM>.``;
  * the device stays silent to a bare Open (0x02) — the app pipelines
    Open(0x02) + Seek(0x05) + Playback(0x15) + 0x07 in one burst, then reads;
  * payload framing/crypto is AES-256-CBC, IV ``b"0"*16``, key = stream_key.

No dependency on quii-helper's archive module (works with 0.3.0+).
"""

from __future__ import annotations

import hashlib
import re
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_IV = b"0" * 16
_PORT = 34567
# File-channel username — note it is "adminapp", distinct from the CGI
# LAN_USERNAME ("adminapp2"). Verified against captured app traffic.
_FILE_USER = b"adminapp"

# Native archive filename: idx _ start(Y-M-D-h-m-s) _ end(Y-M-D-h-m-s) _ 7 ints
_FN = re.compile(
    r"([0-9]{1,5})_"
    r"([0-9]{1,4})-([0-9]{1,2})-([0-9]{1,2})-([0-9]{1,2})-([0-9]{1,2})-([0-9]{1,2})_"
    r"([0-9]{1,4})-([0-9]{1,2})-([0-9]{1,2})-([0-9]{1,2})-([0-9]{1,2})-([0-9]{1,2})"
    + r"_(-?[0-9]{1,10})" * 7
    + r"(?:\.[a-z0-9]{1,8})?",
    re.IGNORECASE,
)


def _aes(data: bytes, key: bytes, *, decrypt: bool) -> bytes:
    k = key[:32].ljust(32, b"\0")
    ctx = Cipher(algorithms.AES(k), modes.CBC(_IV))
    c = ctx.decryptor() if decrypt else ctx.encryptor()
    n = len(data) - (len(data) % 16)
    if n <= 0:
        return b""
    return c.update(data[:n]) + c.finalize()


def _packed_time(y: int, mo: int, d: int, h: int, mi: int, s: int) -> int:
    return ((y - 2000) << 26) | (mo << 22) | (d << 17) | (h << 12) | (mi << 6) | s


def _header(kind: int) -> bytearray:
    h = bytearray(32)
    h[0] = kind
    struct.pack_into("<QH", h, 1, int(time.time()), 32)
    return h


def _seal(h: bytearray, key: bytes, body: bytes = b"") -> bytes:
    size = (((len(body) + 32) + 15) // 16) * 16
    struct.pack_into("<H", h, 9, size)
    ext = (body + hashlib.sha256(bytes(h) + body).digest()).ljust(size, b"\0")
    return _aes(bytes(h), key, decrypt=False) + _aes(ext, key, decrypt=False)


def build_file_info(filename: str, start_time: str, idx: str = "0") -> tuple[bytes, int]:
    """Return the 19-byte native file selector + the seek timestamp.

    On the wT7 ``idx`` is 0 for pictures (the on-disk offset lives in the
    filename fields), so the default is fine for doorbell snapshots.
    """
    name = (filename or "").replace("\n", "")
    match = _FN.fullmatch(name)
    if match is None:
        raise ValueError(f"unsupported archive filename: {name!r}")
    f = [int(x) for x in match.groups()]  # 0:idx 1-6:start 7-12:end 13-19:flags
    stamp = _packed_time(*f[1:7])
    seek = stamp
    m = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})[tT ](\d{2}):(\d{2}):(\d{2})[zZ]?", start_time or ""
    )
    if m:
        seek = _packed_time(*[int(x) for x in m.groups()])
    info = bytearray(19)
    struct.pack_into("<HI", info, 0, f[0], stamp)
    info[6] = f[13] & 255
    struct.pack_into("<I", info, 7, f[15] & 0xFFFFFFFF)
    info[11:15] = bytes((f[17] & 255, f[16] & 255, f[14] & 255, (f[18] >> 5) & 1))
    tail = (int(idx) if f[17] else _packed_time(*f[7:13]))
    struct.pack_into("<I", info, 15, tail & 0xFFFFFFFF)
    return bytes(info), seek


def _setup_pkt() -> bytes:
    h = bytearray(32)
    h[0], h[9] = 0xA9, 1  # file setup: byte 9 == 1 (unlike live/talk)
    return bytes(h)


def download_record_jpeg(
    host: str,
    filename: str,
    start_time: str,
    stream_key: str,
    dynamic_password: str,
    oem_id: str,
    *,
    idx: str = "0",
    timeout: float = 12.0,
) -> bytes:
    """Download the saved JPEG for one archive record over LAN TCP 34567.

    Returns the JPEG bytes (possibly without a trailing EOI if the device cut
    the transfer), or ``b""`` if no image was produced. Raises on setup refusal.
    """
    if not (stream_key and dynamic_password):
        raise ValueError("stream_key and dynamic_password are required")
    key = stream_key.encode("utf-8")
    file_info, seek_stamp = build_file_info(filename, start_time, idx)
    oem = oem_id.replace(",", "").encode("utf-8")
    body = _FILE_USER + b"&&" + dynamic_password.encode("utf-8") + b"." + oem + b"."

    open_h = _header(2)
    struct.pack_into("<H", open_h, 11, len(body))
    open_h[13:32] = file_info
    open_pkt = _seal(open_h, key, body)
    seek_h = _header(5)
    struct.pack_into("<I", seek_h, 11, seek_stamp)
    seek_pkt = _seal(seek_h, key)
    pb_h = _header(0x15)
    pb_h[29:31] = b"\x01\x01"
    pb_pkt = _seal(pb_h, key)
    go_pkt = _seal(_header(7), key)

    sock = socket.create_connection((host, _PORT), timeout=min(8.0, timeout))
    try:
        sock.sendall(_setup_pkt())
        reply = b""
        while len(reply) < 32:
            chunk = sock.recv(32 - len(reply))
            if not chunk:
                break
            reply += chunk
        if len(reply) < 32 or reply[9] != 0 or reply[10] != 2:
            raise RuntimeError(
                f"archive setup rejected (status={reply[9] if len(reply) > 9 else '?'})"
            )
        sock.sendall(open_pkt + seek_pkt + pb_pkt + go_pkt)  # pipelined
        resp = bytearray()
        sock.settimeout(2.0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            resp += chunk
    finally:
        sock.close()

    st = bytes(resp)
    o = 0
    media = bytearray()
    while o + 32 <= len(st):
        h = _aes(st[o:o + 32], key, decrypt=True)
        if len(h) < 18:
            break
        ptype = h[0]
        cmd_sz = int.from_bytes(h[9:11], "little")
        media_sz = int.from_bytes(h[11:15], "little")
        is_media = ptype in (0xA0, 0xA1, 0xA2, 0xA3)
        rsz = media_sz if is_media else cmd_sz
        if rsz < 0:
            break
        avail = len(st) - (o + 32)
        if avail <= 0:
            break
        take = min(rsz, avail)
        bd = st[o + 32:o + 32 + take]
        if is_media:
            clen = min(cmd_sz, len(bd))
            moff = min(h[16] | (h[17] << 8), len(bd))
            b2 = bytearray(bd)
            if clen:
                dec = _aes(bd[:clen], key, decrypt=True)
                b2[:len(dec)] = dec
            media += bytes(b2[moff:])
            o += 32 + take
            if take < rsz:
                break
        else:
            if take < rsz:
                break
            o += 32 + rsz
        if ptype == 0x08:
            break

    blob = bytes(media)
    soi = blob.find(b"\xff\xd8\xff")
    if soi < 0:
        return b""
    eoi = blob.rfind(b"\xff\xd9")
    return blob[soi:eoi + 2] if eoi > soi else blob[soi:]
