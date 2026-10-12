"""Elevated repair task, installer side: --install-repair/--uninstall-repair dispatch, the PowerShell command, the Inno hooks,
and static + (when pwsh exists) live FAKE-mode checks of repair.ps1. Real registration/ACLs are exercised in Windows CI."""
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from connector import repair_setup as rs  # noqa: E402

REPAIR = (ROOT / "connector" / "repair.ps1").read_text(encoding="utf-8")
INSTALL = (ROOT / "connector" / "repair_install.ps1").read_text(encoding="utf-8")
ISS = (ROOT / "packaging" / "YoussefConnector.iss").read_text(encoding="utf-8")


class R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_command_install_and_uninstall():
    c = rs.command("install")
    assert c[0].replace("/", "\\").lower().endswith("\\windowspowershell\\v1.0\\powershell.exe")
    assert c[1:6] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    assert c[6] == str(rs.INSTALLER) and rs.INSTALLER.is_file()
    assert c[7:] == ["-Mode", "install", "-Source", str(rs.SCRIPT)] and rs.SCRIPT.is_file()
    assert rs.command("uninstall")[7:] == ["-Mode", "uninstall"]
    with pytest.raises(ValueError):
        rs.command("run")


def test_run_maps_rc_and_writes_output(tmp_path, monkeypatch):
    monkeypatch.setenv("PSModulePath", "C:\\Program Files\\PowerShell\\7\\Modules")
    seen = {}

    def runner(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return R(0, "repair_install: installed\n")
    out = tmp_path / "o.txt"
    assert rs.run("install", out, runner=runner) == 0
    assert "installed" in out.read_text(encoding="utf-8") and seen["capture_output"] and seen["timeout"] == rs.TIMEOUT
    assert "creationflags" in seen
    assert not any(k.upper() == "PSMODULEPATH" for k in seen["env"]) and "PATH" in {k.upper() for k in seen["env"]}
    assert rs.run("uninstall", out, runner=lambda c, **k: R(5, "", "boom")) == 1
    assert "rc=5" in out.read_text(encoding="utf-8") and "boom" in out.read_text(encoding="utf-8")

    def raiser(c, **k):
        raise subprocess.TimeoutExpired(c, 1)
    assert rs.run("install", None, runner=raiser) == 1


def test_main_dispatch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(rs, "run", lambda mode, out=None: calls.append((mode, out)) or 0)
    assert rs.main(["--install-repair", "--selftest-out", str(tmp_path / "x")]) == 0
    assert rs.main(["--uninstall-repair"]) == 0
    assert rs.main(["--selftest-out"]) == 2
    assert calls == [("install", str(tmp_path / "x")), ("uninstall", None)]


def test_entry_dispatches_before_guard_and_app(monkeypatch):
    """The elevated run must not count as an app start (guard) nor load the app (single instance, heavy imports)."""
    from connector import guard
    monkeypatch.setattr(guard, "begin", lambda *a, **k: pytest.fail("guard ran for --install-repair"))
    monkeypatch.setattr(rs, "main", lambda argv: 7 if "--install-repair" in argv else 8)
    monkeypatch.setattr(sys, "argv", ["YoussefConnector.exe", "--install-repair"])
    monkeypatch.delitem(sys.modules, "connector.main", raising=False)
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(ROOT / "packaging" / "entry.py"), run_name="__main__")
    assert e.value.code == 7
    assert "connector.main" not in sys.modules


def test_iss_hooks():
    assert re.search(r"^PrivilegesRequired=lowest$", ISS, re.M)          # the app itself stays per-user
    ver = re.search(r'^#define RepairVer "(\d+)"', ISS, re.M).group(1)
    assert re.search(rf"^# youssef-repair-version: {ver} #$", REPAIR, re.M)   # installed-script freshness marker in sync
    assert "Pos('youssef-repair-version: {#RepairVer} #'" in ISS
    assert "ShellExec('runas', ExpandConstant('{app}\\{#AppExe}'), Flag" in ISS
    assert "(CurStep = ssPostInstall) and (not WizardSilent) and (not RepairCurrent())" in ISS   # auto-update never prompts
    assert "RunRepairSetup('--install-repair')" in ISS and "RunRepairSetup('--uninstall-repair')" in ISS
    assert "CurUninstallStep = usUninstall" in ISS


def test_repair_script_static():
    assert "Get-PnpDevice -Class Bluetooth -PresentOnly" in REPAIR
    assert "-match '^(USB|PCI)\\\\'" in REPAIR
    assert "pnputil.exe /restart-device" in REPAIR and "Disable-PnpDevice" in REPAIR and "Enable-PnpDevice" in REPAIR
    assert "Restart-Svc 'bthserv'" in REPAIR and "Restart-Svc 'BTAGService'" in REPAIR
    assert not re.search(r"VID_[0-9A-F]{4}|PID_[0-9A-F]{4}|VEN_[0-9A-F]{4}|DEV_[0-9A-F]{4}", REPAIR, re.I)   # no device IDs
    # the output folder can only move in FAKE mode; the SYSTEM task always writes next to the script
    assert "if ($fake -and $env:YOUSSEF_REPAIR_OUT) { $outDir = $env:YOUSSEF_REPAIR_OUT }" in REPAIR
    assert "$outDir = $PSScriptRoot" in REPAIR
    assert "[IO.File]::Replace($tmp, $dest, [NullString]::Value)" in REPAIR   # atomic last.json
    assert REPAIR.isascii() and INSTALL.isascii()                            # Windows PowerShell 5.1 reads BOM-less files as ANSI


def test_install_script_static():
    # SIDs only: localized Windows has other names for Users/Administrators
    assert "'S-1-5-18'" in INSTALL and "'S-1-5-32-544'" in INSTALL and "'S-1-5-32-545'" in INSTALL
    assert not re.search(r"BUILTIN\\|NT AUTHORITY|'Administrators'|'Users'", INSTALL)
    assert "$TaskSddl = 'D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;BU)'" in INSTALL
    assert "$TaskName = 'YoussefRepair'" in INSTALL
    assert """$TaskArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $Script + '"'""" in INSTALL
    assert "$def.Principal.RunLevel = 1" in INSTALL and "$def.Principal.LogonType = 5" in INSTALL
    assert "SetAccessRuleProtection($true, $false)" in INSTALL
    code = "\n".join(ln for ln in INSTALL.splitlines() if not ln.lstrip().startswith("#"))
    assert "GetFolderPath('CommonApplicationData')" in code and "$env:ProgramData" not in code
    assert "Triggers" not in INSTALL


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_repair_script_fake_run_writes_last_json(tmp_path):
    env = dict(os.environ, YOUSSEF_REPAIR_FAKE="1", YOUSSEF_REPAIR_OUT=str(tmp_path))
    for _ in range(2):   # second run replaces an existing last.json
        r = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(rs.SCRIPT)], env=env,
                           capture_output=True, text=True, timeout=120)
        d = json.loads((tmp_path / "last.json").read_text(encoding="utf-8"))
        assert r.returncode == d["rc"] and d["rc"] in (0, 1, 2)
        assert d["fake"] is True and d["time"].endswith("Z") and d["epoch"] > 0 and d["version"] == 1
        assert d["steps"][0]["step"] == "find_adapters"
        assert not any(str(s.get("detail", "")).startswith("pnputil") for s in d["steps"])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["last.json"]
