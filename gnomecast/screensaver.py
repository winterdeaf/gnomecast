"""Optional screensaver inhibition over D-Bus.

D-Bus is optional: if ``dbus`` isn't importable (or no screensaver service is
found) this degrades to a no-op rather than crashing.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    import dbus  # type: ignore
    _DBUS_AVAILABLE = True
except Exception:  # pragma: no cover - depends on host
    dbus = None
    _DBUS_AVAILABLE = False

_SCREENSAVER_PATHS = [
    ("org.freedesktop.ScreenSaver", "/ScreenSaver"),
    ("org.mate.ScreenSaver", "/ScreenSaver"),
]


class ScreenSaver:
    """Inhibit/uninhibit the desktop screensaver while casting."""

    def __init__(self):
        self._iface = self._find_interface()
        self._cookie = None

    def _find_interface(self):
        if not _DBUS_AVAILABLE:
            return None
        try:
            bus = dbus.SessionBus()
        except Exception as e:  # pragma: no cover
            log.debug("no dbus session bus: %s", e)
            return None
        for name, path in _SCREENSAVER_PATHS:
            try:
                obj = bus.get_object(name, path)
                return dbus.Interface(obj, dbus_interface=name)
            except Exception as e:  # pragma: no cover
                log.debug("screensaver %s unavailable: %s", name, e)
        return None

    def inhibit(self) -> None:
        if not self._iface or self._cookie is not None:
            return
        try:
            self._cookie = self._iface.Inhibit("Gnomecast", "Playing media")
            log.debug("screensaver inhibited")
        except Exception as e:  # pragma: no cover
            log.debug("could not inhibit screensaver: %s", e)

    def restore(self) -> None:
        if not self._iface or self._cookie is None:
            return
        try:
            self._iface.UnInhibit(self._cookie)
            log.debug("screensaver restored")
        except Exception as e:  # pragma: no cover
            log.debug("could not restore screensaver: %s", e)
        finally:
            self._cookie = None
