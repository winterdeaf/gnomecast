"""Thin, well-tested wrappers around the ``ffmpeg``/``ffprobe`` binaries.

Nothing in here imports GTK or pychromecast, so it can be unit-tested in
isolation (and is, in ``tests/test_ffmpeg.py``).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

#: Resolved lazily so importing the module never fails on a machine without ffmpeg.
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class FFmpegNotFound(RuntimeError):
    """Raised when the ffmpeg/ffprobe binaries cannot be located."""


def have_ffmpeg() -> bool:
    """Return ``True`` if both ffmpeg and ffprobe are on ``PATH``."""
    return shutil.which(FFMPEG) is not None and shutil.which(FFPROBE) is not None


def require_ffmpeg() -> None:
    if not have_ffmpeg():
        raise FFmpegNotFound(
            "ffmpeg/ffprobe not found on PATH; please install the 'ffmpeg' package."
        )


def probe(path: str) -> dict:
    """Return ffprobe's JSON description of *path* as a dict.

    Uses the structured JSON output rather than scraping ffmpeg's stderr,
    which is both stable across ffmpeg versions and reliably enumerates
    subtitle streams in containers like Matroska/MKV.
    """
    require_ffmpeg()
    cmd = [
        FFPROBE,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    log.debug("probing: %s", " ".join(cmd))
    out = subprocess.check_output(cmd)
    return json.loads(out.decode("utf-8", "replace"))


_TIME_RE = re.compile(r"(?P<h>\d+):(?P<m>\d+):(?P<s>\d+(?:\.\d+)?)")
_SIZE_RE = re.compile(r"([\d.]+)\s*([kKmMgG])i?[bB]")
_PROGRESS_TOKEN_RE = re.compile(r"\s+(?==)|(?<==)\s+")


def parse_time(value: Optional[str]) -> float:
    """Convert an ffmpeg ``HH:MM:SS.ms`` string to seconds.

    Modern ffmpeg emits ``N/A`` before the first frame; that and any other
    unparseable value yields ``0.0`` rather than raising.
    """
    if not value:
        return 0.0
    m = _TIME_RE.search(value)
    if not m:
        return 0.0
    return int(m["h"]) * 3600 + int(m["m"]) * 60 + float(m["s"])


def parse_size(value: Optional[str]) -> int:
    """Convert an ffmpeg size token (``1142542kB``, ``11KiB``, ``N/A``) to bytes."""
    if not value:
        return 0
    m = _SIZE_RE.search(value)
    if not m:
        return 0
    mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def parse_progress_line(line: str) -> dict:
    """Parse one ffmpeg ``-stats`` progress line into a ``key=value`` dict.

    Handles the variable whitespace ffmpeg inserts around ``=`` and the
    ``size=``/``Lsize=`` (final line) and ``time=`` fields, including ``N/A``.

    Returns a dict that always contains ``bytes`` (int) and ``seconds`` (float).
    """
    collapsed = _PROGRESS_TOKEN_RE.sub("", line)
    fields = dict(
        tok.split("=", 1) for tok in collapsed.split() if tok.count("=") == 1
    )
    fields["bytes"] = parse_size(fields.get("size") or fields.get("Lsize"))
    fields["seconds"] = parse_time(fields.get("time"))
    return fields
