"""Low-latency audio passthrough (capture card input -> speakers) over WASAPI.

The capture card's audio clock (derived from the console's HDMI signal) and the
output device's clock are independent crystals, so they always run at slightly
different speeds. Measured with a Cam Link 4K: 18 ppm apart with the PS5 at
60 Hz, but 0.2 % (2000+ ppm) when the PS5 outputs 59.94 Hz. A plain duplex
stream silently drops or repeats a sample every time that error adds up to one
sample: from one click a second up to ~100 slips a second (warbly, off-key).

So input and output are separate streams joined by a ring buffer, and the
output side resamples with a ratio that is continuously trimmed to hold the
buffer at a fixed fill level, i.e. to match the input's real rate. The same resampler converts to the
output device's native rate, so Windows' own converter is never used.
"""

import math
import time

import numpy as np

from . import devices

RING_SECONDS = 1.0
MARGIN_SECONDS = 0.005       # safety margin on top of the measured block sizes
UNDERRUN_BACKOFF = 0.004     # extra buffering added after each underrun...
MAX_BACKOFF = 0.060          # ...up to this much
# Drift control runs in two modes. "Acquire" (at start, or when the drift
# changes) locks on within seconds. "Track" then follows only slow changes, so
# irregular input does not become pitch wobble: while streaming video, a Cam
# Link 4K drops ~10 ms of audio every ~5 s, a sawtooth the buffer absorbs.
#                 (fill smoothing s, KP /s, KI /s^2)
ACQUIRE_GAINS = (0.3, 0.5, 0.1)
TRACK_GAINS = (3.0, 0.05, 0.002)
ACQUIRE_SECONDS = 8.0
DETECT_SMOOTHING = 1.0       # seconds; faster average used to spot a drift change
REACQUIRE_ERROR = 0.015      # track mode this far off target -> the drift changed
MAX_TRIM = 0.01              # +-1 %; real clock pairs have been seen 0.2 % apart
MAX_EXCESS_SECONDS = 0.080   # buffer this far above target -> jump forward
STALL_SECONDS = 2.0


class AudioPassthrough:
    def __init__(self):
        self._input = None
        self._output = None
        self.volume = 100
        self.muted = False
        self.input_name = ""
        self.output_name = ""
        self.error = ""
        self._reset_state(48000.0, 48000.0, 2)

    def _reset_state(self, in_rate, out_rate, channels):
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._ring = np.zeros((int(in_rate * RING_SECONDS), channels), np.float32)
        self._written = 0            # total input frames written (int)
        self._read_pos = 0.0         # fractional input frame position of the reader
        self._primed = False
        self._fill_avg = 0.0
        self._fill_fast = 0.0
        self._integral = 0.0
        self._trim = 0.0
        self._gains = ACQUIRE_GAINS
        self._acquire_left = ACQUIRE_SECONDS
        self._in_block_max = 0
        self._out_need_max = 0.0
        self._backoff = 0.0
        self._clock = time.perf_counter
        self._input_mark = (0, self._clock())   # (_written, time it was published)
        self.underruns = 0
        self._ramp = np.arange(8192, dtype=np.float64)
        self._taps = np.arange(-1, 3)

    # --- public ------------------------------------------------------------

    @property
    def active(self):
        return self._output is not None

    @property
    def stalled(self):
        """True if the input device stopped delivering (e.g. card unplugged)."""
        return self.active and self._clock() - self._input_mark[1] > STALL_SECONDS

    def start(self, input_name, output_name=""):
        import sounddevice as sd

        self.stop()
        self.error = ""
        if not input_name:
            return False

        source = next((d for d in devices.list_audio_devices("input") if d.name == input_name), None)
        if source is None:
            self.error = f"Audio device not found: {input_name}"
            return False
        sink = None
        if output_name:
            sink = next((d for d in devices.list_audio_devices("output") if d.name == output_name), None)
        sink = sink or devices.default_audio_output()
        if sink is None:
            self.error = "No audio output device available"
            return False

        in_channels = min(2, source.channels)
        out_channels = min(2, sink.channels)
        self._reset_state(float(source.samplerate), float(sink.samplerate), in_channels)
        # Both streams run at their device's native (mix format) rate, so
        # auto_convert should never kick in; it is only a fallback.
        wasapi = sd.WasapiSettings(auto_convert=True)
        try:
            self._output = sd.OutputStream(device=sink.index, samplerate=self.out_rate,
                                           channels=out_channels, dtype="float32",
                                           latency="low", extra_settings=wasapi,
                                           callback=self._on_output)
            self._input = sd.InputStream(device=source.index, samplerate=self.in_rate,
                                         channels=in_channels, dtype="float32",
                                         latency="low", extra_settings=wasapi,
                                         callback=self._on_input)
            self._input.start()
            self._output.start()
        except sd.PortAudioError as e:
            self.stop()
            self.error = f"Could not start audio: {e}"
            print(self.error)
            return False

        self.input_name = source.name
        self.output_name = sink.name
        print(f"Audio: {source.name} ({self.in_rate:.0f} Hz) -> {sink.name} ({self.out_rate:.0f} Hz), "
              f"device latency {self._input.latency * 1000:.1f} ms in / "
              f"{self._output.latency * 1000:.1f} ms out")
        return True

    def stop(self):
        for stream in (self._input, self._output):
            if stream is not None:
                try:
                    stream.close()
                except Exception as e:
                    print(f"Error closing audio stream: {e}")
        self._input = None
        self._output = None
        self.input_name = ""
        self.output_name = ""

    def _set_gains(self, gains, error):
        """Switch control mode without a jump in pitch: rescale the integral so
        the new gains produce the current trim."""
        _, kp, ki = gains
        self._integral = (self._trim - kp * error) / ki
        self._gains = gains
        self._acquire_left = ACQUIRE_SECONDS if gains is ACQUIRE_GAINS else 0.0

    # --- real-time callbacks -------------------------------------------------
    # These run on PortAudio threads: no locks, no Qt, minimal allocation. The
    # writer publishes _written only after copying, and the reader only reads
    # below the _written it sampled, so no lock is needed.

    def _on_input(self, indata, frames, time_info, status):
        ring = self._ring
        capacity = len(ring)
        start = self._written % capacity
        first = min(frames, capacity - start)
        ring[start:start + first] = indata[:first]
        if first < frames:
            ring[:frames - first] = indata[first:]
        if frames > self._in_block_max:
            self._in_block_max = frames
        self._written += frames
        self._input_mark = (self._written, self._clock())

    def _on_output(self, outdata, frames, time_info, status):
        written = self._written
        # Input arrives in blocks (~10 ms), so `written` alone only moves in
        # steps and a slow drift would stay invisible until it crossed a whole
        # block. Interpolating from the last block's arrival time gives a
        # continuous estimate of the input position for the drift control.
        marked, marked_at = self._input_mark
        estimated = marked + min(self._in_block_max,
                                 max(0.0, (self._clock() - marked_at) * self.in_rate))
        base_step = self.in_rate / self.out_rate
        need = frames * base_step * (1.0 + MAX_TRIM) + 3.0
        if need > self._out_need_max:
            self._out_need_max = need
        target = (self._in_block_max + self._out_need_max
                  + (MARGIN_SECONDS + self._backoff) * self.in_rate)
        fill = estimated - self._read_pos

        # Re-aligning the read position keeps the integral: it holds the
        # learned clock drift, which has not changed just because of a glitch.
        if not self._primed:
            if written - self._read_pos < target:
                outdata.fill(0.0)
                return
            self._read_pos = estimated - target
            self._fill_avg = self._fill_fast = fill = target
            self._primed = True
        elif fill - target > MAX_EXCESS_SECONDS * self.in_rate:
            # Fell far behind (e.g. output device hiccup): skip ahead.
            self._read_pos = estimated - target
            self._fill_avg = self._fill_fast = fill = target

        # Drift control: trim the resampling ratio to hold the fill at target.
        dt = frames / self.out_rate
        smoothing, kp, ki = self._gains
        self._fill_avg += (fill - self._fill_avg) * min(1.0, dt / smoothing)
        self._fill_fast += (fill - self._fill_fast) * min(1.0, dt / DETECT_SMOOTHING)
        error = (self._fill_avg - target) / self.in_rate          # seconds
        fast_error = (self._fill_fast - target) / self.in_rate
        self._integral = max(-MAX_TRIM / ki, min(MAX_TRIM / ki, self._integral + error * dt))
        self._trim = max(-MAX_TRIM, min(MAX_TRIM, kp * error + ki * self._integral))
        if self._acquire_left > 0:
            self._acquire_left -= dt
            if self._acquire_left <= 0:
                self._set_gains(TRACK_GAINS, error)
        elif abs(fast_error) > REACQUIRE_ERROR:
            self._set_gains(ACQUIRE_GAINS, error)
        step = base_step * (1.0 + self._trim)

        if math.floor(self._read_pos + step * (frames - 1)) + 2 >= written:
            # Not enough input: play silence and re-buffer with more headroom.
            outdata.fill(0.0)
            self.underruns += 1
            self._backoff = min(MAX_BACKOFF, self._backoff + UNDERRUN_BACKOFF)
            self._primed = False
            if fast_error < -MARGIN_SECONDS / 2:
                self._set_gains(ACQUIRE_GAINS, error)  # ran dry while draining: drift changed
            return

        if frames > len(self._ramp):
            self._ramp = np.arange(frames, dtype=np.float64)
        positions = self._read_pos + step * self._ramp[:frames]
        self._read_pos += step * frames

        # Catmull-Rom cubic interpolation between input samples.
        base = np.floor(positions)
        t = (positions - base).astype(np.float32)[:, None]
        index = (base.astype(np.int64)[:, None] + self._taps) % len(self._ring)
        taps = self._ring[index]                     # (frames, 4, channels)
        y0, y1, y2, y3 = taps[:, 0], taps[:, 1], taps[:, 2], taps[:, 3]
        y = y1 + 0.5 * t * (y2 - y0 + t * (2.0 * y0 - 5.0 * y1 + 4.0 * y2 - y3
                                          + t * (3.0 * (y1 - y2) + y3 - y0)))

        gain = 0.0 if self.muted else self.volume / 100.0
        out_channels = outdata.shape[1]
        if y.shape[1] >= out_channels:
            np.multiply(y[:, :out_channels], gain, out=outdata)
        else:
            outdata[:] = y[:, :1] * gain             # mono source -> every output channel
