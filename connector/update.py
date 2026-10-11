"""Auto-update from GitHub Releases (unauthenticated). Ported from the private launcher's try_update flow.

Manifest: <releases>/latest/download/latest.json = {"version", "url", "sha256"} (override: env YC_UPDATE_URL, used by CI).
Flow: newer? -> download YoussefConnector-Setup-<ver>.exe -> verify SHA-256 against the `<exe>.sha256` file (and the manifest
value when present) -> launch the Inno installer silently (it closes the running app: CloseApplications=force +
/FORCECLOSEAPPLICATIONS + a taskkill in PrepareToInstall, so replacing a running app never fails with "file in use") and
exit. A sha mismatch rejects the download, logs it and keeps the current version. Never raises."""
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = "alf7ar/youssef-connector"
MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/latest.json"
UA = "YoussefConnector-updater/1"
INSTALLER_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/FORCECLOSEAPPLICATIONS"]


def parse_version(v: str) -> tuple:
    """'v1.2.3' / '1.2.3-beta' -> (1, 2, 3). Unparseable parts count as 0."""
    out = []
    for part in str(v or "").strip().lstrip("vV").split("-")[0].split("+")[0].split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_ok(url: str) -> bool:
    return url.lower().startswith(("https://", "http://127.0.0.1", "http://localhost"))


def _get(url: str, timeout: float = 15) -> bytes:
    if not _local_ok(url):
        raise ValueError("update url must be https")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_manifest(url: str = None) -> dict:
    m = json.loads(_get(url or os.environ.get("YC_UPDATE_URL") or MANIFEST_URL, 8).decode("utf-8"))
    if not all(m.get(k) for k in ("version", "url")):
        raise ValueError("manifest incomplete")
    if not _local_ok(str(m["url"])):
        raise ValueError("manifest url must be https")
    return m


def download(url: str, dest: Path, timeout: float = 60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dest)


def update_attempted_recently(version: str, home: Path, within: float = 3600) -> bool:
    """Guard against an update loop: the same target version was already launched less than an hour ago."""
    try:
        st = json.loads((home / "update_state.json").read_text(encoding="utf-8"))
        return st.get("version") == version and time.time() - float(st.get("ts", 0)) < within
    except (OSError, ValueError):
        return False


def note_update_attempt(version: str, home: Path):
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "update_state.json").write_text(json.dumps({"version": version, "ts": time.time()}), encoding="utf-8")
    except OSError:
        pass


def launch_installer(path: Path):
    import subprocess
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path), *INSTALLER_ARGS], creationflags=flags if os.name == "nt" else 0, close_fds=True)


def expected_sha(m: dict) -> str:
    """SHA-256 from the release's `<exe>.sha256` file; when the manifest also carries one it must agree."""
    txt = _get(m["url"] + ".sha256", 15).decode("ascii", "replace").strip()
    sha = (txt.split() or [""])[0].lower()
    man = str(m.get("sha256") or "").strip().lower()
    if len(sha) != 64 or (man and man != sha):
        return ""
    return sha


def try_update(local: str, home: Path, log=print, manifest_url: str = None, launch=launch_installer,
               tmpdir: Path = None) -> bool:
    """True if an installer was launched (the caller must exit now). Never raises."""
    try:
        m = fetch_manifest(manifest_url)
        if not is_newer(m["version"], local):
            return False
        if update_attempted_recently(m["version"], home):
            return False
        log(f"update: {local} -> {m['version']}")
        dest = (tmpdir or Path(tempfile.gettempdir()) / "YoussefConnector-Update") / f"YoussefConnector-Setup-{m['version']}.exe"
        download(m["url"], dest)
        want = expected_sha(m)
        if not want or sha256_file(dest) != want:
            dest.unlink(missing_ok=True)
            log("update: sha256 mismatch or missing, rejected; keeping the current version")
            return False
        note_update_attempt(m["version"], home)
        launch(dest)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"update check failed (ignored): {type(e).__name__}: {e}")
        return False
