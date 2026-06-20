from gnomecast import ffmpeg

from .conftest import requires_ffmpeg


def test_parse_time():
    assert ffmpeg.parse_time("01:04:21.14") == 3861.14
    assert ffmpeg.parse_time("00:00:00") == 0.0
    assert ffmpeg.parse_time("N/A") == 0.0
    assert ffmpeg.parse_time(None) == 0.0
    assert ffmpeg.parse_time("") == 0.0


def test_parse_size():
    assert ffmpeg.parse_size("1142542kB") == 1142542 * 1024
    assert ffmpeg.parse_size("11KiB") == 11 * 1024
    assert ffmpeg.parse_size("5MiB") == 5 * 1024 ** 2
    assert ffmpeg.parse_size("1.5GiB") == int(1.5 * 1024 ** 3)
    assert ffmpeg.parse_size("N/A") == 0
    assert ffmpeg.parse_size(None) == 0
    assert ffmpeg.parse_size("") == 0


def test_parse_progress_line_legacy():
    line = "frame=92578 fps=3937 q=-1.0 size= 1142542kB time=01:04:21.14 bitrate=2424.1kbits/s speed= 164x"
    d = ffmpeg.parse_progress_line(line)
    assert d["bytes"] == 1142542 * 1024
    assert d["seconds"] == 3861.14
    assert d["frame"] == "92578"


def test_parse_progress_line_modern_lsize_na():
    line = "frame=   20 fps=0.0 q=-1.0 Lsize=      11KiB time=N/A bitrate=N/A speed=61.6x"
    d = ffmpeg.parse_progress_line(line)
    assert d["bytes"] == 11 * 1024
    assert d["seconds"] == 0.0


@requires_ffmpeg
def test_have_ffmpeg():
    assert ffmpeg.have_ffmpeg() is True


@requires_ffmpeg
def test_probe_returns_streams(mkv_file):
    data = ffmpeg.probe(mkv_file)
    kinds = sorted(s["codec_type"] for s in data["streams"])
    assert kinds == ["audio", "subtitle", "video"]
