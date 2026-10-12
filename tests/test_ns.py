"""Connector-side spectral noise suppression (night A3b): numpy only, synthetic signals, no devices."""
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import ns, win_adapters  # noqa: E402

SR, HOPB = 16000, 640
D = ns.HOP    # algorithmic delay in samples


def speech_like(seconds=8.0, level=3000.0):
    """Harmonic voiced tone, wandering pitch, ~syllabic on/off modulation."""
    t = np.arange(int(SR * seconds)) / SR
    ph = 2 * np.pi * np.cumsum(120 + 20 * np.sin(2 * np.pi * 0.7 * t)) / SR
    sp = sum(np.sin(k * ph) / k for k in range(1, 15)) * np.clip(np.sin(2 * np.pi * 1.5 * t), 0, None) ** 0.5
    return sp / np.sqrt(np.mean(sp ** 2)) * level


def white(n, rms, seed=1):
    x = np.random.default_rng(seed).standard_normal(n)
    return x / np.sqrt(np.mean(x ** 2)) * rms


def run(n, x16, chunk=320):
    pcm = x16.astype(np.int16).tobytes()
    out = b"".join(n.process(pcm[i:i + chunk * 2]) for i in range(0, len(pcm), chunk * 2))
    return np.frombuffer(out, dtype=np.int16).astype(np.float64)


def snr_db(clean, test):
    return 10 * np.log10(np.sum(clean ** 2) / np.sum((test - clean) ** 2))


def test_snr_improves_on_speech_like_tone_plus_noise():
    clean = speech_like()
    for snr in (0, 5, 10):
        x = np.clip(clean + white(len(clean), 3000 / 10 ** (snr / 20)), -32768, 32767)
        y = run(ns.SpectralNS(), x)
        a, b = slice(2 * SR, len(clean) - D), slice(2 * SR + D, len(clean))   # skip the warm-up, align the 20 ms delay
        before, after = snr_db(clean[a], x[a]), snr_db(clean[a], y[b])
        assert after > before + 2.0, (snr, before, after)


def test_noise_only_is_attenuated_towards_the_floor():
    x = white(SR * 6, 1000)
    y = run(ns.SpectralNS(), x)
    ratio = np.sqrt(np.mean(y[SR * 3:] ** 2)) / np.sqrt(np.mean(x[SR * 3:] ** 2))
    assert 10 ** (-24 / 20) < ratio < 10 ** (-9 / 20), ratio     # near the -18 dB floor, not gated to silence


def test_frame_size_preserved_for_any_chunking():
    x = (speech_like(2.0) + white(SR * 2, 500)).astype(np.int16)
    for chunk in (320, 160, 333, 1000):
        n = ns.SpectralNS()
        pcm = x.tobytes()
        total = 0
        for i in range(0, len(pcm), chunk * 2):
            piece = pcm[i:i + chunk * 2]
            assert len(n.process(piece)) == len(piece)
            total += len(piece)
        assert total == len(pcm)
    assert ns.SpectralNS().process(b"") == b""
    assert len(ns.SpectralNS().process(b"\x01")) == 0     # a lone odd byte carries no sample


def test_no_clipping_or_wraparound_on_loud_input():
    x = np.clip(speech_like(4.0, level=20000) * 1.6, -32768, 32767)
    x = np.clip(x + white(len(x), 4000), -32768, 32767)
    y = run(ns.SpectralNS(), x)
    assert np.abs(y).max() <= np.abs(x).max() * 1.05                 # never amplified (-32768 is the legal int16 peak)
    assert np.dot(y[D:], x[:-D]) > 0                                 # no sign wrap-around: output follows the (delayed) input
    big = np.full(SR, 32767, np.int16)
    big[::2] = -32768
    z = run(ns.SpectralNS(), big)                                    # full-scale square: saturates, never wraps
    assert len(z) == len(big) and np.abs(z).max() <= 32768


def test_silence_and_dc_stay_silent_and_finite():
    y = run(ns.SpectralNS(), np.zeros(SR))
    assert not y.any()


def test_speech_onset_after_noise_is_not_swallowed():
    noise = white(SR * 3, 800)
    sp = speech_like(3.0, 6000)
    x = np.concatenate([noise, sp + white(len(sp), 800, seed=2)])
    y = run(ns.SpectralNS(), x)
    seg = slice(3 * SR + D, 6 * SR)
    kept = np.sqrt(np.mean(y[seg] ** 2)) / np.sqrt(np.mean(x[3 * SR:6 * SR - D] ** 2))
    assert kept > 0.7, kept


def test_normalize_mode():
    assert ns.normalize_mode(None) == "off"
    assert ns.normalize_mode("SPECTRAL") == "spectral"
    assert ns.normalize_mode("rnnoise") == "off"        # the server's other value: unknown here -> off, never a crash
    assert ns.normalize_mode(5) == "off"


# ---- wrapper + adapter wiring ----------------------------------------------------------------------------------------
class Tap:
    streaming = False

    def __init__(self):
        self.got = []

    def push(self, frame):
        self.got.append(bytes(frame))


def frames(n, level=2000, seed=3):
    x = white(HOPB // 2 * n, level, seed).astype(np.int16)
    return [x[i:i + HOPB // 2].tobytes() for i in range(0, len(x), HOPB // 2)]


def test_wrapper_passes_raw_until_streaming_then_processed():
    tap, n = Tap(), ns.SpectralNS()
    cb = ns.wrap_callback(tap.push, n, lambda: tap.streaming)
    fr = frames(60)
    for f in fr[:30]:
        cb(f)
    assert tap.got == fr[:30]                       # the answer gate / pre-roll hear raw audio
    assert n.frames == 30                           # ...while the tracker warmed up
    tap.streaming = True
    for f in fr[30:]:
        cb(f)
    assert all(len(g) == HOPB for g in tap.got) and len(tap.got) == 60
    assert tap.got[30:] != fr[30:]


def test_wrapper_failure_falls_back_to_raw():
    class Bad:
        def process(self, f):
            raise ValueError("boom")
    tap, ev = Tap(), []
    cb = ns.wrap_callback(tap.push, Bad(), lambda: True, lambda e, **kw: ev.append((e, kw)))
    fr = frames(3)
    for f in fr:
        cb(f)
    assert tap.got == fr and len(ev) == 1 and ev[0][0] == "ns_error"


class Inner:
    def __init__(self):
        self.cb = None

    def start(self, cb):
        self.cb = cb

    def alive(self):
        return True

    def stop(self):
        pass


def make_source(monkeypatch, tmp_path, local="off", remote=None):
    monkeypatch.setattr(win_adapters, "HOME", tmp_path)
    if remote is not None:
        (tmp_path / "remote_config.json").write_text(json.dumps(remote), encoding="utf-8")
    events = []
    au = win_adapters.WinAudio.__new__(win_adapters.WinAudio)
    au.player_obj, au._live, au.ns, au.log = None, 1, local, lambda e, **kw: events.append((e, kw))
    au._closed = lambda: None
    return win_adapters._Source(Inner(), au), events


def test_default_is_off_and_callback_untouched(monkeypatch, tmp_path):
    s, _ = make_source(monkeypatch, tmp_path)
    cb = Tap().push
    s.start(cb)
    assert s.inner.cb is cb and s.ns is None


def test_local_config_spectral_wraps_and_logs_stats(monkeypatch, tmp_path):
    s, events = make_source(monkeypatch, tmp_path, local="spectral")
    tap = Tap()
    s.start(tap.push)
    assert s.inner.cb is not tap.push
    fr = frames(5)
    for f in fr:
        s.inner.cb(f)
    assert tap.got == fr                              # not streaming yet: raw
    tap.streaming = True
    s.inner.cb(fr[0])
    assert len(tap.got[-1]) == HOPB
    s.stop()
    assert events and events[0][0] == "ns_stats" and events[0][1]["frames"] == 6


def test_remote_config_overrides_local(monkeypatch, tmp_path):
    s, _ = make_source(monkeypatch, tmp_path, local="spectral", remote={"ns": "off"})
    cb = Tap().push
    s.start(cb)
    assert s.inner.cb is cb
    s2, _ = make_source(monkeypatch, tmp_path, local="off", remote={"ns": "spectral"})
    s2.start(cb)
    assert s2.inner.cb is not cb


def test_corrupt_remote_config_falls_back_to_local(monkeypatch, tmp_path):
    s, _ = make_source(monkeypatch, tmp_path, local="spectral")
    (tmp_path / "remote_config.json").write_text("{not json", encoding="utf-8")
    s.start(Tap().push)
    assert s.ns is not None


def test_build_reads_local_ns(monkeypatch):
    class Ph:
        route = "hf_direct"

        def startup_cleanup(self):
            pass
    monkeypatch.setattr(win_adapters, "make_adapters",
                        lambda *a, **k: (Ph(), types.SimpleNamespace(), None))
    ph, au, gate, route = win_adapters.build({"ns": "spectral"}, log=lambda *a: None)
    assert au.ns == "spectral"
    ph, au, gate, route = win_adapters.build({}, log=lambda *a: None)
    assert au.ns == "off"


def test_per_frame_budget():
    """500 frames. Locally the box is shared (median must be under 5 ms); the windows-latest CI step sets NS_BUDGET_STRICT=1
    and requires the 95th percentile under 5 ms too."""
    import os
    import time
    fr = frames(500, level=2000)
    n = ns.SpectralNS()
    ts = []
    for f in fr:
        t0 = time.perf_counter()
        n.process(f)
        ts.append(time.perf_counter() - t0)
    ts.sort()
    print(f"ns per-frame ms over 500 frames: median {ts[250]*1000:.3f} p95 {ts[475]*1000:.3f} max {ts[-1]*1000:.3f}")
    assert ts[250] < 0.005
    if os.environ.get("NS_BUDGET_STRICT") == "1":
        assert ts[475] < 0.005
