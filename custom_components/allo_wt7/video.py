"""LAN video bridge to the wT7 using the open-source ``quii-helper`` library.

The wT7 speaks the proprietary Quvii/Qualvision ``tdkcloud`` protocol over a
direct LAN TCP channel (port 34567, AES-256).  Rather than reimplementing that
stack, this integration uses the MIT-licensed ``quii-helper`` package, which
has been tested against this device family, and drives it in *LAN mode* with
credentials the integration fetches from the cloud (``data-encode-key`` ->
stream key, plus the paired auth code).

**The device allows only ONE live video session at a time** (a second concurrent
play is rejected with "status 11").  That one session is shared with the
official Allo Plus app, so this module is deliberately frugal with it:

- All device access is serialized through :class:`WT7Video` (one per device).
- While a live stream is running, stills are pulled from our *own* local RTSP
  server (one FFmpeg frame) instead of opening a second device session.
- The live stream is released as soon as nobody is watching (idle watchdog),
  with a hard cap as a backstop, so the Allo Plus app is free the rest of the
  time.  First viewer wins: if the app already holds the session our attempt
  fails cleanly rather than kicking it.

``quii-helper``'s ``Camera`` API is synchronous (blocking sockets + an FFmpeg
subprocess), so every public call here is meant to run inside a Home Assistant
executor thread, never on the event loop.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .client import StreamCredentials
from .const import STREAM_PORT

_LOGGER = logging.getLogger(__name__)

# Idle-release tuning for the shared single video session.
_STARTUP_GRACE_S = 30.0   # stop if no viewer ever connects within this window
_IDLE_GRACE_S = 12.0      # stop this long after the last viewer disconnects
_MAX_SESSION_S = 600.0    # absolute cap so HA can never hog the camera forever
_FFMPEG_STILL_TIMEOUT = 20
_LIVE_SETTLE_S = 3.0      # time for a freshly started live session to be accepted

# The wT7 encodes anamorphic video: coded 960x1080 with a 2:1 pixel aspect
# ratio (SAR), i.e. a 16:9 (1920x1080) display picture.  HA sizes the camera
# by the still image and does not apply the SAR, so a raw 960x1080 still makes
# the whole camera render tall/squished.  We normalize to square pixels at the
# real display resolution so stills (and the aspect HA assumes) are correct.
_DISPLAY_SCALE = "scale=1920:1080,setsar=1"


def _sar_correct(jpeg: bytes) -> bytes:
    """Rescale a raw 960x1080 JPEG to its true 16:9 display geometry."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", "pipe:0", "-vf", _DISPLAY_SCALE,
         "-f", "mjpeg", "pipe:1"],
        input=jpeg,
        capture_output=True,
        timeout=_FFMPEG_STILL_TIMEOUT,
    )
    if proc.returncode != 0 or not proc.stdout:
        return jpeg  # fall back to the uncorrected still rather than failing
    return proc.stdout


def _build_lan_camera(
    host: str,
    creds: StreamCredentials,
    channel: int,
    stream: int,
    data_dir: str | Path | None = None,
):
    """Construct a LAN-mode quii-helper Camera (no .env, no cloud, no assets).

    Imported lazily so the dependency is only required when video is actually
    used, and a missing/broken install surfaces as a clear runtime error rather
    than breaking the whole integration import.
    """
    from quii_helper import Camera  # noqa: PLC0415

    return Camera(
        connection_mode="lan",
        device_host=host,
        device_id=creds.umid,
        stream_key=creds.stream_key,
        auth_code=creds.auth_code,
        stream_port=STREAM_PORT,
        channel=channel,
        stream=stream,
        tls_verify=False,
        data_dir=data_dir,
    )


def _direct_snapshot(
    host: str, creds: StreamCredentials, channel: int, stream: int
) -> bytes:
    """Open a short LAN session, capture one JPEG, return its bytes."""
    with tempfile.TemporaryDirectory(prefix="wt7_snap_") as tmp:
        cam = _build_lan_camera(host, creds, channel, stream, data_dir=tmp)
        path = cam.snapshot(timeout_seconds=_FFMPEG_STILL_TIMEOUT)
        return _sar_correct(Path(path).read_bytes())


def _grab_from_rtsp(url: str) -> bytes:
    """Pull a single JPEG frame from our own local RTSP server via FFmpeg.

    This reuses the already-open stream session (no second device session).
    """
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-rtsp_transport", "tcp", "-i", url,
            "-frames:v", "1", "-q:v", "2", "-vf", _DISPLAY_SCALE,
            "-f", "mjpeg", "pipe:1",
        ],
        capture_output=True,
        timeout=_FFMPEG_STILL_TIMEOUT,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg could not grab a frame from the live stream")
    return proc.stdout


class _RtspBridge:
    """Runs one quii-helper RTSP server in a daemon thread, released when idle.

    HA's ``stream`` component connects to the local RTSP URL as a viewer; when
    it stops viewing the viewer count drops to zero and the watchdog tears the
    session down, freeing the device's single video slot for the Allo Plus app.
    """

    def __init__(
        self, host: str, rtsp_port: int, channel: int, stream: int
    ) -> None:
        self._host = host
        self._rtsp_port = rtsp_port
        self._channel = channel
        self._stream = stream
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._holds = 0
        self._holds_lock = threading.Lock()

    def hold(self, delta: int) -> None:
        """Keep the device session open (+1) / allow idle release again (-1)."""
        with self._holds_lock:
            self._holds = max(0, self._holds + delta)

    @property
    def url(self) -> str:
        return f"rtsp://127.0.0.1:{self._rtsp_port}/live"

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, creds: StreamCredentials) -> str:
        """Blocking: start the server (if needed) and return its URL."""
        if self.is_running():
            return self.url
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(creds,),
            name=f"wt7_rtsp_{self._rtsp_port}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=20)
        return self.url

    def _run(self, creds: StreamCredentials) -> None:
        try:
            cam = _build_lan_camera(
                self._host, creds, self._channel, self._stream
            )
            with cam.serve_rtsp(port=self._rtsp_port) as stream:
                self._ready.set()
                self._watch(stream)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("wT7 RTSP bridge crashed")
        finally:
            self._ready.set()  # unblock any waiter even on failure

    def _watch(self, stream) -> None:
        """Hold the session open until idle, capped, or stopped."""
        started = time.monotonic()
        last_active = started
        grace_from = started
        seen_viewer = False
        while not self._stop.is_set():
            try:
                clients = stream.server.client_count
            except Exception:  # noqa: BLE001
                clients = 0
            now = time.monotonic()
            if self._holds > 0:
                # Talk-back in progress: keep the session, restart the clocks.
                last_active = grace_from = now
            if clients > 0:
                seen_viewer = True
                last_active = now
            if seen_viewer and clients == 0 and now - last_active > _IDLE_GRACE_S:
                _LOGGER.debug("wT7 stream idle — releasing device session")
                break
            if not seen_viewer and now - grace_from > _STARTUP_GRACE_S:
                _LOGGER.debug("wT7 stream had no viewer — releasing session")
                break
            if now - started > _MAX_SESSION_S:
                _LOGGER.debug("wT7 stream hit max session cap — releasing")
                break
            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)
        self._thread = None


class WT7Video:
    """Per-device manager that serializes the single shared video session."""

    def __init__(
        self, host: str, rtsp_port: int, *, channel: int = 1, stream: int = 2
    ) -> None:
        self._host = host
        self._rtsp_port = rtsp_port
        self._channel = channel
        self._stream = stream
        self._lock = threading.Lock()
        self._bridge: _RtspBridge | None = None
        self._talk_lock = threading.Lock()

    def _streaming(self) -> bool:
        return self._bridge is not None and self._bridge.is_running()

    def snapshot_bytes(self, creds: StreamCredentials) -> bytes:
        """Blocking (executor): one JPEG, from the live stream if one is up."""
        with self._lock:
            if not self._streaming():
                # No stream: a brief dedicated session is safe.
                return _direct_snapshot(
                    self._host, creds, self._channel, self._stream
                )
            url = self._bridge.url  # type: ignore[union-attr]
        try:
            return _grab_from_rtsp(url)
        except Exception:  # noqa: BLE001
            # Stream went away between the check and the grab — fall back.
            with self._lock:
                return _direct_snapshot(
                    self._host, creds, self._channel, self._stream
                )

    def stream_url(self, creds: StreamCredentials) -> str:
        """Blocking (executor): ensure the RTSP bridge is up, return its URL."""
        with self._lock:
            if not self._streaming():
                self._bridge = _RtspBridge(
                    self._host, self._rtsp_port, self._channel, self._stream
                )
                self._bridge.start(creds)
            return self._bridge.url  # type: ignore[union-attr]

    def send_audio(
        self,
        creds: StreamCredentials,
        source: str,
        cancel: threading.Event | None = None,
    ) -> None:
        """Blocking (executor): play ``source`` through the door speaker.

        The door only plays talk-back audio while a live video session is
        open, so the live bridge is started (or reused) and held for the
        duration, then released to the normal idle watchdog.
        """
        from .talk import speak  # noqa: PLC0415

        with self._talk_lock:
            with self._lock:
                fresh = not self._streaming()
                if fresh:
                    self._bridge = _RtspBridge(
                        self._host, self._rtsp_port, self._channel, self._stream
                    )
                    self._bridge.start(creds)
                bridge = self._bridge
                bridge.hold(1)  # type: ignore[union-attr]
            try:
                if fresh:
                    time.sleep(_LIVE_SETTLE_S)  # let the device accept play
                if not bridge.is_running():  # type: ignore[union-attr]
                    raise RuntimeError(
                        "the wT7 video session is busy (is the Allo Plus app "
                        "open?) — talk-back needs it"
                    )
                speak(self._host, creds, source, cancel)
            finally:
                bridge.hold(-1)  # type: ignore[union-attr]

    def stop(self) -> None:
        """Release the device session (called on unload)."""
        with self._lock:
            if self._bridge is not None:
                self._bridge.stop()
                self._bridge = None
