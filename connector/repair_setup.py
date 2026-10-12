"""Elevated install/uninstall of the "YoussefRepair" SYSTEM scheduled task (Bluetooth adapter + service restart).

Runs only as `YoussefConnector.exe --install-repair` / `--uninstall-repair [--selftest-out FILE]`, which the installer starts
once through UAC (ShellExecute "runas"). packaging/entry.py dispatches here before the crash guard and the app load, so the
elevated process (possibly another account: a standard user types admin credentials) touches no user data. The work is
done by repair_install.ps1 (bundled next to this file, like repair.ps1 itself). Stdlib only. Exit code 0 ok, 1 failed.
Declined UAC = this never runs: the app works, connector/repair.py reports "not_installed"."""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "YoussefRepair"
HERE = Path(__file__).resolve().parent
INSTALLER = HERE / "repair_install.ps1"
SCRIPT = HERE / "repair.ps1"
TIMEOUT = 120


def system_dir() -> str:
    """%SystemRoot%\\System32 from the OS, not from the (user-controlled) environment."""
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260):
            return buf.value
    except Exception:  # noqa: BLE001  (not Windows: tests)
        pass
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")


def command(mode: str) -> list:
    if mode not in ("install", "uninstall"):
        raise ValueError(mode)
    ps = os.path.join(system_dir(), "WindowsPowerShell", "v1.0", "powershell.exe")
    cmd = [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER), "-Mode", mode]
    if mode == "install":
        cmd += ["-Source", str(SCRIPT)]
    return cmd


def run(mode: str, out=None, runner=subprocess.run) -> int:
    """Run repair_install.ps1; its output goes to `out` (windowed exe: no console). -> 0 ok, 1 failed."""
    # PowerShell 7 module paths inherited from a pwsh parent make Windows PowerShell fail to load its own core modules
    # (Get-Acl, Get-FileHash ...): let powershell.exe rebuild the default PSModulePath
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    try:
        r = runner(command(mode), capture_output=True, text=True, timeout=TIMEOUT, env=env,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rc = 0 if r.returncode == 0 else 1
        text = f"{mode}-repair rc={r.returncode}\n{r.stdout or ''}{r.stderr or ''}"
    except Exception as e:  # noqa: BLE001
        rc, text = 1, f"{mode}-repair error: {type(e).__name__}: {e}\n"
    if out:
        try:
            Path(out).write_text(text, encoding="utf-8")
        except OSError:
            pass
    return rc


def main(argv) -> int:
    out = argv[argv.index("--selftest-out") + 1] if "--selftest-out" in argv and argv.index("--selftest-out") + 1 < len(argv) else None
    if "--uninstall-repair" in argv:
        return run("uninstall", out)
    if "--install-repair" in argv:
        return run("install", out)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
