"""Windows-only COM/GC stress (connector/comstress.py), each variant in its own process so a native crash cannot take pytest down."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="COM is Windows-only")


def _run(*flags):
    p = subprocess.run([sys.executable, "-m", "connector.comstress", *flags], cwd=ROOT, capture_output=True, text=True, timeout=240)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def test_guarded_pattern_survives_500():
    rc, out, err = _run("--n", "500")
    print("guarded:", rc, out, err[-500:])
    assert rc == 0, (rc, out, err[-2000:])
    r = json.loads(out.splitlines()[-1])
    assert r["created"] == 500 and r["mta_keeper_alive"] is True
    assert r["finalised_on_gc_thread"] > 0, r     # the wrappers really were released outside the creating thread


def test_old_pattern_record_only():
    """Records whether the pre-0.1.1 pattern (no MTA keeper) crashes on this runner. Never fails on the outcome."""
    rc, out, err = _run("--no-keeper", "--n", "500")
    verdict = "SURVIVED" if rc == 0 else f"CRASHED rc={rc} (0xC0000005 = {0xC0000005 - 2**32})"
    print(f"OLD PATTERN (no keeper), N=500: {verdict}\n{out}\n{err[-800:]}")
