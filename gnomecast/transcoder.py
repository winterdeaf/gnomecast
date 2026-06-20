"""On-the-fly transcoding to a Chromecast-friendly MP4.

The transcoder is independent of pychromecast and GTK: it takes a
:class:`~gnomecast.media.MediaInfo`, the chosen streams and the target device
:class:`~gnomecast.devices.Capabilities`, and exposes progress plus a
``wait_for_byte`` hook used by the HTTP server to stream while transcoding.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
from typing import Callable, Optional

from . import devices, ffmpeg
from .media import AudioStream, MediaInfo, Stream

log = logging.getLogger(__name__)

#: 128 MiB look-ahead window when streaming a (fast-start) mp4 while it grows.
DEFAULT_BUFFER = 128 * 1024 * 1024


class Transcoder:
    def __init__(
        self,
        media: MediaInfo,
        video_stream: Optional[Stream],
        audio_stream: Optional[AudioStream],
        caps: devices.Capabilities,
        cast_type: str = "cast",
        *,
        on_progress: Optional[Callable[["Transcoder"], None]] = None,
        on_done: Optional[Callable[["Transcoder"], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        force_audio: bool = False,
        force_video: bool = False,
        tmp_dir: Optional[str] = None,
        autostart: bool = True,
    ):
        self.media = media
        self.source_fn = media.path
        self.video_stream = video_stream
        self.audio_stream = audio_stream
        self.caps = caps
        self.cast_type = cast_type
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_error = on_error

        self.p: Optional[subprocess.Popen] = None
        self.trans_fn: Optional[str] = None
        self.progress_bytes = 0
        self.progress_seconds = 0.0
        self.done = False

        # -- transcode decision --------------------------------------------
        container = media.container
        self.transcode_container = (
            container not in devices.NATIVE_VIDEO_CONTAINERS
            and container not in devices.AUDIO_CONTAINERS
        )
        self.transcode_video = bool(video_stream) and (
            force_video or not devices.can_play_video(video_stream.codec, caps, cast_type)
        )
        self.transcode_audio = bool(audio_stream) and (
            force_audio or not devices.can_play_audio(audio_stream.codec, caps)
        )
        self.transcode = (
            self.transcode_container or self.transcode_video or self.transcode_audio
        )
        log.info(
            "transcode=%s (container=%s video=%s audio=%s)",
            self.transcode,
            self.transcode_container,
            self.transcode_video,
            self.transcode_audio,
        )

        if not self.transcode:
            self.done = True
            if self.on_done:
                self.on_done(self)
            return

        self.trans_fn = _make_temp_mp4(tmp_dir)
        self.cmd = self.build_command()
        log.info("transcode cmd: %s", " ".join(self.cmd))
        if autostart:
            self.start()

    # -- ffmpeg command ----------------------------------------------------

    def _audio_target_codec(self) -> str:
        # Multichannel -> ac3 when the device (probably) supports it, else mp3.
        ac3 = self.caps.ac3
        if (ac3 or ac3 is None) and self.audio_stream and self.audio_stream.channels > 2:
            return "ac3"
        return "mp3"

    def build_command(self) -> list:
        cmd = [
            ffmpeg.FFMPEG, "-y", "-nostats",
            "-i", self.source_fn,
            "-map", self.video_stream.map_spec,
        ]
        if self.audio_stream:
            if self.transcode_audio:
                cmd += ["-map", self.audio_stream.map_spec,
                        "-c:a", self._audio_target_codec(), "-b:a", "256k"]
            else:
                cmd += ["-map", self.audio_stream.map_spec, "-c:a", "copy"]
        if self.transcode_video:
            # libx264 'veryfast' is ~3-5x faster than the default 'medium'
            # preset with only a modest size cost - a good trade for casting.
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        else:
            cmd += ["-c:v", "copy"]
        # faststart relocates the moov atom to the front of the finished file
        # for fast, seekable playback once the (complete) file is served.
        cmd += ["-movflags", "+faststart"]
        cmd += ["-progress", "pipe:1", self.trans_fn]
        return cmd

    @property
    def fn(self) -> str:
        return self.trans_fn if self.transcode else self.source_fn

    # -- execution ---------------------------------------------------------

    def start(self) -> None:
        self.p = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        t = threading.Thread(target=self._monitor, daemon=True)
        t.start()

    def _monitor(self) -> None:
        assert self.p and self.p.stdout
        for raw in self.p.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "total_size":
                self.progress_bytes = _safe_int(value)
            elif key == "out_time_us":
                self.progress_seconds = _safe_int(value) / 1_000_000
            elif key == "progress" and value == "end":
                break
            if self.on_progress:
                self.on_progress(self)
        self.p.stdout.close()
        ret = self.p.wait()
        if ret:
            err = self.p.stderr.read().decode("utf-8", "replace") if self.p.stderr else ""
            log.error("transcode failed (%s): %s", ret, err)
            if self.on_error:
                self.on_error(err)
            return
        self.done = True
        if self.on_done:
            self.on_done(self)

    def wait_for_byte(self, offset: int, buffer: int = DEFAULT_BUFFER) -> None:
        """Block until *offset* is safely available to serve."""
        if self.done:
            return
        import time

        if self.source_fn.lower().endswith(".mp4"):
            while not self.done and offset > self.progress_bytes + buffer:
                log.debug("waiting for byte %s (have %s)", offset, self.progress_bytes + buffer)
                time.sleep(0.5)
        else:
            while not self.done:
                log.debug("waiting for transcode to finish")
                time.sleep(0.5)

    def destroy(self) -> None:
        if self.p and self.p.poll() is None:
            self.p.terminate()
        if self.trans_fn and os.path.isfile(self.trans_fn):
            try:
                os.remove(self.trans_fn)
            except OSError:
                pass

    def __del__(self):  # pragma: no cover - best effort cleanup
        try:
            self.destroy()
        except Exception:
            pass


def _make_temp_mp4(tmp_dir: Optional[str]) -> str:
    tmp_dir = tmp_dir or ("/var/tmp" if os.path.isdir("/var/tmp") else None)
    fd, path = tempfile.mkstemp(
        suffix=".mp4", prefix="gnomecast_pid%i_transcode_" % os.getpid(), dir=tmp_dir
    )
    os.close(fd)
    os.remove(path)
    return path


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cleanup_orphans() -> None:
    """Remove transcode/thumbnail temp files left behind by dead processes."""
    pid_re = re.compile(r"gnomecast_pid(\d+)_")
    for tmpdir in ("/tmp", "/var/tmp"):
        if not os.path.isdir(tmpdir):
            continue
        for name in os.listdir(tmpdir):
            if not name.startswith("gnomecast_"):
                continue
            path = os.path.join(tmpdir, name)
            m = pid_re.search(name)
            if m and _pid_running(int(m.group(1))):
                continue
            try:
                os.remove(path)
                log.debug("removed orphan temp file %s", path)
            except OSError:
                pass
