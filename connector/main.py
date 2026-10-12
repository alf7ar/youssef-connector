"""Youssef Connector: entry point. Runs the platform-neutral core (connector/core.py) with the Windows phone/audio adapters.

First run: a small page on 127.0.0.1 asks for the connect code (base64url JSON {"u": hub url, "t": token}) copied from the
dashboard and checks it with GET /connector/ping. Config lives in %LOCALAPPDATA%\\YoussefConnector\\config.json.
Flags: --selftest [--selftest-out FILE]  imports of every module, faulthandler, COM init through micguard (Windows), VAD model
                                         + a fake call through the core against the bundled mock hub; ZERO audio devices, no
                                         connect code; exits 0/1 within 60 s (a hang dumps all stacks and exits)
       --e2e [--selftest-out FILE]       the real app flow (config, status page, core) with fake adapters against the mock hub
       --no-update                       skip the update check
       --supervise                       keep the app running: start it as a child and relaunch it when it dies (connector/supervisor.py)
Env (tests/CI only): YC_HOME (data dir), YC_UPDATE_URL (manifest override), YC_UPDATE_EVERY (seconds between update checks),
YC_FAKE=1 (fake adapters), YC_PORT (status page port), YC_SUPERVISED=1 (set by --supervise for its child: distinct exit codes).
Exit codes: 0 everything else that is clean; EXIT_UPDATE / EXIT_REPLACED only under YC_SUPERVISED=1 (otherwise 0, as before);
1 fatal error. The supervisor does not relaunch after 0, EXIT_UPDATE or EXIT_REPLACED."""
import asyncio
import base64
import contextlib
import importlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import guard, logsend, power, update

MUTEX = guard.MUTEX
PORT = 5076
UA = "YoussefConnector/1"
EXIT_UPDATE = 10      # an update installer was launched: the installer replaces the files and starts the new version
EXIT_REPLACED = 11    # another connector took over this token: relaunching would make the two fight over it
SUPERVISOR_STOP = {0: "clean exit", EXIT_UPDATE: "update", EXIT_REPLACED: "replaced by another connector"}


def exit_code(code: int) -> int:
    """EXIT_UPDATE / EXIT_REPLACED are only reported to a supervisor (YC_SUPERVISED=1); a plain run still exits 0."""
    return code if os.environ.get("YC_SUPERVISED") == "1" else 0


# ------------------------------------------------------------------ paths / config / connect code (pure)
def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parents[1])


def read_version() -> str:
    try:
        return (resource_dir() / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def app_home() -> Path:
    env = os.environ.get("YC_HOME")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "YoussefConnector"


def read_config(home: Path) -> dict:
    try:
        return json.loads((home / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_config(home: Path, cfg: dict):
    home.mkdir(parents=True, exist_ok=True)
    p = home / "config.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def parse_connect_code(code: str) -> tuple:
    """base64url({"u": url, "t": token}) -> (url, token). Raises ValueError with a user-facing message."""
    code = "".join(str(code or "").split())
    try:
        d = json.loads(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)).decode("utf-8"))
        url, tok = str(d["u"]).strip().rstrip("/"), str(d["t"]).strip()
    except Exception:  # noqa: BLE001
        raise ValueError("الكود غير صحيح / Invalid code") from None
    if not tok or not url.lower().startswith(("https://", "http://127.0.0.1", "http://localhost", "wss://", "ws://127.0.0.1")):
        raise ValueError("الكود غير صحيح / Invalid code")
    return url, tok


TOKEN_REJECTED_MSG = "الكود قديم أو غير صحيح، جاري إعادة المحاولة كل ٥ دقايق / Code rejected by the server (old or wrong), retrying every 5 minutes"


def http_base(url: str) -> str:
    return url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)


def ws_base(url: str) -> str:
    return url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)


def check_code(url: str, token: str, timeout: float = 10) -> tuple:
    """(ok, message): the hub accepts the token iff GET /connector/ping answers {"ok": true}."""
    req = urllib.request.Request(http_base(url) + "/connector/ping", headers={"X-Connector-Token": token, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (r.status == 200 and json.loads(r.read().decode("utf-8")).get("ok") is True), "OK"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 4401):
            return False, "الكود قديم أو غير صحيح / Wrong or old code"
        return False, f"خطأ في الخادم / Server error {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"لا يوجد اتصال / No connection ({type(e).__name__})"


def single_instance() -> bool:
    """True if we are the only instance (Windows named mutex; other OS: always True)."""
    if os.name != "nt":
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    h = k32.CreateMutexW(None, False, MUTEX)
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(ctypes.c_void_p(h))
        return False
    single_instance.handle = h
    return True


# ------------------------------------------------------------------ adapters
def build_adapters(clock=None, cfg=None, log=None):
    """(phone, audio, gate, audio_route). YC_FAKE=1 -> the test fakes, otherwise the Windows adapters."""
    if os.environ.get("YC_FAKE") == "1":
        from .fakes import FakeAudio, FakeGate, FakePhone
        return FakePhone("answer", clock=clock, ring_s=0.2), FakeAudio(clock=clock), FakeGate(True), "fake"
    import onnxruntime  # noqa: F401  (load its DLLs before winrt's msvcp140, see PORT_NOTES.md)
    from . import vad, win_adapters  # noqa: F401
    return win_adapters.build(cfg=cfg, log=log)


def _reason_texts(reason) -> tuple:
    from . import core
    return core._reason_texts(reason)


# ------------------------------------------------------------------ app (status page + core)
PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Youssef Connector</title><style>
body{font-family:Segoe UI,Arial,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;color:#222}
h1{font-size:20px}.card{border:1px solid #ddd;border-radius:8px;padding:14px;margin:12px 0}
.ok{color:#0a7d32}.bad{color:#b00020}input{width:100%;padding:8px;box-sizing:border-box;font-family:monospace}
button{padding:8px 16px;margin-top:8px}.warn{color:#8a5a00}small{color:#666}</style></head><body>
<h1>Youssef Connector <small id="ver"></small></h1>
<div id="connect" class="card" style="display:none">
<p>الصق كود الربط من لوحة التحكم. / Paste the connect code from your dashboard.</p>
<input id="code" autocomplete="off" spellcheck="false"><br><button id="go">ربط / Connect</button> <span id="msg"></span></div>
<div id="status" class="card" style="display:none">
<div>الخادم / Hub: <b id="hub"></b></div><div>الهاتف / Phone: <b id="ready"></b></div>
<div id="warn" class="warn"></div>
<div>المكالمة / Call: <b id="call"></b></div>
<button id="logs">إرسال السجلات / Send logs</button> <span id="logmsg"></span></div>
<script>
const $=id=>document.getElementById(id);
async function tick(){try{const s=await (await fetch('/status')).json();$('ver').textContent=s.version;
$('connect').style.display=s.configured?'none':'block';$('status').style.display=s.configured?'block':'none';
$('hub').textContent=s.connected?'متصل / connected':'جاري الاتصال... / connecting...';$('hub').className=s.connected?'ok':'bad';
const r=s.ready||{};$('ready').textContent=r.ok?'جاهز / ready':'مش جاهز: '+(r.reason_ar||r.reason||'')+(r.reason_en?' / '+r.reason_en:'');
$('ready').className=r.ok?'ok':'bad';
$('warn').replaceChildren(...(r.warnings||[]).map(w=>{const d=document.createElement('div');d.textContent='تنبيه: '+w.ar+' / '+w.en;return d;}));
$('call').textContent=s.call||'لا يوجد / none';}catch(e){}}
$('go').onclick=async()=>{$('msg').textContent='...';const r=await fetch('/connect',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('code').value})});
const j=await r.json();$('msg').textContent=j.message;$('msg').className=j.ok?'ok':'bad';tick();};
$('logs').onclick=async()=>{$('logmsg').textContent='...';try{const r=await fetch('/sendlogs',{method:'POST',
headers:{'Content-Type':'application/json'},body:'{}'});const j=await r.json();$('logmsg').textContent=j.message;$('logmsg').className=j.ok?'ok':'bad';}
catch(e){$('logmsg').textContent='فشل / failed';$('logmsg').className='bad';}};
tick();setInterval(tick,2000);
</script></body></html>"""


class App:
    def __init__(self, home: Path, version: str, log=print):
        self.home, self.version, self.log = home, version, log
        self.cfg = read_config(home)
        self.conn = None
        self.phone = None
        self.audio_route = ""
        self.loop = None
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.message = ""
        self.exit_for_update = False
        self.replaced = False
        self.port = 0
        self.crash_pending = False     # set by main() when the previous run crashed: its logs go up once, on connect
        self._crash_retry_at = 0.0

    async def _send_crash_logs(self):
        """The redacted tails of connector.log + crash.log, once after a crash, over the live control socket (never in a call)."""
        c = self.conn
        if not self.crash_pending or c is None or getattr(c, "_ws", None) is None or self.in_call():
            return
        if time.monotonic() < self._crash_retry_at:
            return
        try:
            ok = await c.send_logs(logsend.collect(self.home, self.cfg))
        except Exception as e:  # noqa: BLE001
            ok = False
            self.log("crash_logs_error", f"{type(e).__name__}: {e}"[:200])
        if ok:
            self.crash_pending = False
            self.log("crash_logs_sent")
        else:
            self._crash_retry_at = time.monotonic() + 60

    def configured(self) -> bool:
        return bool(self.cfg.get("url") and self.cfg.get("token"))

    def keep_awake_active(self) -> bool:
        """Connected to the hub and the latest heartbeat said ready (read by the keep-awake thread)."""
        c = self.conn
        return bool(c is not None and getattr(c, "_ws", None) is not None and (getattr(c, "last_ready", None) or {}).get("ok"))

    def status(self) -> dict:
        c = self.conn
        call = getattr(c, "_call", None)
        ready = {}
        if self.phone is not None and c is not None and getattr(c, "_ws", None) is not None:
            with contextlib.suppress(Exception):
                ready = dict(self.phone.ready())
            if ready and not ready.get("ok") and not ready.get("reason_ar"):
                ready["reason_ar"], ready["reason_en"] = _reason_texts(ready.get("reason"))
        st = {"version": self.version, "configured": self.configured(),
                "connected": bool(c is not None and getattr(c, "_ws", None) is not None),
                "ready": ready, "call": (call.id if call is not None and call.reason is None else None),
                "message": self.message}
        if c is not None and getattr(c, "token_rejected", False):
            st["state"] = "token_rejected"
            st["message"] = TOKEN_REJECTED_MSG
        return st

    def send_logs(self) -> tuple:
        """(ok, message): the tails of connector.log + crash.log, redacted, over the control socket. Called from the status-page thread."""
        if self.in_call():
            return False, "فيه مكالمة شغالة، جرب بعدين / A call is in progress, try again after it ends"
        c, loop = self.conn, self.loop
        if c is None or loop is None or getattr(c, "_ws", None) is None:
            return False, "مفيش اتصال بالخادم / Not connected to the hub"
        text = logsend.collect(self.home, self.cfg)
        try:
            ok = asyncio.run_coroutine_threadsafe(c.send_logs(text), loop).result(15)
        except Exception:  # noqa: BLE001
            ok = False
        return (True, "تم إرسال السجلات / Logs sent") if ok else (False, "فشل الإرسال / Send failed")

    def in_call(self) -> bool:
        call = getattr(self.conn, "_call", None)
        return call is not None and call.reason is None

    def try_update(self) -> bool:
        """Update check that never runs during a call; backs up the app dir for the crash-loop rollback before installing."""
        before = (lambda: guard.backup_current(self.home, self.version, log=self.log)) if guard.frozen() else None
        return update.try_update(self.version, self.home, self.log, channel=self.cfg.get("channel"), busy=self.in_call,
                                 before_launch=before)

    def connect(self, code: str) -> tuple:
        try:
            url, tok = parse_connect_code(code)
        except ValueError as e:
            return False, str(e)
        ok, msg = check_code(url, tok)
        if not ok:
            return False, msg
        self.cfg = {"url": url, "token": tok}
        write_config(self.home, self.cfg)
        self.wake.set()
        return True, "تم الربط / Connected"

    def handler(self):
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _host_ok(self):
                return (self.headers.get("Host", "").split(":")[0]) in ("127.0.0.1", "localhost")

            def _send(self, code, body, ctype):
                b = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, "forbidden", "text/plain")
                if self.path == "/status":
                    return self._send(200, json.dumps(app.status()), "application/json")
                if self.path in ("/", "/index.html"):
                    return self._send(200, PAGE, "text/html; charset=utf-8")
                self._send(404, "not found", "text/plain")

            def do_POST(self):
                # JSON content type forces a CORS preflight for foreign pages; the Host check stops DNS rebinding
                if (not self._host_ok() or self.path not in ("/connect", "/sendlogs")
                        or not self.headers.get("Content-Type", "").startswith("application/json")):
                    return self._send(403, "forbidden", "text/plain")
                if self.path == "/sendlogs":
                    ok, msg = app.send_logs()
                    return self._send(200, json.dumps({"ok": ok, "message": msg}), "application/json")
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 8192)
                    code = json.loads(self.rfile.read(n).decode("utf-8")).get("code", "")
                except Exception:  # noqa: BLE001
                    code = ""
                ok, msg = app.connect(code)
                self._send(200, json.dumps({"ok": ok, "message": msg}), "application/json")
        return H

    def serve(self, port: int):
        for p in (port, 0):
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", p), self.handler())
                break
            except OSError:
                continue
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.server = srv
        return self.port

    async def _update_due(self, check_updates: bool) -> bool:
        """Periodic update check (every YC_UPDATE_EVERY s, default 6 h). True if an installer was launched."""
        if not check_updates or time.monotonic() - self._last_check < self._every or self.in_call():
            return False
        self._last_check = time.monotonic()
        if await asyncio.to_thread(self.try_update):
            self.exit_for_update = True
            self.stop_event.set()
            return True
        return False

    async def run(self, check_updates: bool):
        from . import core
        self.loop = asyncio.get_running_loop()
        self._every = float(os.environ.get("YC_UPDATE_EVERY") or 21600)
        self._last_check = time.monotonic()
        while not self.stop_event.is_set():
            if not self.configured():
                await asyncio.sleep(0.5)
                await self._update_due(check_updates)
                continue
            phone, audio, gate, route = build_adapters(cfg=self.cfg, log=self.log)
            self.phone, self.audio_route = phone, route
            rb = guard.rolled_back_info(self.home)
            if rb:
                ready_orig = phone.ready
                phone.ready = lambda: {**ready_orig(), "update_rolled_back": rb}
            self.conn = core.Connector(ws_base(self.cfg["url"]), self.cfg["token"], phone, audio, gate,
                                       audio_route=route, version=self.version, log=lambda *a: self.log(*a))
            task = asyncio.create_task(self.conn.run())
            while not task.done() and not self.stop_event.is_set():
                await asyncio.sleep(0.5)
                await self._send_crash_logs()
                await self._update_due(check_updates)
            if self.stop_event.is_set():
                self.conn.stop()
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                return
            if self.conn.stop_reason == "outdated":
                self.message = "تحديث مطلوب / Update required"
                if check_updates and await asyncio.to_thread(self.try_update):
                    self.exit_for_update = True
                    return
                await asyncio.sleep(60)
            elif self.conn.stop_reason == "replaced":
                self.message = "تم تشغيل موصل آخر / Another connector took over"
                self.replaced = True
                return


def make_logger(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    p = home / "connector.log"
    try:
        if p.exists() and p.stat().st_size > 1_000_000:
            p.replace(home / "connector.log.1")
    except OSError:
        pass

    def log(*a):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
        with contextlib.suppress(Exception):
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if sys.stdout:
            with contextlib.suppress(Exception):
                print(line, flush=True)
    return log


# ------------------------------------------------------------------ selftest / e2e (no devices, no connect code)
def _mock_hub_module():
    d = str(resource_dir() / "tests")
    if d not in sys.path:
        sys.path.insert(0, d)
    import mock_hub
    return mock_hub


async def _fake_call_loop(version: str, app: App = None) -> str:
    """Dial one fake call through the core against the mock hub; returns the `ended` reason."""
    from . import core
    from .fakes import FakeAudio, FakeGate, FakePhone
    mh = _mock_hub_module()
    clock = core.Clock(speed=20.0)

    async def on_call(call):
        await call.send_audio(b"\x00\x10" * 12000)    # 1 s of low-level 24 kHz audio
        await asyncio.sleep(0.3)
        await call.end("agent_end")

    hub = mh.MockHub(token="selftest", on_call=on_call)
    await hub.start()
    try:
        if app is None:
            conn = core.Connector(hub.url, "selftest", FakePhone("answer", clock=clock), FakeAudio(clock=clock), FakeGate(True),
                                  clock=clock, host="selftest", version=version)
            task = asyncio.create_task(conn.run())
        else:
            app.cfg = {"url": hub.url, "token": "selftest"}
            write_config(app.home, app.cfg)
            task = None
        await hub.wait_msg("hello", timeout=20)
        await hub.dial("01012345678", "st1")
        ended = await hub.wait_msg("ended", "st1", timeout=60)
        if app is not None:
            st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{app.port}/status", timeout=5).read())
            assert st["configured"] and st["connected"], st
        return ended["reason"]
    finally:
        if task is not None:
            conn.stop()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await hub.stop()


def _check_vad():
    from . import vad
    c = vad.SileroVAD().voice_confidence(b"\x00\x00" * vad.WINDOW)
    assert 0.0 <= c <= 1.0, c
    return f"silence confidence {c:.3f}"


SELFTEST_LIMIT_S = 55


def selftest(out: Path = None) -> int:
    lines, ok = [], True
    fh = open(out, "w", encoding="utf-8", buffering=1) if out else None   # line-buffered: a hang still leaves the steps done so far

    def emit(line):
        lines.append(line)
        if fh:
            fh.write(line + "\n")

    def step(name, fn):
        nonlocal ok
        try:
            emit(f"START {name}")
            r = fn()
            emit(f"PASS {name}" + (f": {r}" if r else ""))
        except Exception as e:  # noqa: BLE001
            ok = False
            emit(f"FAIL {name}: {type(e).__name__}: {e}")
            emit(traceback.format_exc(limit=4))

    def imports():
        import numpy, onnxruntime, soxr, websockets  # noqa: F401,E401
        mods = ["core", "fakes", "vad", "update", "readiness", "micguard", "comstress", "supervisor"] + (["phone", "win_adapters", "win_audio"] if os.name == "nt" else [])
        for m in mods:
            importlib.import_module(f"{__package__ or 'connector'}.{m}")
        return f"version {read_version()}, {len(mods)} connector modules"

    def crash_log():
        import faulthandler
        assert faulthandler.is_enabled(), "faulthandler is off"
        return "faulthandler on"

    def com_init():
        if os.name != "nt":
            return "skipped (not Windows)"
        from . import comstress, micguard
        micguard._com_thread(lambda c: comstress._create(c))
        assert micguard._mta_keeper and micguard._mta_keeper[0].is_alive(), "MTA keeper thread is not running"
        r = comstress.run(60)
        assert r["created"] == 60 and r["mta_keeper_alive"], r
        return f"micguard COM thread ok, mta-keeper alive, {r['created']} wrappers gc'd off-thread ({r['finalised_on_gc_thread']}) in {r['secs']}s"

    def audio_imports():
        if os.name != "nt":
            return "skipped (not Windows)"
        import comtypes, psutil, pycaw, sounddevice, soundcard  # noqa: F401,E401
        from . import win_adapters, win_audio  # noqa: F401
        return "soundcard/WASAPI/pycaw import ok"

    def fake_loop():
        reason = asyncio.run(asyncio.wait_for(_fake_call_loop(read_version()), 40))
        assert reason == "agent_end", reason
        return f"ended={reason}"

    import faulthandler
    with contextlib.suppress(Exception):   # no --selftest-out and no stderr (windowed exe): no watchdog
        faulthandler.dump_traceback_later(SELFTEST_LIMIT_S, exit=True, file=fh or sys.stderr)   # a hang dumps every thread's stack, exit 1
    t0 = time.time()
    step("imports", imports)
    step("crash-log", crash_log)
    step("com-init", com_init)
    step("audio-imports", audio_imports)
    step("vad-model", _check_vad)
    step("fake-audio-loop", fake_loop)
    faulthandler.cancel_dump_traceback_later()
    emit("SELFTEST " + ("OK" if ok else "FAILED") + f" in {time.time() - t0:.1f}s")
    if fh:
        fh.close()
    print("\n".join(l for l in lines if not l.startswith("START ")), flush=True)
    return 0 if ok else 1


def e2e(out: Path = None) -> int:
    """The real App flow (config file, status page, core) with the fake adapters against the mock hub."""
    os.environ["YC_FAKE"] = "1"
    home = Path(tempfile.mkdtemp(prefix="youssef-e2e-"))
    app = App(home, read_version(), make_logger(home))
    app.serve(0)
    err = None

    async def go():
        runner = asyncio.create_task(app.run(False))
        try:
            return await asyncio.wait_for(_fake_call_loop(app.version, app), 90)
        finally:
            app.stop_event.set()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(runner, 10)

    try:
        reason = asyncio.run(go())
        assert reason == "agent_end", reason
        msg = f"E2E OK ended={reason} config={'yes' if read_config(home).get('token') else 'no'}"
    except Exception as e:  # noqa: BLE001
        err = f"E2E FAILED: {type(e).__name__}: {e}"
        msg = err + "\n" + traceback.format_exc(limit=4)
    print(msg, flush=True)
    if out:
        Path(out).write_text(msg + "\n", encoding="utf-8")
    return 1 if err else 0


_crash_fh = None
CRASH_KEEP = 200 * 1024


def enable_crash_log(home: Path):
    """faulthandler into <home>/crash.log (append, last ~200 KB kept, one timestamp header per run) so a native crash leaves
    a Python stack. Never writes to sys.stderr (None under pythonw); never raises."""
    global _crash_fh
    try:
        import faulthandler
        home.mkdir(parents=True, exist_ok=True)
        p = home / "crash.log"
        if p.exists() and p.stat().st_size > CRASH_KEEP:
            data = p.read_bytes()[-CRASH_KEEP:]
            p.write_bytes(data)
        _crash_fh = open(p, "a", encoding="utf-8")
        _crash_fh.write(f"\n=== run {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} ===\n")
        _crash_fh.flush()
        faulthandler.enable(file=_crash_fh, all_threads=True)
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # a crash of the previous run: faulthandler text in crash.log, or guard.json recorded a fast death (begin() ran already)
    crashed = logsend.crash_pending(app_home()) or int(guard._read(app_home() / "guard.json").get("fd") or 0) > 0
    if "--supervise" not in argv:   # the supervisor's own header would hide the previous child's crash from the next child
        enable_crash_log(app_home())
    out = Path(argv[argv.index("--selftest-out") + 1]) if "--selftest-out" in argv else None
    if "--version" in argv:
        print(read_version())
        return 0
    if "--selftest" in argv:
        return selftest(out)
    if "--e2e" in argv:
        return e2e(out)
    if "--supervise" in argv:
        from . import supervisor
        home = app_home()
        return supervisor.main(argv, home, read_version(), make_logger(home), SUPERVISOR_STOP)
    if not single_instance():
        return 0
    home = app_home()
    log = make_logger(home)
    version = read_version()
    app = App(home, version, log)
    app.crash_pending = crashed
    rb = guard.rolled_back_info(home)
    if rb:
        log(f"update_rolled_back: {rb.get('from')} -> {rb.get('to')} (pinned; {rb.get('from')} is not offered again)")
    port = app.serve(int(os.environ.get("YC_PORT") or PORT))
    log(f"Youssef Connector {version} status page http://127.0.0.1:{port}/")
    if "--no-update" not in argv and app.try_update():
        guard.mark_clean(home)
        return exit_code(EXIT_UPDATE)     # the installer closes/replaces files and relaunches the new version
    if not app.configured():
        with contextlib.suppress(Exception):
            webbrowser.open(f"http://127.0.0.1:{port}/")
    keep_awake = power.KeepAwake(app.keep_awake_active, log).start() if os.name == "nt" else None
    try:
        asyncio.run(app.run("--no-update" not in argv))
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        log("fatal: " + traceback.format_exc())
        return 1
    finally:
        if keep_awake is not None:
            keep_awake.stop()
    guard.mark_clean(home)
    if app.exit_for_update:
        return exit_code(EXIT_UPDATE)
    if app.replaced:
        return exit_code(EXIT_REPLACED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
