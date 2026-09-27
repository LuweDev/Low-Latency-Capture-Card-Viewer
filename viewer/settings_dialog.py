"""Settings dialog. Works on a copy of Settings; the caller applies the result."""

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget)

from . import APP_NAME, APP_VERSION, devices
from .renderer import SCALING_MODES

# Offered when the card can't be asked for its modes (e.g. not connected).
PRESET_MODES = [(1920, 1080, 60), (1280, 720, 60), (2560, 1440, 60), (2560, 1440, 30),
                (3840, 2160, 30), (3840, 2160, 60)]
FORMAT_NOTES = {"MJPG": "compressed, adds a little latency", "YUY2": "uncompressed",
                "NV12": "uncompressed"}


def mode_key(width, height, fps, pixel_format):
    return f"{width}x{height}@{fps}:{pixel_format}"


def parse_mode_key(key):
    size, rest = key.split("@")
    fps, pixel_format = rest.split(":")
    width, height = size.split("x")
    return int(width), int(height), int(fps), pixel_format


class SettingsDialog(QDialog):
    def __init__(self, settings, audio_running, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Settings - {APP_NAME} {APP_VERSION}")
        self.setMinimumWidth(480)
        self._settings = replace(settings)
        self._audio_running = audio_running

        # --- the everyday settings ---
        form = QFormLayout()
        form.setSpacing(8)
        self.video_combo = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_devices)
        row = QHBoxLayout()
        row.addWidget(self.video_combo, 1)
        row.addWidget(refresh)
        form.addRow("Capture device:", _wrap(row))

        self.mode_combo = QComboBox()
        self.mode_combo.setToolTip(
            "The modes this capture card offers, as reported by the card.\n"
            "Some cards only list modes that suit the HDMI signal they are receiving.")
        form.addRow("Capture mode:", self.mode_combo)

        self.scaling_combo = QComboBox()
        for key, label in SCALING_MODES:
            self.scaling_combo.addItem(label, key)
        _select_data(self.scaling_combo, settings.scaling)
        self.scaling_combo.setToolTip(
            "Scaling runs on the GPU and costs the same for every mode.\n"
            "Sharp Bilinear keeps pixels crisp at 2x and above (e.g. 1080p on a 4K screen).\n"
            "Integer Scaling uses whole-number multiples only (black borders).\n"
            "When shrinking below half size, smooth modes use trilinear filtering.")
        form.addRow("Scaling:", self.scaling_combo)


        # --- advanced (collapsed) ---
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("Advanced")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.RightArrow)
        self.advanced_toggle.setAutoRaise(True)
        self.advanced_toggle.toggled.connect(self._show_advanced)

        self.advanced = QWidget()
        advanced_form = QFormLayout(self.advanced)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.setSpacing(8)
        self.audio_in_combo = QComboBox()
        self.audio_in_combo.setToolTip("Picked automatically when you choose a capture device,\n"
                                       "if it has an audio input with a matching name.")
        advanced_form.addRow("Audio input:", self.audio_in_combo)
        self.audio_out_combo = QComboBox()
        advanced_form.addRow("Audio output:", self.audio_out_combo)

        self.renderer_combo = QComboBox()
        self.renderer_combo.addItem("Direct3D 11 (recommended)", "d3d11")
        self.renderer_combo.addItem("OpenGL", "opengl")
        _select_data(self.renderer_combo, settings.renderer)
        advanced_form.addRow("Renderer:", self.renderer_combo)

        self.tearing_check = QCheckBox("Allow tearing (Direct3D only)")
        self.tearing_check.setChecked(settings.allow_tearing)
        self.tearing_check.setToolTip(
            "Off: frames are shown at the next screen refresh. With G-Sync / FreeSync active\n"
            "that costs nothing, since the screen refreshes when a frame arrives.\n"
            "On: in borderless fullscreen without G-Sync, frames are shown immediately,\n"
            "even mid-refresh: up to one refresh less latency, but with tearing.\n"
            "Windowed mode is always composited by Windows and never tears.")
        advanced_form.addRow("", self.tearing_check)

        note = QLabel("Close OBS (or anything else using the capture card) first. "
                      "Most capture cards can only be opened by one program at a time.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #9a9a9a;")
        advanced_form.addRow(note)
        self.advanced.hide()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.advanced_toggle)
        layout.addWidget(self.advanced)
        layout.addStretch(1)
        layout.addWidget(buttons)
        layout.setSizeConstraint(QVBoxLayout.SetFixedSize)   # shrink back when collapsed

        self._populate_video()
        self._populate_audio()
        self.video_combo.currentIndexChanged.connect(lambda *_: self._populate_modes())
        self.video_combo.currentIndexChanged.connect(lambda *_: self._match_audio_input())

    def _show_advanced(self, shown):
        self.advanced.setVisible(shown)
        self.advanced_toggle.setArrowType(Qt.DownArrow if shown else Qt.RightArrow)

    def _refresh_devices(self):
        self._settings = self.result_settings()  # keep current selections
        if not self._audio_running:
            devices.rescan_audio_devices()
        self._populate_video()
        self._populate_audio()

    def _populate_modes(self):
        s = self._settings
        wanted = mode_key(s.width, s.height, s.fps, s.pixel_format)
        if self.mode_combo.count():  # keep the user's current choice when switching device
            wanted = self.mode_combo.currentData() or wanted
        self.mode_combo.clear()
        self.mode_combo.addItem("Device default", mode_key(0, 0, 0, "auto"))
        device = self.video_combo.currentData()
        modes = devices.list_video_modes(device) if device is not None and device.index >= 0 else []
        if modes:
            for m in modes:
                note = FORMAT_NOTES.get(m.pixel_format, "")
                detail = m.pixel_format + (", " + note if note else "")
                label = f"{m.width} x {m.height} @ {m.fps:g} fps  ({detail})"
                self.mode_combo.addItem(label, mode_key(m.width, m.height, round(m.fps), m.pixel_format))
        else:
            for w, h, fps in PRESET_MODES:
                self.mode_combo.addItem(f"{w} x {h} @ {fps} fps", mode_key(w, h, fps, "auto"))

        width, height, fps, fmt = parse_mode_key(wanted)
        if not _select_data(self.mode_combo, wanted):
            match = None
            if fmt == "auto":
                # A saved "any format" choice: the same size and rate in any format.
                for i in range(self.mode_combo.count()):
                    if parse_mode_key(self.mode_combo.itemData(i))[:3] == (width, height, fps):
                        match = i
                        break
            if match is not None:
                self.mode_combo.setCurrentIndex(match)
            else:
                self._add_unoffered(width, height, fps, fmt, bool(modes))


    def _add_unoffered(self, width, height, fps, fmt, device_listed):
        if width == 0 and fps == 0:
            return
        label = f"{width} x {height} @ {fps} fps" + (f" ({fmt})" if fmt != "auto" else "")
        if device_listed:
            label += "  - not offered by this card"
        self.mode_combo.addItem(label, mode_key(width, height, fps, fmt))
        self.mode_combo.setCurrentIndex(self.mode_combo.count() - 1)

    def _populate_video(self):
        self.video_combo.clear()
        found = devices.list_video_devices()
        self.video_combo.addItem("None", None)
        for device in found:
            self.video_combo.addItem(device.label, device)
        s = self._settings
        current = devices.find_video_device(found, s.video_device_name, s.video_device_path,
                                            s.video_device_occurrence)
        if current is not None:
            _select_data(self.video_combo, current)
        elif s.video_device_name:
            missing = devices.VideoDevice(-1, s.video_device_name, s.video_device_path,
                                          s.video_device_occurrence)
            self.video_combo.addItem(f"{missing.label} (not connected)", missing)
            self.video_combo.setCurrentIndex(self.video_combo.count() - 1)
        self._populate_modes()

    def _match_audio_input(self):
        """Choosing a capture device selects its audio input, e.g. "Cam Link 4K"
        -> "Digital Audio Interface (Cam Link 4K)". No match: no audio."""
        device = self.video_combo.currentData()
        name = device.name.lower() if device is not None else ""
        for i in range(self.audio_in_combo.count()):
            if name and name in self.audio_in_combo.itemText(i).lower():
                self.audio_in_combo.setCurrentIndex(i)
                return
        self.audio_in_combo.setCurrentIndex(0)   # "None (no audio)"

    def _populate_audio(self):
        s = self._settings
        self.audio_in_combo.clear()
        self.audio_in_combo.addItem("None (no audio)", "")
        for device in devices.list_audio_devices("input"):
            self.audio_in_combo.addItem(device.name, device.name)
        _select_data(self.audio_in_combo, s.audio_input, add_missing=bool(s.audio_input),
                     label=f"{s.audio_input} (not connected)")

        self.audio_out_combo.clear()
        self.audio_out_combo.addItem("System default", "")
        for device in devices.list_audio_devices("output"):
            self.audio_out_combo.addItem(device.name, device.name)
        _select_data(self.audio_out_combo, s.audio_output, add_missing=bool(s.audio_output),
                     label=f"{s.audio_output} (not connected)")

    def result_settings(self):
        s = replace(self._settings)
        device = self.video_combo.currentData()
        if device is None:
            s.video_device_name, s.video_device_path, s.video_device_occurrence = "", "", 0
        else:
            s.video_device_name = device.name
            s.video_device_path = device.path
            s.video_device_occurrence = device.occurrence
        s.width, s.height, s.fps, s.pixel_format = parse_mode_key(self.mode_combo.currentData())
        s.audio_input = self.audio_in_combo.currentData() or ""
        s.audio_output = self.audio_out_combo.currentData() or ""
        s.scaling = self.scaling_combo.currentData()
        s.renderer = self.renderer_combo.currentData()
        s.allow_tearing = self.tearing_check.isChecked()
        return s


def _wrap(layout):
    layout.setContentsMargins(0, 0, 0, 0)
    widget = QWidget()
    widget.setLayout(layout)
    return widget


def _select_data(combo, value, add_missing=False, label=""):
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            combo.setCurrentIndex(i)
            return True
    if add_missing:
        combo.addItem(label, value)
        combo.setCurrentIndex(combo.count() - 1)
    return False
