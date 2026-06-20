from gnomecast.media import AudioStream, MediaInfo, SubtitleStream

from .conftest import requires_ffmpeg

SYNTHETIC = {
    "format": {"duration": "123.45"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc",
         "tags": {"title": "Main"}},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "channels": 6, "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"title": "English", "language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"title": "PGS"}},
        # attached cover art must be ignored as a video stream
        {"index": 4, "codec_type": "video", "codec_name": "mjpeg",
         "disposition": {"attached_pic": 1}},
    ],
}


def test_from_probe_basic():
    m = MediaInfo.from_probe("/x/file.mkv", SYNTHETIC)
    assert m.container == "mkv"
    assert m.duration == 123.45
    assert len(m.video_streams) == 1
    assert m.video_streams[0].codec == "hevc"
    assert m.video_streams[0].map_spec == "0:0"


def test_audio_channels_label():
    m = MediaInfo.from_probe("/x/file.mkv", SYNTHETIC)
    a = m.audio_streams[0]
    assert isinstance(a, AudioStream)
    assert a.channels == 6
    assert a.channels_label == "5.1"
    assert "ac3/5.1" in a.details()
    # title falls back to language-tagged default
    assert "eng" in a.title


def test_subtitle_text_vs_image():
    m = MediaInfo.from_probe("/x/file.mkv", SYNTHETIC)
    assert len(m.subtitle_streams) == 2
    text = m.text_subtitles
    assert len(text) == 1
    assert text[0].codec == "subrip"
    assert text[0].title == "English"
    pgs = [s for s in m.subtitle_streams if not s.text_based][0]
    assert isinstance(pgs, SubtitleStream)
    assert pgs.codec == "hdmv_pgs_subtitle"


def test_complex_probe_lists_all_subtitles_including_image():
    """Regression: image-only subtitle tracks must still be listed, and an
    odd stream (missing channels, data/attachment) must not drop the rest."""
    probe = {
        "format": {"duration": "7849.5"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio", "codec_name": "ac3", "channels": 6},
            {"index": 2, "codec_type": "audio", "codec_name": "dts"},  # no channels
            {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
             "tags": {"language": "eng"}},
            {"index": 5, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
             "tags": {"language": "fre"}},
            {"index": 6, "codec_type": "attachment", "codec_name": "ttf"},
            {"index": 7, "codec_type": "data"},
        ],
    }
    m = MediaInfo.from_probe("/x/film.mkv", probe)
    assert len(m.video_streams) == 1
    assert [a.channels for a in m.audio_streams] == [6, 2]  # missing -> default 2
    # both image subtitles are listed even though none are text-based
    assert len(m.subtitle_streams) == 2
    assert m.text_subtitles == []
    assert all(not s.text_based for s in m.subtitle_streams)
    assert m.subtitle_streams[0].map_spec == "0:4"


@requires_ffmpeg
def test_probe_real_mkv_detects_subtitle(mkv_file):
    """Regression: subtitle tracks in MKV containers must be detected."""
    m = MediaInfo.probe(mkv_file)
    assert len(m.video_streams) == 1
    assert len(m.audio_streams) == 1
    assert len(m.subtitle_streams) == 1
    assert m.subtitle_streams[0].text_based
    assert m.duration > 1.0
