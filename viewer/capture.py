"""Capture thread and the latest-frame handoff to the renderer."""

import collections
import os
import threading
import time

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import cv2  # noqa: E402  (must come after the log level is set)
import numpy as np  # noqa: E402
from PySide6.QtCore import QThread, Signal  # noqa: E402

from . import devices  # noqa: E402

PIXEL_FORMATS = ["auto", "MJPG", "YUY2", "NV12"]

# OpenCV's DirectShow read() waits up to 1 s for a frame, so two failed reads
# in a row means the device has stopped delivering.
MAX_FAILED_READS = 2
RETRY_INTERVAL = 2.0

# Frame rate measurement (the driver reports 0 when the rate is left at the
# device default, and a nominal 60 for 59.94 Hz sources).
MEASURE_SECONDS = 5.0
STANDARD_RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0,
                  100.0, 119.88, 120.0, 144.0, 165.0, 240.0)

# Stall watchdog: when the HDMI signal drops or the card misbehaves, cards
# keep the stream open but fall to ~1 fps of black frames, and some stay that
# way until reopened. Reopen once the rate has been below STALL_FRACTION of
# normal for STALL_WINDOW seconds.
STALL_FRACTION = 0.25
STALL_WINDOW = 1.5

# OpenCV's DirectShow backend keeps global state that is not safe to use from
# two threads at once (it crashed with an access violation when a replacement
# capture thread opened the device while the old one was still stuck in a
# driver call). Only one thread may touch a device at a time.
_device_lock = threading.Lock()

# Threads that did not stop in time are parked here so Python does not destroy
# a running QThread (which aborts the process).
_orphaned_threads = []


class FrameSlot:
    """Single-slot mailbox holding only the newest frame.

    A Qt signal per frame would queue frames whenever the GUI thread falls
    behind, and every queued frame is added latency. Instead the capture thread
    overwrites this slot and notifies only when the renderer has consumed the
    previous notification, so the renderer always draws the newest frame and
    nothing can pile up.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._timestamp = 0.0
        self._seq = 0
        self._notify_pending = False

    def put(self, frame, timestamp):
        """Store a frame. Returns True if the consumer needs to be notified."""
        with self._lock:
            self._frame = frame
            self._timestamp = timestamp
            self._seq += 1
            if self._notify_pending:
                return False
            self._notify_pending = True
            return True

    def take(self, last_seq):
        """Return (frame, seq, timestamp) if newer than last_seq, else None."""
        with self._lock:
            self._notify_pending = False
            if self._frame is None or self._seq == last_seq:
                return None
            return self._frame, self._seq, self._timestamp

    def clear(self):
        with self._lock:
            self._frame = None
            self._notify_pending = False


class CaptureThread(QThread):
    frame_ready = Signal()
    opened = Signal(int, int, float, str)   # width, height, fps, pixel format
    status = Signal(str)                    # message for the overlay, "" while streaming
    note = Signal(str)                      # one-off message worth showing (e.g. mode refused)

    def __init__(self, settings, slot):
        super().__init__()
        self.device_name = settings.video_device_name
        self.device_path = settings.video_device_path
        self.device_occurrence = settings.video_device_occurrence
        self.width = settings.width
        self.height = settings.height
        self.fps = settings.fps
        self.pixel_format = settings.pixel_format
        self.slot = slot
        self.capture_fps = 0.0
        self.offered_modes = []
        self._intervals = collections.deque(maxlen=300)  # ms between recent frames
        self._stop_event = threading.Event()
        self._retry_hint = ""
        self._mode_warned = False
        # Last healthy frame rate as measured (never the requested rate: the
        # card may not deliver that at all). Kept across reconnects so a card
        # that comes back already degraded is still recognised as stalled.
        self._healthy_fps = 0.0

    def stop(self, timeout_ms=5000):
        self._stop_event.set()
        if not self.wait(timeout_ms):
            print(f"Capture thread did not stop within {timeout_ms} ms (driver stuck?)")
            _orphaned_threads.append(self)

    def frame_interval_stats(self):
        """(mean, spread) of recent frame intervals in ms; 95 % of frames arrive
        within mean +- spread."""
        intervals = np.array(list(self._intervals))
        if len(intervals) < 10:
            return None
        mean = intervals.mean()
        return mean, float(np.percentile(np.abs(intervals - mean), 95))

    def run(self):
        devices.ensure_com_initialized()
        devices.boost_thread("Capture")
        while not self._stop_event.is_set():
            cap = None
            if not _device_lock.acquire(timeout=0.25):
                # A previous capture thread is still stuck in a driver call.
                self.status.emit("Waiting for the previous connection to close...")
                while not _device_lock.acquire(timeout=0.25):
                    if self._stop_event.is_set():
                        return
            try:
                if self._stop_event.is_set():
                    break
                cap = self._open()
                if cap is not None:
                    try:
                        self._stream(cap)
                    finally:
                        cap.release()
            finally:
                _device_lock.release()
            if not self._stop_event.is_set():
                self._stop_event.wait(RETRY_INTERVAL if cap is None else 0.2)
        self.capture_fps = 0.0

    def _open(self):
        # Re-enumerate on every attempt: the index can change when devices are
        # plugged in or removed, and this lets the card be hot-plugged.
        device = devices.find_video_device(devices.list_video_devices(), self.device_name,
                                           self.device_path, self.device_occurrence)
        if device is None:
            self.status.emit(f"{self.device_name} not found.\nWaiting for it to be connected...")
            return None

        # After a failed attempt, keep its explanation on screen while retrying.
        self.status.emit(f"Opening {device.label}...{self._retry_hint}")
        self.offered_modes = devices.list_video_modes(device)
        start = time.perf_counter()
        cap = cv2.VideoCapture(device.index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            took = time.perf_counter() - start
            print(f"Could not open {device.label} (attempt took {took:.1f} s)")
            if took > 5:
                # A healthy device opens or refuses within a second or two.
                hint = "The card seems to have stopped responding: unplug it and plug it back in."
            else:
                hint = "Is another program (e.g. OBS) using it?"
            self.status.emit(f"Could not open {device.label}.\n{hint}\nRetrying...")
            self._retry_hint = f"\n\nLast attempt failed. {hint}"
            return None
        self._retry_hint = ""
        self._configure(cap, device)
        print(f"Opened {device.label} (DirectShow index {device.index}) in "
              f"{(time.perf_counter() - start) * 1000:.0f} ms: {describe(cap)}")
        return cap

    def _requested_label(self):
        size = f"{self.width}x{self.height}" if self.width and self.height else "default size"
        rate = f" @ {self.fps} fps" if self.fps else ""
        fmt = f" {self.pixel_format}" if self.pixel_format in PIXEL_FORMATS[1:] else ""
        return size + rate + fmt

    def _configure(self, cap, device):
        fourcc = self.pixel_format if self.pixel_format in PIXEL_FORMATS[1:] else None
        want_size = (self.width, self.height) if self.width > 0 and self.height > 0 else None
        modes = self.offered_modes

        if modes:
            # The card lists what it can do: don't ask for anything else. A
            # refused request makes the driver fall back to its default mode
            # anyway, after several slow device restarts.
            matches = [m for m in modes
                       if (want_size is None or (m.width, m.height) == want_size)
                       and (fourcc is None or m.pixel_format == fourcc)]
            fast_enough = [m for m in matches if not self.fps or m.fps >= self.fps - 0.5]
            if not fast_enough:
                offered = ", ".join(m.label for m in modes[:6])
                message = f"{device.label} doesn't offer {self._requested_label()}. It offers: {offered}."
                print(message)
                if not self._mode_warned:
                    self._mode_warned = True
                    self.note.emit(message)
                if not matches:
                    return  # leave the device in its default mode
        # With the format on "auto", OpenCV already prefers uncompressed
        # formats over MJPG when a size is offered in several.

        # Order matters with OpenCV's DirectShow backend: FPS is applied by
        # restarting the device at its default size, so it has to be set
        # before the size, and the pixel format is only applied when the size
        # changes.
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        if self.fps > 0:
            cap.set(cv2.CAP_PROP_FPS, self.fps)
        if want_size:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_size[1])

        if fourcc and fourcc_of(cap) != fourcc and want_size:
            # The device was already at the requested size, so the format was
            # never applied. Bounce through another size to force it.
            bounce = (640, 480) if want_size != (640, 480) else (320, 240)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, bounce[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, bounce[1])
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_size[1])
            if fourcc_of(cap) != fourcc:
                print(f"Device did not accept pixel format {fourcc} at {want_size[0]}x{want_size[1]}")

    def _stream(self, cap):
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        fmt = fourcc_of(cap)
        self.opened.emit(width, height, fps, fmt)
        self.status.emit("Waiting for video...")

        failed_reads = 0
        receiving = False
        frames = 0
        fps_start = time.perf_counter()
        measure_start = measure_count = 0
        last_frame = 0.0
        recent = collections.deque()  # arrival times within the last STALL_WINDOW
        first_frame = 0.0

        while not self._stop_event.is_set():
            # read() blocks until the driver delivers a new frame; it returns
            # the newest one, so no pacing or sleeping is needed (or wanted).
            ok, frame = cap.read()
            now = time.perf_counter()
            if not ok or frame is None:
                failed_reads += 1
                if failed_reads >= MAX_FAILED_READS:
                    print("Capture: no frames for 2 s, reconnecting")
                    self.status.emit("No signal. Reconnecting...")
                    self.capture_fps = 0.0
                    return
                continue
            failed_reads = 0

            if not receiving:
                receiving = True
                self.status.emit("")
                measure_start, measure_count = now, 0
                first_frame = now
            if last_frame:
                self._intervals.append((now - last_frame) * 1000.0)
            last_frame = now

            h, w = frame.shape[:2]
            if (w, h) != (width, height):
                width, height = w, h
                self.opened.emit(width, height, fps, fmt)

            if self.slot.put(frame, now):
                self.frame_ready.emit()

            # Measure the real frame rate once, from frame arrival times.
            if measure_start:
                measure_count += 1
                if now - measure_start >= MEASURE_SECONDS:
                    measured = snap_rate((measure_count - 1) / (now - measure_start))
                    measure_start = 0
                    if measured in STANDARD_RATES:  # ignore readings taken mid-glitch
                        self._healthy_fps = measured
                    if abs(measured - fps) > 0.005:
                        fps = measured
                        self.opened.emit(width, height, fps, fmt)

            frames += 1
            if now - fps_start >= 1.0:
                self.capture_fps = frames / (now - fps_start)
                frames = 0
                fps_start = now

            # Stall watchdog.
            recent.append(now)
            while recent and now - recent[0] > STALL_WINDOW:
                recent.popleft()
            healthy = self._healthy_fps
            # Armed only once frames have been flowing for a while: cards often
            # deliver the first few frames slowly after opening.
            if (healthy >= 10 and now - first_frame > STALL_WINDOW + 1.0
                    and len(recent) < healthy * STALL_WINDOW * STALL_FRACTION):
                print(f"Capture: stalled at {len(recent) / STALL_WINDOW:.1f} fps "
                      f"(normally {healthy:g}), reconnecting")
                self.status.emit("Video stalled. Reconnecting...")
                self.capture_fps = 0.0
                return


def snap_rate(measured):
    """Round a measured frame rate to the standard rate it clearly matches."""
    # Card clocks are often a little off (e.g. 30.03); the measurement itself
    # is good to ~0.01 %, so 59.94 and 60 (0.1 % apart) are still told apart.
    nearest = min(STANDARD_RATES, key=lambda rate: abs(measured - rate))
    if abs(measured - nearest) / nearest < 0.003:
        return nearest
    return round(measured, 2)


def fourcc_of(cap):
    code = int(cap.get(cv2.CAP_PROP_FOURCC))
    if code <= 0:
        return ""
    text = "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))
    return text if text.isprintable() else ""


def describe(cap):
    fmt = fourcc_of(cap)
    fps = cap.get(cv2.CAP_PROP_FPS)
    return (f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
            + (f" @ {fps:.2f} fps" if fps > 0 else "")
            + (f", format {fmt}" if fmt else ""))
