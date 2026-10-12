"""Connector-side noise suppression for the callee microphone: numpy only, no model, no third-party code (night A3b).

Spectral Wiener gate on a 16 kHz stream: 40 ms sqrt-Hann STFT window, 20 ms hop (one core frame = 640 bytes = one hop),
noise floor from minimum statistics (smoothed power spectrum, minimum over the last ~2 s, bias-compensated), decision-directed
prior SNR, gain floor -18 dB, gain smoothed over frequency (3 taps) and over time (fast attack, slower release) so no musical
noise. Algorithmic delay one hop (20 ms). Output has exactly as many samples as the input.

Config "ns": "off" (default, today's behaviour) | "spectral". The wrapper only replaces frames once the core is streaming
(core._Tap.streaming), so the Silero answer gate keeps hearing raw audio; before that it only feeds the noise tracker (warm start).
"""
import time

import numpy as np

RATE = 16000
HOP = 320                 # 20 ms
WIN = 2 * HOP             # 40 ms
BINS = WIN // 2 + 1
FLOOR_DB = -18.0
MODES = ("off", "spectral")
_WINDOW = np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(WIN) / WIN)).astype(np.float32)   # sqrt-Hann: win^2 sums to 1 at 50 %


def normalize_mode(v) -> str:
    """Anything but a known mode (case-insensitive) means off."""
    v = str(v or "off").strip().lower()
    return v if v in MODES else "off"


class SpectralNS:
    SMOOTH = 0.85         # power smoothing for the noise tracker
    HISTORY = 100         # frames of smoothed power the minimum runs over (2 s)
    BIAS = 1.7            # minimum of a smoothed periodogram underestimates the mean noise power
    DD = 0.92             # decision-directed weight of the previous clean estimate
    ATTACK = 0.7          # gain rising: follows within ~1 frame (speech onsets stay intact)
    RELEASE = 0.2         # gain falling: ~5 frames (no pumping)
    WARMUP = 10           # frames passed through while the noise tracker has nothing to go on

    def __init__(self, floor_db: float = FLOOR_DB):
        self.floor = float(10 ** (floor_db / 20))
        self.reset()

    def reset(self):
        self._prev = np.zeros(HOP, np.float32)          # previous input hop (first half of the next window)
        self._tail = np.zeros(HOP, np.float32)          # overlap-add tail of the previous window
        self._pend = np.zeros(0, np.float32)            # input not yet a whole hop
        self._fifo = np.zeros(0, np.float32)            # processed output not yet handed out
        self._hist = np.full((self.HISTORY, BINS), np.inf, np.float32)
        self._hi = 0
        self._ps = None                                 # smoothed power
        self._s2 = np.zeros(BINS, np.float32)           # previous clean power estimate
        self._g = np.ones(BINS, np.float32)
        self.frames = 0
        self.total_s = 0.0
        self.max_s = 0.0

    # ---- one hop -----------------------------------------------------------------------------------------------------
    def _hop(self, new: np.ndarray) -> np.ndarray:
        buf = np.concatenate((self._prev, new))
        self._prev = new
        X = np.fft.rfft(buf * _WINDOW)
        P = (X.real * X.real + X.imag * X.imag).astype(np.float32)
        self._ps = P if self._ps is None else self.SMOOTH * self._ps + (1 - self.SMOOTH) * P
        self._hist[self._hi] = self._ps
        self._hi = (self._hi + 1) % self.HISTORY
        noise = self.BIAS * self._hist.min(axis=0) + 1e-3
        if self.frames < self.WARMUP:
            g = np.ones(BINS, np.float32)
            self._s2 = P
        else:
            post = P / noise
            xi = self.DD * self._s2 / noise + (1 - self.DD) * np.maximum(post - 1.0, 0.0)
            g = np.maximum(xi / (1.0 + xi), self.floor).astype(np.float32)
            p = np.pad(g, 1, mode="edge")                          # smooth over frequency
            g = 0.25 * p[:-2] + 0.5 * p[1:-1] + 0.25 * p[2:]
            a = np.where(g > self._g, self.ATTACK, self.RELEASE)   # and over time
            g = self._g + a * (g - self._g)
            self._s2 = (g * g * P).astype(np.float32)
        self._g = g
        y = np.fft.irfft(X * g, WIN).astype(np.float32) * _WINDOW
        out = self._tail + y[:HOP]
        self._tail = y[HOP:]
        return out

    # ---- stream API --------------------------------------------------------------------------------------------------
    def process(self, pcm: bytes) -> bytes:
        """PCM16 mono 16 kHz bytes in -> the same number of bytes out (delayed by one hop; zeros during the first hop)."""
        t0 = time.perf_counter()
        n = len(pcm) // 2
        if n == 0:
            return b""
        x = np.frombuffer(pcm, dtype=np.int16, count=n).astype(np.float32) * (1.0 / 32768.0)
        self._pend = np.concatenate((self._pend, x)) if self._pend.size else x
        outs = [self._fifo]
        while self._pend.size >= HOP:
            outs.append(self._hop(self._pend[:HOP]))
            self._pend = self._pend[HOP:]
            self.frames += 1
        buf = np.concatenate(outs) if len(outs) > 1 else self._fifo
        if buf.size < n:
            buf = np.concatenate((np.zeros(n - buf.size, np.float32), buf))
        self._fifo = buf[n:]
        res = np.clip(np.rint(buf[:n] * 32768.0), -32768, 32767).astype(np.int16).tobytes()
        dt = time.perf_counter() - t0
        self.total_s += dt
        self.max_s = max(self.max_s, dt)
        return res

    def stats(self) -> dict:
        f = max(1, self.frames)
        return {"frames": self.frames, "avg_ms": round(1000 * self.total_s / f, 3), "max_ms": round(1000 * self.max_s, 3)}


def wrap_callback(cb, ns: SpectralNS, is_streaming, on_event=None):
    """Capture callback for the source: always feeds `ns` (so its noise estimate is warm), but hands the processed frame to `cb`
    only while is_streaming() is true; before that the raw frame (the answer gate and the pre-roll hear unprocessed audio).
    Any failure inside ns turns it off for the rest of the call and passes raw audio."""
    state = {"ok": True}

    def f(frame: bytes):
        if state["ok"]:
            try:
                out = ns.process(frame)
                if is_streaming():
                    frame = out
            except Exception as e:  # noqa: BLE001
                state["ok"] = False
                if on_event:
                    on_event("ns_error", error=repr(e))
        cb(frame)
    return f
