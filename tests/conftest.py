"""Shared fixtures. Generates real media with ffmpeg so the backend is tested
against actual files (including the MKV-with-subtitles case that regressed)."""

import shutil
import subprocess

import pytest

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")

SRT = """1
00:00:00,000 --> 00:00:01,000
hello world

2
00:00:01,000 --> 00:00:02,000
second cue
"""


@pytest.fixture(scope="session")
def srt_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("subs") / "ext.srt"
    p.write_text(SRT, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="session")
def mkv_file(tmp_path_factory, srt_file):
    """An MKV with H.264 video, AAC audio and an embedded SRT subtitle track."""
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("media") / "sample.mkv"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-i", srt_file,
        "-map", "0:v", "-map", "1:a", "-map", "2:s",
        "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt",
        "-metadata:s:s:0", "title=English", "-metadata:s:s:0", "language=eng",
        "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True)
    return str(out)


@pytest.fixture(scope="session")
def mp4_h264_aac(tmp_path_factory):
    """A plain MP4 (H.264 + AAC) that needs no transcoding."""
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True)
    return str(out)
