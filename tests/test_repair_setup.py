"""Elevated repair task, installer side: --install-repair/--uninstall-repair dispatch, the PowerShell command, the Inno hooks,
and static + (when pwsh exists) live FAKE-mode checks of repair.ps1. Real registration/ACLs are exercised in Windows CI."""
import base64
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
UNINSTALL = (ROOT / "connector" / "repair_uninstall.ps1").read_text(encoding="utf-8")
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
    monkeypatch.setenv("Path", "C:\\Users\\tester\\bin")
    assert rs.run("install", out, runner=runner) == 0
    env = seen["env"]
    assert [k for k in env if k.upper() == "PSMODULEPATH"] == ["PSModulePath"] and [k for k in env if k.upper() == "PATH"] == ["PATH"]
    assert env["PATH"] == rs.system_dir()
    assert env["PSModulePath"].replace("/", "\\").lower().endswith("\\windowspowershell\\v1.0\\modules")
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
    assert "RunRepairSetup('--install-repair')" in ISS and "RunRepairSetup('--uninstall-repair')" not in ISS
    assert "CurUninstallStep = usUninstall" in ISS
    # Program Files (Users cannot create it), never ProgramData
    assert "ExpandConstant('{commonpf}\\YoussefConnector\\repair\\repair.ps1')" in ISS
    assert "DirExists(ExpandConstant('{commonpf}\\YoussefConnector'))" in ISS
    assert "commonappdata" not in ISS and re.search(r"^ArchitecturesInstallIn64BitMode=x64compatible$", ISS, re.M)
    # uninstall elevates only System32 powershell.exe with the fixed encoded command
    assert ("ShellExec('runas', ExpandConstant('{sys}\\WindowsPowerShell\\v1.0\\powershell.exe'),\n"
            "                 '-NoProfile -NonInteractive -EncodedCommand ' + RepairUninstallCmd()") in ISS


def uninstall_code(text=UNINSTALL):
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))


def iss_encoded():
    block = ISS[ISS.index("function RepairUninstallCmd(): String;"):ISS.index("{ END-REPAIR-UNINSTALL }")]
    return "".join(re.findall(r"^    '([A-Za-z0-9+/=]+)'", block, re.M))


def test_iss_encoded_uninstall_matches_script():
    assert base64.b64decode(iss_encoded()).decode("utf-16-le") == uninstall_code()
    assert len(uninstall_code().splitlines()) <= 40


def test_uninstall_script_static():
    code = uninstall_code()
    assert "OpenBaseKey('LocalMachine', 'Registry64')" in code and "GetValue('ProgramFilesDir')" in code
    assert "$env:" not in code.replace("$env:PSModulePath =", "").replace("$env:PATH =", "")   # reads no environment
    assert code.index("$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')") < code.index("New-Object")
    # known names only, non-recursive, and only inside folders that pass the ours check
    assert "foreach ($f in 'repair.ps1', 'last.json', 'last.json.tmp') { [IO.File]::Delete((Join-Path $dir $f)) }" in code
    assert "[IO.Directory]::Delete($dir, $false)" in code and "[IO.Directory]::Delete($base, $false)" in code
    assert code.index("if (-not (Ours $base))") < code.index("if (-not (Ours $dir))") < code.index("[IO.File]::Delete(")
    for bad in ("Remove-Item", "rmdir", "-Recurse", "Delete($dir, $true)", "Delete($base, $true)", "cmd.exe", "icacls"):
        assert bad not in UNINSTALL and bad not in INSTALL, bad
    assert "S-1-5-32-544', 'S-1-5-18'" in code and "AreAccessRulesProtected" in code and "ReparsePoint" in code


def test_repair_script_static():
    assert "Get-PnpDevice -Class Bluetooth -PresentOnly" in REPAIR
    assert "-match '^(USB|PCI)\\\\'" in REPAIR
    assert "/restart-device" in REPAIR and "Disable-PnpDevice" in REPAIR and "Enable-PnpDevice" in REPAIR
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
    assert "SetAccessRuleProtection($true, $false)" in INSTALL and "$acl.SetOwner($SidAdmins)" in INSTALL
    code = "\n".join(ln for ln in INSTALL.splitlines() if not ln.lstrip().startswith("#"))
    assert "OpenBaseKey('LocalMachine', 'Registry64')" in code and "GetValue('ProgramFilesDir')" in code
    assert "ProgramData" not in code and "CommonApplicationData" not in code
    assert "Triggers.Create" not in INSTALL and "$t.Definition.Triggers.Count -ne 0" in code
    # env reset before the first cmdlet (New-Object) can autoload a module
    assert code.index("$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')") < code.index("New-Object")
    assert "$env:PATH = [Environment]::SystemDirectory" in code
    # never adopt: a folder that is not ours is renamed aside; new folders/files get their DACL at creation
    assert "if ((Test-There $Base) -and -not (Test-Ours $Base)) { Move-Aside $Base }" in code
    # predicate functions must emit nothing on the pipeline besides their bool (CI found a truthy ('log', $false) array)
    for fn in ("Test-Ours", "Test-Reparse", "Test-There"):
        body = code[code.index("function " + fn):]
        body = body[:body.index("\n}\n")]
        assert all("| Out-Host" in ln for ln in body.splitlines() if re.search(r"\bSay\b|Write-Output", ln)), fn
    assert "[IO.Directory]::CreateDirectory($p, (New-Acl $true))" in code
    assert "[IO.FileMode]::CreateNew" in code and "(New-Acl $false))" in code
    # task: deleted first, then TASK_CREATE with the SDDL in the same call; action verified exactly
    assert code.index("Remove-RepairTask\n") < code.index("RegisterTaskDefinition(")
    assert "RegisterTaskDefinition($TaskName, $def, 2, 'SYSTEM', $null, 5, $TaskSddl)" in code
    assert "SetSecurityDescriptor" not in code and ", 6, 'SYSTEM'" not in code
    assert "$a.Path -ne $PowerShellExe -or $a.Arguments -ne $TaskArgs" in code
    # uninstall + rollback share repair_uninstall.ps1
    assert "$Uninstaller = Join-Path $PSScriptRoot 'repair_uninstall.ps1'" in code and code.count("& $Uninstaller") == 2


def test_repair_script_uses_absolute_pnputil():
    assert "$pnputil = [IO.Path]::Combine([Environment]::SystemDirectory, 'pnputil.exe')" in REPAIR
    assert "& $pnputil /restart-device" in REPAIR and "& pnputil" not in REPAIR


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_ps1_files_parse(tmp_path):
    enc = tmp_path / "encoded.ps1"
    enc.write_text(base64.b64decode(iss_encoded()).decode("utf-16-le"), encoding="utf-8")
    files = [ROOT / "connector" / n for n in ("repair.ps1", "repair_install.ps1", "repair_uninstall.ps1")] + [enc]
    ps = ("$bad = 0; foreach ($f in $env:PS_FILES.Split('|')) { $e = $null; $null = [System.Management.Automation.Language.Parser]::"
          "ParseFile($f, [ref]$null, [ref]$e); foreach ($x in $e) { $bad++; Write-Output \"$f $($x.Extent.StartLineNumber): "
          "$($x.Message)\" } }; exit $bad")
    env = dict(os.environ, PS_FILES="|".join(map(str, files)))
    r = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


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
