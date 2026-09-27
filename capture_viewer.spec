# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller --clean capture_viewer.spec

# Qt modules the app never imports. PyInstaller only bundles what is imported,
# but listing these keeps optional hooks from pulling them in.
QT_EXCLUDES = [
    f"PySide6.{m}" for m in (
        "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
        "QtBluetooth", "QtCharts", "QtDataVisualization", "QtDesigner", "QtHelp",
        "QtMultimedia", "QtMultimediaWidgets", "QtNetwork", "QtNfc", "QtPdf", "QtPdfWidgets",
        "QtPositioning", "QtQml", "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickWidgets",
        "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialPort", "QtSql", "QtSvg",
        "QtSvgWidgets", "QtTest", "QtTextToSpeech", "QtUiTools", "QtWebChannel",
        "QtWebEngineCore", "QtWebEngineWidgets", "QtWebSockets", "QtXml",
    )
]

a = Analysis(
    ['capture_viewer.py'],
    pathex=[],
    binaries=[],
    datas=[('image.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=QT_EXCLUDES + ['tkinter', 'matplotlib', 'pandas', 'PIL', 'pyaudio'],
    noarchive=False,
)

# OpenCV's FFmpeg plugin (~28 MB) is only used for video files; capture goes
# through DirectShow, which is built into cv2 itself.
a.binaries = [b for b in a.binaries if 'opencv_videoio_ffmpeg' not in b[0]]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Low Latency Capture Card Viewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-compressed Qt DLLs load slower and sometimes break
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='image.ico',
)
