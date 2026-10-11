"""Capture-side level work (night B11): the Hands-Free microphone endpoint volume (opt-in config capture_volume) and the speech-level
summary in the call-end log. Windows-only modules are replaced by fakes; synthetic IDs only."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import core, micguard, win_adapters, win_audio  # noqa: E402

EID = "{0.0.1.00000000}.{abc}"


class EV:
    def __init__(self, v=0.3, m=False):
        self.v, self.m = v, m
        self.sets = []

    def GetMasterVolumeLevelScalar(self):
        return self.v

    def GetMute(self):
        return self.m

    def SetMasterVolumeLevelScalar(self, v, c):
        self.v = v
        self.sets.append(v)

    def SetMute(self, m, c):    # must never be called by the capture path
        raise AssertionError("capture path must not change mute")


def run_capture_volume(monkeypatch, mode, ev):
    opened = []
    monkeypatch.setattr(micguard, "_com_thread", lambda fn: fn(None))
    monkeypatch.setattr(micguard.WinMics, "_en", lambda self, c: "enum")
    monkeypatch.setattr(micguard.WinMics, "_ev", lambda self, c, en, eid: opened.append((en, eid)) or ev)
    got = []
    s = win_audio.IdSource(EID)
    s.on_event = lambda e, **kw: got.append((e, kw))
    s.capture_volume = mode
    s._capture_volume()
    return opened, got


def test_max_mode_logs_then_sets_one_endpoint_by_id(monkeypatch):
    ev = EV(0.3, True)
    opened, got = run_capture_volume(monkeypatch, "max", ev)
    assert opened == [("enum", EID)]
    assert got == [("capture_volume", {"endpoint": EID[-14:], "mode": "max", "before_scalar": 0.3, "mute": True, "after_scalar": 1.0})]
    assert ev.sets == [1.0]


def test_log_mode_only_reads(monkeypatch):
    ev = EV(0.4, False)
    _, got = run_capture_volume(monkeypatch, "log", ev)
    assert ev.sets == [] and got[0][1] == {"endpoint": EID[-14:], "mode": "log", "before_scalar": 0.4, "mute": False}


def test_com_failure_is_logged_not_raised(monkeypatch):
    def boom(fn):
        raise OSError("no device")
    monkeypatch.setattr(micguard, "_com_thread", boom)
    got = []
    s = win_audio.IdSource(EID)
    s.on_event = lambda e, **kw: got.append((e, kw))
    s.capture_volume = "max"
    s._capture_volume()
    assert got[0][0] == "capture_volume" and "OSError" in got[0][1]["error"]


@pytest.mark.parametrize("mode,runs", [("off", 0), ("log", 1), ("max", 1)])
def test_start_touches_the_endpoint_only_when_configured(monkeypatch, mode, runs):
    ran = []
    monkeypatch.setattr(win_audio.IdSource, "_run", lambda self, cb: None)
    monkeypatch.setattr(win_audio.IdSource, "_capture_volume", lambda self: ran.append(1))
    s = win_audio.IdSource(EID)
    s.capture_volume = mode
    s.start(lambda b: None)
    s.thread.join()
    import time
    time.sleep(0.05)
    assert len(ran) == runs


def test_default_is_off_and_config_is_strict(monkeypatch):
    assert win_audio.IdSource(EID).capture_volume == "off"
    ph = types.SimpleNamespace(route="hf_direct", startup_cleanup=lambda: None)
    au = win_adapters.WinAudio(types.SimpleNamespace(_log=lambda *a, **k: None), 1.0, True)
    assert au.capture_volume == "off"
    monkeypatch.setattr(win_adapters, "make_adapters", lambda *a: (ph, au, None))
    for cfg, want in (({}, "off"), ({"capture_volume": "max"}, "max"), ({"capture_volume": "log"}, "log"),
                      ({"capture_volume": True}, "off"), ({"capture_volume": "MAX"}, "off"), ({"capture_volume": "yes"}, "off")):
        win_adapters.build(cfg, lambda *a: None)
        assert au.capture_volume == want, cfg


def test_source_gets_the_mode_and_the_log_callback(monkeypatch):
    logs = []
    ph = types.SimpleNamespace(_log=lambda *a, **k: logs.append(a), devs=(EID, "render"), route="hf_direct")
    au = win_adapters.WinAudio(ph, 1.0, True)
    au.capture_volume = "max"
    monkeypatch.setattr(win_adapters.win_audio, "AUDIO_BUSY", types.SimpleNamespace(set=lambda: None, clear=lambda: None))
    src = au.source()
    assert src.inner.capture_volume == "max" and src.inner.on_event == au.log


def test_level_summary_uses_only_sound_frames():
    silent = [-100.0] * 300
    speech = [-30.0 + i % 10 for i in range(100)]     # -30 .. -21
    s = core.level_summary(silent + speech)
    assert s["active_frames"] == 100 and -27 <= s["p50"] <= -24 and s["p90"] == -21.0 and s["max"] == -21.0
    assert core.level_summary(silent) == {"active_frames": 0}
    assert core.level_summary([]) == {"active_frames": 0}
