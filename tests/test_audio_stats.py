"""audio_stats telemetry (A4): per-call capture format/discontinuities/rate, outgoing Loudness gain and peak, frames sent.
Windows modules are replaced by stubs; no network. Run: .venv/bin/python -m pytest -q tests/test_audio_stats.py"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from connector import win_adapters, win_audio  # noqa: E402
import test_core as tc  # noqa: E402

EID = "{0.0.1.00000000}.{abc}"


def block(amp=3000, hz=300, n=1600):   # one 100 ms block of 16 kHz PCM16
    t = np.arange(n) / win_audio.RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def test_loudness_stats_speech_gain_and_limited_peak():
    loud = win_audio.Loudness()
    for _ in range(20):
        loud.process(block())
    st = loud.stats()
    assert len(loud.gains) == 20
    assert st["out_gain_p50_db"] <= st["out_gain_max_db"]
    assert st["out_peak_dbfs"] <= -2.0          # the lookahead limiter holds the output below about -2.2 dBFS
    assert st["out_peak_dbfs"] > -20.0          # but the speech really got through


def test_loudness_stats_silence_has_no_gain_and_no_peak():
    loud = win_audio.Loudness()
    loud.process(np.zeros(1600, dtype=np.int16))
    assert loud.gains == [] and loud.stats() == {"out_peak_dbfs": None}


def test_player_stats_reads_loudness_and_none_without_it():
    loud = win_audio.Loudness()
    pl = win_adapters._Player(SimpleNamespace(feed=lambda b: None), loud, owner=None)
    pl.feed(block().tobytes())
    st = pl.stats()
    assert "out_gain_p50_db" in st and st["out_peak_dbfs"] is not None
    assert win_adapters._Player(SimpleNamespace(), None, owner=None).stats() == {}


def test_idsource_discontinuities_are_per_call_delta(monkeypatch):
    monkeypatch.setattr(win_audio._sc, "discontinuities", 2, raising=False)
    src = win_audio.IdSource(EID)
    monkeypatch.setattr(win_audio._sc, "discontinuities", 7, raising=False)
    src.in_ch, src.nsamp, src.t_start, src.t_last = 2, 16000, 10.0, 11.0
    assert src.stats() == {"in_channels": 2, "in_rate_est": 16000, "discontinuities": 5}


def test_source_stats_carries_registry_capture_format(monkeypatch):
    monkeypatch.setattr(win_audio._sc, "discontinuities", 0, raising=False)
    owner = SimpleNamespace(phone=SimpleNamespace(cap_format="16000Hz/16bit/1ch"))
    src = win_adapters._Source(win_audio.IdSource(EID), owner)
    st = src.stats()
    assert st["cap_format"] == "16000Hz/16bit/1ch" and st["discontinuities"] == 0 and st["in_rate_est"] is None
    cable = win_adapters._Source(SimpleNamespace(), owner)     # SdSource (cable route) reports no capture telemetry
    assert cable.stats() == {"cap_format": "16000Hz/16bit/1ch"}


def test_call_end_emits_one_audio_stats_line_with_frames_sent():
    async def go():
        async def script(call):
            await call.send_audio(tc.tone(1.0), pace_s=0.001)
            await asyncio.sleep(0.3)
            await call.end("agent_end")
        hub = tc.MockHub(on_call=script)
        async with tc.rig("answer", hub=hub) as r:
            lines = []
            r.conn.log = lambda *a: lines.append(a)
            await tc.place(r)
            await hub.wait_msg("ended", "c1")
            stats = [json.loads(a[1]) for a in lines if a[0] == "audio_stats"]
            assert len(stats) == 1
            assert stats[0]["frames_sent"] == len(hub.calls["c1"].audio) > 0
    tc.run(go())
