"""Talk-back: play audio through the wT7's door speaker over the LAN channel.

The device accepts audio on a *separate* QUII TCP session (port 34567, same
AES-256 key as the video session) opened in "talk" mode:

    Setup(0xA9, hdr[9]=2) -> Open(0x0B, user&&pass + /talk path)
        -> Request(0x0C, 8 kHz) -> Request(0x0D) -> Media(0xA2) ...

Audio is G.711 A-law, 8 kHz mono, in 320-byte (40 ms) frames.  The door only
plays it while a *live video session* is also open, so the caller
(:class:`~.video.WT7Video`) holds one for the duration.

Like ``video.py`` this is blocking code meant for an executor thread.
"""

from __future__ import annotations

import logging
import struct
import subprocess
import threading
import time

from .client import StreamCredentials
from .const import STREAM_PORT

_LOGGER = logging.getLogger(__name__)

_USER = "adminapp2"
_TALK_PATH = "/talk/idc=1&ap=2"
_NO_CHANNEL = 0xFFFF
_SAMPLE_RATE = 8000
_FRAME_BYTES = 320  # 40 ms of 8 kHz A-law
_FRAME_S = 0.04
_SILENCE = b"\xd5" * _FRAME_BYTES
_KEEPALIVE_S = 8.0
MAX_TALK_SECONDS = 60.0


def _reply_wait(client, label: str, count: int, timeout: float) -> None:
    """Read (and ignore) a few replies so the socket never backs up."""
    client.transport.set_timeout(timeout)
    for _ in range(count):
        try:
            client.recv_message()
        except TimeoutError:
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("wT7 talk %s: reply read stopped (%s)", label, err)
            return


class TalkSession:
    """One talk TCP session to the door station."""

    def __init__(self, host: str, creds: StreamCredentials) -> None:
        from quii_helper.device.security.auth import (  # noqa: PLC0415
            encode_device_password,
        )
        from quii_helper.protocols.tcp.transport.client import (  # noqa: PLC0415
            QuiiClient,
        )

        self._key = creds.stream_key.encode()[:32]
        self._password = encode_device_password(creds.auth_code)
        self._client = QuiiClient(
            host, STREAM_PORT, _USER, self._password, _TALK_PATH,
            creds.stream_key,
        )
        self._last_keepalive = 0.0

    # -- low-level packet builders -------------------------------------

    def _aes(self, data: bytes) -> bytes:
        from quii_helper.protocols.quii.crypto import (  # noqa: PLC0415
            aes_cbc_crypt,
        )

        return aes_cbc_crypt(data, self._key, crypto_mode=2, decrypt=False)

    def _request(self, opcode: int, arg: int, rate: int = 0) -> bytes:
        """Empty-body talk request (0x0C / 0x0D): header + SHA, encrypted."""
        from quii_helper.protocols.quii.crypto import (  # noqa: PLC0415
            compute_ext_data_len,
            sha_bytes,
        )

        _total, padded = compute_ext_data_len(
            0, crypto_mode=2, sha_mode=1, include_sha=True
        )
        header = bytearray(32)
        header[0] = opcode
        header[1:9] = struct.pack("<Q", int(time.time()))
        header[9:11] = struct.pack("<H", padded)
        header[11:13] = struct.pack("<H", _NO_CHANNEL)
        header[13] = arg
        if rate:
            header[15:17] = struct.pack("<H", rate)
        body = (sha_bytes(bytes(header), sha_mode=1) + b"\x00" * padded)[:padded]
        return self._aes(bytes(header)) + self._aes(body)

    def _audio(self, alaw: bytes) -> bytes:
        """Media packet (0xA2): AES(header) + AES(body[:32]) + body[32:]."""
        payload = (
            bytes([0x00, 0x00, 0x01, 0xF0, 0x0E, 0x02])
            + struct.pack("<H", len(alaw))
            + alaw
        )
        header = bytearray(32)
        header[0] = 0xA2
        header[1:9] = struct.pack("<Q", int(time.time()))
        header[9:11] = struct.pack("<H", 32)
        header[11:15] = struct.pack("<I", len(payload))
        return self._aes(bytes(header)) + self._aes(payload[:32]) + payload[32:]

    # -- session lifecycle ---------------------------------------------

    def open(self) -> None:
        from quii_helper.protocols.quii.live_bodies import (  # noqa: PLC0415
            build_live_path_body,
        )
        from quii_helper.protocols.quii.live_envelope import (  # noqa: PLC0415
            build_live_play_packet_from_body,
            build_live_setup_packet,
        )

        client = self._client
        client.connect(timeout=5)
        setup = bytearray(build_live_setup_packet())
        setup[9] = 2  # talk dialect (live uses 0)
        client.transport.send_all(bytes(setup))
        reply = client.recv_setup()
        if reply["status"] != 0 or reply["crypto_mode"] != 2:
            raise RuntimeError("door station rejected the talk session")

        body = build_live_path_body(_USER, self._password, _TALK_PATH)
        _h, _b, packet = build_live_play_packet_from_body(
            body,
            packet_type=0x0B,
            ext_len_low=0xFF,
            ext_len_high=0xFF,
            play_param=0,
            stream_flag=0,
            crypto_mode=2,
            key=self._key,
            encrypt=True,
        )
        client.transport.send_all(packet)
        _reply_wait(client, "open", 1, 1.0)
        client.transport.send_all(self._request(0x0C, 1, _SAMPLE_RATE))
        _reply_wait(client, "0C", 1, 0.6)
        client.transport.send_all(self._request(0x0D, 1))
        _reply_wait(client, "0D", 1, 0.6)
        self._last_keepalive = time.monotonic()

    def send_frame(self, alaw: bytes) -> None:
        self._client.transport.send_all(self._audio(alaw))
        now = time.monotonic()
        if now - self._last_keepalive >= _KEEPALIVE_S:
            from quii_helper.protocols.quii.live_packets import (  # noqa: PLC0415
                build_live_keepalive_packet,
            )

            _h, _b, packet = build_live_keepalive_packet(
                crypto_mode=2, key=self._key, encrypt=True
            )
            self._client.transport.send_all(packet)
            self._last_keepalive = now

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def speak(
    host: str,
    creds: StreamCredentials,
    source: str,
    cancel: threading.Event | None = None,
) -> None:
    """Decode ``source`` with FFmpeg and play it through the door speaker.

    ``source`` is anything FFmpeg can read (URL or local file).  Audio is
    paced in real time and capped at :data:`MAX_TALK_SECONDS`.
    """
    session = TalkSession(host, creds)
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-i", source,
            "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE), "-f", "alaw",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        session.open()
        start = time.monotonic()
        frames = 0
        while True:
            if cancel is not None and cancel.is_set():
                break
            chunk = ffmpeg.stdout.read(_FRAME_BYTES)  # type: ignore[union-attr]
            if not chunk:
                break
            if len(chunk) < _FRAME_BYTES:
                chunk += b"\xd5" * (_FRAME_BYTES - len(chunk))
            session.send_frame(chunk)
            frames += 1
            if frames * _FRAME_S > MAX_TALK_SECONDS:
                _LOGGER.warning("wT7 talk capped at %.0f s", MAX_TALK_SECONDS)
                break
            # Real-time pacing against the wall clock (no drift).
            delay = start + frames * _FRAME_S - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        if frames == 0 and not (cancel is not None and cancel.is_set()):
            err = ffmpeg.stderr.read().decode(errors="replace").strip()  # type: ignore[union-attr]
            raise RuntimeError(f"ffmpeg produced no audio: {err[:200] or 'unreadable source'}")
        for _ in range(6):  # let the tail drain, then end on silence
            session.send_frame(_SILENCE)
            time.sleep(_FRAME_S)
    finally:
        if ffmpeg.poll() is None:
            ffmpeg.kill()
        session.close()
