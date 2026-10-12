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
                  in_call=lambda: in_call, clock=clk, wall=clk, on_paired=lambda: reported.append(1), **kw)
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
    assert p.tick() == "backoff" and len(pairs) == 1      # same device within the hour: not prompted again


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


def test_add_phone_pressed_during_an_attempt_is_not_pushed_back():
    p, clk, pairs, scans, rep = make(has=False, cands=[], pair_ok=False)

    async def scan():
        p.request_add_phone()          # pressed while the no-phone scan is running
        return []
    p.scan = scan
    assert p.tick() == "no_candidate"
    assert p._next_at == clk.t + pr.ADD_SCAN_EVERY_S


def test_importing_pairing_does_not_load_phone_or_winrt():
    """winrt must load after onnxruntime (PORT_NOTES.md), so importing pairing at app start must stay winrt-free."""
    import subprocess
    root = Path(__file__).resolve().parents[1]
    code = "import sys; import connector.pairing; bad=[m for m in sys.modules if m=='connector.phone' or m.startswith('winrt')]; print(bad)"
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "[]", out


# ---------------------------------------------------------------- R-c5 fixes: call re-check, scan-error gate, stranger backoff, busy()
def test_call_starting_during_the_scan_stops_the_pairing():
    st = {"call": False}
    p, clk, pairs, scans, rep = make(has=True, cands=[Cand("TEST iPhone")], in_call=False)
    real_scan = p.scan

    async def scan():
        st["call"] = True            # the hub dispatches a call while the inquiry runs
        return await real_scan()
    p.scan, p.in_call = scan, lambda: st["call"]
    p.request_add_phone()
    assert p.tick() == "in_call" and not pairs and not rep


def test_default_has_phone_failed_scan_is_unknown_gate_closed(monkeypatch):
    class Ph:
        scan_info = {"ok": False, "candidates": 0}

        @staticmethod
        async def find_iphone_mac():
            return ""
    monkeypatch.setattr(pr, "phone", Ph)
    with pytest.raises(RuntimeError):
        pr._default_has_phone()
    scans = []

    async def scan():
        scans.append(1)
        return [Cand("TEST iPhone")]
    p = pr.Pairer(scan=scan, clock=Clock(), on_paired=lambda: None)          # default has_phone
    assert p.tick() == "has_phone" and not scans                              # failed scan: no pairing
    Ph.scan_info = {"ok": True, "candidates": 0}
    assert pr._default_has_phone() is False                                   # clean empty scan: no phone
    monkeypatch.setattr(Ph, "find_iphone_mac", staticmethod(lambda: _coro("02:00:00:00:00:01")), raising=False)
    assert pr._default_has_phone() is True


async def _coro(v):
    return v


def test_stranger_backoff_hourly_daily_and_never_after_two_declines(tmp_path):
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("Other iPhone")], pair_ok=False, home=tmp_path)
    assert p.tick() == "failed"
    clk.t += pr.BACKOFF_HOUR_S - 10
    assert p.tick() == "backoff" and len(pairs) == 1                          # not within the hour
    clk.t += pr.AFTER_ATTEMPT_S + 20
    assert p.tick() == "failed" and len(pairs) == 2                           # second decline
    for _ in range(5):                                                        # never again
        clk.t += pr.BACKOFF_HOUR_S + 1
        assert p.tick() == "backoff"
    assert len(pairs) == 2
    # persisted in HOME: a restart keeps it, and the file carries no device name
    raw = (tmp_path / pr.BACKOFF_FILE).read_text(encoding="utf-8")
    assert "Other" not in raw and "iPhone" not in raw
    p2, clk2, pairs2, *_ = make(has=False, cands=[Cand("Other iPhone")], pair_ok=False, home=tmp_path)
    clk2.t = clk.t + 10
    assert p2.tick() == "backoff" and not pairs2


def test_stranger_backoff_three_a_day_cap_and_per_device():
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone")], pair_ok=False)
    p._backoff_record("TEST iPhone", "start")
    p._backoff[p._key("TEST iPhone")]["times"] = [clk.t - 7200, clk.t - 5400, clk.t - 4000]   # 3 prompts in the last day
    assert p._backoff_blocks("TEST iPhone") == "day" and p._backoff_blocks("Another phone") == ""
    p._backoff[p._key("TEST iPhone")]["times"] = [clk.t - 90000, clk.t - 80000, clk.t - 4000]
    assert p._backoff_blocks("TEST iPhone") == ""                             # older ones age out


def test_one_prompt_per_hour_over_a_day_with_a_stranger_ignoring_it():
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone")], pair_ok=False)
    for _ in range(24 * 60 * 30):                                             # 24 h in 2 s ticks
        p.tick()
        clk.t += 2.0
    assert len(pairs) == pr.BACKOFF_MAX_DECLINES


def test_add_phone_bypasses_backoff_and_pairing_clears_it(tmp_path):
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone")], pair_ok=False, home=tmp_path)
    p.tick()
    clk.t += pr.AFTER_ATTEMPT_S + 1
    assert p.tick() == "backoff"
    p.request_add_phone()
    assert p.tick() == "failed" and len(pairs) == 2                           # explicit request: no backoff
    pr_ok, clk2, pairs2, *_ = make(has=False, cands=[Cand("TEST iPhone")], pair_ok=True, home=tmp_path)
    pr_ok._backoff = dict(p._backoff)
    pr_ok.request_add_phone()
    assert pr_ok.tick() == "paired" and pr_ok._backoff == {}


def test_busy_is_true_only_while_an_attempt_runs():
    seen = []
    p, clk, pairs, scans, rep = make(has=False, cands=[Cand("TEST iPhone")])
    real_pair = p.pair

    async def pair(c):
        seen.append((p.busy(), pr.busy()))
        return await real_pair(c)
    p.pair = pair
    assert not p.busy() and not pr.busy()
    assert p.tick() == "paired"
    assert seen == [(True, True)] and not p.busy() and not pr.busy()
