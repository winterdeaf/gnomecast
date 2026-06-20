from gnomecast import devices


def test_capabilities_lookup():
    caps = devices.capabilities_for("Unknown manufacturer", "Chromecast Ultra")
    assert caps.h265 is True and caps.ac3 is True


def test_capabilities_audio_device_default():
    caps = devices.capabilities_for("X", "Y", cast_type="audio")
    assert caps.h265 is False


def test_capabilities_unknown_default():
    caps = devices.capabilities_for("X", "Y")
    assert caps.h265 is None and caps.ac3 is None


def test_can_play_video():
    ultra = devices.capabilities_for("Unknown manufacturer", "Chromecast Ultra")
    base = devices.capabilities_for("Unknown manufacturer", "Chromecast")
    assert devices.can_play_video("hevc", ultra)
    assert not devices.can_play_video("hevc", base)
    assert devices.can_play_video("h264", base)
    # unknown device defaults to h265-capable
    unknown = devices.capabilities_for("X", "Y")
    assert devices.can_play_video("hevc", unknown)
    # audio cast type never plays video codecs beyond... well, h264 only
    assert not devices.can_play_video("hevc", unknown, cast_type="audio")


def test_can_play_audio():
    ultra = devices.capabilities_for("Unknown manufacturer", "Chromecast Ultra")
    base = devices.capabilities_for("Unknown manufacturer", "Chromecast")
    assert devices.can_play_audio("ac3", ultra)
    assert not devices.can_play_audio("ac3", base)
    assert devices.can_play_audio("aac", base)
    assert devices.can_play_audio(None, base) is True
