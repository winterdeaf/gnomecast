"""Subtitle handling without third-party caption libraries.

Chromecast only accepts WebVTT over HTTP. We need to turn three things into
WebVTT:

* embedded subtitle *streams* inside the media container, and
* external subtitle *files* the user supplies (``.srt``, ``.vtt``, ``.ass`` ...).

SubRip (``.srt``) is converted with a tiny, dependency-free routine. Everything
else is handed to ffmpeg's WebVTT muxer. This removes the old pycaption →
cssutils dependency chain entirely.
"""

from __future__ import annotations

import logging
import re
import subprocess

from . import ffmpeg
from .media import MediaInfo, SubtitleStream

log = logging.getLogger(__name__)

WEBVTT_HEADER = "WEBVTT\n\n"

# SRT timestamps use a comma decimal separator; WebVTT uses a dot.
_SRT_TS_RE = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


def read_text(path: str) -> str:
    """Read a subtitle file as text, tolerating latin-1 and a leading BOM."""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def srt_to_webvtt(text: str) -> str:
    """Convert SubRip text to WebVTT (no external dependencies)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    body = _SRT_TS_RE.sub(r"\1.\2", text).strip()
    return WEBVTT_HEADER + body + "\n"


def _ffmpeg_to_webvtt(args: list) -> str:
    """Run ffmpeg with *args*, muxing WebVTT to stdout, and return it."""
    ffmpeg.require_ffmpeg()
    cmd = [ffmpeg.FFMPEG, "-y", "-loglevel", "error", *args, "-f", "webvtt", "-"]
    log.debug("subtitle convert: %s", " ".join(cmd))
    out = subprocess.check_output(cmd, stderr=subprocess.PIPE)
    return out.decode("utf-8", "replace")


def convert_file(path: str) -> str:
    """Return the WebVTT representation of an external subtitle *file*."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext == "vtt":
        return read_text(path)
    if ext == "srt":
        return srt_to_webvtt(read_text(path))
    # ass/ssa/sub/… -> let ffmpeg do the heavy lifting.
    return _ffmpeg_to_webvtt(["-i", path])


def extract_stream(path: str, stream: SubtitleStream) -> str:
    """Extract one embedded subtitle *stream* from *path* as WebVTT."""
    return _ffmpeg_to_webvtt(["-i", path, "-map", stream.map_spec, "-c:s", "webvtt"])


def load_embedded(media: MediaInfo) -> None:
    """Populate ``webvtt`` on each text-based subtitle stream of *media*.

    Image-based subtitles (PGS/VOBSUB/…) are skipped; failures on individual
    streams are logged and that stream is left without ``webvtt`` rather than
    aborting the whole file.
    """
    for stream in media.subtitle_streams:
        if not stream.text_based:
            log.debug(
                "skipping image-based subtitle stream %s (%s): cannot convert "
                "to WebVTT",
                stream.index, stream.codec,
            )
            continue
        try:
            stream.webvtt = extract_stream(media.path, stream)
        except subprocess.CalledProcessError as e:  # pragma: no cover - ffmpeg edge
            log.warning("could not extract subtitle stream %s: %s", stream.index, e)
