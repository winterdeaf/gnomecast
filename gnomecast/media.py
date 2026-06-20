"""Media metadata model, built from ffprobe's structured JSON output.

The previous implementation scraped ``ffmpeg -i`` stderr with regexes, which
missed subtitle streams in several Matroska/MKV files. Parsing ffprobe JSON is
both stable and reliably enumerates every stream.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from . import ffmpeg

log = logging.getLogger(__name__)

#: Subtitle codecs that are text based and can be converted to WebVTT.
#: Image based subtitles (PGS/VOBSUB/DVB) cannot be converted to WebVTT and are
#: not supported for casting; they are still surfaced but flagged
#: ``text_based=False`` and excluded from the subtitle selector.
TEXT_SUBTITLE_CODECS = frozenset(
    {"subrip", "srt", "ass", "ssa", "webvtt", "vtt", "mov_text", "text", "stl"}
)

_CHANNEL_LABELS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}


@dataclass(kw_only=True)
class Stream:
    """A single media stream."""

    index: int
    codec: Optional[str]
    title: str
    language: Optional[str] = None

    @property
    def map_spec(self) -> str:
        """ffmpeg ``-map`` selector for this stream (input 0)."""
        return f"0:{self.index}"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.title


@dataclass(kw_only=True)
class AudioStream(Stream):
    channels: int = 2

    @property
    def channels_label(self) -> str:
        return _CHANNEL_LABELS.get(self.channels, str(self.channels))

    def details(self) -> str:
        return f"{self.title} ({self.codec}/{self.channels_label})"


@dataclass(kw_only=True)
class SubtitleStream(Stream):
    text_based: bool = True
    #: Filled in by :func:`gnomecast.subtitles.extract_embedded`.
    webvtt: Optional[str] = None


@dataclass(kw_only=True)
class MediaInfo:
    path: str
    container: str
    duration: float = 0.0
    video_streams: List[Stream] = field(default_factory=list)
    audio_streams: List[AudioStream] = field(default_factory=list)
    subtitle_streams: List[SubtitleStream] = field(default_factory=list)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_probe(cls, path: str, data: dict) -> "MediaInfo":
        """Build a :class:`MediaInfo` from an ffprobe JSON *data* dict."""
        fmt = data.get("format", {}) or {}
        duration = _to_float(fmt.get("duration"))
        info = cls(
            path=path,
            container=os.path.splitext(path)[1].lstrip(".").lower(),
            duration=duration,
        )
        n_video = n_audio = n_sub = 0
        for s in data.get("streams", []):
            # Parse each stream defensively: a single unexpected stream must
            # never prevent the others (e.g. subtitles) from being detected.
            try:
                kind = s.get("codec_type")
                tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
                language = tags.get("language")
                index = int(s.get("index", 0))
                codec = s.get("codec_name")
                if kind == "video":
                    # Skip cover-art / attached pictures masquerading as video.
                    if (s.get("disposition") or {}).get("attached_pic"):
                        continue
                    n_video += 1
                    info.video_streams.append(
                        Stream(
                            index=index,
                            codec=codec,
                            language=language,
                            title=tags.get("title") or f"Video #{n_video}",
                        )
                    )
                elif kind == "audio":
                    n_audio += 1
                    info.audio_streams.append(
                        AudioStream(
                            index=index,
                            codec=codec,
                            language=language,
                            title=tags.get("title") or _default_label("Audio", n_audio, language),
                            channels=_to_int(s.get("channels"), 2),
                        )
                    )
                elif kind == "subtitle":
                    n_sub += 1
                    info.subtitle_streams.append(
                        SubtitleStream(
                            index=index,
                            codec=codec,
                            language=language,
                            title=tags.get("title") or _default_label("Subtitle", n_sub, language),
                            text_based=codec in TEXT_SUBTITLE_CODECS,
                        )
                    )
            except Exception:  # pragma: no cover - guards against odd probes
                log.exception("skipping unparseable stream: %r", s)
        return info

    @classmethod
    def probe(cls, path: str) -> "MediaInfo":
        """Probe *path* with ffprobe and return a :class:`MediaInfo`."""
        return cls.from_probe(path, ffmpeg.probe(path))

    # -- convenience --------------------------------------------------------

    @property
    def text_subtitles(self) -> List[SubtitleStream]:
        return [s for s in self.subtitle_streams if s.text_based]

    def details(self) -> str:
        return "\n".join(
            [
                f"File: {os.path.basename(self.path)}",
                "Video: " + ", ".join(f"{s.title} ({s.codec})" for s in self.video_streams),
                "Audio: " + ", ".join(s.details() for s in self.audio_streams),
                "Subtitles: " + ", ".join(s.title for s in self.subtitle_streams),
            ]
        )


def _default_label(prefix: str, n: int, language: Optional[str]) -> str:
    if language:
        return f"{prefix} #{n} ({language})"
    return f"{prefix} #{n}"


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
