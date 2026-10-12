"""Windows call audio for the Youssef connector: the iPhone Hands-Free endpoints (hf_direct, WASAPI by MMDevice ID via
soundcard) or the virtual-cable endpoints (cable, WASAPI via PortAudio), 16 kHz mono PCM16, 20 ms blocks.

Everything below the imports is copied verbatim from the private dialer's native/pipeline.py (see PORT_NOTES.md for the
source line ranges). Importable on any platform: Windows-only packages (sounddevice, soundcard, pycaw, comtypes) are
imported lazily inside the functions that need them."""
import sys
import threading
import time

import numpy as np

RATE = 16000                 # pipeline + PipeWire rate (mSBC is 16 kHz)
CHUNK = 640                  # 20 ms of 16 kHz mono s16
TTS_RATE = 24000             # VoiceTut and OpenAI TTS both return PCM16 24 kHz
OUT_TARGET_DBFS = -8.0      # Youssef's speech is raised to this RMS level on the phone line (owner: maximum loudness and clarity)
OUT_MAX_GAIN_DB = 24.0      # never boost more than this (keeps breaths/noise from being pumped up)
OUT_CEILING = 0.97          # soft limiter ceiling, about -0.3 dBFS: loud but never hard-clipped (the HFP link distorts on overload)
OUT_KNEE = 0.85             # the limiter knee is OUT_KNEE x ceiling
OUT_LIMIT_MARGIN = 0.80     # the final lookahead limiter's limit = margin x ceiling (about -2.2 dBFS): headroom for the rare CVSD
                            # fallback, where Windows' 16 -> 8 kHz conversion overshoots knee-flattened peaks by up to ~1.7 dB
                            # (measured offline with soxr; 0.88 still hard-clipped there). Costs ~0.3 dB RMS on the normal link.
OUT_PEAK_DRIVE = 1.0        # the AGC never pushes a block's peak beyond this x ceiling: the soft knee only catches the AGC's
                            # lag (~1% of speech samples); 2.0 flat-topped 4-5% of them, audibly harsh on the wideband link
OUT_LP_HZ = 7000.0          # the call link is mSBC 16 kHz (audio_devices logs 16000Hz/16bit/1ch in-call): only the top octave
                            # is trimmed; the former 3.5 kHz low-pass (tuned for a CVSD link that is not negotiated) muffled Youssef
HP_HZ = 100.0               # keeps the fundamental of a male voice (~100-130 Hz); only DC/rumble below is removed
PRESENCE_HZ, PRESENCE_DB, PRESENCE_Q = 2200.0, 4.0, 0.9   # speech presence boost for clarity on the phone link


class Biquad:
    """Stateful 2nd-order IIR section (direct form II transposed, RBJ cookbook), state kept across blocks."""

    def __init__(self, b, a):
        a0 = a[0]
        self.b0, self.b1, self.b2 = b[0] / a0, b[1] / a0, b[2] / a0
        self.a1, self.a2 = a[1] / a0, a[2] / a0
        self.z1 = self.z2 = 0.0

    @classmethod
    def highpass(cls, fs: float, f0: float, q: float = 0.70710678):   # q = 1/sqrt(2): 2nd-order Butterworth
        w, c = 2 * np.pi * f0 / fs, np.cos(2 * np.pi * f0 / fs)
        al = np.sin(w) / (2 * q)
        return cls(((1 + c) / 2, -(1 + c), (1 + c) / 2), (1 + al, -2 * c, 1 - al))

    @classmethod
    def peaking(cls, fs: float, f0: float, gain_db: float, q: float):
        w, c = 2 * np.pi * f0 / fs, np.cos(2 * np.pi * f0 / fs)
        big_a = 10 ** (gain_db / 40)
        al = np.sin(w) / (2 * q)
        return cls((1 + al * big_a, -2 * c, 1 - al * big_a), (1 + al / big_a, -2 * c, 1 - al / big_a))

    def process(self, x: np.ndarray) -> np.ndarray:
        b0, b1, b2, a1, a2, z1, z2 = self.b0, self.b1, self.b2, self.a1, self.a2, self.z1, self.z2
        out = [0.0] * len(x)
        for i, v in enumerate(x.tolist()):
            o = b0 * v + z1
            z1 = b1 * v - a1 * o + z2
            z2 = b2 * v - a2 * o
            out[i] = o
        self.z1, self.z2 = z1, z2
        return np.asarray(out, dtype=np.float32)


class FirLowpass:
    """Linear-phase windowed-sinc low-pass (np.convolve, state kept across blocks), placed BEFORE the final limiter so the
    limited peaks are the peaks that reach the phone. The call link is mSBC 16 kHz (measured in-call), so the cutoff is
    OUT_LP_HZ = 7 kHz, which only trims the top octave; OUT_LIMIT_MARGIN carries the headroom for a CVSD 8 kHz fallback.
    Delay: (taps - 1) / 2 samples."""

    def __init__(self, fs: float, fc: float, taps: int = 129, beta: float = 8.0):
        k = np.arange(taps) - (taps - 1) / 2
        h = np.sinc(2 * fc / fs * k) * np.kaiser(taps, beta)
        self.h, self.state = h / h.sum(), np.zeros(taps - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        z = np.concatenate([self.state, x.astype(np.float64)])
        self.state = z[-(len(self.h) - 1):]
        return np.convolve(z, self.h, "valid").astype(np.float32)


class LookaheadLimiter:
    """Peak limiter that never exceeds `limit`: gain = moving average of a sliding minimum of the per-sample required gain, so
    it moves smoothly (no waveshaping harmonics). Delay: `look` samples."""

    def __init__(self, limit: float, look: int = 40):
        self.limit, self.look = limit, look
        self.buf = np.zeros(3 * look, dtype=np.float32)

    def process(self, y: np.ndarray) -> np.ndarray:
        L, n = self.look, len(y)
        Y = np.concatenate([self.buf, y.astype(np.float32)])
        m = len(Y)
        need = np.minimum(1.0, self.limit / np.maximum(np.abs(Y), 1e-9))
        h = need[:m - L].copy()
        for k in range(1, L + 1):
            h = np.minimum(h, need[k:m - L + k])
        cs = np.concatenate([[0.0], np.cumsum(h, dtype=np.float64)])
        idx = np.arange(m - n - L, m - L)
        g = (cs[idx + 1] - cs[idx - L]) / (L + 1)
        self.buf = Y[-3 * L:]
        return (Y[idx] * g).astype(np.float32)


class Loudness:
    """Streaming loudness for 16 kHz PCM16: 100 Hz high-pass + 2.2 kHz presence peak (stateful across blocks), slow automatic
    gain towards OUT_TARGET_DBFS during speech (never driving a block's peak beyond OUT_PEAK_DRIVE x ceiling), a soft-knee
    limiter, a 7 kHz low-pass and a final lookahead limiter, so the output never hard-clips on the mSBC 16 kHz link, nor
    after Windows' 16 -> 8 kHz conversion should the link fall back to CVSD. Gain only moves during speech, so pauses stay
    quiet. Shared by the hosted agent (eleven.py) and the cascade's ElevenLabs voice, so Youssef is equally loud and clear
    on both engines. Adds ~6.5 ms delay."""

    def __init__(self, target_dbfs: float = OUT_TARGET_DBFS, max_gain_db: float = OUT_MAX_GAIN_DB,
                 ceiling: float = OUT_CEILING, eq: bool = True):
        self.target = 10 ** (target_dbfs / 20)
        self.max_gain = 10 ** (max_gain_db / 20)
        self.ceiling = ceiling
        self.eq = eq
        self.clear()
        self.gains, self.peak = [], 0.0   # per-call telemetry: AGC gain of each speech block, output peak (linear)

    def clear(self):
        """Fresh filter/limiter state (e.g. after an interruption); the AGC gain is kept."""
        if not hasattr(self, "g"):
            self.g = 10 ** (10 / 20)   # start at +10 dB so the first word is already loud
        self.filters = [Biquad.highpass(RATE, HP_HZ), Biquad.peaking(RATE, PRESENCE_HZ, PRESENCE_DB, PRESENCE_Q)] if self.eq else []
        self.lp = FirLowpass(RATE, OUT_LP_HZ)
        self.limiter = LookaheadLimiter(self.ceiling * OUT_LIMIT_MARGIN)
        self.delay = (len(self.lp.h) - 1) // 2 + self.limiter.look

    def process(self, x: np.ndarray) -> np.ndarray:
        if not len(x):
            return x
        f = x.astype(np.float32) / 32768.0
        for flt in self.filters:
            f = flt.process(f)
        rms = float(np.sqrt(np.mean(f * f)))
        if rms > 0.003:  # speech, not silence
            want = min(max(self.target / rms, 1.0), self.max_gain)
            want = max(min(want, OUT_PEAK_DRIVE * self.ceiling / max(float(np.max(np.abs(f))), 1e-6)), 1.0)
            self.g += (want - self.g) * (0.5 if want < self.g else 0.15)  # fast down, slower up
            self.gains.append(self.g)
        y = f * self.g
        t = self.ceiling * OUT_KNEE
        a = np.abs(y)
        over = a > t
        if over.any():
            y[over] = np.sign(y[over]) * (t + (self.ceiling - t) * np.tanh((a[over] - t) / (self.ceiling - t)))
        y = self.limiter.process(self.lp.process(y))
        self.peak = max(self.peak, float(np.max(np.abs(y))))
        return np.clip(y * 32767.0, -32768, 32767).astype(np.int16)

    def stats(self) -> dict:
        """Per-call summary for the audio_stats line: AGC gain over speech (dB, median and max) and the output peak (dBFS)."""
        out = {"out_peak_dbfs": round(20 * np.log10(self.peak), 1) if self.peak > 0 else None}
        if self.gains:
            db = 20 * np.log10(np.asarray(self.gains, dtype=np.float64))
            out.update(out_gain_p50_db=round(float(np.median(db)), 1), out_gain_max_db=round(float(db.max()), 1))
        return out

    def drain(self) -> np.ndarray:
        """The audio still held in the filter/limiter delay (call at the end of an utterance)."""
        return self.process(np.zeros(self.delay, dtype=np.int16))


class LoudBlocks:
    """Cuts a PCM16 byte stream into 100 ms blocks for Loudness.process, so cached and freshly streamed audio get the same
    gain behaviour whatever the network chunking was."""
    SIZE = 3200

    def __init__(self, loud: Loudness):
        self.loud, self.buf = loud, b""

    def push(self, b: bytes) -> list:
        self.buf += b
        out = []
        while len(self.buf) >= self.SIZE:
            blk, self.buf = self.buf[:self.SIZE], self.buf[self.SIZE:]
            out.append(self.loud.process(np.frombuffer(blk, dtype=np.int16)).tobytes())
        return out

    def flush(self) -> bytes:
        blk, self.buf = self.buf[:len(self.buf) // 2 * 2], b""
        out = self.loud.process(np.frombuffer(blk, dtype=np.int16)).tobytes() if blk else b""
        return out + self.loud.drain().tobytes()   # end of the sentence: play the audio still held in the delay


HF_MATCH = ("iphone", "hands-free hf audio")  # WASAPI names: "Microphone (iPhone Hands-Free HF Audio)" / "Speakers (iPhone Hands-Free HF Audio)"


_PA_LOCK = threading.Lock()
AUDIO_BUSY = threading.Event()  # set while a call's streams are open: never re-initialise PortAudio then
_PA_EXEC = None  # one thread for every PortAudio init/enumerate/open/close: WDM-KS streams opened on another thread
                 # than the one that ran Pa_Initialize never deliver input callbacks


def pa_call(fn, *a, **kw):
    global _PA_EXEC
    if _PA_EXEC is None:
        from concurrent.futures import ThreadPoolExecutor
        _PA_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="portaudio")
    return _PA_EXEC.submit(fn, *a, **kw).result()


def find_hf_devices(match=HF_MATCH, refresh: bool = True) -> tuple:
    return pa_call(_find_hf_devices, match, refresh)


def _find_hf_devices(match=HF_MATCH, refresh: bool = True) -> tuple:
    """(input_index, output_index, info) of the WASAPI Hands-Free endpoints (present while the iPhone is connected),
    None when absent. PortAudio enumerates once per init, so re-init to see endpoints that appeared after start
    (skipped while a call's streams are open: Pa_Terminate would close them)."""
    import sounddevice as sd
    with _PA_LOCK:
        if refresh and not AUDIO_BUSY.is_set():
            try:
                sd._terminate()
                sd._initialize()
            except Exception:  # noqa: BLE001
                pass
        return _find_hf(sd, match)


def _find_hf(sd, match):
    """Prefer WASAPI ("Microphone (iPhone Hands-Free HF Audio)"); fall back to WDM-KS, where PortAudio shows the raw name
    "Input (@System32\\drivers\\bthhfenum.sys,#4;%1 Hands-Free HF Audio%0 ;(iPhone))" (WASAPI may not list it)."""
    found = {}
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].lower()
        if not all(m in name for m in match):
            continue
        api = sd.query_hostapis(d["hostapi"])["name"]
        rank = 0 if api.endswith("WASAPI") else 1 if "WDM-KS" in api else None
        if rank is None:
            continue
        for kind in ("input", "output"):
            if d[f"max_{kind}_channels"] > 0 and (kind not in found or rank < found[kind][0]):
                found[kind] = (rank, i, api, int(d["default_samplerate"]))
    ii = found["input"][1] if "input" in found else None
    oi = found["output"][1] if "output" in found else None
    info = {k[:3] if k == "output" else "in": f"{v[2]} #{v[1]} @{v[3]}" for k, v in found.items()}
    return ii, oi, info


def _stream_args(sd, device: int) -> tuple:
    """(device rate, extra_settings): WASAPI shared mode converts to 16 kHz itself; WDM-KS runs at the device rate."""
    d = sd.query_devices(device)
    if sd.query_hostapis(d["hostapi"])["name"].endswith("WASAPI"):
        return RATE, sd.WasapiSettings(auto_convert=True)
    return int(d["default_samplerate"]), None


class SdSource:  # start/stop run on the PortAudio thread (pa_call)
    """Callee audio: WASAPI capture from the Hands-Free microphone endpoint, 16 kHz mono s16 (WASAPI auto-convert
    resamples from the endpoint's native mSBC 16 kHz / CVSD 8 kHz), 20 ms blocks, optional gain."""

    def __init__(self, device: int, gain: float = 1.0, dump=None):
        self.device, self.gain, self.dump = device, gain, dump
        self.stream = None
        self.levels = []
        self.error = None
        self.stopped = False

    def start(self, cb):
        pa_call(self._start, cb)

    def _bs(self, sd):
        api = sd.query_hostapis(sd.query_devices(self.device)["hostapi"])["name"]
        return CHUNK // 2 if api.endswith("WASAPI") else 0  # WDM-KS: driver period (a forced 20 ms block repeated buffers)

    def _start(self, cb):
        import sounddevice as sd

        dev_rate, extra = _stream_args(sd, self.device)
        rs = None
        if dev_rate != RATE:
            import soxr
            rs = soxr.ResampleStream(dev_rate, RATE, 1, dtype="int16")
        self.dev_rate = dev_rate

        def callback(indata, frames, t, status):
            x = np.frombuffer(bytes(indata), dtype=np.int16)
            if rs is not None:
                x = rs.resample_chunk(x)
            if self.gain != 1.0:
                x = np.clip(x.astype(np.float32) * self.gain, -32768, 32767).astype(np.int16)
            r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0
            self.levels.append(round(20 * np.log10(r / 32768), 1) if r > 0 else -120.0)
            if self.dump:
                self.dump.write(x.tobytes())
            pend.extend(x.tobytes())
            while len(pend) >= CHUNK:
                cb(bytes(pend[:CHUNK]))
                del pend[:CHUNK]

        pend = bytearray()

        def finished():
            self.error = self.error or "stream finished"

        with _PA_LOCK:
            self.stream = sd.RawInputStream(samplerate=dev_rate, blocksize=self._bs(sd), channels=1, dtype="int16",
                                            device=self.device, callback=callback, finished_callback=finished,
                                            extra_settings=extra)
            self.stream.start()

    def alive(self):
        if self.stream is None:  # not opened yet (pipeline still starting): not "gone"
            return not self.stopped and self.error is None
        return self.stream.active and self.error is None

    def stop(self):
        pa_call(self._stop)

    def _stop(self):
        self.stopped = True
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.abort()
                s.close()
            except Exception:  # noqa: BLE001
                pass


class SdPlayer:
    """Bot audio to the Hands-Free speaker endpoint. The WASAPI callback pulls one 20 ms block at a time (silence when idle),
    so the device clock paces playback. A new utterance starts once 100 ms is queued (or 150 ms after its first byte),
    which absorbs TTS jitter; flush() (barge-in) drops the queue at once. Same interface as Player."""
    max_queue = 0.2  # CallOutput keeps at most this much queued here

    def __init__(self, device: int, dump=None, prebuffer: float = 0.1):
        self.device, self.dump, self.prebuffer = device, dump, prebuffer
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.stream = None
        self.ticks = 0
        self.primed = False
        self.first_at = None
        self.error = None
        self._rs = None
        self._dev_rate = RATE

    def feed(self, b: bytes):
        with self.lock:
            if not self.buf and not self.primed:
                self.first_at = time.monotonic()
            self.buf += b

    def flush(self) -> int:
        with self.lock:
            n = len(self.buf)
            self.buf.clear()
            self.primed, self.first_at = False, None
        return n

    def pending(self) -> float:
        with self.lock:
            return len(self.buf) / (RATE * 2)

    def _callback(self, outdata, frames, t, status):
        if self._rs is not None:  # device at 8 kHz: pull 16 kHz, resample down
            need16 = frames * 2 * RATE // self._dev_rate
            chunk = self._take(need16)
            y = self._rs.resample_chunk(np.frombuffer(chunk, dtype=np.int16)).tobytes()
            outdata[:] = y[:frames * 2].ljust(frames * 2, b"\0")
            return
        need = frames * 2
        outdata[:] = self._take(need)

    def _take(self, need: int) -> bytes:
        with self.lock:
            if not self.primed and self.buf and (len(self.buf) >= self.prebuffer * RATE * 2 or
                                                 (self.first_at and time.monotonic() - self.first_at > 0.15)):
                self.primed = True
            if self.primed:
                chunk = bytes(self.buf[:need])
                del self.buf[:need]
                if not self.buf:
                    self.primed, self.first_at = False, None
            else:
                chunk = b""
        chunk = chunk.ljust(need, b"\0")
        if self.dump:
            self.dump.write(chunk)
        self.ticks += 1
        return chunk

    def start(self):
        pa_call(self._start)

    def _bs(self, sd):
        api = sd.query_hostapis(sd.query_devices(self.device)["hostapi"])["name"]
        return CHUNK // 2 if api.endswith("WASAPI") else 0

    def _start(self):
        import sounddevice as sd
        if self.stream is None:
            def finished():
                self.error = self.error or "stream finished"
            dev_rate, extra = _stream_args(sd, self.device)
            self._dev_rate = dev_rate
            if dev_rate != RATE:
                import soxr
                self._rs = soxr.ResampleStream(RATE, dev_rate, 1, dtype="int16")
            with _PA_LOCK:
                self.stream = sd.RawOutputStream(samplerate=dev_rate, blocksize=self._bs(sd), channels=1, dtype="int16",
                                                 device=self.device, callback=self._callback,
                                                 finished_callback=finished, extra_settings=extra)
                self.stream.start()

    def alive(self):
        return self.stream is not None and self.stream.active and self.error is None

    def stop(self):
        pa_call(self._stop)

    def _stop(self):
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:  # noqa: BLE001
                pass


def _com_init():
    """Join the process MTA on this thread (soundcard only initialises COM on the thread that imports it)."""
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # S_FALSE / RPC_E_CHANGED_MODE are fine
    except Exception:  # noqa: BLE001
        pass


def _com_mta():
    """Join the MTA on this (fresh) thread the way comtypes expects, like phone._com_thread: sys.coinit_flags = 0 BEFORE
    comtypes is imported, so its import-time CoInitializeEx asks for the MTA too (with the default STA flag it raised
    RPC_E_CHANGED_MODE "Cannot change thread mode after it is set" on every call)."""
    sys.coinit_flags = 0   # COINIT_MULTITHREADED, read by comtypes at import
    import comtypes
    comtypes.CoInitializeEx(0)
    return comtypes


def _sc():
    """soundcard's WASAPI wrapper (opens an endpoint by its MMDevice ID, which PortAudio cannot do for the Hands-Free
    endpoints). Imported lazily; its data-discontinuity warnings are counted, not printed."""
    import warnings
    from soundcard import mediafoundation as mf
    if not getattr(_sc, "hooked", False):
        warnings.simplefilter("always", mf.SoundcardRuntimeWarning)
        orig = warnings.showwarning

        def show(message, category, *a, **kw):
            if category is mf.SoundcardRuntimeWarning:
                _sc.discontinuities += 1
                return
            orig(message, category, *a, **kw)
        warnings.showwarning = show
        _sc.hooked, _sc.discontinuities = True, 0
    return mf


class IdSource:
    """Callee audio from a capture endpoint opened by MMDevice ID ("hf_direct": the iPhone Hands-Free microphone, which
    WASAPI enumeration hides). Shared mode, 16 kHz float -> s16 mono, 20 ms chunks from a reader thread. WASAPI's own
    resampler converts from the endpoint format; gaps (no packets for 4 periods) come back as silence, so the pipeline
    keeps a steady clock. Same interface as SdSource."""

    def __init__(self, endpoint_id: str, gain: float = 1.0, dump=None):
        self.endpoint_id, self.gain, self.dump = endpoint_id, gain, dump
        self.levels = []
        self.error = None
        self.stopped = False
        self.started = False
        self.thread = None
        self.on_event = None   # callback(event, **kw) set by the engine, used for the capture_volume log line
        self.capture_volume = "off"   # config "capture_volume": "off" | "log" (read the Hands-Free mic endpoint volume) | "max" (log, then set 1.0)
        self.disc0 = getattr(_sc, "discontinuities", 0)   # soundcard's counter is process-wide: this call's count is the delta
        self.in_ch, self.nsamp, self.t_start, self.t_last = None, 0, None, None   # telemetry: stream channels, samples, timing

    def start(self, cb):
        self.thread = threading.Thread(target=self._run, args=(cb,), daemon=True, name="id-source")
        self.thread.start()
        if self.capture_volume in ("log", "max"):
            threading.Thread(target=self._capture_volume, daemon=True, name="id-capvol").start()

    def _capture_volume(self):
        """Opt-in (config capture_volume): log the Hands-Free CAPTURE endpoint's master volume + mute and, in "max" mode, set the volume to
        1.0, so a low Windows microphone level never makes the callee quieter before the server hears it (the render side is already
        maxed by IdPlayer). Same one-endpoint-by-ID COM path as IdPlayer; never raises, only logs. The mute state is only reported."""
        info = {"endpoint": str(self.endpoint_id)[-14:], "mode": self.capture_volume}
        try:
            from . import micguard
            mics = micguard.WinMics()

            def fn(comtypes):
                ev = mics._ev(comtypes, mics._en(comtypes), self.endpoint_id)
                out = {"before_scalar": round(float(ev.GetMasterVolumeLevelScalar()), 2), "mute": bool(ev.GetMute())}
                if self.capture_volume == "max":
                    ev.SetMasterVolumeLevelScalar(1.0, None)
                    out["after_scalar"] = round(float(ev.GetMasterVolumeLevelScalar()), 2)
                return out
            info.update(micguard._com_thread(fn) or {"error": "no result"})
        except Exception as e:  # noqa: BLE001
            info["error"] = f"{type(e).__name__}: {e}"[:150]
        try:
            if self.on_event:
                self.on_event("capture_volume", **info)
        except Exception:  # noqa: BLE001
            pass

    def _run(self, cb):
        try:
            mf = _sc()      # first: soundcard's import-time CoInitialize raises if this thread already has COM
            _com_init()     # other threads: join the MTA
            mic = mf._Microphone(mf._Device(self.endpoint_id))
            with mic.recorder(samplerate=RATE, channels=1, blocksize=RATE // 10) as rec:  # 100 ms WASAPI buffer
                self.started = True
                while not self.stopped:
                    raw = rec.record(numframes=CHUNK // 2)
                    self.in_ch = raw.shape[1]
                    now = time.monotonic()
                    self.t_start = self.t_start or now
                    self.t_last = now
                    self.nsamp += raw.shape[0]
                    f = raw[:, 0] * self.gain
                    x = (np.clip(f, -1.0, 1.0) * 32767).astype(np.int16)
                    r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
                    self.levels.append(round(20 * np.log10(r / 32768), 1) if r > 0 else -120.0)
                    if self.dump:
                        self.dump.write(x.tobytes())
                    cb(x.tobytes())
        except Exception as e:  # noqa: BLE001  (0x88890004 AUDCLNT_E_DEVICE_INVALIDATED: hands-free link dropped)
            if not self.stopped:
                self.error = f"{type(e).__name__}: {e}"[:200]

    def alive(self):
        return not self.stopped and self.error is None and (self.thread is None or self.thread.is_alive())

    def stats(self) -> dict:
        """Capture telemetry: channels of the stream WASAPI returned, its measured sample rate, soundcard discontinuities this call."""
        span = (self.t_last - self.t_start) if self.t_start is not None else 0.0
        return {"in_channels": self.in_ch, "in_rate_est": round(self.nsamp / span) if span > 0 else None,
                "discontinuities": getattr(_sc, "discontinuities", 0) - self.disc0}

    def stop(self):
        self.stopped = True
        if self.thread is not None:
            self.thread.join(timeout=2)


class IdPlayer(SdPlayer):
    """Bot audio to a render endpoint opened by MMDevice ID ("hf_direct": the iPhone Hands-Free speaker). A writer thread
    takes 20 ms at a time from the SdPlayer jitter buffer (100 ms prebuffer per utterance, silence when idle) and blocks
    while the WASAPI buffer (100 ms) is full, so the device clock paces it."""

    def __init__(self, endpoint_id: str, dump=None, prebuffer: float = 0.1):
        super().__init__(None, dump=dump, prebuffer=prebuffer)
        self.endpoint_id = endpoint_id
        self.stopped = False
        self.thread = None
        self.on_event = None   # callback(event, **kw) set by the engine, used for the endpoint_volume log line
        self.endpoint_volume = True   # config "endpoint_volume": false skips the max-volume step

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="id-player")
            self.thread.start()
            if self.endpoint_volume:
                threading.Thread(target=self._max_endpoint_volume, daemon=True, name="id-volume").start()

    def _max_endpoint_volume(self):
        """Each call: log the Hands-Free render endpoint's master volume + mute, then set 1.0 and unmute, so Windows' own
        volume never makes Youssef quieter. Opens only this one endpoint by its MMDevice ID (micguard's helper and COM thread;
        no device enumeration: pycaw GetAllDevices access-violated in the frozen exe). Never raises, only logs."""
        info = {"endpoint": str(self.endpoint_id)[-14:]}
        try:
            from . import micguard
            mics = micguard.WinMics()

            def fn(comtypes):
                ev = mics._ev(comtypes, mics._en(comtypes), self.endpoint_id)
                out = {}
                out["before_scalar"], out["before_mute"] = round(float(ev.GetMasterVolumeLevelScalar()), 2), bool(ev.GetMute())
                ev.SetMasterVolumeLevelScalar(1.0, None)
                ev.SetMute(0, None)
                out["after_scalar"], out["after_mute"] = round(float(ev.GetMasterVolumeLevelScalar()), 2), bool(ev.GetMute())
                return out
            info.update(micguard._com_thread(fn) or {"error": "no result"})
        except Exception as e:  # noqa: BLE001
            info["error"] = f"{type(e).__name__}: {e}"[:150]
        try:
            if self.on_event:
                self.on_event("endpoint_volume", **info)
        except Exception:  # noqa: BLE001
            pass

    def _run(self):
        try:
            mf = _sc()      # first: soundcard's import-time CoInitialize raises if this thread already has COM
            _com_init()     # other threads: join the MTA
            spk = mf._Speaker(mf._Device(self.endpoint_id))
            with spk.player(samplerate=RATE, channels=1, blocksize=RATE // 10) as p:  # 100 ms WASAPI buffer: no underruns
                while not self.stopped:
                    x = np.frombuffer(self._take(CHUNK), dtype=np.int16).astype(np.float32) / 32768
                    p.play(x)
        except Exception as e:  # noqa: BLE001
            if not self.stopped:
                self.error = f"{type(e).__name__}: {e}"[:200]

    def alive(self):
        return not self.stopped and self.error is None and self.thread is not None and self.thread.is_alive()

    def stop(self):
        self.stopped = True
        if self.thread is not None:
            self.thread.join(timeout=2)


CABLE_CAPTURE, CABLE_PLAYBACK = "CABLE Output", "Hi-Fi Cable Input"   # "cable" route, WASAPI endpoint name prefixes


def find_cable_devices() -> tuple:
    """(input_index, output_index, info): WASAPI "CABLE Output (VB-Audio Virtual Cable)" (what the call plays to
    "CABLE Input") and "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)" (what the call records from "Hi-Fi Cable Output")."""
    def find():
        import sounddevice as sd
        with _PA_LOCK:
            if not AUDIO_BUSY.is_set():
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:  # noqa: BLE001
                    pass
            out = {}
            for i, d in enumerate(sd.query_devices()):
                if not sd.query_hostapis(d["hostapi"])["name"].endswith("WASAPI"):
                    continue
                for kind, name in (("input", CABLE_CAPTURE), ("output", CABLE_PLAYBACK)):
                    if d["name"].lower().startswith(name.lower()) and d[f"max_{kind}_channels"] > 0 and kind not in out:
                        out[kind] = (i, d["name"])
        ii, oi = out.get("input", (None,))[0], out.get("output", (None,))[0]
        return ii, oi, {k[:3] if k == "output" else "in": f"WASAPI #{v[0]} {v[1]}" for k, v in out.items()}
    return pa_call(find)
