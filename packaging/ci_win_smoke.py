"""Real-Windows syntax smoke for the self-heal command lines (CI only, windows-latest).

Runs the exact command lines the heal modules use and checks that they PARSE and return a plausible rc. It does not
need Bluetooth. Fails only on syntax-class errors (rc 87, "The parameter is incorrect", PowerShell ParserError,
command not found). Prints a JSON table of results."""
import asyncio
import json
import subprocess
import sys

BTHUSB_Q = "*[System[Provider[@Name='BTHUSB'] and (EventID=3)]]"
APPERR_Q = "*[System[Provider[@Name='Application Error']]]"
PS_PNP = "Get-PnpDevice -Class Bluetooth -PresentOnly -ErrorAction SilentlyContinue | Select InstanceId"
PS_APPS = "Get-StartApps | findstr YourPhone"

# (name, argv, allowed rcs, informational?)
CASES = [
    ("wevtutil_bthusb", ["wevtutil", "qe", "System", "/q:" + BTHUSB_Q, "/f:xml", "/c:5", "/rd:true"], {0}, False),
    ("wevtutil_apperror", ["wevtutil", "qe", "Application", "/q:" + APPERR_Q, "/f:xml", "/c:1", "/rd:true"], {0}, False),
    ("schtasks_query", ["schtasks", "/query", "/tn", "YoussefRepair"], {0, 1}, False),
    ("pnputil_bluetooth", ["pnputil", "/enum-devices", "/class", "Bluetooth"], {0, 1, 259}, False),
    ("netsh_winhttp_proxy", ["netsh", "winhttp", "show", "proxy"], {0}, False),
    ("ps_get_pnpdevice", ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_PNP], {0, 1}, False),  # rc 1 with no output seen on the runner (no BT class)
    # informational only: the Phone Link app is normally absent on the runner. The explorer.exe launch is NOT run.
    ("ps_yourphone_apps", ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_APPS], {0, 1}, True),
]
BAD_TEXT = ("the parameter is incorrect", "parsererror", "is not recognized", "unexpected token", "missing closing")


def run_case(name, argv, ok_rcs, info):
    r = {"name": name, "rc": None, "ok": False, "info": info, "note": ""}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60, errors="replace")
    except Exception as e:
        r["note"] = f"{type(e).__name__}: {e}"
        return r
    out = (p.stdout or "") + (p.stderr or "")
    low = out.lower()
    r["rc"] = p.returncode
    r["bytes"] = len(out)
    r["head"] = out.strip()[:120].replace("\n", " ")
    bad = [t for t in BAD_TEXT if t in low]
    if p.returncode == 87 or bad:
        r["note"] = "SYNTAX: rc=%s %s" % (p.returncode, bad)
    elif p.returncode in ok_rcs:
        r["ok"] = True
    else:
        r["note"] = "unexpected rc"
    return r


def winrt_case():
    r = {"name": "winrt_get_radios", "rc": None, "ok": False, "info": False, "note": ""}
    try:
        from winrt.windows.devices.radios import Radio

        async def _radios():
            return await Radio.get_radios_async()

        radios = asyncio.run(_radios())
        r["count"] = len(list(radios))
        r["ok"] = True  # empty on the runner is expected; any list means the API call worked
    except ImportError as e:
        r["note"] = f"ImportError: {e}"
    except Exception as e:
        # the call itself ran but the runner has no radio stack: not a syntax problem
        r["note"] = f"{type(e).__name__}: {e}"
        r["ok"] = True
    return r


def main():
    results = [run_case(*c) for c in CASES] + [winrt_case()]
    print(json.dumps(results, indent=1))
    for r in results:
        print("%-22s rc=%-5s %s %s" % (r["name"], r["rc"], "OK  " if r["ok"] else "FAIL", r["note"]))
    # fail only on syntax-class errors (not informational cases, not unexpected-but-parsed rcs, not missing winrt)
    fatal = [r["name"] for r in results if not r["info"] and r["note"].startswith(("SYNTAX", "FileNotFoundError"))]
    if fatal:
        print("SYNTAX FAILURES:", fatal)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
