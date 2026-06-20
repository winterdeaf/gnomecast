import threading
import time
import urllib.request

import pytest

from gnomecast.server import MediaServer


@pytest.fixture
def running_server(tmp_path):
    data = bytes(range(256)) * 1024  # 256 KiB of known bytes
    media = tmp_path / "movie.mp4"
    media.write_bytes(data)

    srv = MediaServer(host="127.0.0.1")
    srv.media_path_provider = lambda: str(media)
    srv.subtitles_provider = lambda: "WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(srv.subtitles_url, timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    yield srv, data
    srv.shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=5)


def test_full_get(running_server):
    srv, data = running_server
    resp = _get(srv.media_url("k", "mp4"))
    body = resp.read()
    assert resp.status == 200
    assert body == data
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Access-Control-Allow-Origin"] == "*"


def test_range_request(running_server):
    srv, data = running_server
    resp = _get(srv.media_url("k", "mp4"), headers={"Range": "bytes=10-19"})
    body = resp.read()
    assert resp.status == 206
    assert body == data[10:20]
    assert resp.headers["Content-Range"] == f"bytes 10-19/{len(data)}"
    assert resp.headers["Content-Length"] == "10"


def test_open_ended_range(running_server):
    srv, data = running_server
    resp = _get(srv.media_url("k", "mp4"), headers={"Range": "bytes=100-"})
    body = resp.read()
    assert resp.status == 206
    assert body == data[100:]


def test_subtitles(running_server):
    srv, _ = running_server
    resp = _get(srv.subtitles_url)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/vtt")
    assert resp.read().decode().startswith("WEBVTT")


def test_404(running_server):
    srv, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{srv.base_url}/nope")
    assert exc.value.code == 404


def test_wait_for_byte_called(running_server):
    srv, _ = running_server
    seen = []
    srv.wait_for_byte = lambda offset: seen.append(offset)
    _get(srv.media_url("k", "mp4"), headers={"Range": "bytes=42-99"}).read()
    assert seen == [42]
