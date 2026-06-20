![alt text](https://raw.githubusercontent.com/keredson/gnomecast/master/screenshot.png)

Gnomecast ![logo](https://github.com/keredson/gnomecast/raw/master/icons/gnomecast_16.png)
=========

This is a native Linux GUI for casting local files to Chromecast devices.  It supports:

- Both audio and video files (anything `ffmpeg` can read)
- Realtime transcoding (only when needed)
- Subtitles (embedded and external SRT files)
- Fast scrubbing (waiting 20s for buffering to skip 30s ahead is wrong!)
- 4K videos on the Chromecast Ultra!

What's New
----------

* `2.0`: Modernized, dependency-light rewrite. Metadata now comes from
  `ffprobe` JSON (reliably detects subtitles in MKV and other containers),
  the HTTP server uses only the Python standard library, and subtitle
  conversion is built in. Removes the `pycaption`/`cssutils`, `bottle`,
  `paste` and `html5lib` dependencies. Split into a tested backend package
  plus a thin GTK front-end.
* `1.9`: Multi video/audio stream support.
* `1.8`: 5.1/7.1 surround sound E/AC3 support.
* `1.7`: Drag and drop files into the main UI.
* `1.6`: Mutiple file / queuing support.

Install
-------
Gnomecast needs `ffmpeg`, GTK3 (PyGObject) and `pychromecast`. On Debian/Ubuntu:

```
$ sudo apt install ffmpeg python3-pip python3-gi gir1.2-gtk-3.0
$ pip3 install gnomecast
```

On Arch Linux, use the `gnomecast-git` AUR package (see `packaging/PKGBUILD`),
which pulls in `ffmpeg`, `gtk3`, `python-gobject` and `python-pychromecast`
(`python-dbus` optionally enables screensaver inhibition).

If installing in a `mkvirtualenv` built virtual environment, make sure you include the `--system-site-packages` parameter to get the GTK bindings.

### Dependencies

* **Runtime:** `ffmpeg`/`ffprobe` (system binaries), GTK3 via PyGObject,
  `pychromecast`. `dbus-python` is optional (screensaver inhibition).
* No more `pycaption`, `cssutils`/`css-parser`, `bottle`, `paste` or
  `html5lib`.

Run
---

After installing, log out and log back in.  It will be in your launcher:

![alt text](https://raw.githubusercontent.com/keredson/gnomecast/master/launcher.png)

You can also run it from the command line:

```
$ gnomecast
```

Or:

```
$ python3 -m gnomecast
```

You can also configure the port used for the HTTP server via the environment variable `GNOMECAST_HTTP_PORT`:

```
$ GNOMECAST_HTTP_PORT=8010 python3 -m gnomecast
```

*Please report bugs, including video files that don't work for you!*

Tests
-----

The backend (everything except the GTK UI) is covered by a pytest suite,
including real ffmpeg integration tests that build an MKV with embedded
subtitles and verify probing, subtitle extraction and transcoding:

```
$ pip install pytest
$ pytest
```

Architecture
------------

* `gnomecast/ffmpeg.py` - ffmpeg/ffprobe wrappers and progress parsing
* `gnomecast/media.py` - media model built from ffprobe JSON
* `gnomecast/subtitles.py` - SRT/ASS/VTT -> WebVTT conversion
* `gnomecast/devices.py` - Chromecast capability table
* `gnomecast/transcoder.py` - on-the-fly transcoding
* `gnomecast/server.py` - stdlib HTTP media server with Range support
* `gnomecast/app.py` - the GTK front-end
* `gnomecast/cli.py` - command-line entry point

My File Won't Play!
-------------------

Chromecasts are picky, and the built in media receiver doesn't give any feedback regarding why it won't play something.  (It just flashes and quits on the main TV.)  If your file won't play, please click the info button:

![image](https://user-images.githubusercontent.com/2049665/66446007-978b5780-e9fd-11e9-87cc-c01f07c67271.png)

And then the "Report File Doesn't Play" button:

![image](https://user-images.githubusercontent.com/2049665/66446040-b12c9f00-e9fd-11e9-8acf-b3bc0d28c971.png)

So I can fix it!

Thanks To...
------------

- https://github.com/balloob/pychromecast
- https://www.ffmpeg.org/

And everyone who made this project hit [HN's front page](https://news.ycombinator.com/item?id=16386173) and #2 on GitHub's trending list!  That's so awesome!!!

![alt text](https://raw.githubusercontent.com/keredson/gnomecast/master/trending.png)


Transcoding
-----------
Chromecasts only support a handful of media formats.  See: https://developers.google.com/cast/docs/media

So some amount of transcoding is necessary if your video files don't conform.  But we're smart about it.  If you have an `.mkv` file with `h264` video and `AAC` audio, we use `ffmpeg` to simply rewrite the container (to `.mp4`) without touching the underlying streams, which my XPS 13 can at around 100x realtime (it's fully IO bound).

Now if you have that same `.mkv` file with and `A3C` audio stream (which Chromecast doesn't support) we'll rewrite the container, copy the `h264` stream as is and only transcode the audio (at about 20x).

If neither your file's audio or video streams are supported, then it'll do a full transcode, using `libx264 -preset veryfast` (much faster than libx264's default `medium` preset).

We write the transcoded file to `/var/tmp` (or `/tmp`) and, once it's complete, serve it as a `faststart` MP4 - the Chromecast sees a stable file size, so seeking and buffering are glitch-free.

Subtitles
---------
Chromecast only supports a handful of subtitle formats, `.srt` not included.  But it does support [WebVTT](https://w3c.github.io/webvtt/).  So for *text* subtitles (embedded SubRip/ASS or external `.srt`/`.vtt`/`.ass` files) we convert them to WebVTT and attach them through Chromecast's API.  Image-based subtitles (PGS/VOBSUB/DVB) can't be converted to WebVTT and aren't supported.

*Image-based* subtitles (PGS/`hdmv_pgs_subtitle`, VOBSUB, DVB) can't be sent as WebVTT - there's no text to convert.  These are listed in the subtitle dropdown marked `(burn-in)`; selecting one re-encodes the video with the subtitle bitmaps overlaid ("burned in"), which is the only way to show them on a Chromecast.
