"""Gnomecast - a native Linux GUI for casting local media to Chromecast.

This package is split into a dependency-light, fully testable backend
(:mod:`gnomecast.ffmpeg`, :mod:`gnomecast.media`, :mod:`gnomecast.subtitles`,
:mod:`gnomecast.server`, :mod:`gnomecast.transcoder`, :mod:`gnomecast.devices`)
and a thin GTK front-end (:mod:`gnomecast.app`).
"""

__version__ = "2.1.0"

__all__ = ["__version__"]
