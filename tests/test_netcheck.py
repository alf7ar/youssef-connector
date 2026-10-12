"""netcheck: captive portal, proxy and hub reachability probes (all HTTP and Windows commands mocked; no network, no dotted IPv4
literals because the public-repo gate rejects them), the idle-only probe round, and the readiness warnings / ready["net"]."""
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import netcheck, readiness  # noqa: E402

HUB = "https://hub.test"


@pytest.fixture(autouse=True)
def clean_state():
    netcheck.reset_for_tests()
    yield
    netcheck.reset_for_tests()


def boom(*a, **k):
    raise AssertionError("must not be called")


# ---- captive portal
def test_captive_open_internet_and_portal_and_no_answer():
    assert netcheck.probe_captive(lambda url, t: (200, "Microsoft Connect Test")) is False
    assert netcheck.probe_captive(lambda url, t: (200, "<html>Sign in to the Wi-Fi</html>")) is True
    assert netcheck.probe_captive(lambda url, t: (200, "")) is True
    assert netcheck.probe_captive(lambda url, t: (_ for _ in ()).throw(OSError("no route"))) is None


def test_captive_url_and_timeout_passed():
    seen = []
    netcheck.probe_captive(lambda url, t: seen.append((url, t)) or (200, "Microsoft Connect Test"))
    assert seen == [("http://www.msftconnecttest.com/connecttest.txt", netcheck.PROBE_TIMEOUT)]


# ---- proxy
def test_parse_winhttp_english_and_unknown():
    direct = "\nCurrent WinHTTP proxy settings:\n\n    Direct access (no proxy server).\n"
    proxied = "\nCurrent WinHTTP proxy settings:\n\n    Proxy Server(s) :  proxy.test:3128\n    Bypass List     :  <-loopback>\n"
    assert netcheck.parse_winhttp(direct) is False
    assert netcheck.parse_winhttp(proxied) is True
    assert netcheck.parse_winhttp("garbage") is None
    assert netcheck.parse_winhttp(None) is None


@pytest.mark.parametrize("winhttp, env, expected", [
    ("    Direct access (no proxy server).", {}, False),
    ("    Proxy Server(s) :  proxy.test:3128", {}, True),
    ("    Direct access (no proxy server).", {"http": "http://proxy.test:3128"}, True),
    (None, {}, None),
    (None, {"no": "localhost"}, None),
    ("    Direct access (no proxy server).", {"no": "localhost"}, False),
])
def test_probe_proxy_combinations(winhttp, env, expected):
    got = netcheck.probe_proxy(run=lambda cmd, timeout=10.0: winhttp, env_proxies=lambda: env)
    assert got is expected


def test_probe_proxy_runs_netsh_winhttp():
    cmds = []
    netcheck.probe_proxy(run=lambda cmd, timeout=10.0: cmds.append(cmd) or "Direct access", env_proxies=dict)
    assert cmds == [["netsh", "winhttp", "show", "proxy"]]


# ---- hub reachability
def test_probe_server_any_http_answer_is_reachable():
    assert netcheck.probe_server(HUB, head=lambda url, t: True) is True
    assert netcheck.probe_server(HUB, head=lambda url, t: False) is False
    assert netcheck.probe_server("", head=boom) is None


def test_http_head_status_codes_count_as_reachable(monkeypatch):
    def refused(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", None, None)

    def down(req, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(netcheck.urllib.request, "urlopen", refused)
    assert netcheck._http_head(HUB, 1.0) is True
    monkeypatch.setattr(netcheck.urllib.request, "urlopen", down)
    assert netcheck._http_head(HUB, 1.0) is False


# ---- rounds, warnings, idle-only
def test_round_updates_state_and_warnings():
    st = netcheck.run_round(HUB, captive=lambda: True, proxy=lambda: True, server=lambda base: False)
    assert st["captive_portal"] is True and st["proxy"] is True and st["server"] is False and st["checked_at"] > 0
    assert netcheck.warning_codes() == ["captive_portal", "proxy_blocking"]


def test_proxy_with_reachable_hub_is_not_blocking():
    netcheck.run_round(HUB, captive=lambda: False, proxy=lambda: True, server=lambda base: True)
    assert netcheck.warning_codes() == []


def test_unknown_values_give_no_warning():
    netcheck.run_round(HUB, captive=lambda: None, proxy=lambda: None, server=lambda base: None)
    assert netcheck.warning_codes() == []
    assert netcheck.snapshot()["captive_portal"] is None


def test_round_stops_when_a_call_starts():
    calls = []
    state = iter([False, True])   # busy is checked before each probe: the first probe runs, the second sees a call

    def captive():
        calls.append("captive")
        return True

    netcheck.run_round(HUB, busy=lambda: next(state), captive=captive, proxy=boom, server=boom)
    assert calls == ["captive"]
    assert netcheck.snapshot()["proxy"] is None   # the interrupted round keeps the old values


def test_round_does_not_probe_during_call():
    before = netcheck.snapshot()
    netcheck.run_round(HUB, busy=lambda: True, captive=boom, proxy=boom, server=boom)
    assert netcheck.snapshot() == before


def test_probe_exception_is_unknown():
    netcheck.run_round(HUB, captive=lambda: 1 / 0, proxy=lambda: False, server=lambda base: True)
    st = netcheck.snapshot()
    assert st["captive_portal"] is None and st["proxy"] is False


def test_start_is_idempotent_and_skips_without_base():
    t = netcheck.start(lambda: "", busy=lambda: False, every=3600.0, first=0.0)
    assert t is not None and t.daemon
    assert netcheck.start(lambda: "", every=3600.0, first=0.0) is None


# ---- readiness integration
def test_readiness_warning_texts_and_codes():
    for code in ("captive_portal", "proxy_blocking"):
        w = readiness.warning(code)
        assert w["code"] == code and w["ar"] and w["en"]


def test_readiness_collect_warnings_includes_net(monkeypatch):
    monkeypatch.setattr(readiness, "_run", lambda cmd, timeout=15.0: None)   # Windows probes say nothing
    readiness._cache.clear()
    readiness._inflight.clear()
    netcheck.run_round(HUB, captive=lambda: True, proxy=lambda: False, server=lambda base: True)
    assert [w["code"] for w in readiness.collect_warnings("")] == ["captive_portal"]


def test_ready_dict_carries_net_state():
    out = readiness._with_net({"ok": True})
    assert out["net"] == netcheck.snapshot()
    netcheck.run_round(HUB, captive=lambda: False, proxy=lambda: False, server=lambda base: True)
    assert readiness._with_net({"ok": True})["net"]["server"] is True
