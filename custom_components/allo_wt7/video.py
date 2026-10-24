"""LAN video bridge to the wT7 using the open-source ``quii-helper`` library.

The wT7 speaks the proprietary Quvii/Qualvision ``tdkcloud`` protocol over a
direct LAN TCP channel (port 34567, AES-256).  Rather than reimplementing that
stack, this integration uses the MIT-licensed ``quii-helper`` package, which
has been tested against this device family, and drives it in *LAN mode* with
credentials the integration already fetches from the cloud
(``data-encode-key`` -> stream key, plus the paired auth code).

``quii-helper``'s ``Camera`` API is synchronous (blocking sockets + an FFmpeg
subprocess), so every call here is meant to run inside a Home Assistant
executor thread, never on the event loop.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from .client import StreamCredentials
from .const import STREAM_PORT

_LOGGER = logging.getLogger(__name__)


def _build_lan_camera(
    host: str,
    creds: StreamCredentials,
    channel: int,
    stream: int,
    data_dir: str | Path | None = None,
):
    """Construct a LAN-mode quii-helper Camera (no .env, no cloud, no assets).

    Imported lazily so the dependency is only required when video is actually
    used, and so a missing/broken install degrades to a clear error at call
    time instead of breaking the whole integration import.
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


def grab_snapshot_bytes(
    host: str,
    creds: StreamCredentials,
    *,
    channel: int = 1,
    stream: int = 2,
    timeout_seconds: int = 20,
) -> bytes:
    """Blocking: open a LAN session, capture one JPEG, return its bytes.

    Runs entirely in a temp directory that is cleaned up before returning.
    """
    with tempfile.TemporaryDirectory(prefix="wt7_snap_") as tmp:
        # data_dir in the constructor keeps generated files out of the HA
        # config dir and in a directory cleaned up on return.
        cam = _build_lan_camera(host, creds, channel, stream, data_dir=tmp)
        path = cam.snapshot(timeout_seconds=timeout_seconds)
        return Path(path).read_bytes()


class WT7VideoBridge:
    """Owns a long-lived quii-helper RTSP server for the camera entity.

    HA's ``stream`` component consumes a URL, so this serves the decoded live
    H.264 at ``rtsp://127.0.0.1:<port>/live`` and hands that URL to HA.  The
    server runs in its own daemon thread (quii-helper's ``serve_rtsp`` is a
    blocking context manager); :meth:`start` is idempotent and :meth:`stop`
    tears the thread down cleanly on unload.
    """

    def __init__(
        self,
        host: str,
        rtsp_port: int,
        *,
        channel: int = 1,
        stream: int = 2,
    ) -> None:
        self._host = host
        self._rtsp_port = rtsp_port
        self._channel = channel
        self._stream = stream
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._url: str | None = None

    @property
    def url(self) -> str:
        return f"rtsp://127.0.0.1:{self._rtsp_port}/live"

    def start(self, creds: StreamCredentials) -> str:
        """Blocking: ensure the RTSP server is running and return its URL.

        Idempotent — if the server is already up it just returns the URL.
        """
        with self._lock:
            if self._thread and self._thread.is_alive():
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
        # Wait briefly for the session to come up so the URL is usable when we
        # return it; a timeout here is non-fatal (HA retries the source).
        self._ready.wait(timeout=20)
        return self.url

    def _run(self, creds: StreamCredentials) -> None:
        try:
            cam = _build_lan_camera(
                self._host, creds, self._channel, self._stream
            )
            with cam.serve_rtsp(port=self._rtsp_port):
                self._ready.set()
                # Block until stop() is called; the context keeps the LAN
                # session + RTSP server alive for HA to pull from.
                self._stop.wait()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("wT7 RTSP bridge crashed")
        finally:
            self._ready.set()  # unblock any waiter even on failure

    def stop(self) -> None:
        """Signal the server thread to exit and wait for it."""
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)
        self._thread = None
