"""A tiny, dependency-free HTTP server for streaming media to the Chromecast.

Replaces the previous bottle + paste stack with the standard library's
``ThreadingHTTPServer``. It serves two things:

* ``/subtitles.vtt`` - the currently selected WebVTT track, and
* ``/media/<id>.<ext>`` - the current media/transcode file, with HTTP
  ``Range`` support (required by Chromecast) and a ``wait_for_byte`` hook so we
  can stream a file that is still being transcoded.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

log = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 1024 * 1024


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def local_ip() -> Optional[str]:
    """Best-effort detection of the LAN IP the Chromecast can reach us on."""
    try:
        hostname_ips = [
            ip
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]
            if not ip.startswith("127.")
        ]
        if hostname_ips:
            return hostname_ips[0]
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
    except OSError:
        return None


class MediaServer:
    """Owns the HTTP server and the (mutable) current playback state."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None):
        self.host = host or local_ip() or "0.0.0.0"
        self.port = port or pick_free_port()

        # Updated by the application before each play.
        self.media_path_provider: Callable[[], Optional[str]] = lambda: None
        self.subtitles_provider: Callable[[], Optional[str]] = lambda: None
        self.wait_for_byte: Callable[[int], None] = lambda offset: None

        self._httpd: Optional[ThreadingHTTPServer] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def media_url(self, key, ext: str) -> str:
        return f"{self.base_url}/media/{key}.{ext}"

    @property
    def subtitles_url(self) -> str:
        return f"{self.base_url}/subtitles.vtt"

    def serve_forever(self) -> None:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        log.info("serving media on %s", self.base_url)
        self._httpd.serve_forever()

    def shutdown(self) -> None:  # pragma: no cover - lifecycle
        if self._httpd:
            self._httpd.shutdown()


def _make_handler(server: MediaServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # Quieter logging routed through the logging module.
        def log_message(self, fmt, *args):  # noqa: N802
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_HEAD(self):  # noqa: N802
            self._dispatch(head_only=True)

        def do_GET(self):  # noqa: N802
            self._dispatch(head_only=False)

        # -- routing -------------------------------------------------------

        def _dispatch(self, head_only: bool):
            path = self.path.split("?", 1)[0]
            if path == "/subtitles.vtt":
                self._serve_subtitles(head_only)
            elif path.startswith("/media/"):
                self._serve_media(head_only)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _serve_subtitles(self, head_only: bool):
            subs = server.subtitles_provider()
            if subs is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = subs.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/vtt; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _serve_media(self, head_only: bool):
            media_path = server.media_path_provider()
            if not media_path:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            start, end = self._parse_range()
            if start is not None:
                server.wait_for_byte(start)

            try:
                size = os.path.getsize(media_path)
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            if start is None:
                self._send_full(media_path, size, head_only)
            else:
                self._send_range(media_path, size, start, end, head_only)

        # -- helpers -------------------------------------------------------

        def _parse_range(self):
            header = self.headers.get("Range")
            if not header:
                return None, None
            m = _RANGE_RE.search(header)
            if not m:
                return None, None
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else None
            return start, end

        def _common_headers(self):
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self._cors()

        def _send_full(self, path, size, head_only):
            self.send_response(HTTPStatus.OK)
            self._common_headers()
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if not head_only:
                self._copy(path, 0, size - 1)

        def _send_range(self, path, size, start, end, head_only):
            if end is None or end >= size:
                end = size - 1
            if start >= size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self._common_headers()
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if not head_only:
                self._copy(path, start, end)

        def _copy(self, path, start, end):
            remaining = end - start + 1
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    while remaining > 0:
                        chunk = f.read(min(_CHUNK, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                log.debug("client disconnected while streaming %s", path)

    return Handler
