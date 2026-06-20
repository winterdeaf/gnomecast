"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gnomecast",
        description="Cast local audio/video files to a Chromecast.",
    )
    p.add_argument("file", nargs="?", help="media file to load on startup")
    p.add_argument("-d", "--device", help="Chromecast friendly name to use")
    p.add_argument("-s", "--subtitles", help="external subtitle file to load")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="increase logging verbosity (-v, -vv)")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING - min(args.verbose, 2) * 10
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger(__name__).info("gnomecast %s starting", __version__)

    from .transcoder import cleanup_orphans

    cleanup_orphans()

    # Imported lazily so --help/--version work without GTK installed.
    from .app import Gnomecast

    Gnomecast().run(fn=args.file, device=args.device, subtitles=args.subtitles)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
