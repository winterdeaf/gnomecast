"""GTK front-end, built on the tested :mod:`gnomecast` backend modules."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

import pychromecast  # noqa: E402

from . import __version__, devices, ffmpeg, subtitles  # noqa: E402
from .assets import LOGO_SVG  # noqa: E402
from .media import MediaInfo, SubtitleStream  # noqa: E402
from .screensaver import ScreenSaver  # noqa: E402
from .server import MediaServer  # noqa: E402
from .transcoder import Transcoder  # noqa: E402

log = logging.getLogger(__name__)

AUDIO_EXTS = ("aac", "mp3", "wav")

# files_store column indices
C_NAME, C_PATH, C_DURATION, C_DURATION_STR, C_THUMB, C_PROGRESS, C_ICON, C_TRANSCODER, C_MEDIA = range(9)


class MediaFile:
    """GUI-side model wrapping probing + thumbnail generation for one file."""

    def __init__(self, path: str, on_ready=None):
        self.path = path
        self.info: MediaInfo | None = None
        self.thumbnail: str | None = None
        self.probe_data: dict | None = None
        self.error: str | None = None
        self._ready = threading.Event()
        threading.Thread(target=self._load, args=(on_ready,), daemon=True).start()

    def _load(self, on_ready):
        try:
            self.probe_data = ffmpeg.probe(self.path)
            self.info = MediaInfo.from_probe(self.path, self.probe_data)
            subtitles.load_embedded(self.info)
            self._make_thumbnail()
        except Exception as e:  # pragma: no cover - depends on file
            self.error = str(e)
            log.exception("failed to load %s", self.path)
        finally:
            self._ready.set()
            if on_ready:
                on_ready(self)

    def _make_thumbnail(self):
        # Seek ~20% into the file (input seek = fast) to avoid black
        # intro/title frames that produced a black preview.
        duration = self.info.duration if self.info else 0.0
        seek = duration * 0.2 if duration > 5 else 0.0
        fd, thumb = tempfile.mkstemp(
            suffix=".jpg", prefix="gnomecast_pid%i_thumbnail_" % os.getpid()
        )
        os.close(fd)
        os.remove(thumb)
        subprocess.run(
            [ffmpeg.FFMPEG, "-y", "-loglevel", "error",
             "-ss", str(seek), "-i", self.path,
             "-frames:v", "1", "-vf", "scale=600:-1", thumb],
            stderr=subprocess.DEVNULL,
        )
        if os.path.isfile(thumb):
            self.thumbnail = thumb

    def wait(self):
        self._ready.wait()

    @property
    def duration(self) -> float:
        return self.info.duration if self.info else 0.0


class Gnomecast:
    def __init__(self):
        port = os.environ.get("GNOMECAST_HTTP_PORT")
        self.server = MediaServer(port=int(port) if port else None)
        self.cast = None
        self.last_known_player_state = None
        self.last_known_current_time = None
        self.last_time_current_time = None
        self.fn = None
        self.video_stream = None
        self.audio_stream = None
        self.last_fn_played = None
        self.transcoder = None
        self.duration = None
        self.subtitles = None
        self.seeking = False
        self.last_known_volume_level = None
        self.autoplay = False
        self.screensaver = ScreenSaver()

    # -- lifecycle ---------------------------------------------------------

    def run(self, fn=None, device=None, subtitles=None):
        self.build_gui()
        self.init_casts(device=device)
        threading.Thread(target=self.check_ffmpeg, daemon=True).start()
        self.server.media_path_provider = lambda: self.transcoder.fn if self.transcoder else None
        self.server.subtitles_provider = lambda: self.subtitles
        self.server.wait_for_byte = self._wait_for_byte
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self.monitor_cast, daemon=True).start()
        if fn:
            self.queue_files([fn])
        if subtitles:
            self.select_subtitles_file(subtitles)
        if fn and subtitles:
            self.autoplay = True
        Gtk.main()

    def _wait_for_byte(self, offset):
        if self.transcoder:
            self.transcoder.wait_for_byte(offset)

    def check_ffmpeg(self):
        if ffmpeg.have_ffmpeg():
            return

        def f():
            dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR,
                                       Gtk.ButtonsType.CLOSE, "FFMPEG not Found")
            dialog.format_secondary_text(
                "Could not find ffmpeg. Please install the 'ffmpeg' package."
            )
            dialog.run()
            dialog.destroy()
        GLib.idle_add(f)

    # -- casting / status --------------------------------------------------

    def _caps(self):
        if not self.cast:
            return devices.Capabilities(), "cast"
        info = self.cast.cast_info
        return (
            devices.capabilities_for(info.manufacturer, info.model_name, info.cast_type),
            info.cast_type,
        )

    def update_status(self, did_transcode=False):
        if did_transcode:
            self.update_button_visible()
            self.prep_next_transcode()

        def f():
            for row in self.files_store:
                duration = row[C_DURATION]
                transcoder = row[C_TRANSCODER]
                if transcoder and duration:
                    row[C_PROGRESS] = 100 if transcoder.done else int(
                        transcoder.progress_seconds * 100 // duration
                    )
        GLib.idle_add(f)

    def monitor_cast(self):
        while True:
            time.sleep(1)
            if not self.cast:
                continue
            seeking = self.seeking
            mc = self.cast.media_controller
            state = mc.status.player_state
            if state != self.last_known_player_state:
                if state == "PLAYING" and self.last_known_player_state == "BUFFERING" and seeking:
                    self.seeking = False
                if state == "IDLE" and self.last_known_player_state == "PLAYING":
                    self.check_for_next_in_queue()
                if state == "PLAYING":
                    self.screensaver.inhibit()
                else:
                    self.screensaver.restore()
                self.last_known_player_state = state

                def f():
                    self.update_media_button_states()
                    self.update_status()
                GLib.idle_add(f)
            elif self.transcoder and not self.transcoder.done:
                GLib.idle_add(self.update_status)
            if self.last_known_current_time != mc.status.current_time:
                self.last_known_current_time = mc.status.current_time
                self.last_time_current_time = time.time()
            if not seeking and state == "PLAYING":
                GLib.idle_add(
                    lambda: self.scrubber_adj.set_value(
                        mc.status.current_time + time.time() - self.last_time_current_time
                    )
                )

    def init_casts(self, widget=None, device=None):
        self.cast_store.clear()
        self.cast_store.append([None, "Searching local network - please wait..."])
        self.cast_combo.set_active(0)
        threading.Thread(target=self.load_casts, kwargs={"device": device}, daemon=True).start()

    def load_casts(self, device=None):
        chromecasts = pychromecast.get_chromecasts()
        if isinstance(chromecasts, tuple) and len(chromecasts) == 2:
            chromecasts = chromecasts[0]

        def f():
            self.cast_store.clear()
            self.cast_store.append([None, "Select a cast device..."])
            self.cast_store.append([-1, "Add a non-local Chromecast..."])
            for cc in chromecasts:
                name = cc.cast_info.friendly_name
                if cc.cast_type != "cast":
                    name = "%s (%s)" % (name, cc.cast_type)
                self.cast_store.append([cc, name])
            if device:
                found = False
                for i, cc in enumerate(chromecasts):
                    if device == cc.cast_info.friendly_name:
                        self.cast_combo.set_active(i + 2)
                        found = True
                if not found:
                    self.cast_combo.set_active(0)
                    dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR,
                                               Gtk.ButtonsType.CLOSE, "Chromecast Not Found")
                    dialog.format_secondary_text("The device '%s' wasn't found." % device)
                    dialog.run()
                    dialog.destroy()
        GLib.idle_add(f)

    def update_media_button_states(self):
        mc = self.cast.media_controller if self.cast else None
        active = ("BUFFERING", "PLAYING", "PAUSED")
        can_control = bool(self.transcoder and self.cast and mc.status.player_state in active)
        playable_states = ("BUFFERING", "PLAYING", "PAUSED", "IDLE", "UNKNOWN")
        self.play_button.set_sensitive(
            bool(self.transcoder and self.cast and mc.status.player_state in playable_states and self.fn)
        )
        self.volume_button.set_sensitive(bool(self.cast))
        self.stop_button.set_sensitive(can_control)
        self.rewind_button.set_sensitive(can_control)
        self.forward_button.set_sensitive(can_control)
        playing = bool(self.cast and mc.status.player_state == "PLAYING")
        self.play_button.set_image(
            Gtk.Image(stock=Gtk.STOCK_MEDIA_PAUSE if playing else Gtk.STOCK_MEDIA_PLAY)
        )
        if self.transcoder and self.duration:
            self.scrubber_adj.set_upper(self.duration)
            self.scrubber.set_sensitive(True)
        else:
            self.scrubber.set_sensitive(False)
        self.update_button_visible()

    # -- GUI construction --------------------------------------------------

    def build_gui(self):
        self.win = win = Gtk.ApplicationWindow(title="Gnomecast v%s" % __version__)
        win.set_border_width(0)
        win.set_icon(self.get_logo_pixbuf(color="#000000"))
        enforce_target = Gtk.TargetEntry.new("text/plain", Gtk.TargetFlags(4), 129)
        win.drag_dest_set(Gtk.DestDefaults.ALL, [enforce_target], Gdk.DragAction.COPY)
        win.connect("drag-data-received", self.on_drag_data_received)
        self.cast_store = Gtk.ListStore(object, str)

        vbox_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self.thumbnail_image = Gtk.Image()
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        vbox_outer.pack_start(self.thumbnail_image, True, False, 0)
        alignment = Gtk.Alignment(xscale=1, yscale=1)
        alignment.add(vbox)
        alignment.set_padding(16, 20, 16, 16)
        vbox_outer.pack_start(alignment, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, False, False, 0)
        self.cast_combo = Gtk.ComboBox.new_with_model(self.cast_store)
        self.cast_combo.set_entry_text_column(1)
        renderer_text = Gtk.CellRendererText()
        self.cast_combo.pack_start(renderer_text, True)
        self.cast_combo.add_attribute(renderer_text, "text", 1)
        hbox.pack_start(self.cast_combo, True, True, 0)
        refresh_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_REFRESH))
        refresh_button.connect("clicked", self.init_casts)
        hbox.pack_start(refresh_button, False, False, 0)

        win.add(vbox_outer)

        # name, path, duration, duration_str, thumbnail, progress, icon, transcoder, MediaFile
        self.files_store = Gtk.ListStore(str, str, int, str, str, int, str, object, object)
        self.files_store.connect("row-inserted", self.update_button_visible)
        self.files_store.connect("row-deleted", self.update_button_visible)
        self.files_view = Gtk.TreeView(self.files_store)
        self.files_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.files_view.set_headers_visible(False)
        column = Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=C_NAME)
        column.set_expand(True)
        self.files_view.append_column(column)
        self.file_view_column_renderer = r = Gtk.CellRendererText()
        r.props.xalign = 1.0
        self.files_view.append_column(Gtk.TreeViewColumn("Duration", r, text=C_DURATION_STR))
        self.files_view_progress_column = Gtk.TreeViewColumn(
            "Progress", Gtk.CellRendererProgress(), value=C_PROGRESS
        )
        self.files_view.append_column(self.files_view_progress_column)
        self.files_view.append_column(
            Gtk.TreeViewColumn("Playing", Gtk.CellRendererPixbuf(), icon_name=C_ICON)
        )
        self.files_view.get_selection().connect("changed", self.on_files_view_selection_changed)
        self.files_view.connect("row-activated", self.on_files_view_row_activated)

        self.hbox = hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, False, False, 0)
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.add(self.files_view)
        hbox.pack_start(self.scrolled_window, True, True, 0)

        self.btn_vbox = btn_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hbox.pack_start(btn_vbox, True, True, 0)
        self.file_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_ADD))
        self.file_button.set_tooltip_text("Add one or more audio or video files...")
        self.file_button.set_always_show_image(True)
        self.file_button.connect("clicked", self.on_file_clicked)
        btn_vbox.pack_start(self.file_button, True, True, 0)
        self.remove_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_REMOVE))
        self.remove_button.set_tooltip_text("Remove selected file(s).")
        self.remove_button.connect("clicked", self.remove_files)
        self.remove_button.set_sensitive(False)
        btn_vbox.pack_start(self.remove_button, False, False, 0)

        self.file_detail_row = hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(self.file_detail_row, False, False, 0)

        self.stream_store = Gtk.ListStore(str, object, object)
        self.audio_combo = Gtk.ComboBox.new_with_model(self.stream_store)
        self.audio_combo.connect("changed", self.on_audio_combo_changed)
        self.audio_combo.set_entry_text_column(0)
        renderer_text = Gtk.CellRendererText()
        self.audio_combo.pack_start(renderer_text, True)
        self.audio_combo.add_attribute(renderer_text, "text", 0)
        self.file_detail_row.pack_start(self.audio_combo, True, True, 0)

        self.subtitle_store = Gtk.ListStore(str, object, object)
        self.subtitle_combo = Gtk.ComboBox.new_with_model(self.subtitle_store)
        self.subtitle_combo.connect("changed", self.on_subtitle_combo_changed)
        self.subtitle_combo.set_entry_text_column(0)
        renderer_text = Gtk.CellRendererText()
        self.subtitle_combo.pack_start(renderer_text, True)
        self.subtitle_combo.add_attribute(renderer_text, "text", 0)
        self.subtitle_combo.set_active(0)
        self.file_detail_row.pack_start(self.subtitle_combo, True, True, 0)

        file_info_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_DIALOG_INFO))
        file_info_button.connect("clicked", self.show_file_info)
        self.file_detail_row.pack_start(file_info_button, False, False, 0)

        self.scrubber_adj = Gtk.Adjustment(0, 0, 100, 15, 60, 0)
        self.scrubber = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.scrubber_adj)
        self.scrubber.set_digits(0)
        self.scrubber.connect("format-value", lambda scale, s: self.format_timestamp(s))
        self.scrubber.connect("change-value", self.scrubber_move_started)
        self.scrubber.connect("change-value", self.scrubber_moved)
        self.scrubber.set_sensitive(False)
        vbox.pack_start(self.scrubber, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.rewind_button = self._media_button(Gtk.STOCK_MEDIA_REWIND, self.rewind_clicked, hbox)
        self.play_button = self._media_button(Gtk.STOCK_MEDIA_PLAY, self.play_clicked, hbox)
        self.forward_button = self._media_button(Gtk.STOCK_MEDIA_FORWARD, self.forward_clicked, hbox)
        self.stop_button = self._media_button(Gtk.STOCK_MEDIA_STOP, self.stop_clicked, hbox)
        self.volume_button = Gtk.VolumeButton()
        self.volume_button.set_value(1)
        self.volume_button.connect("value-changed", self.volume_moved)
        self.volume_button.set_sensitive(False)
        hbox.pack_start(self.volume_button, True, False, 0)
        vbox.pack_start(hbox, False, False, 0)

        self.cast_combo.connect("changed", self.on_cast_combo_changed)
        win.connect("delete-event", self.quit)
        win.connect("key_press_event", self.on_key_press)
        win.show_all()
        self.update_button_visible()
        win.resize(1, 1)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)

    def _media_button(self, stock, handler, box):
        b = Gtk.Button(None, image=Gtk.Image(stock=stock))
        b.connect("clicked", handler)
        b.set_sensitive(False)
        b.set_relief(Gtk.ReliefStyle.NONE)
        box.pack_start(b, True, False, 0)
        return b

    # -- subtitle/audio combos --------------------------------------------

    def add_extra_subtitle_options(self):
        self.subtitle_store.prepend(["No subtitles.", None, None])
        self.subtitle_store.append(["Add subtitle file...", None, self.on_new_subtitle_clicked])
        self.subtitle_combo.set_active(0)

    def on_drag_data_received(self, widget, drag_context, x, y, data, info, time):
        fn = data.get_text()
        if fn.startswith("file://"):
            fn = urllib.parse.unquote(fn[len("file://"):]).strip()
            self.queue_files([fn])

    def update_button_visible(self, x=None, y=None, z=None):
        count = len(self.files_store)
        self.scrolled_window.set_visible(bool(count))
        self.remove_button.set_visible(bool(count))
        self.file_button.set_label("" if count else "  Add one or more audio or video files...")
        self.file_button.get_child().set_padding(1, 0, 2, 0)
        self.hbox.set_child_packing(self.btn_vbox, not count, not count, 0, Gtk.PackType.START)
        self.file_detail_row.set_visible(bool(self.fn))

    def scrubber_move_started(self, scale, scroll_type, seconds):
        self.seeking = True

    def on_files_view_selection_changed(self, selection):
        model, treeiter = selection.get_selected_rows()
        self.remove_button.set_sensitive(bool(treeiter))

    def remove_files(self, w):
        store, paths = self.files_view.get_selection().get_selected_rows()
        for path in reversed(paths):
            it = store.get_iter(path)
            transcoder = store.get_value(it, C_TRANSCODER)
            if transcoder:
                transcoder.destroy()
            fn = store.get_value(it, C_PATH)
            store.remove(it)
            if self.fn == fn:
                self.unselect_file()

    def on_files_view_row_activated(self, widget, row, col):
        model = widget.get_model()
        fn = model[row][C_PATH]
        self.unselect_file()
        self.fn = fn
        self.transcoder = model[row][C_TRANSCODER]
        self.duration = model[row][C_DURATION]
        thumbnail_fn = model[row][C_THUMB]
        if thumbnail_fn and os.path.isfile(thumbnail_fn):
            self.thumbnail_image.set_from_file(thumbnail_fn)
        self.stop_cast()

        def f():
            self.win.resize(1, 1)
            self.scrubber_adj.set_value(0)
            for r in self.files_store:
                r[C_ICON] = "video-x-generic" if self.fn == r[C_PATH] else None
            # Repopulate the audio/subtitle selectors for the activated file.
            threading.Thread(target=self.update_audio_tracks, daemon=True).start()
            threading.Thread(target=self.update_subtitles, daemon=True).start()
            self.update_button_visible()
            self.update_media_button_states()
        GLib.idle_add(f)
        return True

    def queue_files(self, files):
        # Absolute paths so rows reliably match self.fn.
        files = [os.path.abspath(os.path.expanduser(f)) for f in files]
        existing = {row[C_PATH] for row in self.files_store}
        files = [f for f in files if f not in existing]
        for fn in files:
            if not os.path.isfile(fn):
                self._error_dialog("File Not Found", "Could not find media file: %s" % fn)
                continue
            display = os.path.basename(fn)
            if len(display) > 40:
                display = display[:30] + "..." + display[-10:]

            def on_ready(mf):
                def f():
                    for row in self.files_store:
                        if row[C_PATH] == mf.path:
                            row[C_DURATION] = int(mf.duration)
                            row[C_DURATION_STR] = self.humanize_seconds(mf.duration)
                            if mf.thumbnail:
                                row[C_THUMB] = mf.thumbnail
                    if self.fn == mf.path:
                        self.duration = mf.duration
                        if mf.thumbnail:
                            self.thumbnail_image.set_from_file(mf.thumbnail)
                            self.win.resize(1, 1)
                    self.update_media_button_states()
                    self.update_status()
                GLib.idle_add(f)

            mf = MediaFile(fn, on_ready=on_ready)
            self.files_store.append([display, fn, 0, "...", None, 0, None, None, mf])
        self.scrolled_window.set_visible(True)
        if files and self.fn is None:
            self.select_file(files[0])
        _1, _2, width, height = self.files_view_progress_column.cell_get_size()
        height += self.file_view_column_renderer.get_padding().ypad * 2 + 2
        self.scrolled_window.set_min_content_height(height * min(len(self.files_store), 6))

    # -- transport controls ------------------------------------------------

    def volume_moved(self, button, volume):
        if self.last_known_volume_level != volume and self.cast:
            self.last_known_volume_level = volume
            self.cast.set_volume(volume)

    def scrubber_moved(self, scale, scroll_type, seconds):
        self.seeking = True
        if self.cast:
            self.cast.media_controller.seek(seconds)

    def humanize_seconds(self, s):
        s = int(s)
        hours, minutes, seconds = s // 3600, (s // 60) % 60, s % 60
        if hours:
            return "%ih %im %is" % (hours, minutes, seconds)
        if minutes:
            return "%im %is" % (minutes, seconds)
        return "%is" % seconds

    def format_timestamp(self, s):
        """Compact H:MM:SS / M:SS for the scrubber label (avoids UI overflow)."""
        s = int(s)
        hours, minutes, seconds = s // 3600, (s // 60) % 60, s % 60
        if hours:
            return "%d:%02d:%02d" % (hours, minutes, seconds)
        return "%d:%02d" % (minutes, seconds)

    def stop_clicked(self, widget):
        self.stop_cast()

    def stop_cast(self):
        # Newer pychromecast raises RequestFailed when STOP is sent with no
        # active media session; guard against it.
        if not self.cast:
            return
        mc = self.cast.media_controller
        if not mc.status or mc.status.media_session_id is None:
            return
        try:
            mc.stop()
        except pychromecast.error.RequestFailed:
            pass

    def get_logo_pixbuf(self, width=200, color=None):
        svg = LOGO_SVG.replace("#aaaaaa", color) if color else LOGO_SVG
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg.encode()))
        return GdkPixbuf.Pixbuf.new_from_stream(stream, None)

    def quit(self, a=0, b=0):
        for row in self.files_store:
            transcoder = row[C_TRANSCODER]
            if transcoder:
                transcoder.destroy()
            thumb = row[C_THUMB]
            if thumb and os.path.isfile(thumb):
                os.remove(thumb)
        self.screensaver.restore()
        Gtk.main_quit()

    def forward_clicked(self, widget):
        self.seek_delta(30)

    def rewind_clicked(self, widget):
        self.seek_delta(-10)

    def seek_delta(self, delta):
        mc = self.cast.media_controller
        seconds = mc.status.current_time + time.time() - self.last_time_current_time + delta
        self.last_time_current_time = time.time()
        mc.status.current_time = seconds
        self.scrubber_adj.set_value(seconds)
        self.seeking = True
        mc.seek(seconds)

    def play_clicked(self, widget):
        if not self.cast:
            log.info("no cast selected")
            return
        cast = self.cast
        mc = cast.media_controller
        if mc.status.player_state in ("IDLE", "UNKNOWN") or self.last_fn_played != self.fn:
            self.last_fn_played = self.fn
            cast.wait()
            mc = cast.media_controller
            kwargs = {}
            if self.subtitles:
                kwargs["subtitles"] = self.server.subtitles_url
            current_time = self.scrubber_adj.get_value()
            if current_time:
                kwargs["current_time"] = current_time
            ext = "".join(ch for ch in self.fn.rsplit(".", 1)[-1] if ch.isalnum()).lower()
            content_type = "audio/%s" % ext if ext in AUDIO_EXTS else "video/mp4"
            mc.play_media(self.server.media_url(hash(self.fn), ext), content_type, **kwargs)
            self.prep_next_transcode()
        elif mc.status.player_state == "PLAYING":
            mc.pause()
        elif mc.status.player_state == "PAUSED":
            mc.play()

    # -- file/subtitle dialogs --------------------------------------------

    def on_file_clicked(self, widget):
        dialog = Gtk.FileChooserDialog(
            "Please choose an audio or video file...", self.win, Gtk.FileChooserAction.OPEN,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK),
        )
        dialog.set_select_multiple(True)
        downloads = os.path.expanduser("~/Downloads")
        if os.path.isdir(downloads):
            dialog.set_current_folder(downloads)
        filt = Gtk.FileFilter()
        filt.set_name("Videos")
        filt.add_mime_type("video/*")
        filt.add_mime_type("audio/*")
        dialog.add_filter(filt)
        if dialog.run() == Gtk.ResponseType.OK:
            self.queue_files(dialog.get_filenames())
        dialog.destroy()

    def on_new_subtitle_clicked(self):
        dialog = Gtk.FileChooserDialog(
            "Please choose a subtitle file...", self.win, Gtk.FileChooserAction.OPEN,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK),
        )
        if self.fn:
            dialog.set_current_folder(os.path.dirname(self.fn))
        filt = Gtk.FileFilter()
        filt.set_name("Subtitles")
        for pat in ("*.srt", "*.vtt", "*.ass", "*.ssa"):
            filt.add_pattern(pat)
        dialog.add_filter(filt)
        if dialog.run() == Gtk.ResponseType.OK:
            self.select_subtitles_file(dialog.get_filename())
        else:
            self.subtitle_combo.set_active(0)
        dialog.destroy()

    def select_subtitles_file(self, fn):
        if not os.path.isfile(fn):
            self._error_dialog("File Not Found", "Could not find subtitles file: %s" % fn)
            return
        fn = os.path.abspath(fn)
        display_name = os.path.basename(fn)
        try:
            self.subtitles = subtitles.convert_file(fn)
        except Exception as e:
            self._error_dialog("Subtitle Error", "Could not read subtitles: %s" % e)
            return
        pos = len(self.subtitle_store)
        stream = SubtitleStream(index=-1, codec=None, title=display_name, webvtt=self.subtitles)
        self.subtitle_store.append([display_name, stream, None])
        self.subtitle_combo.set_active(pos)

    def unselect_file(self):
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        self.fn = None
        self.stream_store.clear()
        self.subtitle_store.clear()
        self.subtitle_combo.set_active(0)
        self.transcoder = None
        self.duration = None
        self.stop_cast()

        def f():
            self.scrubber_adj.set_value(0)
            for row in self.files_store:
                row[C_ICON] = None
            self.win.resize(1, 1)
            self.update_button_visible()
        GLib.idle_add(f)

    def select_file(self, fn):
        self.unselect_file()
        if not os.path.isfile(fn):
            self._error_dialog("File Not Found", "Could not find media file: %s" % fn)
            return
        fn = os.path.abspath(fn)
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        self.fn = fn
        self.stream_store.clear()
        self.subtitle_store.clear()
        self.stop_cast()

        def f():
            self.scrubber_adj.set_value(0)
            for row in self.files_store:
                if self.fn == row[C_PATH]:
                    thumb = row[C_THUMB]
                    if thumb:
                        self.thumbnail_image.set_from_file(thumb)
                        self.win.resize(1, 1)
                    row[C_ICON] = "video-x-generic"
                    self.duration = row[C_DURATION]
                else:
                    row[C_ICON] = None
            threading.Thread(target=self.update_transcoders, daemon=True).start()
            threading.Thread(target=self.update_audio_tracks, daemon=True).start()
            threading.Thread(target=self.update_subtitles, daemon=True).start()
            self.update_button_visible()
            self.update_media_button_states()
        GLib.idle_add(f)

    # -- transcoder orchestration -----------------------------------------

    update_transcoders_lock = threading.Lock()

    def _new_transcoder(self, media, video_stream, audio_stream, prev=None):
        if prev:
            prev.destroy()
        caps, cast_type = self._caps()
        return Transcoder(
            media, video_stream, audio_stream, caps, cast_type,
            on_done=lambda tr: GLib.idle_add(self.update_status, True),
            on_progress=lambda tr: None,
            on_error=self.error_callback,
        )

    def _replay_if_playing(self):
        mc = self.cast.media_controller if self.cast else None
        if mc and mc.status.player_state in ("BUFFERING", "PLAYING", "PAUSED"):
            self.stop_clicked(None)
            self.cast.wait()
            threading.Timer(1, lambda: GLib.idle_add(lambda: self.play_clicked(None))).start()

    def update_transcoders(self):
        with self.update_transcoders_lock:
            if self.cast and self.fn:
                for row in self.files_store:
                    if row[C_PATH] != self.fn:
                        continue
                    transcoder = row[C_TRANSCODER]
                    mf = row[C_MEDIA]
                    mf.wait()
                    if not mf.info:
                        continue
                    if not self.video_stream and mf.info.video_streams:
                        self.video_stream = mf.info.video_streams[0]
                    if not self.audio_stream and mf.info.audio_streams:
                        self.audio_stream = mf.info.audio_streams[0]
                    if (not transcoder or self.fn != transcoder.source_fn
                            or self.audio_stream != transcoder.audio_stream):
                        self.transcoder = self._new_transcoder(
                            mf.info, self.video_stream, self.audio_stream, transcoder
                        )
                        row[C_TRANSCODER] = self.transcoder
                if self.autoplay:
                    self.autoplay = False
                    self.play_clicked(None)
            if not self.cast:
                for row in self.files_store:
                    transcoder = row[C_TRANSCODER]
                    if transcoder:
                        transcoder.destroy()
                        row[C_TRANSCODER] = None
            GLib.idle_add(self.update_media_button_states)

    def check_for_next_in_queue(self):
        nxt = False
        for row in self.files_store:
            fn = row[C_PATH]
            if nxt:
                self.autoplay = True
                self.select_file(fn)
                nxt = False
            if self.cast and self.fn and self.fn == fn:
                nxt = True

    def prep_next_transcode(self):
        transcode_next = False
        for row in self.files_store:
            fn = row[C_PATH]
            transcoder = row[C_TRANSCODER]
            mf = row[C_MEDIA]
            if transcode_next and not transcoder and mf.info:
                video = mf.info.video_streams[0] if mf.info.video_streams else None
                audio = mf.info.audio_streams[0] if mf.info.audio_streams else None
                row[C_TRANSCODER] = self._new_transcoder(mf.info, video, audio, transcoder)
                transcode_next = False
            if self.cast and self.fn == fn and transcoder and transcoder.done:
                transcode_next = True

    def get_media_file(self):
        for row in self.files_store:
            if self.fn == row[C_PATH]:
                return row[C_MEDIA]
        return None

    def update_subtitles(self):
        mf = self.get_media_file()
        if mf is None:
            return
        mf.wait()
        if not mf.info:
            return
        usable = mf.info.text_subtitles
        log.info(
            "listing %d text subtitle track(s) for %s (%d image-based skipped)",
            len(usable), os.path.basename(self.fn),
            len(mf.info.subtitle_streams) - len(usable),
        )

        def f():
            self.subtitle_store.clear()
            for stream in usable:
                self.subtitle_store.append([stream.title, stream, None])
            self.add_extra_subtitle_options()
        GLib.idle_add(f)
        ext = self.fn.rsplit(".", 1)[-1]
        for sext in ("vtt", "srt"):
            candidate = self.fn[: -len(ext)] + sext
            if os.path.isfile(candidate):
                self.select_subtitles_file(candidate)
                break

    def update_audio_tracks(self):
        mf = self.get_media_file()
        if mf is None:
            return
        mf.wait()
        if not mf.info:
            return

        def f():
            self.stream_store.clear()
            for video_stream in mf.info.video_streams:
                for audio_stream in mf.info.audio_streams:
                    self.stream_store.append(
                        ["%s - %s" % (video_stream.title, audio_stream.title),
                         video_stream, audio_stream]
                    )
            self.audio_combo.set_active(0)
        GLib.idle_add(f)

    def on_key_press(self, widget, event, user_data=None):
        key = Gdk.keyval_name(event.keyval)
        if key == "q" and (event.state & Gdk.ModifierType.CONTROL_MASK):
            self.quit()
            return True
        return False

    def select_cast(self, cast):
        self.cast = cast
        if cast:
            self.last_known_volume_level = cast.media_controller.status.volume_level
            self.volume_button.set_value(cast.media_controller.status.volume_level or 1)
        self.last_known_player_state = None
        self.update_media_button_states()
        threading.Thread(target=self.update_transcoders, daemon=True).start()

    # -- dialogs -----------------------------------------------------------

    def _error_dialog(self, title, message):
        def f():
            dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR,
                                       Gtk.ButtonsType.CLOSE, title)
            dialog.format_secondary_text(message)
            dialog.run()
            dialog.destroy()
        GLib.idle_add(f)

    def error_callback(self, msg):
        def f():
            dialog = Gtk.MessageDialog(
                self.win, Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                Gtk.MessageType.INFO, Gtk.ButtonsType.OK,
                "\nGnomecast encountered an error converting your file.",
            )
            dialog.set_title("Transcoding Error")
            dialog.set_default_size(1, 400)
            box = dialog.get_content_area()
            buf = Gtk.TextBuffer()
            buf.set_text(msg)
            tv = Gtk.TextView(buffer=buf)
            tv.set_editable(False)
            sw = Gtk.ScrolledWindow()
            sw.set_border_width(5)
            sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            sw.add(tv)
            box.pack_end(sw, True, True, 0)
            dialog.show_all()
            dialog.run()
            dialog.destroy()
        GLib.idle_add(f)

    def show_file_info(self, b=None):
        mf = self.get_media_file()
        if not mf or not mf.info:
            return
        msg = "\n" + mf.info.details()
        if self.cast:
            msg += "\nDevice: %s (%s)" % (self.cast.cast_info.model_name, self.cast.cast_info.manufacturer)
        msg += "\nGnomecast: v%s" % __version__
        dialog = Gtk.MessageDialog(
            self.win, Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            Gtk.MessageType.INFO, Gtk.ButtonsType.OK, msg,
        )
        dialog.set_title("File Info")
        dialog.set_default_size(1, 400)
        import json
        probe_json = json.dumps(mf.probe_data, indent=2) if mf.probe_data else ""
        if self.cast:
            title = "Error playing %s" % os.path.basename(self.fn)
            body = (
                "[Please describe what happened here...]\n\n"
                "[Please link to the download here...]\n\n"
                "------------------------------------------------------------\n\n"
                "%s\n\n```\n%s\n```" % (mf.info.details(), probe_json)
            )
            url = "https://github.com/keredson/gnomecast/issues/new?title=%s&body=%s" % (
                urllib.parse.quote(title), urllib.parse.quote(body)
            )
            dialog.add_action_widget(
                Gtk.LinkButton(uri=url, label="Report File Doesn't Play"), 10
            )
        box = dialog.get_content_area()
        buf = Gtk.TextBuffer()
        buf.set_text(probe_json)
        tv = Gtk.TextView(buffer=buf)
        tv.set_editable(False)
        sw = Gtk.ScrolledWindow()
        sw.set_border_width(5)
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(tv)
        box.pack_end(sw, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def get_nonlocal_cast(self):
        dialog = Gtk.MessageDialog(
            self.win, Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            Gtk.MessageType.QUESTION, Gtk.ButtonsType.OK_CANCEL,
            "\nPlease specify the IP address or hostname of a Chromecast device:",
        )
        dialog.set_title("Add a non-local Chromecast")
        box = dialog.get_content_area()
        entry = Gtk.Entry()
        box.pack_end(entry, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        text = entry.get_text()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and text:
            try:
                cast = pychromecast.Chromecast(text)
                self.cast_store.append([cast, text])
                self.cast_combo.set_active(len(self.cast_store) - 1)
            except pychromecast.error.ChromecastConnectionError:
                self._error_dialog("Chromecast Not Found", "The Chromecast '%s' wasn't found." % text)

    def on_cast_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is None:
            return
        cast, name = combo.get_model()[tree_iter][:2]
        if cast == -1:
            self.get_nonlocal_cast()
        else:
            self.select_cast(cast)

    def on_subtitle_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is None:
            return
        text, stream, callback = combo.get_model()[tree_iter]
        if callback:
            callback()
            return
        self.subtitles = stream.webvtt if stream else None
        self._replay_if_playing()

    def on_audio_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is None:
            return
        text, video_stream, audio_stream = combo.get_model()[tree_iter]
        self.video_stream = video_stream
        self.audio_stream = audio_stream
        threading.Thread(target=self.update_transcoders, daemon=True).start()
