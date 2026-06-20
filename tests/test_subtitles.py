from gnomecast import subtitles
from gnomecast.media import MediaInfo

from .conftest import requires_ffmpeg


def test_srt_to_webvtt():
    srt = "1\r\n00:00:01,000 --> 00:00:04,000\r\nHello <i>world</i>\r\n"
    vtt = subtitles.srt_to_webvtt(srt)
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:04.000" in vtt
    assert "<i>world</i>" in vtt
    assert "," not in vtt.split("-->")[0].splitlines()[-1]


def test_read_text_handles_bom(tmp_path):
    p = tmp_path / "b.srt"
    p.write_bytes("\ufeffhi".encode("utf-8"))
    assert subtitles.read_text(str(p)) == "hi"


def test_convert_file_vtt_passthrough(tmp_path):
    p = tmp_path / "x.vtt"
    p.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n")
    assert subtitles.convert_file(str(p)).startswith("WEBVTT")


def test_convert_file_srt(srt_file):
    out = subtitles.convert_file(srt_file)
    assert out.startswith("WEBVTT")
    assert "hello world" in out


@requires_ffmpeg
def test_extract_embedded_from_mkv(mkv_file):
    m = MediaInfo.probe(mkv_file)
    subtitles.load_embedded(m)
    sub = m.subtitle_streams[0]
    assert sub.webvtt is not None
    assert sub.webvtt.startswith("WEBVTT")
    assert "hello world" in sub.webvtt
