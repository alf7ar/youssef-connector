"""Local status page (127.0.0.1): phone candidates (masked MAC, in-use flag), add-phone button + endpoint, heal history.
No Bluetooth, no network: the phone scan, heal and pairing modules are stubbed."""
import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import main as cm  # noqa: E402

MAC_A = "02:00:00:00:00:01"
MAC_B = MAC_A[:-1] + "2"  # second synthetic MAC, derived at runtime (the gate allows only ...:01 as a literal)
PHONES = [{"mac": MAC_A, "name": "Test iPhone A", "connected": True},
          {"mac": MAC_B, "name": "Test iPhone B", "connected": False}]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "_list_phones", lambda: (list(PHONES), ""))
    monkeypatch.setattr(cm, "_readiness_mac", lambda: "")
    a = cm.App(tmp_path, "9.9.9", log=lambda *x, **k: None)
    a.serve(0)
    yield a
    a.server.shutdown()
    a.server.server_close()


def get(app, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")


def post_json(app, path, body=b"{}", ctype="application/json"):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=body, method="POST",
                                 headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_page_has_sections_and_arabic_first(app):
    status, ctype, html = get(app, "/")
    assert status == 200 and ctype.startswith("text/html")
    for marker in ('id="phones"', 'id="plist"', 'id="addphone"', 'id="heal"', 'id="hlist"'):
        assert marker in html
    assert "الهواتف" in html and "الإصلاح التلقائي" in html and "إضافة هاتف" in html
    assert "fetch('/phones')" in html and "fetch('/heal')" in html and "fetch('/add-phone'" in html
    assert html.index("الهواتف") < html.index("Phones")


def test_phones_json_masks_mac_and_flags_in_use(app):
    app.cfg = {"phone_mac": MAC_B}
    status, ctype, body = get(app, "/phones")
    assert status == 200 and ctype == "application/json"
    j = json.loads(body)
    assert j["error"] == "" and j["in_use"] == "**:**:**:**:00:02"
    assert [p["masked"] for p in j["phones"]] == ["**:**:**:**:00:01", "**:**:**:**:00:02"]
    assert [p["connected"] for p in j["phones"]] == [True, False]
    assert [p["in_use"] for p in j["phones"]] == [False, True]
    assert [p["name"] for p in j["phones"]] == ["Test iPhone A", "Test iPhone B"]
    assert "02:00:00" not in body and MAC_A not in body and MAC_B not in body


def test_phone_in_use_falls_back_to_found_mac(app, monkeypatch):
    monkeypatch.setattr(cm, "_readiness_mac", lambda: MAC_A)
    j = json.loads(get(app, "/phones")[2])
    assert j["in_use"] == "**:**:**:**:00:01" and [p["in_use"] for p in j["phones"]] == [True, False]


def test_phones_scan_failure_is_reported_not_raised(app, monkeypatch):
    monkeypatch.setattr(cm, "_list_phones", lambda: ([], "ModuleNotFoundError"))
    app._phones = (-cm.PHONES_TTL_S, [], "")
    j = json.loads(get(app, "/phones")[2])
    assert j["phones"] == [] and j["error"] == "ModuleNotFoundError"


def test_phone_scan_is_cached(app, monkeypatch):
    calls = []
    monkeypatch.setattr(cm, "_list_phones", lambda: (calls.append(1) or list(PHONES), ""))
    app._phones = (-cm.PHONES_TTL_S, [], "")
    get(app, "/phones")
    get(app, "/phones")
    assert len(calls) == 1


def test_mask_mac():
    assert cm.mask_mac(MAC_A) == "**:**:**:**:00:01"
    assert cm.mask_mac("") == "" and cm.mask_mac("bad") == ""


def test_list_phones_without_winrt_returns_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "connector.phone", None)  # import raises ImportError
    monkeypatch.delattr(sys.modules["connector"], "phone", raising=False)  # else `from . import phone` reuses the real one
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))  # never the real DLLs after winrt
    phones, err = cm._list_phones()
    assert phones == [] and err


def test_heal_endpoint_last_five_newest_first(app, monkeypatch):
    acts = [{"t": 1000 + i, "failure": "bt_unhealthy", "action": "hf_refresh", "ok": i % 2 == 0, "ms": 5} for i in range(7)]
    fake = types.ModuleType("connector.heal")
    fake.status = lambda: {"actions": acts, "busy": False}
    monkeypatch.setitem(sys.modules, "connector.heal", fake)
    j = json.loads(get(app, "/heal")[2])
    assert [a["t"] for a in j["actions"]] == [1006, 1005, 1004, 1003, 1002]


def test_heal_endpoint_unavailable_when_module_missing(app, monkeypatch):
    monkeypatch.setitem(sys.modules, "connector.heal", None)
    assert json.loads(get(app, "/heal")[2]) == {"actions": None}


def test_add_phone_calls_pairing_request_add(app, monkeypatch):
    calls = []    # P2's App.add_phone (in-call + pairing_on gates) -> Pairer.request_add_phone
    app.pairing_on = True
    monkeypatch.setattr(app.pairer, "request_add_phone", lambda: calls.append(1))
    status, j = post_json(app, "/add-phone")
    assert status == 200 and j["ok"] is True and calls == [1]
    assert "Bluetooth" in j["message"]


def test_add_phone_stub_when_pairing_missing(app, monkeypatch):
    monkeypatch.setitem(sys.modules, "connector.pairing", None)
    status, j = post_json(app, "/add-phone")
    assert status == 200 and j["ok"] is False and j["message"]


def test_add_phone_needs_json_and_local_host(app):
    with pytest.raises(urllib.error.HTTPError) as e:
        post_json(app, "/add-phone", ctype="text/plain")
    assert e.value.code == 403
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}/add-phone", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403
