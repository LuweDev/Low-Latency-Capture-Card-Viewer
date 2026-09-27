"""Main window: wires capture, audio, the renderer and overlays together."""

import ctypes
import gc
import math
import threading
import time
from functools import partial

from PySide6.QtCore import QByteArray, QEvent, QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMenu

from . import APP_NAME, devices
from .audio import AudioPassthrough
from .capture import CaptureThread, FrameSlot
from .d3d_renderer import D3DSurface
from .overlay import Overlay
from .renderer import SCALING_LABELS, SCALING_MODES, GLVideoWidget, SoftwareVideoWidget
from .settings_dialog import SettingsDialog

CURSOR_HIDE_MS = 2500
VOLUME_STEP = 5
AUDIO_RETRY_SECONDS = 5.0      # first retry; doubles up to AUDIO_RETRY_MAX
AUDIO_RETRY_MAX = 30.0
RESIZE_MARGIN = 6              # px from the edge that resize a title-less window
VIDEO_SETTINGS = ("video_device_name", "video_device_path", "video_device_occurrence",
                  "width", "height", "fps", "pixel_format")
WM_DISPLAYCHANGE = 0x007E


def borderless_geometry(screen, overhang):
    """Window rect for borderless fullscreen on `screen`, plus hidden insets.

    Direct3D (overhang=False): the window exactly covers the monitor, so the
    flip-model swap chain can be shown directly by the display hardware
    ("independent flip"), which G-Sync needs. Windows composites it normally
    again whenever another window (menu, settings) is on top.

    OpenGL (overhang=True): an OpenGL window exactly covering the monitor is
    presented by the driver in a way that hides other windows (the settings
    dialog). Making it a few pixels larger than the monitor, on an edge with no
    neighbouring screen, keeps it composited. The overhang is reported as
    device-pixel insets so the video is laid out in the visible area only.
    """
    geometry = screen.geometry()
    if not overhang:
        return geometry, (0, 0, 0, 0)
    dpr = screen.devicePixelRatio()
    # Smallest logical overhang that is a whole number of device pixels, so
    # the window's device size stays exact (no 1 px rescale of the video).
    extra = next((e for e in range(1, 9) if abs(e * dpr - round(e * dpr)) < 1e-6), 1)
    extra_px = round(extra * dpr)
    others = [s.geometry() for s in QGuiApplication.screens() if s is not screen]
    x, y, w, h = geometry.x(), geometry.y(), geometry.width(), geometry.height()
    candidates = [
        (QRect(x, y, w, h + extra), QRect(x, y + h, w, extra), (0, extra_px, 0, 0)),   # bottom
        (QRect(x, y - extra, w, h + extra), QRect(x, y - extra, w, extra), (extra_px, 0, 0, 0)),  # top
        (QRect(x, y, w + extra, h), QRect(x + w, y, extra, h), (0, 0, 0, extra_px)),   # right
        (QRect(x - extra, y, w + extra, h), QRect(x - extra, y, extra, h), (0, 0, extra_px, 0)),  # left
    ]
    for rect, strip, insets in candidates:
        if not any(strip.intersects(other) for other in others):
            return rect, insets
    return candidates[0][0], candidates[0][2]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", ctypes.c_uint), ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long)]


class MainWindow(QMainWindow):
    audio_failed = Signal(str)  # emitted from the audio worker thread

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(160, 90)

        self.slot = FrameSlot()
        self.overlay = Overlay()
        self.capture = None
        self.source = None  # (width, height, fps, pixel format) of the open device
        self.audio = AudioPassthrough()
        self.audio.volume = settings.volume
        self.audio.muted = settings.muted
        self._audio_retry_at = 0.0
        self._audio_retry_delay = AUDIO_RETRY_SECONDS
        self._audio_busy = False
        self._audio_pending = None  # (rescan, report_errors) queued while busy
        self.audio_failed.connect(lambda message: self._toast(message, duration=5))
        self._autosize_pending = not settings.window_geometry
        self._fullscreen = False
        self._restore_state = None  # (geometry, maximized) to return to from fullscreen
        self._drag_origin = None    # where a drag-to-move started (title bar hidden)
        self._info_due = False

        self.video = None
        self._input = None          # the widget that receives mouse input
        self._native_surface = False
        self._screen_hooked = False
        self._setup_renderer(settings.renderer)
        self._build_actions()

        self._cursor_timer = QTimer(self, singleShot=True, interval=CURSOR_HIDE_MS)
        self._cursor_timer.timeout.connect(self._hide_cursor)
        self._tick_timer = QTimer(self, interval=500)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()
        self._fade_timer = QTimer(self, interval=16)
        self._fade_timer.timeout.connect(self._animate_overlay)

        self._apply_window_flags()
        if not (settings.window_geometry
                and self.restoreGeometry(QByteArray.fromHex(settings.window_geometry.encode()))):
            self.resize(1280, 720)

        QTimer.singleShot(0, self._startup)

    # --- renderer -----------------------------------------------------------

    def _setup_renderer(self, kind):
        if kind != "opengl":
            try:
                surface = D3DSurface(self, self.slot, self.overlay)
            except Exception as e:
                print(f"Direct3D 11 unavailable ({e}); using OpenGL")
            else:
                self._use_native_surface(True)
                self._attach_renderer(surface, self)
                surface.init_failed.connect(
                    lambda reason: QTimer.singleShot(0, lambda: self._fall_back("opengl", reason)))
                return
        self._set_widget_renderer(GLVideoWidget(self.slot, self.overlay))

    def _use_native_surface(self, on):
        """Direct3D presents into this window's own client area, like a game.
        Qt must then not paint the window itself."""
        self._native_surface = on
        if on:
            self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_PaintOnScreen, on)
        self.setAttribute(Qt.WA_NoSystemBackground, on)
        self.setAttribute(Qt.WA_OpaquePaintEvent, on)
        self.setMouseTracking(on)
        if on and self.windowHandle() is not None and not self._screen_hooked:
            self.windowHandle().screenChanged.connect(self._screen_changed)
            self._screen_hooked = True

    def _attach_renderer(self, renderer, input_widget):
        renderer.mode = self.settings.scaling
        renderer.allow_tearing = self.settings.allow_tearing
        if self._input is not None and self._input is not input_widget:
            self._input.removeEventFilter(self)
        input_widget.installEventFilter(self)
        input_widget.setMouseTracking(True)
        self.video = renderer
        self._input = input_widget

    def _set_widget_renderer(self, widget):
        if isinstance(widget, GLVideoWidget):
            widget.gl_failed.connect(
                lambda reason: QTimer.singleShot(0, lambda: self._fall_back("software", reason)))
        self.setCentralWidget(widget)  # deletes any previous widget renderer
        self._attach_renderer(widget, widget)

    def _release_renderer(self):
        if isinstance(self.video, D3DSurface):
            self.video.shutdown()        # swap chain first, while the window exists
            self.video.deleteLater()
            self._use_native_surface(False)
        elif self.centralWidget() is not None:
            self.takeCentralWidget().deleteLater()

    def set_renderer(self, kind):
        fullscreen = self._fullscreen
        self.exit_fullscreen()           # the fullscreen geometry depends on the renderer
        self._release_renderer()
        self._setup_renderer(kind)
        self.update()
        if fullscreen:
            QTimer.singleShot(0, self.enter_fullscreen)

    def _fall_back(self, kind, reason):
        self.exit_fullscreen()
        self._release_renderer()
        if kind == "opengl":
            self._set_widget_renderer(GLVideoWidget(self.slot, self.overlay))
            self._toast(f"Switched to OpenGL. {reason}", duration=8)
        else:
            self._set_widget_renderer(SoftwareVideoWidget(self.slot, self.overlay))
            self._toast("OpenGL unavailable, using software rendering", duration=4)

    # Qt paint plumbing for the native (Direct3D) surface.

    def paintEngine(self):
        return None if getattr(self, "_native_surface", False) else super().paintEngine()

    def paintEvent(self, event):
        if self._native_surface:
            self.video.render()
        else:
            super().paintEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._native_surface:
            self.video.render()

    def event(self, event):
        # Qt delivers events during construction, before our attributes exist.
        if getattr(self, "_native_surface", False) and self.video is not None:
            if event.type() == QEvent.Paint:
                # QMainWindow opens a QPainter on paint events (for dock
                # separators); this window is drawn by Direct3D only.
                self.video.render()
                return True
            if event.type() == QEvent.WinIdChange:
                self.video.window_handle_changed()
        return super().event(event)

    def nativeEvent(self, event_type, message):
        if self._native_surface and event_type == b"windows_generic_MSG":
            msg = _MSG.from_address(int(message))
            if msg.message == WM_DISPLAYCHANGE:
                # e.g. HDR switched on/off: the swap chain format may need to change.
                QTimer.singleShot(500, self._screen_changed)
        return super().nativeEvent(event_type, message)

    def _screen_changed(self, *_):
        if self._native_surface:
            self.video.check_output()

    # --- actions ------------------------------------------------------------

    def _action(self, text, slot, shortcuts=(), checkable=False):
        action = QAction(text, self)
        action.setShortcuts([QKeySequence(s) for s in shortcuts])
        action.setCheckable(checkable)
        action.triggered.connect(lambda *_: slot())
        self.addAction(action)  # shortcuts work without the menu open
        return action

    def _build_actions(self):
        self.act_settings = self._action("Settings...", self.open_settings, ["S"])
        self.act_fullscreen = self._action("Borderless", self.toggle_fullscreen,
                                           ["F11", "Alt+Return"], checkable=True)
        self._action("Exit borderless", self.exit_fullscreen, ["Escape"])
        self.act_on_top = self._action("Always on top", self.toggle_always_on_top, ["P"], checkable=True)
        self.act_on_top.setChecked(self.settings.always_on_top)
        self.act_title_bar = self._action("Hide title bar", self.toggle_title_bar, ["H"], checkable=True)
        self.act_title_bar.setChecked(self.settings.hide_title_bar)
        self.act_reset_size = self._action("Reset window size to default", self.resize_to_source, ["Ctrl+1"])
        self.act_info = self._action("Show info", self.toggle_info, ["I"], checkable=True)
        self.act_mute = self._action("Mute", self.toggle_mute, ["M"], checkable=True)
        self.act_mute.setChecked(self.settings.muted)
        self._action("Volume up", partial(self.change_volume, VOLUME_STEP), ["Up"])
        self._action("Volume down", partial(self.change_volume, -VOLUME_STEP), ["Down"])
        self.act_reconnect = self._action("Reconnect device", self.reconnect, ["R"])
        self.act_quit = self._action("Exit", self.close)

        self.scaling_group = QActionGroup(self)
        self.scaling_actions = []
        for key, label in SCALING_MODES:
            action = QAction(label, self, checkable=True)
            action.setData(key)
            action.setChecked(key == self.settings.scaling)
            action.triggered.connect(lambda *_, k=key: self.set_scaling(k))
            self.scaling_group.addAction(action)
            self.scaling_actions.append(action)

    def _startup(self):
        self.start_capture()
        self.start_audio()
        if not self.settings.video_device_name:
            QTimer.singleShot(250, self.open_settings)
        if self._native_surface and self.windowHandle() is not None and not self._screen_hooked:
            self.windowHandle().screenChanged.connect(self._screen_changed)
            self._screen_hooked = True
        # Everything created so far lives for the whole session; moving it out
        # of the garbage collector's view keeps its occasional full passes
        # short, so they can't show up as frame-time spikes.
        QTimer.singleShot(3000, lambda: (gc.collect(), gc.freeze()))

    # --- capture -----------------------------------------------------------

    def start_capture(self):
        self.stop_capture()
        if not self.settings.video_device_name:
            self._set_status("No capture device selected.\nRight-click and choose Settings (or press S).")
            return
        thread = CaptureThread(self.settings, self.slot)
        thread.frame_ready.connect(partial(self._on_frame_ready, thread))
        thread.opened.connect(partial(self._on_opened, thread))
        thread.status.connect(partial(self._on_status, thread))
        thread.note.connect(partial(self._on_note, thread))
        self.capture = thread
        thread.start(QThread.HighPriority)

    def stop_capture(self, timeout_ms=1500):
        thread, self.capture = self.capture, None
        if thread is not None:
            thread.stop(timeout_ms)
        self.source = None
        self.video.reset_source()
        self._update_title()

    def reconnect(self):
        self.start_capture()
        self.start_audio()

    # Signals from a thread that has since been replaced may still be queued,
    # so every handler checks that it came from the current one.

    def _on_frame_ready(self, thread):
        if thread is not self.capture:
            return
        # Paint now rather than update(): update() queues the paint as a
        # low-priority event, which measured up to 8-30 ms late at times.
        self.video.repaint()
        if self._info_due:
            # Rebuild the info panel right after a frame, in the idle time
            # before the next one, so it never delays a frame.
            self._info_due = False
            self._refresh_info()

    def _on_opened(self, thread, width, height, fps, pixel_format):
        if thread is not self.capture:
            return
        self.source = (width, height, fps, pixel_format)
        self._update_title()
        if self._autosize_pending:
            self._autosize_pending = False
            self.resize_to_source(quiet=True)

    def _on_status(self, thread, text):
        if thread is not self.capture:
            return
        self._set_status(text)
        if not text and self.audio.stalled:
            # Video is back after a dropout; the card's audio usually is too,
            # so retry now instead of waiting out the retry backoff.
            self._audio_retry_at = 0.0
            self._audio_retry_delay = AUDIO_RETRY_SECONDS

    def _on_note(self, thread, text):
        if thread is self.capture:
            self._toast(text, duration=10)

    def _set_status(self, text):
        self.overlay.status = text
        self.video.refresh_overlay()

    def _update_title(self):
        parts = [APP_NAME]
        if self.settings.video_device_name:
            parts.append(self.settings.video_device_name)
        if self.source:
            width, height, fps, _ = self.source
            parts.append(f"{width}x{height} @ {format_fps(fps)}" if fps > 0 else f"{width}x{height}")
        self.setWindowTitle(" - ".join(parts))

    # --- audio -------------------------------------------------------------

    def start_audio(self):
        self._audio_retry_delay = AUDIO_RETRY_SECONDS
        self._audio_retry_at = time.monotonic() + AUDIO_RETRY_SECONDS
        self._run_audio_task(rescan=False, report_errors=True)

    def _check_audio(self):
        """Restart audio if the input died or never started (card unplugged)."""
        wanted = bool(self.settings.audio_input)
        if not wanted or self._audio_busy or time.monotonic() < self._audio_retry_at:
            return
        if self.audio.active and not self.audio.stalled:
            self._audio_retry_delay = AUDIO_RETRY_SECONDS
            return
        print("Audio input stalled or missing, restarting")
        self._audio_retry_at = time.monotonic() + self._audio_retry_delay
        self._audio_retry_delay = min(AUDIO_RETRY_MAX, self._audio_retry_delay * 2)
        self._run_audio_task(rescan=True, report_errors=False)

    def _run_audio_task(self, rescan, report_errors):
        """Stop and (re)start audio on a worker thread.

        Opening a device that has stopped responding can block for seconds,
        which must not freeze the window.
        """
        if self._audio_busy:
            self._audio_pending = (rescan, report_errors)  # run when the current one ends
            return
        self._audio_busy = True
        input_name, output_name = self.settings.audio_input, self.settings.audio_output

        def task():
            try:
                self.audio.stop()
                # PortAudio only sees devices that existed when it started, and
                # a replugged card comes back as a new device. Rescanning
                # blocks briefly, so only when the device is actually missing.
                if rescan and input_name not in {d.name for d in devices.list_audio_devices("input")}:
                    devices.rescan_audio_devices()
                if input_name and not self.audio.start(input_name, output_name) and report_errors:
                    self.audio_failed.emit(self.audio.error)
            except Exception as e:
                print(f"Audio task failed: {e}")
            finally:
                self._audio_busy = False

        threading.Thread(target=task, name="audio-start", daemon=True).start()

    def change_volume(self, delta):
        if not self.audio.active:
            self._toast("No audio device selected" if not self.settings.audio_input
                        else self.audio.error or "Audio is not running")
            return
        volume = max(0, min(100, self.audio.volume + delta))
        self.audio.volume = self.settings.volume = volume
        if delta > 0 and self.audio.muted:
            self.toggle_mute(show=False)
        self._toast(f"Volume {volume}%", level=volume / 100)

    def toggle_mute(self, show=True):
        self.audio.muted = self.settings.muted = not self.audio.muted
        self.act_mute.setChecked(self.audio.muted)
        if show:
            self._toast("Muted" if self.audio.muted else f"Volume {self.audio.volume}%",
                        level=None if self.audio.muted else self.audio.volume / 100)

    # --- window modes ----------------------------------------------------------

    def set_scaling(self, key):
        self.settings.scaling = key
        self.video.mode = key
        for action in self.scaling_actions:
            action.setChecked(action.data() == key)
        self._toast(f"Scaling: {SCALING_LABELS[key]}")

    def _apply_window_flags(self):
        """Match the window flags to the current modes. Borderless fullscreen
        and a hidden title bar both need a frameless window; returns True if
        the flags changed (Qt then hides the window until it is shown again)."""
        wanted = {Qt.FramelessWindowHint: self._fullscreen or self.settings.hide_title_bar,
                  Qt.WindowStaysOnTopHint: self.settings.always_on_top}
        flags = self.windowFlags()
        if all(bool(flags & flag) == on for flag, on in wanted.items()):
            return False
        for flag, on in wanted.items():
            flags = flags | flag if on else flags & ~flag
        self.setWindowFlags(flags)
        return True

    def _reapply_flags_keeping_geometry(self):
        """Apply flag changes in windowed mode without moving the video: the
        client area keeps its place on screen whether or not a frame is added."""
        if self._fullscreen:
            self._apply_window_flags()
            self.show()
            return
        if self.isMaximized():
            if self._apply_window_flags():
                self.showMaximized()
            return
        client = self.geometry()
        if self._apply_window_flags():
            self.setGeometry(client)
            self.show()

    def toggle_always_on_top(self):
        self.settings.always_on_top = not self.settings.always_on_top
        self.act_on_top.setChecked(self.settings.always_on_top)
        self._reapply_flags_keeping_geometry()
        self._toast("Window pinned" if self.settings.always_on_top else "Window unpinned")

    def toggle_title_bar(self):
        self.settings.hide_title_bar = not self.settings.hide_title_bar
        self.act_title_bar.setChecked(self.settings.hide_title_bar)
        if not self._fullscreen:   # while borderless it applies on leaving fullscreen
            self._reapply_flags_keeping_geometry()
        self._toast("Title bar hidden" if self.settings.hide_title_bar else "Title bar visible")

    def toggle_fullscreen(self):
        if self._fullscreen:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        """Borderless fullscreen (a frameless window over the whole monitor)."""
        if self._fullscreen:
            return
        self._restore_state = (self.saveGeometry(), self.isMaximized())
        rect, insets = borderless_geometry(self.screen(), overhang=not self._native_surface)
        if self.isMaximized():
            self.showNormal()
        self._fullscreen = True
        self._apply_window_flags()
        self.setGeometry(rect)
        self.video.insets = insets
        self.show()
        self.activateWindow()
        self.act_fullscreen.setChecked(True)

    def exit_fullscreen(self):
        if not self._fullscreen:
            return
        self._fullscreen = False
        geometry, maximized = self._restore_state
        self.video.insets = (0, 0, 0, 0)
        self._apply_window_flags()   # back to framed, unless the title bar is hidden
        self.restoreGeometry(geometry)
        if maximized:
            self.showMaximized()
        else:
            self.show()
        self.act_fullscreen.setChecked(False)

    def resize_to_source(self, quiet=False):
        """Default window size: one video pixel per screen pixel, centred
        (shrunk to fit if the video is larger than the screen)."""
        if not self.source:
            if not quiet:
                self._toast("No video yet")
            return
        self.exit_fullscreen()
        if self.isMaximized():
            self.showNormal()
        screen = self.screen()
        dpr = screen.devicePixelRatio()
        available = screen.availableGeometry()
        if self.settings.hide_title_bar:
            extra_w = extra_h = 0
        else:
            frame = self.frameGeometry()
            extra_w, extra_h = frame.width() - self.width(), frame.height() - self.height()
            if extra_h <= 0:  # not shown yet: typical Windows 11 title bar + borders
                extra_w, extra_h = 16, 39
        width, height = self.source[0] / dpr, self.source[1] / dpr
        scale = min(1.0, (available.width() - extra_w) / width, (available.height() - extra_h) / height)
        self.resize(round(width * scale), round(height * scale))
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())
        if scale < 1.0 and not quiet:
            self._toast("Video is larger than the screen; sized to fit instead")

    # --- overlays -------------------------------------------------------------

    def toggle_info(self):
        self.overlay.info_visible = not self.overlay.info_visible
        self.act_info.setChecked(self.overlay.info_visible)
        self._refresh_info()

    def _tick(self):
        self.video.stats.reset_if_idle()
        if self._audio_pending and not self._audio_busy:
            pending, self._audio_pending = self._audio_pending, None
            self._run_audio_task(*pending)
        self._check_audio()
        if self.overlay.info_visible:
            if self.video.stats.display_fps > 5:
                self._info_due = True   # done right after the next frame
            else:
                self._refresh_info()

    def _refresh_info(self):
        if not self.overlay.info_visible:
            self.video.refresh_overlay()
            return
        s = self.settings
        video = self.video
        stats = video.stats
        lines = [f"Device    {s.video_device_name or '-'}"]
        if self.source:
            width, height, fps, fmt = self.source
            rate = f" @ {format_fps(fps)}" if fps > 0 else ""
            lines.append(f"Source    {width}x{height}{rate}  {fmt}".rstrip())
        capture_fps = self.capture.capture_fps if self.capture else 0.0
        intervals = self.capture.frame_interval_stats() if self.capture else None
        if intervals:
            mean, jitter = intervals
            lines.append(f"Capture   {capture_fps:.1f} fps, frames every {mean:.1f} ms "
                         f"+-{jitter:.1f} ms")
        else:
            lines.append(f"Capture   {capture_fps:.1f} fps")
        age = f"{stats.frame_age_ms:.1f} ms" if stats.frame_age_ms is not None else "-"
        lines.append(f"Display   {stats.display_fps:.1f} fps, frame age {age}, skipped {stats.skipped}")
        src_w, src_h = video.source_size
        _, _, out_w, out_h = video.output_rect
        if src_w and out_w:
            lines.append(f"Scaling   {SCALING_LABELS[video.mode]} -> {out_w}x{out_h} "
                         f"({out_w / src_w:.2f}x, {video.active_filter})")
        else:
            lines.append(f"Scaling   {SCALING_LABELS[video.mode]}")
        refresh = self.screen().refreshRate()
        if isinstance(video, D3DSurface):
            present = "tearing allowed" if video.allow_tearing and video.tearing_supported else "v-synced"
            output = f"HDR10 output, SDR white {video.white_nits:.0f} nits" if video.hdr else "SDR output"
            lines.append(f"Renderer  {video.renderer_name}, {output}, {present}, display {refresh:.0f} Hz")
        else:
            lines.append(f"Renderer  {video.renderer_name}, display {refresh:.0f} Hz")
        if self.audio.active:
            lines.append(f"Audio     {self.audio.input_name}  ({self.audio.in_rate / 1000:g} kHz)")
            lines.append(f"          -> {self.audio.output_name}  ({self.audio.out_rate / 1000:g} kHz)")
        else:
            lines.append(f"Audio     {self.audio.error or 'off'}")
        self.overlay.info_lines = lines
        self._prepare_overlay()
        video.refresh_overlay()

    def _prepare_overlay(self):
        """Render the overlay images now (between frames) so drawing the next
        frame only has to pick them up."""
        dpr = self.devicePixelRatioF()
        top, bottom, left, right = self.video.insets
        visible = self._input.rect().adjusted(math.ceil(left / dpr), math.ceil(top / dpr),
                                              -math.ceil(right / dpr), -math.ceil(bottom / dpr))
        self.overlay.layers(visible, dpr)

    def _toast(self, text, level=None, duration=1.2):
        self.overlay.show_toast(text, level, duration)
        self.video.refresh_overlay()
        self._fade_timer.start()

    def _animate_overlay(self):
        # While video plays, the fade rides along with the video frames; this
        # only paints on its own when nothing else is drawing.
        self.video.refresh_overlay()
        if not self.overlay.active() or not self.overlay._toast_visible():
            self._fade_timer.stop()
            self.video.refresh_overlay()

    # --- mouse and cursor -----------------------------------------------------

    def _can_drag(self):
        """Dragging the video moves the window (any normal-sized window)."""
        return not self._fullscreen and not self.isMaximized()

    def _edges_at(self, pos):
        # Resize edges only without a title bar; a framed window has its own.
        if not (self.settings.hide_title_bar and self._can_drag()):
            return Qt.Edges()
        rect = self._input.rect()
        edges = Qt.Edges()
        if pos.x() < RESIZE_MARGIN:
            edges |= Qt.LeftEdge
        elif pos.x() >= rect.width() - RESIZE_MARGIN:
            edges |= Qt.RightEdge
        if pos.y() < RESIZE_MARGIN:
            edges |= Qt.TopEdge
        elif pos.y() >= rect.height() - RESIZE_MARGIN:
            edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges):
        if not edges:
            return None
        horizontal = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        vertical = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
        if horizontal and vertical:
            top_left_or_bottom_right = (bool(edges & Qt.LeftEdge) == bool(edges & Qt.TopEdge))
            return Qt.SizeFDiagCursor if top_left_or_bottom_right else Qt.SizeBDiagCursor
        return Qt.SizeHorCursor if horizontal else Qt.SizeVerCursor

    def _hide_cursor(self):
        if self.isActiveWindow() and self._drag_origin is None:
            self._input.setCursor(Qt.BlankCursor)

    def _show_cursor(self, pos=None):
        cursor = self._cursor_for(self._edges_at(pos)) if pos is not None else None
        if cursor is not None:
            self._input.setCursor(cursor)
        else:
            self._input.unsetCursor()
        self._cursor_timer.start()

    def eventFilter(self, obj, event):
        if obj is self._input:
            kind = event.type()
            if kind == QEvent.MouseMove:
                pos = event.position().toPoint()
                if (self._drag_origin is not None and event.buttons() & Qt.LeftButton
                        and (event.globalPosition().toPoint() - self._drag_origin).manhattanLength()
                        >= QApplication.startDragDistance()):
                    # Dragging the video moves the window. Started only once
                    # the mouse moves, so a double-click still toggles
                    # borderless (a caption double-click would maximise).
                    self._drag_origin = None
                    self.windowHandle().startSystemMove()
                    return True
                self._show_cursor(pos)
            elif kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                if self._can_drag():
                    edges = self._edges_at(event.position().toPoint())
                    if edges:
                        self.windowHandle().startSystemResize(edges)
                        return True
                    self._drag_origin = event.globalPosition().toPoint()
            elif kind == QEvent.MouseButtonRelease:
                self._drag_origin = None
            elif kind == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                self._drag_origin = None
                self.toggle_fullscreen()
                return True
            elif kind == QEvent.Wheel:
                steps = event.angleDelta().y() / 120
                if steps:
                    self.change_volume(round(steps * VOLUME_STEP) or (VOLUME_STEP if steps > 0 else -VOLUME_STEP))
                return True
        return super().eventFilter(obj, event)

    def contextMenuEvent(self, event):
        self._drag_origin = None
        self._show_cursor()
        menu = QMenu(self)
        menu.addAction(self.act_settings)
        scaling_menu = menu.addMenu("Scaling")
        for action in self.scaling_actions:
            scaling_menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.act_fullscreen)
        menu.addAction(self.act_on_top)
        menu.addAction(self.act_title_bar)
        menu.addAction(self.act_reset_size)
        menu.addSeparator()
        menu.addAction(self.act_mute)
        menu.addAction(self.act_reconnect)
        menu.addSeparator()
        menu.addAction(self.act_info)
        menu.addAction(self.act_quit)
        menu.exec(event.globalPos())

    # --- settings ----------------------------------------------------------

    def open_settings(self):
        self._show_cursor()
        dialog = SettingsDialog(self.settings, self.audio.active, self)
        if dialog.exec() != QDialog.Accepted:
            return
        old, new = self.settings, dialog.result_settings()
        self.settings = new
        if new.scaling != old.scaling:
            self.set_scaling(new.scaling)
        if new.renderer != old.renderer:
            self.set_renderer(new.renderer)
        self.video.allow_tearing = new.allow_tearing
        if self.capture is None or any(getattr(old, k) != getattr(new, k) for k in VIDEO_SETTINGS):
            self.start_capture()
        if (old.audio_input, old.audio_output) != (new.audio_input, new.audio_output) \
                or (new.audio_input and not self.audio.active):
            self.start_audio()
        new.save()

    def closeEvent(self, event):
        # Stop the watchdogs first, or they "recover" the audio being shut down.
        self._tick_timer.stop()
        self._fade_timer.stop()
        self._audio_pending = None
        s = self.settings
        geometry = self._restore_state[0] if self._fullscreen else self.saveGeometry()
        s.window_geometry = bytes(geometry.toHex()).decode("ascii")
        s.volume = self.audio.volume
        s.muted = self.audio.muted
        s.save()
        # Disappear immediately; a hung capture device can take a while to let go.
        self.hide()
        QGuiApplication.processEvents()
        # Closing a stream on a dead device can block for seconds; the OS
        # releases it at exit anyway.
        if not self._audio_busy and not self.audio.stalled:
            self.audio.stop()
        self.stop_capture(timeout_ms=1500)
        if isinstance(self.video, D3DSurface):
            self.video.shutdown()  # release the swap chain while its window still exists
        super().closeEvent(event)


def format_fps(fps):
    return f"{fps:.2f}".rstrip("0").rstrip(".") + " fps"
