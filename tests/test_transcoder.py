import os
import time

from gnomecast import devices
from gnomecast.media import MediaInfo
from gnomecast.transcoder import Transcoder

from .conftest import requires_ffmpeg


def _info(container, vcodec="h264", acodec="aac", channels=2):
    data = {
        "format": {"duration": "10"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": vcodec},
            {"index": 1, "codec_type": "audio", "codec_name": acodec, "channels": channels},
        ],
    }
    return MediaInfo.from_probe(f"/x/f.{container}", data)


def _build(container, **kw):
    m = _info(container, **{k: kw.pop(k) for k in list(kw) if k in ("vcodec", "acodec", "channels")})
    caps = kw.pop("caps", devices.Capabilities(h265=True, ac3=True))
    return Transcoder(
        m, m.video_streams[0], m.audio_streams[0], caps, autostart=False, **kw
    )


def test_mp4_h264_aac_no_transcode():
    t = _build("mp4")
    assert t.transcode is False
    assert t.done is True
    assert t.fn == "/x/f.mp4"


def test_mkv_remux_only():
    # h264/aac in mkv: container must change, but codecs are copied.
    t = _build("mkv")
    assert t.transcode is True
    assert t.transcode_container is True
    assert t.transcode_video is False
    assert t.transcode_audio is False
    cmd = t.build_command()
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert "copy" in cmd[cmd.index("-c:a") + 1]


def test_hevc_on_base_device_transcodes_video():
    base = devices.Capabilities(h265=False, ac3=False)
    t = _build("mkv", vcodec="hevc", caps=base)
    assert t.transcode_video is True
    cmd = t.build_command()
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "veryfast" in cmd  # fast preset, not libx264's slow 'medium' default


def test_multichannel_ac3_target():
    t = _build("mkv", acodec="dts", channels=6, caps=devices.Capabilities(ac3=True, h265=True))
    assert t.transcode_audio is True
    assert t._audio_target_codec() == "ac3"


@requires_ffmpeg
def test_real_transcode_to_mp4(mkv_file):
    m = MediaInfo.probe(mkv_file)
    done = []
    t = Transcoder(
        m, m.video_streams[0], m.audio_streams[0],
        devices.Capabilities(h265=True, ac3=True),
        on_done=lambda tr: done.append(tr),
    )
    assert t.transcode is True
    for _ in range(200):
        if t.done:
            break
        time.sleep(0.1)
    assert t.done is True
    assert done and os.path.getsize(t.fn) > 0
    assert t.progress_seconds > 0
    out = MediaInfo.probe(t.fn)
    assert out.container == "mp4"
    assert out.video_streams[0].codec == "h264"
    t.destroy()
