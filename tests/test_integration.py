"""End-to-end backend flow: probe -> transcode -> serve over HTTP with Range.

Exercises the same pipeline the GTK app drives, without GTK or a Chromecast.
"""

import threading
import time
import urllib.request

from gnomecast import devices, subtitles
from gnomecast.media import MediaInfo
from gnomecast.server import MediaServer
from gnomecast.transcoder import Transcoder

from .conftest import requires_ffmpeg


@requires_ffmpeg
def test_probe_transcode_serve(mkv_file):
    media = MediaInfo.probe(mkv_file)
    subtitles.load_embedded(media)
    assert media.subtitle_streams[0].webvtt.startswith("WEBVTT")

    caps = devices.capabilities_for("Unknown manufacturer", "Chromecast Ultra")
    t = Transcoder(media, media.video_streams[0], media.audio_streams[0], caps)
    for _ in range(300):
        if t.done:
            break
        time.sleep(0.1)
    assert t.done

    srv = MediaServer(host="127.0.0.1")
    srv.media_path_provider = lambda: t.fn
    srv.subtitles_provider = lambda: media.subtitle_streams[0].webvtt
    srv.wait_for_byte = t.wait_for_byte
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        # full fetch
        full = urllib.request.urlopen(srv.media_url("k", "mp4"), timeout=10).read()
        assert len(full) > 0
        # range fetch
        req = urllib.request.Request(srv.media_url("k", "mp4"), headers={"Range": "bytes=0-99"})
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 206
        assert resp.read() == full[:100]
        # subtitles
        vtt = urllib.request.urlopen(srv.subtitles_url, timeout=10).read().decode()
        assert "hello world" in vtt
    finally:
        srv.shutdown()
        t.destroy()
