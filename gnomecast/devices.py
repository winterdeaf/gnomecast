"""Chromecast hardware capability table and codec decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Codecs a Chromecast can play natively (no transcode needed).
PLAYABLE_VIDEO_H265 = frozenset({"h264", "h265", "hevc"})
PLAYABLE_VIDEO_BASE = frozenset({"h264"})
PLAYABLE_AUDIO_AC3 = frozenset({"aac", "mp3", "ac3"})
PLAYABLE_AUDIO_BASE = frozenset({"aac", "mp3"})

#: Containers a Chromecast can stream without remuxing.
NATIVE_VIDEO_CONTAINERS = frozenset({"mp4"})
AUDIO_CONTAINERS = frozenset({"aac", "mp3", "wav"})


@dataclass(frozen=True)
class Capabilities:
    """Per-device codec support. ``None`` means "use the safe default"."""

    h265: Optional[bool] = None
    ac3: Optional[bool] = None


# Keyed by (manufacturer, model_name) as reported by pychromecast.
HARDWARE = {
    ("Unknown manufacturer", "Chromecast"): Capabilities(h265=False, ac3=False),
    ("Unknown manufacturer", "Chromecast Ultra"): Capabilities(h265=True, ac3=True),
    ("Unknown manufacturer", "Google Home Mini"): Capabilities(h265=False, ac3=False),
    ("Unknown manufacturer", "Google Home"): Capabilities(h265=False, ac3=False),
    ("VIZIO", "P75-F1"): Capabilities(h265=True, ac3=True),
}


def capabilities_for(manufacturer: str, model: str, cast_type: str = "cast") -> Capabilities:
    """Return :class:`Capabilities` for a device, with sensible fallbacks."""
    caps = HARDWARE.get((manufacturer, model))
    if caps is not None:
        return caps
    # Audio-only devices never do video/h265.
    if cast_type == "audio":
        return Capabilities(h265=False, ac3=None)
    return Capabilities()


def can_play_video(codec: str, caps: Capabilities, cast_type: str = "cast") -> bool:
    h265 = caps.h265
    if cast_type == "audio":
        h265 = False
    if h265 is None:
        h265 = True  # default: assume modern device with h265
    allowed = PLAYABLE_VIDEO_H265 if h265 else PLAYABLE_VIDEO_BASE
    return codec in allowed


def can_play_audio(codec: Optional[str], caps: Capabilities) -> bool:
    if not codec:
        return True
    # Only treat an existing ac3 stream as playable when ac3 is known-supported
    # (matches historical behaviour: unknown -> transcode to be safe).
    allowed = PLAYABLE_AUDIO_AC3 if caps.ac3 else PLAYABLE_AUDIO_BASE
    return codec in allowed
