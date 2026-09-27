"""Persistent user settings, stored as JSON in %APPDATA%."""

import json
import os
from dataclasses import asdict, dataclass, fields

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                            "LowLatencyCaptureViewer")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")


@dataclass
class Settings:
    # Video source. Matched by device path first (stable across USB ports on
    # most cards), then by name + occurrence to tell identical cards apart.
    video_device_name: str = ""
    video_device_path: str = ""
    video_device_occurrence: int = 0
    width: int = 1920            # 0 = device default
    height: int = 1080
    fps: int = 60                # 0 = device default
    pixel_format: str = "auto"   # auto / MJPG / YUY2 / NV12

    # Audio passthrough (WASAPI device names). "" input = off, "" output = default.
    audio_input: str = ""
    audio_output: str = ""
    volume: int = 100
    muted: bool = False

    # Display
    scaling: str = "bilinear"
    renderer: str = "d3d11"      # d3d11 / opengl
    allow_tearing: bool = False  # present immediately instead of at the next refresh
    always_on_top: bool = False
    hide_title_bar: bool = False
    window_geometry: str = ""    # hex-encoded QMainWindow.saveGeometry()

    @classmethod
    def load(cls):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls()
        defaults = cls()
        values = {}
        for field in fields(cls):
            if field.name in data:
                value = data[field.name]
                # Ignore values of the wrong type (hand-edited or older files).
                if isinstance(value, type(getattr(defaults, field.name))):
                    values[field.name] = value
        return cls(**values)

    def save(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            tmp_path = SETTINGS_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp_path, SETTINGS_PATH)
        except OSError as e:
            print(f"Could not save settings: {e}")
