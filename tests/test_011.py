"""0.1.1 fixes: iPhone lookup (aqs_filter, connected-first, re-scan throttle), crash.log, endpoint volume without enumeration,
phone_states logging. Windows-only modules are replaced by fakes."""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import main as cm  # noqa: E402
from connector import micguard, readiness, win_adapters, win_audio  # noqa: E402


# ---------------------------------------------------------------- phone.find_iphone_mac with fake winrt
def load_phone(monkeypatch, DeviceInformation, BluetoothDevice):
    mods = {n: types.ModuleType(n) for n in ("winrt", "winrt.windows", "winrt.windows.applicationmodel",
                                             "winrt.windows.applicationmodel.calls", "winrt.windows.devices",
                                             "winrt.windows.devices.bluetooth", "winrt.windows.devices.enumeration")}
    mods["winrt.windows.applicationmodel"].calls = mods["winrt.windows.applicationmodel.calls"]
    bt = mods["winrt.windows.devices.bluetooth"]
    bt.BluetoothConnectionStatus = types.SimpleNamespace(CONNECTED=1, DISCONNECTED=0)
    bt.BluetoothDevice = BluetoothDevice
    mods["winrt.windows.devices.enumeration"].DeviceInformation = DeviceInformation
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    monkeypatch.delitem(sys.modules, "connector.phone", raising=False)
    import importlib
    return importlib.import_module("connector.phone")


class Info:
    def __init__(self, id, name):
        self.id, self.name = id, name


def make_bt(devs):
    class Dev:
        def __init__(self, addr, status):
            self.bluetooth_address, self.connection_status = addr, status

        def close(self):
            pass

    class BT:
        @staticmethod
        def get_device_selector_from_pairing_state(p):
            return "sel"

        @staticmethod
        async def from_id_async(id):
            return Dev(*devs[id])
    return BT


DEVS = {"a": (0xAA0000000001, 0), "b": (0xBB0000000002, 1)}
INFOS = [Info("a", "iPhone A"), Info("x", "Headset"), Info("b", "iPhone B")]


def test_find_uses_aqs_filter_and_prefers_connected(monkeypatch):
    calls = []

    class DI:
        @staticmethod
        async def find_all_async_aqs_filter(sel):
            calls.append("aqs")
            return INFOS

        @staticmethod
        async def find_all_async(sel):
            raise TypeError("Invalid parameter count")
    ph = load_phone(monkeypatch, DI, make_bt(DEVS))
    assert asyncio.run(ph.find_iphone_mac()) == "BB:00:00:00:00:02" and calls == ["aqs"]


def test_find_falls_back_to_find_all_async_and_first_when_none_connected(monkeypatch):
    class DI:
        @staticmethod
        async def find_all_async(sel):
            return INFOS
    ph = load_phone(monkeypatch, DI, make_bt({"a": (0xAA0000000001, 0), "b": (0xBB0000000002, 0)}))
    assert asyncio.run(ph.find_iphone_mac()) == "AA:00:00:00:00:01"


def test_find_logs_error_once(monkeypatch, capsys):
    class DI:
        @staticmethod
        async def find_all_async(sel):
            raise TypeError("Invalid parameter count")
    ph = load_phone(monkeypatch, DI, make_bt(DEVS))
    assert asyncio.run(ph.find_iphone_mac()) == "" and asyncio.run(ph.find_iphone_mac()) == ""
    assert capsys.readouterr().out.count("find_iphone_mac failed") == 1


# ---------------------------------------------------------------- readiness re-scan
def test_rescan_throttled_and_config_mac_is_only_preferred(monkeypatch):
    finds = []

    class P:
        @staticmethod
        async def find_iphone_mac():
            finds.append(1)
            return "BB:00:00:00:00:02"

        @staticmethod
        async def iphone(mac):
            return {"connected": mac == "BB:00:00:00:00:02"}

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
    monkeypatch.setattr(readiness, "phone", P)
    monkeypatch.setattr(readiness, "_phone_link_running", lambda: True)
    monkeypatch.setattr(readiness, "battery_saver_on", lambda: False)
    monkeypatch.setattr(readiness, "session_locked", lambda: False)
    monkeypatch.setattr(readiness, "_mac", ["AA:00:00:00:00:01"])
    monkeypatch.setattr(readiness, "_rescan_t", [-1e9])
    info = {}
    assert readiness.auto_ready(info, "", lambda *a, **k: None)[0] is True   # cached phone off -> re-scan -> B connected
    assert readiness._mac[0] == "BB:00:00:00:00:02" and len(finds) == 1
    readiness._mac[0] = "AA:00:00:00:00:01"
    assert readiness.auto_ready({}, "", lambda *a, **k: None) == (False, "iphone_not_connected")   # throttled
    assert len(finds) == 1
    readiness._rescan_t[0] = -1e9
    # configured phone_mac is only the preferred one: when it is not connected the re-scan still runs (tests/test_discovery.py)
    assert readiness.auto_ready({}, "AA:00:00:00:00:01", lambda *a, **k: None)[0] is True
    assert len(finds) == 2 and readiness._mac[0] == "BB:00:00:00:00:02"


# ---------------------------------------------------------------- crash.log
def test_crash_log_header_and_cap(tmp_path, monkeypatch):
    import faulthandler
    monkeypatch.setattr(cm, "CRASH_KEEP", 1000)
    (tmp_path / "crash.log").write_bytes(b"x" * 5000)
    try:
        cm.enable_crash_log(tmp_path)
        assert faulthandler.is_enabled()
        data = (tmp_path / "crash.log").read_bytes()
        assert len(data) < 1200 and b"=== run " in data and data.startswith(b"x")
    finally:
        faulthandler.disable()
        cm._crash_fh.close()


# ---------------------------------------------------------------- endpoint volume: GetDevice, never GetAllDevices
def test_endpoint_volume_opens_one_endpoint(monkeypatch):
    class EV:
        v, m = 0.3, True

        def GetMasterVolumeLevelScalar(self):
            return EV.v

        def GetMute(self):
            return EV.m

        def SetMasterVolumeLevelScalar(self, v, c):
            EV.v = v

        def SetMute(self, m, c):
            EV.m = bool(m)
    opened = []
    monkeypatch.setattr(micguard, "_com_thread", lambda fn: fn(None))
    monkeypatch.setattr(micguard.WinMics, "_en", lambda self, c: "enum")
    monkeypatch.setattr(micguard.WinMics, "_ev", lambda self, c, en, eid: opened.append((en, eid)) or EV())
    ev = []
    p = win_audio.IdPlayer("{0.0.0.00000000}.{abc}")
    p.on_event = lambda e, **kw: ev.append((e, kw))
    p._max_endpoint_volume()
    assert opened == [("enum", "{0.0.0.00000000}.{abc}")]
    assert ev[0][0] == "endpoint_volume"
    assert ev[0][1] == {"endpoint": "{0.0.0.00000000}.{abc}"[-14:], "before_scalar": 0.3, "before_mute": True,
                        "after_scalar": 1.0, "after_mute": False}


def test_endpoint_volume_killswitch(monkeypatch):
    ran = []
    monkeypatch.setattr(win_audio.IdPlayer, "_run", lambda self: None)
    monkeypatch.setattr(win_audio.IdPlayer, "_max_endpoint_volume", lambda self: ran.append(1))
    p = win_audio.IdPlayer("id")
    p.endpoint_volume = False
    p.start()
    p.thread.join()
    assert ran == []
    ph = types.SimpleNamespace(_log=lambda *a, **k: None)
    au, = (win_adapters.WinAudio(ph, 1.0, True),)
    assert au.endpoint_volume is True
    monkeypatch.setattr(win_adapters, "make_adapters", lambda *a: (types.SimpleNamespace(route="hf_direct", startup_cleanup=lambda: None), au, None))
    win_adapters.build({"endpoint_volume": False}, lambda *a: None)
    assert au.endpoint_volume is False


# ---------------------------------------------------------------- phone_states: change-only, rate limited
def test_phone_states_change_only(monkeypatch):
    logs = []
    flags = {"call_active": False}
    calls = [{"state": "dialing"}]
    fake = types.SimpleNamespace(hf_endpoints=lambda: {"capture": {"state": 4}, "render": {"state": 4}},
                                 phone_calls=lambda: calls, call_flags=lambda: flags)
    monkeypatch.setattr(win_adapters, "phone", fake)
    w = win_adapters.WinPhone("hf_direct", "", lambda ev, s="": logs.append((ev, s)))
    w.t_dial = 100.0
    seq = [(100.1, "dialing"), (100.2, "dialing"), (100.3, "talking"), (100.9, "talking"), (101.0, "dialing")]
    for t, st in seq:
        calls[0] = {"state": st}
        w._before_audio(t)
    got = [l for l in logs if l[0] == "phone_states"]
    assert len(got) == 2   # dialing (first), talking at 100.3 is <0.4 s later -> suppressed; dialing at 101.0 after the change
    # change inside the 0.4 s window is picked up at the next tick instead of lost
    assert '"dialing"' in got[0][1] and '"call_active": false' in got[0][1]


def test_call_end_logs_reason(monkeypatch):
    logs = []
    fake = types.SimpleNamespace(hf_endpoints=lambda: {}, phone_calls=lambda: [], call_flags=lambda: {"call_active": False})
    monkeypatch.setattr(win_adapters, "phone", fake)
    monkeypatch.setattr(win_adapters.micguard, "restore", lambda *a, **k: {})
    w = win_adapters.WinPhone("hf_direct", "", lambda ev, s="": logs.append((ev, s)))
    w.t_dial = 100.0
    w.seen = True
    assert w._before_audio(100.0) == "idle"
    assert w._before_audio(100.0 + win_adapters.GONE_S + 1) == "ended"
    assert any(l[0] == "call_ended" and "phone_service_lists_no_live_call" in l[1] for l in logs)
