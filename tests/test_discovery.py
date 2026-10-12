"""Phone discovery: any paired phone (name, class of device or HandsfreeAG service), configured phone_mac as the preferred
device, no_phone_paired vs iphone_not_connected, Hands-Free endpoint matching without "iphone" in the name. Fake winrt."""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_011 import Info, load_phone  # noqa: E402
from connector import readiness  # noqa: E402

def fmt(n):   # synthetic locally-administered address built from a number (no literals for the public-repo gate)
    return ":".join(f"{(0x020000000000 + n) >> s & 0xFF:02X}" for s in range(40, -8, -8))


A, B, C = fmt(0xA), fmt(0xB), fmt(0xC)
HFAG = "0000111f-0000-1000-8000-00805f9b34fb"


def make_bt(devs):
    """devs: id -> dict(addr, connected, major=None, uuids=None, api="long"|None)."""
    class Dev:
        def __init__(self, v):
            self.bluetooth_address, self.connection_status = v["addr"], 1 if v["connected"] else 0
            if v.get("major") is not None:
                self.class_of_device = types.SimpleNamespace(major_class=v["major"])
            if v.get("uuids") is not None:
                services = types.SimpleNamespace(services=[types.SimpleNamespace(service_id=types.SimpleNamespace(uuid=u))
                                                           for u in v["uuids"]])

                async def get(mode):
                    return services
                # pywinrt 3.x: only the metadata-named overload exists; older: only get_rfcomm_services_async
                setattr(self, "get_rfcomm_services_with_cache_mode_async" if v.get("api") == "long"
                        else "get_rfcomm_services_async", get)

        def close(self):
            pass

    class BT:
        @staticmethod
        def get_device_selector_from_pairing_state(p):
            return "sel"

        @staticmethod
        async def from_id_async(id):
            return Dev(devs[id])
    return BT


def scan(monkeypatch, infos, devs):
    class DI:
        @staticmethod
        async def find_all_async_aqs_filter(sel):
            return infos
    ph = load_phone(monkeypatch, DI, make_bt(devs))
    sys.modules["winrt.windows.devices.bluetooth"].BluetoothCacheMode = types.SimpleNamespace(CACHED=1)
    return ph


def mac_of(n):
    return 0x020000000000 + n


# ---------------------------------------------------------------- find_iphone_mac candidates
def test_phone_class_device_without_iphone_in_name(monkeypatch):
    ph = scan(monkeypatch, [Info("h", "Headset"), Info("p", "Galaxy S21")],
              {"h": dict(addr=mac_of(0xA), connected=True, major=4), "p": dict(addr=mac_of(0xB), connected=False, major=2)})
    assert asyncio.run(ph.find_iphone_mac()) == B
    assert ph.scan_info == {"ok": True, "candidates": 1}


def test_handsfree_ag_service_makes_a_candidate(monkeypatch):
    ph = scan(monkeypatch, [Info("h", "Headset"), Info("p", "Pixel")],
              {"h": dict(addr=mac_of(0xA), connected=True, major=4, uuids=["0000111e-0000-1000-8000-00805f9b34fb"]),
               "p": dict(addr=mac_of(0xB), connected=False, uuids=[HFAG.upper()])})
    assert asyncio.run(ph.find_iphone_mac()) == B


def test_handsfree_ag_service_pywinrt3_overload_name(monkeypatch):
    ph = scan(monkeypatch, [Info("p", "Pixel")], {"p": dict(addr=mac_of(0xB), connected=False, uuids=[HFAG], api="long")})
    assert asyncio.run(ph.find_iphone_mac()) == B


def test_connected_candidate_beats_first_and_iphone_name_still_works(monkeypatch):
    ph = scan(monkeypatch, [Info("a", "iPhone A"), Info("b", "Pixel"), Info("c", "Speaker")],
              {"a": dict(addr=mac_of(0xA), connected=False), "b": dict(addr=mac_of(0xB), connected=True, major=2),
               "c": dict(addr=mac_of(0xC), connected=True, major=4)})
    assert asyncio.run(ph.find_iphone_mac()) == B
    assert ph.scan_info["candidates"] == 2


def test_no_candidate_is_a_clean_scan_and_unreadable_device_is_skipped(monkeypatch):
    ph = scan(monkeypatch, [Info("gone", "Old"), Info("h", "Headset")], {"h": dict(addr=mac_of(0xA), connected=True, major=4)})
    assert asyncio.run(ph.find_iphone_mac()) == "" and ph.scan_info == {"ok": True, "candidates": 0}


def test_failed_scan_is_not_a_clean_empty_scan(monkeypatch):
    class DI:
        @staticmethod
        async def find_all_async_aqs_filter(sel):
            raise OSError("boom")
    ph = load_phone(monkeypatch, DI, make_bt({}))
    assert asyncio.run(ph.find_iphone_mac()) == "" and ph.scan_info["ok"] is False


# ---------------------------------------------------------------- Hands-Free endpoint matching
def eps(ph, monkeypatch, rows):
    monkeypatch.setattr(ph, "endpoints", lambda: [dict(flow=f, id=i, name="n", device=d, state=s, iface=x) for f, i, d, s, x in rows])


def test_hf_endpoints_match_by_name_or_address_without_iphone(monkeypatch):
    ph = scan(monkeypatch, [], {})
    eps(ph, monkeypatch, [("capture", "c1", "Galaxy S21 Hands-Free HF Audio", 8, ""),
                          ("render", "r1", "Galaxy S21 Hands-Free HF Audio", 8, ""),
                          ("capture", "c2", "Headset Hands-Free AG Audio", 1, ""),
                          ("render", "r2", "Headset Hands-Free AG Audio", 1, "")])
    assert {k: v["id"] for k, v in ph.hf_endpoints().items()} == {"capture": "c1", "render": "r1"}   # generic HF Audio = the phone side
    assert {k: v["id"] for k, v in ph.hf_endpoints(name="Galaxy S21").items()} == {"capture": "c1", "render": "r1"}
    eps(ph, monkeypatch, [("capture", "c1", "Hands-Free HF Audio", 8, r"BTHENUM\DEV_0200000000AA\x"),
                          ("render", "r1", "Hands-Free HF Audio", 8, r"BTHENUM\DEV_0200000000AA\x"),
                          ("capture", "c2", "Hands-Free HF Audio", 1, r"BTHENUM\DEV_0200000000BB\x"),
                          ("render", "r2", "Hands-Free HF Audio", 1, r"BTHENUM\DEV_0200000000BB\x")])
    assert {k: v["id"] for k, v in ph.hf_endpoints(mac=fmt(0xAA)).items()} == {"capture": "c1", "render": "r1"}



def test_hf_endpoints_default_to_the_phone_seen_last_and_keep_iphone_match(monkeypatch):
    ph = scan(monkeypatch, [], {})
    eps(ph, monkeypatch, [("capture", "c1", "iPhone Hands-Free", 1, ""), ("render", "r1", "iPhone Hands-Free", 8, ""),
                          ("render", "r9", "Other Hands-Free HF Audio", 1, "")])
    assert {k: v["id"] for k, v in ph.hf_endpoints().items()} == {"capture": "c1", "render": "r1"}   # iphone tier beats the generic tier
    ph._known.update(mac=A, name="Other")
    assert ph.hf_endpoints()["render"]["id"] == "r9"



def test_non_handsfree_endpoints_never_match(monkeypatch):
    ph = scan(monkeypatch, [], {})
    eps(ph, monkeypatch, [("render", "r1", "Speakers (Realtek)", 1, ""), ("capture", "c1", "iPhone Microphone", 1, "")])
    assert ph.hf_endpoints() == {}


# ---------------------------------------------------------------- readiness: preferred phone, rescan, no_phone_paired
class FakePhone:
    """phones: mac -> dict(connected, paired, name); scan_result is what a re-scan returns."""
    def __init__(self, phones, scan_result="", candidates=None):
        self.phones, self.scan_result, self.finds = phones, scan_result, 0
        self.scan_info = {"ok": True, "candidates": candidates if candidates is not None else (1 if scan_result else 0)}

    async def find_iphone_mac(self):
        self.finds += 1
        return self.scan_result

    async def iphone(self, mac):
        return {"mac": mac, "name": None, "paired": False, "connected": False, **self.phones.get(mac, {})}

    @staticmethod
    def call_flags():
        return {"call_active": False}

    @staticmethod
    def hf_link():
        return "unplugged"

    @staticmethod
    async def bt_radio():
        return {"on": True}

    @staticmethod
    def hf_endpoints():
        return {"capture": {}, "render": {}}

    @staticmethod
    def hf_abnormal(eps):
        return False


@pytest.fixture
def rd(monkeypatch):
    reports = []
    monkeypatch.setattr(readiness, "heal", types.SimpleNamespace(report=lambda f, **kw: reports.append(f) or details.append(kw)))
    details = []
    monkeypatch.setattr(readiness, "_phone_link_running", lambda: True)
    monkeypatch.setattr(readiness, "battery_saver_on", lambda: False)
    monkeypatch.setattr(readiness, "session_locked", lambda: False)
    monkeypatch.setattr(readiness, "_mac", [""])
    monkeypatch.setattr(readiness, "_rescan_t", [-1e9])
    monkeypatch.setattr(readiness, "_hf_due", [None])

    def run(phone, mac=""):
        monkeypatch.setattr(readiness, "phone", phone)
        return readiness.auto_ready({}, mac, lambda *a, **k: None)
    run.reports, run.details = reports, details
    return run


def test_no_candidate_at_all_is_no_phone_paired_with_texts(rd):
    assert rd(FakePhone({})) == (False, "no_phone_paired") and rd.reports == ["no_phone_paired"]
    ar, en = readiness.reason_texts("no_phone_paired")
    assert ar and "paired" in en and readiness.reason_texts("iphone_not_connected") != (ar, en)


def test_configured_mac_not_paired_and_nothing_found_is_no_phone_paired(rd):
    assert rd(FakePhone({}), A) == (False, "no_phone_paired")


def test_paired_but_disconnected_is_iphone_not_connected(rd):
    ph = FakePhone({A: dict(paired=True, connected=False)}, scan_result=A)
    assert rd(ph) == (False, "iphone_not_connected") and rd.reports == ["iphone_not_connected"]
    assert rd.details == [{}]    # R-c5: no MAC in the heal detail (it ends up in connector.log and uploaded logs)


def test_failed_scan_is_iphone_not_connected_not_no_phone(rd):
    ph = FakePhone({})
    ph.scan_info = {"ok": False, "candidates": 0}
    assert rd(ph) == (False, "iphone_not_connected")


def test_preferred_mac_connected_no_rescan(rd):
    ph = FakePhone({A: dict(paired=True, connected=True), B: dict(paired=True, connected=True)}, scan_result=B)
    assert rd(ph, A)[0] is True and ph.finds == 0 and readiness._mac[0] == A


def test_preferred_mac_disconnected_adopts_connected_other_and_returns_to_preferred(rd):
    ph = FakePhone({A: dict(paired=True, connected=False), B: dict(paired=True, connected=True)}, scan_result=B)
    assert rd(ph, A)[0] is True and ph.finds == 1 and readiness._mac[0] == B
    assert rd(ph, A)[0] is True and ph.finds == 1   # fallback remembered: throttled re-scan does not drop it
    ph.phones[A]["connected"] = True
    assert rd(ph, A)[0] is True and readiness._mac[0] == A   # preferred wins again as soon as it is back


def test_preferred_mac_disconnected_does_not_adopt_a_disconnected_other(rd):
    ph = FakePhone({A: dict(paired=True, connected=False), B: dict(paired=True, connected=False)}, scan_result=B)
    assert rd(ph, A) == (False, "iphone_not_connected") and readiness._mac[0] == A


def test_absent_preferred_mac_rescans_and_uses_found_connected_phone(rd):
    ph = FakePhone({B: dict(paired=True, connected=True)}, scan_result=B)
    assert rd(ph, A)[0] is True and readiness._mac[0] == B and ph.finds == 1


def test_rescan_without_config_adopts_first_found_as_before(rd):
    ph = FakePhone({A: dict(paired=True, connected=False)}, scan_result=A)
    assert rd(ph) == (False, "iphone_not_connected") and readiness._mac[0] == A


def test_heal_module_missing_or_raising_is_harmless(rd, monkeypatch):
    monkeypatch.setattr(readiness, "heal", None)
    assert rd(FakePhone({})) == (False, "no_phone_paired")
    monkeypatch.setattr(readiness, "heal", types.SimpleNamespace(report=lambda *a, **k: 1 / 0))
    assert rd(FakePhone({})) == (False, "no_phone_paired")



# ---------------------------------------------------------------- R-c5 fixes
def test_headset_ag_audio_endpoints_are_never_the_phone(monkeypatch):
    ph = scan(monkeypatch, [], {})
    ph._known.update(mac="", name="")
    eps(ph, monkeypatch, [("capture", "h1", "AirPods Hands-Free AG Audio", 1, ""),
                          ("render", "h2", "AirPods Hands-Free AG Audio", 1, "")])
    hf = ph.hf_endpoints()
    assert hf == {} and ph.hf_link() == "absent" and ph.hf_abnormal(hf) is True


def test_generic_hf_audio_endpoints_without_iphone_name_are_the_phone(monkeypatch):
    ph = scan(monkeypatch, [], {})
    ph._known.update(mac="", name="")
    eps(ph, monkeypatch, [("capture", "c1", "Galaxy S21 Hands-Free HF Audio", 8, ""),
                          ("render", "r1", "Galaxy S21 Hands-Free HF Audio", 8, "")])
    assert {k: v["id"] for k, v in ph.hf_endpoints().items()} == {"capture": "c1", "render": "r1"}


def test_scan_does_not_switch_the_phone_in_use_while_it_is_connected(monkeypatch):
    ph = scan(monkeypatch, [Info("b", "Galaxy"), Info("a", "iPhone")],
              {"a": dict(addr=mac_of(0xA), connected=True), "b": dict(addr=mac_of(0xB), connected=True, major=2)})
    eps(ph, monkeypatch, [("capture", "ia", "iPhone Hands-Free HF Audio", 8, ""), ("render", "ra", "iPhone Hands-Free HF Audio", 8, ""),
                          ("capture", "gb", "Galaxy Hands-Free HF Audio", 8, ""), ("render", "gr", "Galaxy Hands-Free HF Audio", 8, "")])
    ph._known.update(mac=A, name="iPhone")
    assert ph.hf_endpoints()["capture"]["id"] == "ia"
    asyncio.run(ph.find_iphone_mac())               # pairing's has_phone / heal / hf-check scan
    assert ph.hf_endpoints()["capture"]["id"] == "ia" and ph._known["mac"] == A


def test_scan_adopts_another_phone_once_the_one_in_use_is_gone(monkeypatch):
    ph = scan(monkeypatch, [Info("b", "Galaxy")], {"b": dict(addr=mac_of(0xB), connected=True, major=2)})
    ph._known.update(mac=A, name="iPhone")          # A is not connected / not paired any more
    assert asyncio.run(ph.find_iphone_mac()) == B and ph._known["mac"] == B


def test_iphone_named_phone_ranks_above_class_only_when_none_connected(monkeypatch):
    ph = scan(monkeypatch, [Info("g", "Galaxy"), Info("a", "iPhone")],
              {"g": dict(addr=mac_of(0xB), connected=False, major=2), "a": dict(addr=mac_of(0xA), connected=False)})
    assert asyncio.run(ph.find_iphone_mac()) == A
