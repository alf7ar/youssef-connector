"""conn-r2 (v0.1.4) integration hooks: static imports + heal action registration in main, server-pushed heal policy
(contract keys only, heavy tiers off by default), bluetooth_adapter_missing ignored by heal, event-log events and heal
status compose in ready()["heal"] whatever the wrap order, remote_config.json lives under micguard.HOME."""
import ast
import inspect
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import heal, micguard, readiness  # noqa: E402
from connector import main as cm  # noqa: E402

EVS = [{"log": "System", "provider": "BTHUSB", "event_id": 3, "record_id": 7}]


@pytest.fixture
def fresh_heal():
    heal.reset_for_tests()
    yield
    heal.reset_for_tests()


# ---- hook 1: static imports + registration before the tick thread ------------------------------------------------------
def test_main_imports_the_sprint_modules_statically():
    tree = ast.parse(Path(cm.__file__).read_text(encoding="utf-8"))
    top = set()
    for node in tree.body:      # module level only (not inside functions)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            top |= {a.name for a in node.names}
    assert {"heal", "bt_actions", "repair", "pairing", "phonelink", "eventwatch", "netcheck"} <= top


def test_start_heal_registers_bt_repair_and_phonelink_before_the_thread(fresh_heal, monkeypatch):
    seen = []
    real_thread = threading.Thread

    def spy_thread(*a, **k):
        seen.append(set(heal.status()["registered"]))      # what is registered when the tick thread is created
        return real_thread(*a, **k)
    monkeypatch.setattr(cm.threading, "Thread", spy_thread)
    app = type("A", (), {"stop_event": threading.Event(), "in_call": lambda self: False})()
    app.stop_event.set()
    cm.start_heal(app, lambda *a: None).join(5)
    want = {"radio_on", "hf_refresh", "phone_connect", "radio_cycle", "adapter_reset", "phone_link_launch"}
    assert seen and want <= seen[0]
    assert heal._actions["adapter_reset"][1] == "heavy" and heal._actions["radio_cycle"][1] == "heavy"


def test_register_heal_actions_survives_a_failing_module(fresh_heal, monkeypatch):
    logs = []
    monkeypatch.setattr(cm.repair, "register", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    names = cm.register_heal_actions(lambda *a: logs.append(a))
    assert "radio_on" in names and logs and logs[0][:2] == ("heal_register_failed", "repair")


def test_registration_is_not_via_selftest_mods_list():
    src = inspect.getsource(cm.start_heal)
    assert "register_heal_actions(" in src
    assert "bt_actions.register(heal)" in inspect.getsource(cm.register_heal_actions)
    assert "repair.register" in inspect.getsource(cm.register_heal_actions)


# ---- hook 2 + 6: server-pushed policy (contract keys only), heavy tiers off by default -----------------------------------
def test_heavy_tiers_off_by_default(fresh_heal):
    p = heal.status()["policy"]
    assert p["radio_cycle"] is False and p["adapter_reset"] is False
    assert p["max_adapter_resets_per_hour"] == 2 and p["max_radio_toggles_per_hour"] == 4


def test_remote_config_heal_policy_contract_keys_only(fresh_heal):
    cm.apply_remote_config({"heal": {"radio_cycle": "yes", "adapter_reset": 1, "max_radio_toggles_per_hour": 3,
                                     "evil": True, "phone_connect": False}, "ns": "spectral"})
    p = heal.status()["policy"]
    assert p["radio_cycle"] is False and p["adapter_reset"] is False      # only a literal True enables a heavy tier
    assert p["max_radio_toggles_per_hour"] == 3 and "evil" not in p and "phone_connect" not in p
    cm.apply_remote_config({"heal": {"radio_cycle": True}})
    assert heal.status()["policy"]["radio_cycle"] is True and heal.status()["policy"]["adapter_reset"] is False


def test_remote_config_without_heal_keeps_policy_and_junk_is_ignored(fresh_heal):
    cm.apply_remote_config({"heal": {"radio_cycle": True}})
    for junk in ({"ns": "off"}, None, "x", ["heal"], {}):
        cm.apply_remote_config(junk)
    assert heal.status()["policy"]["radio_cycle"] is True
    cm.apply_remote_config({"heal": None})      # an explicit empty policy = defaults (heavy off)
    assert heal.status()["policy"]["radio_cycle"] is False


def test_app_run_wires_on_config_and_micguard_home():
    src = inspect.getsource(cm.App.run)
    assert "self.conn.on_config(apply_remote_config)" in src
    assert "home=micguard.HOME" in src


def test_core_persists_remote_config_where_win_adapters_reads_it(tmp_path, monkeypatch, fresh_heal):
    """The Connector home is micguard.HOME: welcome config -> HOME/remote_config.json -> on_config(apply_remote_config)."""
    from connector import core
    from connector.fakes import FakeAudio, FakeGate, FakePhone
    conn = core.Connector("ws://127.0.0.1:1", "tok", FakePhone(), FakeAudio(), FakeGate(), home=tmp_path)
    conn.on_config(cm.apply_remote_config)
    conn._apply_config({"heal": {"adapter_reset": True}, "ns": "spectral"})
    import json
    assert json.loads((tmp_path / "remote_config.json").read_text())["ns"] == "spectral"
    assert heal.status()["policy"]["adapter_reset"] is True
    from connector import win_adapters
    assert win_adapters.HOME == micguard.HOME


# ---- hook 3: H2's bluetooth_adapter_missing is explicitly ignored by heal -------------------------------------------------
def test_bluetooth_adapter_missing_is_ignored_explicitly(fresh_heal):
    calls = []
    for n in ("radio_on", "radio_cycle", "adapter_reset", "hf_refresh"):
        heal.register_action(n, lambda n=n: calls.append(n) or {"ok": True})
    heal.set_policy({"radio_cycle": True, "adapter_reset": True})
    assert "bluetooth_adapter_missing" in heal.IGNORED and "bluetooth_adapter_missing" not in heal.FAILURES
    assert "bluetooth_adapter_missing" in readiness.REASONS
    heal.report("bluetooth_adapter_missing")
    assert heal.tick() is None and calls == [] and heal.status()["active"] == {}


# ---- hook 4: events + heal status compose in ready()["heal"], any wrap order ----------------------------------------------
def test_events_and_heal_status_compose_whatever_the_wrap_order(fresh_heal):
    base = lambda: {"ok": True, "reason": "", "warnings": []}  # noqa: E731
    events_outside = cm.with_events(cm.heal_ready(base), lambda: EVS)()
    events_inside = cm.heal_ready(cm.with_events(base, lambda: EVS))()
    for r in (events_outside, events_inside):
        assert r["heal"]["events"] == EVS
        assert set(heal.status()) <= set(r["heal"]) and r["heal"]["busy"] is False and r["ok"] is True


def test_main_import_loads_no_winrt_module():
    """The static imports must not pull in phone (winrt) / readiness / win_adapters / vad: onnxruntime has to load before
    winrt (PORT_NOTES), else the Windows selftest dies with 0xC0000005 (phonelink imported readiness at module level)."""
    import subprocess
    code = ("import sys; import connector.main; "
            "bad = [m for m in sys.modules if m in ('connector.phone', 'connector.readiness', 'connector.win_adapters', "
            "'connector.vad', 'onnxruntime') or m.startswith('winrt')]; print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]), capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


# ---- hook 5 (W1 finding) + R-c8b LOW-1 on the E1f repair task ------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]


def test_repair_no_adapter_is_rc2_not_a_failed_lookup():
    """Get-PnpDevice -Class Bluetooth with no device: rc 1 + no output (NotFound) = "no adapter" (rc 2, step ok);
    any other lookup error is a failed step (rc 1)."""
    code = (ROOT / "connector" / "repair.ps1").read_text(encoding="utf-8")
    catch = code[code.index("Get-PnpDevice -Class Bluetooth"):code.index("foreach ($id in $ids)")]
    assert "CmdletizationQuery_NotFound*" in catch
    nf, other = catch.split("} else {")
    assert "Add-Step 'find_adapters' $true" in nf and "$script:rc = 2" in nf
    assert "Add-Step 'find_adapters' $false" in other and "$script:rc = 1" in other


def test_repair_py_rc2_reports_no_adapter(tmp_path, monkeypatch):
    import json
    import subprocess
    from connector import repair
    p = tmp_path / "last.json"

    def fake(*args):
        if args[0] == "/run":
            p.write_text(json.dumps({"time": "T1", "steps": [], "rc": 2}))
        return subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(repair, "_schtasks", fake)
    r = repair.adapter_reset(path=p, timeout=5, poll_every=0.01, sleep=lambda s: None)
    assert r["ok"] is False and r["error"] == "no_adapter" and r["rc"] == 2


def test_repair_install_verifies_folder_before_the_task_is_registered():
    code = (ROOT / "connector" / "repair_install.ps1").read_text(encoding="utf-8")
    reg = code.index("RegisterTaskDefinition(")
    assert code.index("Assert-SecureAcl $p }") < code.index("    Remove-RepairTask\n") < reg
    assert code.index("installed script differs from the source") < reg
    assert code.index("unexpected files in the repair folder") < reg
