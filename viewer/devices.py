"""Video and audio device discovery.

Video devices come from the DirectShow system device enumerator. That lists
friendly names without opening any device (tens of milliseconds, versus
hundreds per device when probing with cv2.VideoCapture), and it returns them in
the same order OpenCV's CAP_DSHOW backend uses for its indices.
"""

import ctypes
from ctypes import POINTER, byref, c_int, c_long, c_ulong, c_void_p
from dataclasses import dataclass

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from comtypes.automation import VARIANT
from comtypes.persist import IPropertyBag

COINIT_MULTITHREADED = 0x0

CLSID_SystemDeviceEnum = GUID("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
CLSID_VideoInputDeviceCategory = GUID("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")


class IMoniker(IUnknown):
    _iid_ = GUID("{0000000F-0000-0000-C000-000000000046}")
    # Only the vtable slots up to BindToStorage are declared; the rest are unused.
    _methods_ = [
        # IPersist
        COMMETHOD([], HRESULT, "GetClassID", (["out"], POINTER(GUID), "pClassID")),
        # IPersistStream
        COMMETHOD([], HRESULT, "IsDirty"),
        COMMETHOD([], HRESULT, "Load", (["in"], c_void_p, "pStm")),
        COMMETHOD([], HRESULT, "Save", (["in"], c_void_p, "pStm"), (["in"], c_ulong, "fClearDirty")),
        COMMETHOD([], HRESULT, "GetSizeMax", (["in"], c_void_p, "pcbSize")),
        # IMoniker
        COMMETHOD([], HRESULT, "BindToObject",
                  (["in"], c_void_p, "pbc"), (["in"], c_void_p, "pmkToLeft"),
                  (["in"], POINTER(GUID), "riidResult"), (["out"], POINTER(c_void_p), "ppvResult")),
        COMMETHOD([], HRESULT, "BindToStorage",
                  (["in"], c_void_p, "pbc"), (["in"], c_void_p, "pmkToLeft"),
                  (["in"], POINTER(GUID), "riid"), (["out"], POINTER(POINTER(IUnknown)), "ppvObj")),
    ]


class IEnumMoniker(IUnknown):
    _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Next",
                  (["in"], c_ulong, "celt"),
                  (["out"], POINTER(POINTER(IMoniker)), "rgelt"),
                  (["out"], POINTER(c_ulong), "pceltFetched")),
    ]


class ICreateDevEnum(IUnknown):
    _iid_ = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
    _methods_ = [
        COMMETHOD([], HRESULT, "CreateClassEnumerator",
                  (["in"], POINTER(GUID), "clsidDeviceClass"),
                  (["out"], POINTER(POINTER(IEnumMoniker)), "ppEnumMoniker"),
                  (["in"], c_ulong, "dwFlags")),
    ]


class AM_MEDIA_TYPE(ctypes.Structure):
    _fields_ = [("majortype", GUID), ("subtype", GUID), ("bFixedSizeSamples", c_int),
                ("bTemporalCompression", c_int), ("lSampleSize", c_ulong), ("formattype", GUID),
                ("pUnk", c_void_p), ("cbFormat", c_ulong), ("pbFormat", c_void_p)]


class IEnumPins(IUnknown):
    _iid_ = GUID("{56A86892-0AD4-11CE-B03A-0020AF0BA770}")


class IPin(IUnknown):
    _iid_ = GUID("{56A86891-0AD4-11CE-B03A-0020AF0BA770}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Connect", (["in"], c_void_p, "pReceivePin"), (["in"], c_void_p, "pmt")),
        COMMETHOD([], HRESULT, "ReceiveConnection", (["in"], c_void_p, "pConnector"), (["in"], c_void_p, "pmt")),
        COMMETHOD([], HRESULT, "Disconnect"),
        COMMETHOD([], HRESULT, "ConnectedTo", (["in"], c_void_p, "pPin")),
        COMMETHOD([], HRESULT, "ConnectionMediaType", (["in"], c_void_p, "pmt")),
        COMMETHOD([], HRESULT, "QueryPinInfo", (["in"], c_void_p, "pInfo")),
        COMMETHOD([], HRESULT, "QueryDirection", (["out"], POINTER(c_int), "pPinDir")),
    ]


IEnumPins._methods_ = [
    COMMETHOD([], HRESULT, "Next", (["in"], c_ulong, "cPins"),
              (["out"], POINTER(POINTER(IPin)), "ppPins"), (["out"], POINTER(c_ulong), "pcFetched")),
]


class IBaseFilter(IUnknown):
    _iid_ = GUID("{56A86895-0AD4-11CE-B03A-0020AF0BA770}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetClassID", (["in"], c_void_p, "pClassID")),   # IPersist
        COMMETHOD([], HRESULT, "Stop"),                                           # IMediaFilter
        COMMETHOD([], HRESULT, "Pause"),
        COMMETHOD([], HRESULT, "Run", (["in"], ctypes.c_longlong, "tStart")),
        COMMETHOD([], HRESULT, "GetState", (["in"], c_ulong, "ms"), (["in"], c_void_p, "State")),
        COMMETHOD([], HRESULT, "SetSyncSource", (["in"], c_void_p, "pClock")),
        COMMETHOD([], HRESULT, "GetSyncSource", (["in"], c_void_p, "pClock")),
        COMMETHOD([], HRESULT, "EnumPins", (["out"], POINTER(POINTER(IEnumPins)), "ppEnum")),
    ]


class IAMStreamConfig(IUnknown):
    _iid_ = GUID("{C6E13340-30AC-11D0-A18C-00A0C9118956}")
    _methods_ = [
        COMMETHOD([], HRESULT, "SetFormat", (["in"], c_void_p, "pmt")),
        COMMETHOD([], HRESULT, "GetFormat", (["in"], c_void_p, "ppmt")),
        COMMETHOD([], HRESULT, "GetNumberOfCapabilities",
                  (["out"], POINTER(c_int), "piCount"), (["out"], POINTER(c_int), "piSize")),
        COMMETHOD([], HRESULT, "GetStreamCaps", (["in"], c_int, "iIndex"),
                  (["out"], POINTER(POINTER(AM_MEDIA_TYPE)), "ppmt"), (["in"], c_void_p, "pSCC")),
    ]


FORMAT_VideoInfo = GUID("{05589F80-C356-11CE-BF01-00AA0055595A}")
FORMAT_VideoInfo2 = GUID("{F72A76A0-EB0A-11D0-ACE4-0000C0CC16BA}")
PINDIR_OUTPUT = 1
_KNOWN_SUBTYPES = {"{E436EB7D-524F-11CE-9F53-0020AF0BA770}": "RGB24",
                   "{E436EB7E-524F-11CE-9F53-0020AF0BA770}": "RGB32"}


@dataclass(frozen=True)
class VideoMode:
    width: int
    height: int
    fps: float        # highest rate the device offers for this size and format
    pixel_format: str

    @property
    def label(self):
        return f"{self.width} x {self.height} @ {self.fps:g} fps ({self.pixel_format})"


def list_video_modes(device):
    """Modes the device's capture pin offers, from DirectShow's stream caps.

    Returns [] if the device can't be queried (e.g. busy). Binding the filter
    does not start streaming.
    """
    ensure_com_initialized()
    try:
        moniker = _moniker_at(device.index)
        if moniker is None:
            return []
        address = moniker.BindToObject(None, None, byref(IBaseFilter._iid_))
        # The cast pointer takes over the reference BindToObject returned.
        return _modes_of_filter(ctypes.cast(address, ctypes.POINTER(IBaseFilter)))
    except (OSError, comtypes.COMError, ValueError) as e:
        print(f"Could not query modes of {device.label}: {e}")
        return []


def _moniker_at(index):
    dev_enum = comtypes.CoCreateInstance(CLSID_SystemDeviceEnum, interface=ICreateDevEnum)
    enum = dev_enum.CreateClassEnumerator(byref(CLSID_VideoInputDeviceCategory), 0)
    i = 0
    while enum:
        moniker, fetched = enum.Next(1)
        if not fetched:
            return None
        if i == index:
            return moniker
        i += 1
    return None


def _modes_of_filter(filt):
    best = {}
    pins = filt.EnumPins()
    while True:
        pin, fetched = pins.Next(1)
        if not fetched:
            break
        if pin.QueryDirection() != PINDIR_OUTPUT:
            continue
        try:
            config = pin.QueryInterface(IAMStreamConfig)
        except comtypes.COMError:
            continue
        count, size = config.GetNumberOfCapabilities()
        caps = (ctypes.c_byte * max(size, 128))()
        for i in range(count):
            mt = config.GetStreamCaps(i, ctypes.addressof(caps))
            try:
                mode = _mode_from(mt.contents, caps)
            finally:
                _free_media_type(mt)
            if mode is not None:
                key = (mode.width, mode.height, mode.pixel_format)
                if key not in best or mode.fps > best[key].fps:
                    best[key] = mode
        break  # the first output pin with stream caps is the capture pin
    return sorted(best.values(), key=lambda m: (-m.width * m.height, -m.fps, m.pixel_format))


def _mode_from(mt, caps):
    if mt.formattype == FORMAT_VideoInfo:
        header_offset = 48
    elif mt.formattype == FORMAT_VideoInfo2:
        header_offset = 72
    else:
        return None
    if not mt.pbFormat or mt.cbFormat < header_offset + 12:
        return None
    raw = ctypes.string_at(mt.pbFormat, mt.cbFormat)
    avg_time = int.from_bytes(raw[40:48], "little", signed=True)   # 100 ns units
    width = int.from_bytes(raw[header_offset + 4:header_offset + 8], "little", signed=True)
    height = abs(int.from_bytes(raw[header_offset + 8:header_offset + 12], "little", signed=True))
    # VIDEO_STREAM_CONFIG_CAPS.MinFrameInterval (offset 104) = fastest rate offered.
    min_interval = int.from_bytes(bytes(caps)[104:112], "little", signed=True)
    interval = min(i for i in (min_interval, avg_time) if i > 0) if max(min_interval, avg_time) > 0 else 0
    fps = round(1e7 / interval, 2) if interval else 0.0
    subtype = str(mt.subtype).upper()
    fmt = _KNOWN_SUBTYPES.get(subtype)
    if fmt is None:
        code = mt.subtype.Data1 if hasattr(mt.subtype, "Data1") else int(subtype[1:9], 16)
        fmt = "".join(chr((code >> (8 * k)) & 0xFF) for k in range(4))
        fmt = fmt if fmt.isprintable() else subtype
    return VideoMode(width, height, fps, fmt.strip())


def _free_media_type(mt):
    free = ctypes.windll.ole32.CoTaskMemFree
    free.argtypes = [c_void_p]
    if mt.contents.pbFormat:
        free(mt.contents.pbFormat)
    free(ctypes.cast(mt, c_void_p))


@dataclass(frozen=True)
class VideoDevice:
    index: int        # OpenCV CAP_DSHOW index
    name: str
    path: str         # DirectShow DevicePath; empty for most virtual cameras
    occurrence: int   # 0 for the first device with this name, 1 for the second...

    @property
    def label(self):
        return self.name if self.occurrence == 0 else f"{self.name} #{self.occurrence + 1}"


def boost_thread(task="Capture"):
    """Register the calling thread with Windows' multimedia scheduler (MMCSS),
    which keeps it promptly scheduled when the PC is busy. Best effort."""
    try:
        avrt = ctypes.WinDLL("avrt")
        avrt.AvSetMmThreadCharacteristicsW.restype = c_void_p
        return avrt.AvSetMmThreadCharacteristicsW(task, byref(c_ulong()))
    except OSError:
        return None


def ensure_com_initialized():
    """Initialise COM on the calling thread if nothing else has."""
    try:
        comtypes.CoInitializeEx(COINIT_MULTITHREADED)
    except OSError:
        pass  # Already initialised in another mode (e.g. Qt's STA on the GUI thread)


def list_video_devices():
    ensure_com_initialized()
    try:
        dev_enum = comtypes.CoCreateInstance(CLSID_SystemDeviceEnum, interface=ICreateDevEnum)
        enum = dev_enum.CreateClassEnumerator(byref(CLSID_VideoInputDeviceCategory), 0)
    except (OSError, comtypes.COMError) as e:
        print(f"Video device enumeration failed: {e}")
        return []

    result = []
    seen_names = {}
    while enum:  # NULL when the category is empty
        moniker, fetched = enum.Next(1)
        if not fetched:
            break
        name, path = "", ""
        try:
            bag = moniker.BindToStorage(None, None, byref(IPropertyBag._iid_)).QueryInterface(IPropertyBag)
            name = _read_property(bag, "FriendlyName") or ""
            path = _read_property(bag, "DevicePath") or ""
        except comtypes.COMError:
            pass
        name = name or f"Video device {len(result)}"
        occurrence = seen_names.get(name, 0)
        seen_names[name] = occurrence + 1
        result.append(VideoDevice(len(result), name, path, occurrence))
    return result


def _read_property(bag, name):
    try:
        return bag.Read(name, VARIANT(), None)
    except comtypes.COMError:
        return None


def find_video_device(devices, name, path="", occurrence=0):
    if path:
        for device in devices:
            if device.path == path:
                return device
    for device in devices:
        if device.name == name and device.occurrence == occurrence:
            return device
    for device in devices:
        if device.name == name:
            return device
    return None


# --- Audio -------------------------------------------------------------------
# Only WASAPI devices are offered. MME and DirectSound (what PyAudio picks by
# default) have a minimum latency of 90-120 ms per direction; WASAPI is ~3-10 ms.

@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    channels: int
    samplerate: float


def _wasapi_hostapi():
    import sounddevice as sd
    for i, api in enumerate(sd.query_hostapis()):
        if "WASAPI" in api["name"]:
            return i, api
    return None, None


def list_audio_devices(kind):
    """kind is 'input' or 'output'."""
    import sounddevice as sd
    api_index, _ = _wasapi_hostapi()
    if api_index is None:
        return []
    key = f"max_{kind}_channels"
    return [AudioDevice(i, d["name"], d[key], d["default_samplerate"])
            for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == api_index and d[key] > 0]


def default_audio_output():
    api_index, api = _wasapi_hostapi()
    if api is None or api["default_output_device"] < 0:
        return None
    index = api["default_output_device"]
    for device in list_audio_devices("output"):
        if device.index == index:
            return device
    return None


def rescan_audio_devices():
    """Make PortAudio pick up devices plugged in since startup.

    Must not be called while an audio stream is open.
    """
    import sounddevice as sd
    sd._terminate()
    sd._initialize()
