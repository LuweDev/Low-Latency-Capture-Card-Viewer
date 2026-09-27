"""Low Latency Capture Card Viewer: entry point.

Right-click the video for settings. Shortcuts: F11 / Alt+Enter / double-click
fullscreen, Esc leave fullscreen, S settings, I info overlay, M mute,
mouse wheel / Up / Down volume, R reconnect, Ctrl+1 window to 1:1 size.
"""

import ctypes
import faulthandler
import os
import sys
import time

# Qt waits 5 ms after each repaint request before painting; paint immediately.
os.environ.setdefault("QT_QPA_UPDATE_IDLE_TIME", "0")

ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
LOG_MAX_BYTES = 2 * 1024 * 1024


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class _TimestampedLog:
    """Writes print() output to the log file (and the console, if any)."""

    def __init__(self, file, console):
        self._file = file
        self._console = console
        self._line_start = True

    def write(self, text):
        if self._console is not None:
            try:
                self._console.write(text)
            except Exception:
                pass
        for part in text.splitlines(keepends=True):
            if self._line_start:
                self._file.write(time.strftime("%Y-%m-%d %H:%M:%S  "))
            self._file.write(part)
            self._line_start = part.endswith("\n")
        self._file.flush()

    def flush(self):
        self._file.flush()


def setup_logging():
    from viewer.settings import SETTINGS_DIR

    os.makedirs(SETTINGS_DIR, exist_ok=True)
    path = os.path.join(SETTINGS_DIR, "viewer.log")
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    log = open(path, "a", encoding="utf-8", buffering=1)
    faulthandler.enable(log)  # native crashes (driver faults) get a traceback too
    sys.stdout = _TimestampedLog(log, sys.stdout)
    sys.stderr = _TimestampedLog(log, sys.stderr)
    from viewer import APP_VERSION
    print(f"--- started v{APP_VERSION}, log: {path}")


def dark_palette():
    from PySide6.QtGui import QColor, QPalette

    palette = QPalette()
    for role, color in [
        (QPalette.Window, (43, 43, 43)), (QPalette.WindowText, (255, 255, 255)),
        (QPalette.Base, (35, 35, 35)), (QPalette.AlternateBase, (53, 53, 53)),
        (QPalette.ToolTipBase, (53, 53, 53)), (QPalette.ToolTipText, (255, 255, 255)),
        (QPalette.Text, (255, 255, 255)), (QPalette.Button, (53, 53, 53)),
        (QPalette.ButtonText, (255, 255, 255)), (QPalette.BrightText, (255, 0, 0)),
        (QPalette.Link, (42, 130, 218)), (QPalette.Highlight, (42, 130, 218)),
        (QPalette.HighlightedText, (255, 255, 255)),
    ]:
        palette.setColor(role, QColor(*color))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        palette.setColor(QPalette.Disabled, role, QColor(120, 120, 120))
    return palette


def main():
    setup_logging()

    from PySide6.QtCore import Qt, qInstallMessageHandler
    from PySide6.QtGui import QIcon, QSurfaceFormat
    from PySide6.QtWidgets import QApplication

    from viewer import APP_NAME, capture
    from viewer.settings import Settings

    def qt_message(mode, context, message):
        # Qt tries to give every native window a dark title bar, including the
        # video's borderless child window, and warns when that doesn't apply.
        if "setDarkBorderToWindow" not in message:
            print(f"Qt: {message}")

    qInstallMessageHandler(qt_message)

    # The audio callbacks are Python code on PortAudio threads; by default a
    # thread waiting for the GIL can be kept waiting up to 5 ms.
    sys.setswitchinterval(0.001)

    # No V-Sync: the window is always composited by Windows (which never
    # tears), so waiting for vblank here would only add latency.
    fmt = QSurfaceFormat.defaultFormat()
    fmt.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

    # Keep capture and presentation responsive when the PC is busy.
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),
                                            ABOVE_NORMAL_PRIORITY_CLASS)

    app = QApplication(sys.argv)
    # The GUI thread draws and presents every frame: same scheduling boost.
    from viewer.devices import boost_thread
    boost_thread("Games")
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    icon_path = resource_path("image.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    from viewer.main_window import MainWindow

    window = MainWindow(Settings.load())
    window.show()
    code = app.exec()
    print("--- exiting")

    if window._audio_busy or any(thread.isRunning() for thread in capture._orphaned_threads):
        # A capture or audio driver is stuck inside a call and won't return.
        # Exiting normally would wait on (or crash destroying) that thread;
        # the OS releases the device when the process ends.
        sys.stdout.flush()
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
