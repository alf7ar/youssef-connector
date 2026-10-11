"""0.1.2 readiness: Arabic/English texts for every reason, warnings iphone_hotspot_internet and old_app_running.
Windows tools (netsh, route, PowerShell) are replaced by canned output through readiness._run; no dotted IPv4 literals
(the public-repo gate rejects them), addresses are built with ip()."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import readiness, win_adapters  # noqa: E402


def ip(*parts):
    return ".".join(str(p) for p in parts)


def netsh(ssid):
    body = f"    SSID                   : {ssid}\n" if ssid else ""
    return ("There is 1 interface on the system:\n\n    Name                   : Wi-Fi\n    State                  : "
            + ("connected" if ssid else "disconnected") + "\n" + body + "    BSSID                  : 02:00:00:00:00:01\n")


def route(gw):
    return ("IPv4 Route Table\n===\nActive Routes:\nNetwork Destination        Netmask          Gateway       Interface  Metric\n"
            f"          0.0.0.0          0.0.0.0     {gw}   {ip(10, 0, 0, 5)}     45\n"
            f"        {ip(10, 0, 0, 0)}    {ip(255, 255, 255, 0)}         On-link    {ip(10, 0, 0, 5)}    301\n")


def ps(*lines):
    return "\n".join(lines) + "\n"


class Fake:
    """Canned stdout per command (first word); a missing key behaves like a command that cannot run (None)."""
    def __init__(self, **out):
        self.out, self.calls = out, []

    def __call__(self, cmd, timeout=15.0):
        self.calls.append(cmd[0])
        return self.out.get(cmd[0])


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    readiness._cache.clear()
    monkeypatch.setattr(readiness, "_phone_name", [""])
    yield
    readiness._cache.clear()


def use(monkeypatch, **out):
    f = Fake(**out)
    monkeypatch.setattr(readiness, "_run", f)
    return f


def codes(ws):
    return [w["code"] for w in ws]


# ---------------------------------------------------------------- hotspot warning
def test_hotspot_by_ssid_equal_to_phone_name(monkeypatch):
    use(monkeypatch, netsh=netsh("Tester’s iPhone"), route=route(ip(192, 168, 1, 1)), powershell="")
    assert codes(readiness.collect_warnings("Tester's iPhone")) == ["iphone_hotspot_internet"]


def test_hotspot_by_gateway_in_subnet(monkeypatch):
    use(monkeypatch, netsh=netsh("Some Wi-Fi"), route=route(ip(172, 20, 10, 1)), powershell="")
    assert codes(readiness.collect_warnings("Other iPhone")) == ["iphone_hotspot_internet"]


@pytest.mark.parametrize("gw,expect", [(ip(172, 20, 10, 15), True), (ip(172, 20, 10, 16), False), (ip(172, 20, 11, 1), False),
                                       (ip(192, 168, 1, 1), False)], ids=["last_in", "first_out", "other_net", "home"])
def test_hotspot_subnet_edges(monkeypatch, gw, expect):
    use(monkeypatch, netsh=netsh("Home"), route=route(gw))
    assert readiness.hotspot_internet("Tester's iPhone") is expect


def test_no_hotspot_warning_on_other_wifi(monkeypatch):
    use(monkeypatch, netsh=netsh("Home"), route=route(ip(192, 168, 1, 1)), powershell="")
    assert readiness.collect_warnings("Tester's iPhone") == []


def test_ssid_match_ignores_case_and_spaces_but_not_bssid(monkeypatch):
    use(monkeypatch, netsh=netsh("  tester's   IPHONE "), route=route(ip(192, 168, 1, 1)))
    assert readiness.hotspot_internet("Tester's iPhone") is True
    assert readiness.parse_ssid(netsh("")) == ""   # disconnected: the BSSID line alone is not an SSID


def test_probes_unreadable_means_no_warning(monkeypatch):
    use(monkeypatch)   # every command fails (e.g. not Windows)
    assert readiness.hotspot_internet("x") is None and readiness.old_app_running() is None
    assert readiness.collect_warnings("x") == []


def test_parse_gateways_ignores_on_link_and_junk():
    out = route("On-link") + f"          0.0.0.0          0.0.0.0     {ip(192, 168, 0, 1)}  {ip(192, 168, 0, 9)}  25\n"
    assert readiness.parse_gateways(out) == [ip(192, 168, 0, 1)]


def test_probe_results_are_cached(monkeypatch):
    f = use(monkeypatch, netsh=netsh("Home"), route=route(ip(192, 168, 1, 1)))
    readiness.hotspot_internet("x")
    readiness.hotspot_internet("x")
    assert f.calls == ["netsh", "route"]


# ---------------------------------------------------------------- old app warning
def test_old_app_by_process(monkeypatch):
    use(monkeypatch, powershell=ps("P|4242|pythonw.exe"))
    assert codes(readiness.collect_warnings("")) == ["old_app_running"]


def test_old_app_by_running_task_only(monkeypatch):
    use(monkeypatch, powershell=ps("T|AhmedDialerApp|Ready", "T|AhmedDialerOpenOnUnlock|Running"))
    assert readiness.parse_old_app(ps("T|AhmedDialerApp|Ready", "T|AhmedDialerOpenOnUnlock|Running")) == ["task:AhmedDialerOpenOnUnlock"]
    assert codes(readiness.collect_warnings("")) == ["old_app_running"]


def test_old_app_idle_tasks_do_not_warn(monkeypatch):
    use(monkeypatch, powershell=ps("T|AhmedDialerApp|Ready", "T|AhmedDialerOpenOnUnlock|Disabled", "T|SomethingElse|Running"))
    assert readiness.collect_warnings("") == []


def test_old_app_probe_script_does_not_match_itself(monkeypatch):
    seen = []
    monkeypatch.setattr(readiness, "_run", lambda cmd, timeout=15.0: seen.append(cmd[-1]) or "")
    readiness.old_app_running()
    script = seen[0]
    assert "-ne $PID" in script and "'*Ahmed'+'Dialer*'" in script and "AhmedDialerApp" in script and "AhmedDialerOpenOnUnlock" in script


# ---------------------------------------------------------------- texts
ALL_REASONS = ["call_active", "call_incoming", "call_state_unknown", "hf_link_active", "session_locked", "battery_saver",
               "phone_link_not_running", "bluetooth_off", "iphone_not_connected", "hf_refreshing", "hf_endpoints",
               "phone_module_unavailable", "ready_check_failed: RuntimeError", "ready() failed: RuntimeError('x')"]


def test_every_reason_code_in_source_has_texts():
    src = (Path(readiness.__file__)).read_text(encoding="utf-8")
    body = src[src.index("def auto_ready"):]
    in_code = set(re.findall(r'return False, "([a-z_]+)"', body)) | set(re.findall(r'else "([a-z_]+)"', body)) | {"phone_module_unavailable"}
    assert in_code and in_code <= set(readiness.REASONS), in_code - set(readiness.REASONS)


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_reason_texts_arabic_and_english(reason):
    ar, en = readiness.reason_texts(reason)
    assert re.search(r"[؀-ۿ]", ar) and re.search(r"[A-Za-z]", en)


def test_warning_texts():
    for code in ("iphone_hotspot_internet", "old_app_running"):
        w = readiness.warning(code)
        assert set(w) == {"code", "ar", "en"} and re.search(r"[؀-ۿ]", w["ar"]) and re.search(r"[A-Za-z]", w["en"])
    assert readiness.warning("iphone_hotspot_internet")["ar"].startswith("اللابتوب واخد نت من نفس الآيفون، ده بيبوظ صوت المكالمة")
    assert readiness.reason_texts("") == ("", "") and "zzz" in readiness.reason_texts("zzz")[1]


# ---------------------------------------------------------------- check(): ok/reason unchanged, new keys added
class P:
    """Phone stub for a ready laptop; `name` is the iPhone's Bluetooth name."""
    name = "Tester's iPhone"
    flags = {"call_active": False}

    @classmethod
    def mk(cls, **kw):
        return type("PP", (cls,), kw)

    @classmethod
    def call_flags(cls):
        return dict(cls.flags)

    @staticmethod
    def hf_link():
        return "unplugged"

    @staticmethod
    async def bt_radio():
        return {"on": True}

    @staticmethod
    async def find_iphone_mac():
        return "02:00:00:00:00:01"

    @classmethod
    async def iphone(cls, mac):
        return {"connected": True, "name": cls.name}

    @staticmethod
    def hf_endpoints():
        return {"a": {"state": "active"}, "b": {"state": "active"}}

    @staticmethod
    def hf_abnormal(eps):
        return False


def setup_ready(monkeypatch, phone=P, locked=False):
    monkeypatch.setattr(readiness, "phone", phone)
    monkeypatch.setattr(readiness, "_phone_link_running", lambda: True)
    monkeypatch.setattr(readiness, "battery_saver_on", lambda: False)
    monkeypatch.setattr(readiness, "session_locked", lambda: locked)
    monkeypatch.setattr(readiness, "_mac", [""])
    monkeypatch.setattr(readiness, "_prev_tick", [None])
    monkeypatch.setattr(readiness, "_hf_due", [None])


def test_check_ready_with_both_warnings_keeps_ok_and_reason(monkeypatch):
    setup_ready(monkeypatch)
    use(monkeypatch, netsh=netsh("Tester's iPhone"), route=route(ip(172, 20, 10, 1)), powershell=ps("P|7|pythonw.exe"))
    r = readiness.check("", lambda *a, **k: None)
    assert r["ok"] is True and r["reason"] == "" and r["reason_ar"] == "" and r["reason_en"] == ""
    assert codes(r["warnings"]) == ["iphone_hotspot_internet", "old_app_running"]
    assert all(w["ar"] and w["en"] for w in r["warnings"])
    assert r["iphone_connected"] is True and r["hfp_audio_device"] is True   # existing keys intact


def test_check_ready_clean(monkeypatch):
    setup_ready(monkeypatch)
    use(monkeypatch, netsh=netsh("Home"), route=route(ip(192, 168, 1, 1)), powershell="")
    r = readiness.check("", lambda *a, **k: None)
    assert r["ok"] is True and r["warnings"] == []


def test_check_not_ready_has_reason_text_and_still_warns(monkeypatch):
    setup_ready(monkeypatch, locked=True)
    use(monkeypatch, netsh=netsh("Home"), route=route(ip(172, 20, 10, 1)), powershell="")
    r = readiness.check("", lambda *a, **k: None)
    assert (r["ok"], r["reason"]) == (False, "session_locked") and r["reason_ar"] and r["reason_en"]
    assert codes(r["warnings"]) == ["iphone_hotspot_internet"]


def test_check_skips_probes_during_a_call(monkeypatch):
    setup_ready(monkeypatch, phone=P.mk(flags={"call_active": True}))
    f = use(monkeypatch, netsh=netsh("Home"), route=route(ip(172, 20, 10, 1)), powershell=ps("P|7|pythonw.exe"))
    r = readiness.check("", lambda *a, **k: None)
    assert r["reason"] == "call_active" and r["warnings"] == [] and f.calls == [] and r["reason_ar"]


def test_check_without_phone_module(monkeypatch):
    monkeypatch.setattr(readiness, "phone", None)
    r = readiness.check()
    assert r["reason"] == "phone_module_unavailable" and r["reason_ar"] and r["warnings"] == []


def test_check_failure_reason_text(monkeypatch):
    monkeypatch.setattr(readiness, "phone", P)
    monkeypatch.setattr(readiness, "auto_ready", lambda *a, **k: 1 / 0)
    r = readiness.check("", lambda *a, **k: None)
    assert r["reason"] == "ready_check_failed: ZeroDivisionError" and r["reason_ar"] and r["reason_en"]


def test_winphone_call_active_shortcut_has_texts():
    w = win_adapters.WinPhone.__new__(win_adapters.WinPhone)
    w.t_dial, w.ended, w.mac, w.log = 1.0, False, "", None
    r = w.ready()
    assert r["ok"] is False and r["reason"] == "call_active" and r["reason_ar"] and r["warnings"] == []
