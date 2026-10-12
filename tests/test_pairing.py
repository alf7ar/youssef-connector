"""Gated auto-pair: gate logic, exactly-one-candidate rule, 60 s timeout, accept path, status-page endpoint (no Bluetooth, no winrt)."""
import asyncio
import json
import re
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import main as cm  # noqa: E402
from connector import pairing as pr  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Cand:
    def __init__(self, name):
        self.name = name


class Heal:
    def __init__(self):
        self.calls = []

    def report(self, failure, **kw):
        self.calls.append((failure, kw))


def make(has=False, cands=(), pair_ok=True, in_call=False, logs=None, **kw):
    clk = Clock()
    pairs, scans, reported = [], [], []

    async def scan():
        scans.append(1)
        return list(cands)

    async def pair(c):
        pairs.append(c.name)
        return {"ok": pair_ok, "status": "PAIRED" if pair_ok else "Failed"}
    p = pr.Pairer(lambda *a: (logs if logs is not None else []).append(a), has_phone=lambda: has, scan=scan, pair=pair,
                  in_call=lambda: in_call, clock=clk, on_paired=lambda: reported.append(1), **kw)
    return p, clk, pairs, scans, reported


# ---------------------------------------------------------------- gate
def test_gate_closed_when_phone_paired_and_no_request():
    p, clk, pairs, scans, rep = make(has=True, cands=[Cand("TEST iPhone")])
    assert p.tick() == "has_phone"
    assert not scans and not pairs and not rep
    clk.t += pr.NO_PHONE_EVERY_S - 1
    assert p.tick() is None                      # throttled
    clk.t += 2
    assert p.tick() == "has_phone"


def test_gate_a_no_phone_pairs_single_candidate():
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone")])
    assert p.tick() == "paired"
    assert pairs == ["TEST iPhone"] and rep == [1]


def test_gate_b_add_phone_window_opens_gate_even_with_phone_paired():
    p, clk, pairs, scans, rep = make(has=True, cands=[Cand("TEST iPhone")])
    assert p.tick() == "has_phone"
    assert p.request_add_phone() == pr.ADD_WINDOW_S
    assert p.tick() == "paired"                  # request resets the throttle
    assert pairs == ["TEST iPhone"]
    assert not p.add_window_open()               # success closes the window


def test_add_phone_window_expires_after_120s():
    p, clk, pairs, scans, rep = make(has=True, cands=[])
    p.request_add_phone()
    assert p.tick() == "no_candidate"            # window open, nothing in range
    clk.t += pr.ADD_SCAN_EVERY_S + 1
    assert p.tick() == "no_candidate"            # keeps scanning inside the window
    clk.t += 125
    assert not p.add_window_open()
    assert p.tick() == "has_phone"               # window over, phone paired: gate closed again
    assert len(scans) == 2


def test_no_pairing_during_a_call():
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone")], in_call=True)
    p.request_add_phone()
    assert p.tick() is None and not scans and not pairs


def test_unknown_paired_state_never_pairs():
    p, clk, pairs, scans, rep = make(cands=[Cand("TEST iPhone")])
    p.has_phone = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert p.tick() == "has_phone" and not pairs


# ---------------------------------------------------------------- exactly one candidate
def test_zero_candidates_does_not_pair_and_rescans_later():
    p, clk, pairs, scans, rep = make(has=False, cands=[])
    assert p.tick() == "no_candidate" and not pairs and not rep
    clk.t += pr.NO_PHONE_EVERY_S - 1
    assert p.tick() is None
    clk.t += 2
    assert p.tick() == "no_candidate" and len(scans) == 2


def test_two_candidates_is_ambiguous_nothing_paired():
    logs = []
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone"), Cand("Other iPhone")], logs=logs)
    assert p.tick() == "ambiguous" and not pairs and not rep
    assert any(a[0] == "pairing_attempt" and "ambiguous" in a[1] for a in logs)


def test_failed_pair_backs_off_and_does_not_report():
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone")], pair_ok=False)
    assert p.tick() == "failed" and not rep
    clk.t += pr.AFTER_ATTEMPT_S - 1
    assert p.tick() is None
    clk.t += 2
    assert p.tick() == "failed" and len(pairs) == 2


def test_scan_exception_is_logged_not_raised():
    p, clk, pairs, scans, rep = make(has=False)

    async def bad():
        raise OSError("radio off")
    p.scan = bad
    assert p.tick() == "scan_failed" and not pairs


def test_pick_candidates_by_name_or_phone_class():
    got = pr.pick_candidates([("TEST iPhone", False), ("Speaker", False), ("Car", True), (None, False), ("IPHONE x", True)])
    assert got == [("TEST iPhone", False), ("Car", True), ("IPHONE x", True)]


# ---------------------------------------------------------------- pair_device (fake winrt objects)
class FakeCustom:
    def __init__(self, mode="prompt", status=0):
        self.mode, self.status = mode, status
        self.handler = None
        self.removed = []
        self.kinds = None
        self.accepted = 0

    def add_pairing_requested(self, h):
        self.handler = h
        return "tok"

    def remove_pairing_requested(self, tok):
        self.removed.append(tok)

    async def pair_async(self, kinds):
        self.kinds = kinds
        if self.mode == "hang":
            await asyncio.sleep(3600)
        if self.mode == "raise":
            raise OSError("pairing refused")
        if self.mode == "prompt":
            args = types.SimpleNamespace(pairing_kind=types.SimpleNamespace(name="CONFIRM_PIN_MATCH"), accept=self._accept)
            self.handler(self, args)
        return types.SimpleNamespace(status=self.status)

    def _accept(self):
        self.accepted += 1


def di_with(custom):
    return types.SimpleNamespace(pairing=types.SimpleNamespace(custom=custom))


def test_pair_device_accepts_prompt_and_succeeds():
    c = FakeCustom("prompt", status=0)
    r = asyncio.run(pr.pair_device(di_with(c), 5, kinds=11))
    assert r["ok"] is True and c.accepted == 1 and r["prompts"] == 1 and c.kinds == 11
    assert c.removed == ["tok"]                  # handler always unhooked


def test_pair_device_already_paired_is_ok_other_status_is_not():
    assert asyncio.run(pr.pair_device(di_with(FakeCustom("quiet", status=3)), 5))["ok"] is True
    r = asyncio.run(pr.pair_device(di_with(FakeCustom("quiet", status=19)), 5))
    assert r["ok"] is False and r["prompts"] == 0


def test_pair_device_times_out():
    c = FakeCustom("hang")
    r = asyncio.run(pr.pair_device(di_with(c), 0.05))
    assert r == {"ok": False, "status": "timeout", "prompts": 0} and c.removed == ["tok"]


def test_pair_device_exception_returns_error_and_unhooks():
    c = FakeCustom("raise")
    r = asyncio.run(pr.pair_device(di_with(c), 5))
    assert r["ok"] is False and r["status"] == "error" and "pairing refused" in r["error"] and c.removed == ["tok"]


def test_default_timeout_is_60s():
    assert pr.PAIR_TIMEOUT_S == 60.0 and pr.ADD_WINDOW_S == 120.0


# ---------------------------------------------------------------- heal wiring + status + logging
def test_paired_reports_iphone_not_connected_to_heal(monkeypatch):
    h = Heal()
    monkeypatch.setattr(pr, "heal", h)
    pr._report_paired()
    assert h.calls and h.calls[0][0] == "iphone_not_connected"


def test_report_without_heal_module_is_a_noop(monkeypatch):
    monkeypatch.setattr(pr, "heal", None)
    pr._report_paired()


def test_status_shows_arabic_prompt_only_while_pairing():
    seen = []
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone")])
    orig = p.pair

    async def pair(c):
        seen.append(p.status())
        return await orig(c)
    p.pair = pair
    assert p.status()["active"] is False and "message_ar" not in p.status()
    p.tick()
    assert seen[0]["active"] is True and seen[0]["message_ar"] == "اضغط Pair على الآيفون"
    assert p.status()["active"] is False and p.status()["last"]["result"] == "paired"


def test_logs_have_name_and_result_but_no_mac():
    logs = []
    p, *_ = make(has=False, cands=[Cand("TEST iPhone")], logs=logs)
    p.tick()
    text = " ".join(" ".join(map(str, a)) for a in logs)
    assert "pairing_start" in text and "pairing_attempt" in text and "TEST iPhone" in text and "paired" in text
    assert not re.search(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", text)


# ---------------------------------------------------------------- status page endpoint
def post(base, path, ctype="application/json", host=None):
    req = urllib.request.Request(base + path, data=b"{}", headers={"Content-Type": ctype, **({"Host": host} if host else {})})
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"http": e.code}


def test_add_phone_endpoint(tmp_path):
    app = cm.App(tmp_path, "9.9.9", lambda *a: None)
    port = app.serve(0)
    base = f"http://127.0.0.1:{port}"
    try:
        st = json.loads(urllib.request.urlopen(base + "/status").read())
        assert st["pairing"]["active"] is False
        assert post(base, "/add-phone")["ok"] is False             # not on the Windows adapters: refused
        assert not app.pairer.add_window_open()
        app.pairing_on = True
        assert post(base, "/add-phone")["ok"] is True and app.pairer.add_window_open()
        assert post(base, "/add-phone", ctype="text/plain") == {"http": 403}
        assert post(base, "/add-phone", host="evil.example") == {"http": 403}
        assert b"/add-phone" in urllib.request.urlopen(base + "/").read()
    finally:
        app.server.shutdown()


def test_add_phone_refused_during_a_call(tmp_path):
    app = cm.App(tmp_path, "9.9.9", lambda *a: None)
    app.pairing_on = True
    app.in_call = lambda: True
    ok, _ = app.add_phone()
    assert ok is False and not app.pairer.add_window_open()
