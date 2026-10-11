"""Speech detection for the answer gate: Silero VAD (v6.0 ONNX, MIT, vendored as silero_vad.onnx next to this file) through
onnxruntime. No torch, no network at run time.

Same parameters as the private dialer's eleven.wait_for_speech: 16 kHz PCM16, 512-sample (1024-byte, 32 ms) windows with a
64-sample context, voice confidence >= 0.7 for 0.3 s in a row (ringback is tonal, not speech). The model state is reset
every 5 s, as the original analyzer does."""
import asyncio
import os
import sys
import time
from pathlib import Path

import numpy as np

RATE = 16000
WINDOW = 512                 # samples per inference at 16 kHz
CONTEXT = 64                 # samples carried over from the previous window
WINDOW_BYTES = WINDOW * 2    # 1024 bytes = 32 ms of PCM16
THRESHOLD = 0.7              # voice confidence that counts as speech
SECS_NEEDED = 0.3            # seconds of consecutive speech that mean "the callee answered"
RESET_EVERY = 5.0            # s between model state resets
MODEL_NAME = "silero_vad.onnx"

_session = None


def model_path() -> Path:
    """The bundled model: YOUSSEF_VAD_MODEL, else next to this file, else the PyInstaller bundle."""
    env = os.environ.get("YOUSSEF_VAD_MODEL")
    cands = [Path(env)] if env else []
    cands.append(Path(__file__).with_name(MODEL_NAME))
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        cands += [Path(mei) / "connector" / MODEL_NAME, Path(mei) / MODEL_NAME]
    for p in cands:
        if p.is_file():
            return p
    raise FileNotFoundError(f"{MODEL_NAME} not found (looked in {[str(c) for c in cands]})")


def _get_session():
    global _session
    if _session is None:
        import onnxruntime
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        _session = onnxruntime.InferenceSession(str(model_path()), providers=["CPUExecutionProvider"], sess_options=opts)
    return _session


class SileroVAD:
    """One call's VAD state. voice_confidence(window_bytes) -> 0..1; windows must be exactly WINDOW_BYTES."""

    def __init__(self):
        self.session = _get_session()
        self._last_reset = 0.0
        self.reset_states()

    def reset_states(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def voice_confidence(self, window: bytes) -> float:
        x = np.frombuffer(window, np.int16).astype(np.float32) / 32768.0
        if x.shape[0] != WINDOW:
            raise ValueError(f"need {WINDOW} samples, got {x.shape[0]}")
        inp = np.concatenate((self._context, x[None, :]), axis=1)
        out, self._state = self.session.run(None, {"input": inp, "state": self._state,
                                                   "sr": np.array(RATE, dtype=np.int64)})
        self._context = inp[:, -CONTEXT:]
        now = time.time()
        if now - self._last_reset >= RESET_EVERY:
            self.reset_states()
            self._last_reset = now
        return float(np.asarray(out).ravel()[0])


class SpeechDetector:
    """feed(pcm16 16 kHz bytes of any length) -> True once the callee has spoken for SECS_NEEDED."""

    def __init__(self, vad=None, threshold: float = THRESHOLD, secs_needed: float = SECS_NEEDED):
        self.vad = vad or SileroVAD()
        self.threshold, self.secs_needed = threshold, secs_needed
        self.buf = b""
        self.voiced = 0.0

    def feed(self, frame: bytes) -> bool:
        self.buf += frame
        while len(self.buf) >= WINDOW_BYTES:
            c = self.vad.voice_confidence(self.buf[:WINDOW_BYTES])
            self.buf = self.buf[WINDOW_BYTES:]
            self.voiced = self.voiced + WINDOW_BYTES / 2 / RATE if c >= self.threshold else 0.0
            if self.voiced >= self.secs_needed:
                return True
        return False


async def wait_for_speech(frames_iter, timeout_s: float, detector: SpeechDetector = None) -> bool:
    """AnswerGate body: consume 20 ms frames (async iterator) until the callee speaks (True) or `timeout_s` passes (False).
    The deadline is checked per frame, never by cancelling the iterator (that would close an async generator)."""
    det = detector or SpeechDetector()
    deadline = time.monotonic() + timeout_s
    async for frame in frames_iter:
        if det.feed(frame):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0)   # inference is synchronous: let the event loop breathe between windows
    return False
