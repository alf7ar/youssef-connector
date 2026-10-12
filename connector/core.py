"""Platform-neutral laptop connector core (stdlib + websockets + numpy + soxr; NO Windows imports).

Speaks the hub protocol (control socket /connector/ws + per-call socket /call/{id}/ws, header X-Connector-Token) and
drives the phone/audio through adapters. The Windows adapters (Phone Link / WASAPI) live elsewhere; connector/fakes.py
has the fakes used by tests. Policy notes:
- Control socket lost during a call: the call is hung up locally (the hub drops the call too after 30 s without hb).
- Close code 4001 (replaced by a newer connector): wait REPLACED_WAIT s and reconnect; replaced REPLACED_MAX times within
  REPLACED_WINDOW s = two connectors are fighting, so run() returns with stop_reason "replaced". A welcome min_version above ours
  also stops run(). Close code 4401 (bad token): token_rejected is set and the retry is every TOKEN_RETRY s, not the short backoff.
- Ended before answer: adapter hint `phone.end_hint()` (optional) wins; otherwise ringing <5 s -> busy, longer -> no_answer,
  never rang -> dial_failed. After answer: hint (e.g. call_ended) else callee_hangup.
"""
import asyncio
import collections
import json
import os
import random
import socket
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import numpy as np
import soxr
import websockets
from websockets.asyncio.client import connect

def _reason_texts(reason) -> tuple:
    """(ar, en) of a readiness reason (connector/readiness.py has the texts; plain fallback when it cannot be imported)."""
    try:
        from . import readiness
        return readiness.reason_texts(reason)
    except Exception:  # noqa: BLE001
        return "فحص الجاهزية فشل. لو استمر اقفل البرنامج وافتحه تاني.", "The readiness check failed. Restart the app if it persists."


CONNECTOR_VERSION = "1.0.0"
HB_EVERY = 10.0          # s between heartbeats
DIAL_WINDOW = 90.0       # s from dial to answered before no_answer (as dialer/native/calls.py)
DRAIN_MAX = 8.0          # s max wait for the player to drain after `end`
POLL = 0.25              # s between call_state polls
ACK_TIMEOUT = 10.0       # s to wait for answered_ack / the call socket handshake
BACKOFF = (1, 2, 4, 8, 16, 30)
SRC_RATE, NET_RATE = 16000, 24000
SRC_FRAME_BYTES = 640    # 20 ms PCM16 @ 16 kHz
NET_FRAME_BYTES = 1920   # 80 ms PCM16 @ 24 kHz
PREROLL_FRAMES = 50      # 1 s of 20 ms frames
BUSY_RING_MAX = 5.0
WS_PING_INTERVAL = 10.0  # s between websocket keepalive pings (both sockets); a silent network is noticed after at most
WS_PING_TIMEOUT = 10.0   # interval + timeout + close timeout = 22 s, before the hub gives up on a connector without
WS_CLOSE_TIMEOUT = 2.0   # heartbeats (30 s). The library defaults (20 + 20 + 10) would leave the phone line up for 50 s.
REPLACED_WAIT = 60.0     # s to wait after being replaced (4001) before reconnecting
REPLACED_MAX = 3         # replaced this many times within REPLACED_WINDOW = a fight with another connector: stop
REPLACED_WINDOW = 600.0
TOKEN_RETRY = 300.0      # s between reconnects while the hub rejects the token (4401)
REASONS = {"callee_hangup", "call_ended", "no_answer", "busy", "no_speech", "dial_failed", "audio_error",
           "hangup_cmd", "agent_end"}
DNS_JITTER = (0.5, 1.5)  # backoff multiplier range after a DNS failure (so a fleet does not retry in lockstep)
_ABORT = object()


def _is_dns_error(e: BaseException) -> bool:
    """True when `e` is (or was caused by) a name-resolution failure (socket.gaierror)."""
    seen = 0
    while e is not None and seen < 5:
        if isinstance(e, socket.gaierror):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


def _proxy_for(url: str):
    """The proxy websockets would use for url (HTTPS_PROXY / system settings), None when none applies or it cannot tell."""
    try:
        from websockets.uri import get_proxy, parse_uri
        return get_proxy(parse_uri(url))
    except Exception:  # noqa: BLE001
        return None


def _valid_ip(v):
    """v when it is an IPv4/IPv6 literal string (dns_cache.json is a file on disk: anything else is ignored), else None."""
    if not isinstance(v, str):
        return None
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in v else socket.AF_INET, v)
    except (OSError, ValueError):
        return None
    return v


def _heal_report(failure: str, **detail):
    """Tell the self-heal module (connector/heal.py, optional) about a failure; never raises."""
    try:
        from . import heal
        heal.report(failure, **detail)
    except Exception:  # noqa: BLE001
        pass


def _write_json_atomic(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json_dict(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _vtuple(v: str):
    from .update import version_key   # a pre-release suffix counts: 0.1.2-rc1 is newer than 0.1.1 and older than 0.1.2
    return version_key(v)


def level_summary(levels) -> dict:
    """Capture level summary from the per-20 ms-frame RMS dBFS list: how many frames carried sound (above -60 dBFS), and the median / 90th
    percentile / loudest of those. A mean over all frames is dragged down by the silent ones and says nothing about the speech level."""
    act = sorted(v for v in levels if v > -60.0)
    if not act:
        return {"active_frames": 0}
    return {"active_frames": len(act), "p50": act[len(act) // 2], "p90": act[min(len(act) - 1, int(len(act) * 0.9))], "max": act[-1]}


class Clock:
    """Injectable time: speed>1 makes sleeps shorter and now() run faster (tests)."""
    def __init__(self, speed: float = 1.0):
        self.speed = float(speed)
        self._t0 = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self._t0) * self.speed

    async def sleep(self, s: float):
        await asyncio.sleep(max(0.0, s) / self.speed)

    def sleep_sync(self, s: float):
        time.sleep(max(0.0, s) / self.speed)


# ---- adapter interfaces ---------------------------------------------------------------------------------------------
class PhoneAdapter:
    def ready(self) -> dict: raise NotImplementedError          # {ok, reason, bt_radio, ..., hf_link}
    def dial(self, phone: str) -> bool: raise NotImplementedError
    def call_state(self) -> str: raise NotImplementedError      # idle | ringing | active | ended
    def hangup(self) -> None: raise NotImplementedError
    def end_hint(self) -> Optional[str]: return None            # optional: busy | call_ended | ...


class AudioAdapter:
    def source(self): raise NotImplementedError   # object: start(cb(bytes 640)), stop(), alive()  (16 kHz PCM16, 20 ms)
    def player(self): raise NotImplementedError   # object: start(), feed(pcm16_16k), flush(), pending()->s, stop()


class AnswerGate:
    async def wait_answer(self, frames_iter, timeout_s: float) -> bool: raise NotImplementedError


# ---- resampling -----------------------------------------------------------------------------------------------------
class Resampler:
    """Streaming PCM16 mono resampler (soxr); handles odd chunk lengths; reset() drops filter state."""
    def __init__(self, in_rate: int, out_rate: int):
        self.in_rate, self.out_rate = in_rate, out_rate
        self.reset()

    def reset(self):
        self._rs = soxr.ResampleStream(self.in_rate, self.out_rate, 1, dtype="int16")
        self._odd = b""

    def process(self, pcm: bytes) -> bytes:
        pcm = self._odd + pcm
        n = len(pcm) // 2 * 2
        self._odd = pcm[n:]
        if not n:
            return b""
        out = self._rs.resample_chunk(np.frombuffer(pcm[:n], dtype=np.int16))
        return np.asarray(out, dtype=np.int16).tobytes()


class _Tap:
    """Source frames -> (gating: gate queue + 1 s pre-roll ring) -> (streaming: out queue). Thread-safe push."""
    def __init__(self, loop):
        self.loop = loop
        self.preroll = collections.deque(maxlen=PREROLL_FRAMES)
        self.gate_q: asyncio.Queue = asyncio.Queue()
        self.out_q: asyncio.Queue = asyncio.Queue()
        self.streaming = False

    def push(self, frame: bytes):   # called from the adapter's capture thread
        try:
            self.loop.call_soon_threadsafe(self._on_frame, bytes(frame))
        except RuntimeError:
            pass

    def _on_frame(self, frame: bytes):
        if self.streaming:
            self.out_q.put_nowait(frame)
        else:
            self.preroll.append(frame)
            self.gate_q.put_nowait(frame)

    async def gate_frames(self):
        while True:
            yield await self.gate_q.get()

    def start_streaming(self) -> list:
        pre = list(self.preroll)
        self.streaming = True
        self.gate_q = asyncio.Queue()
        return pre


class _Call:
    def __init__(self, call_id: str, loop):
        self.id = call_id
        self.done = asyncio.Event()
        self.reason: Optional[str] = None
        self.active = asyncio.Event()
        self.ack = asyncio.Event()
        self.answered_at: Optional[float] = None
        self.ring_at: Optional[float] = None
        self.ring_s = 0.0
        self.ws = None
        self.tap = _Tap(loop)
        self.source = None
        self.player = None
        self.draining = False
        self.frames_sent = 0   # 80 ms network frames sent to the hub (audio_stats line)

    def finish(self, reason: str) -> bool:
        if self.reason is None:
            self.reason = reason
            self.done.set()
            return True
        return False


class Connector:
    def __init__(self, base_url: str, token: str, phone: PhoneAdapter, audio: AudioAdapter, gate: AnswerGate,
                 clock: Optional[Clock] = None, host: Optional[str] = None, audio_route: str = "hf_direct",
                 version: str = CONNECTOR_VERSION, log=None, home=None, collect_logs=None):
        self.base = base_url.rstrip("/")
        self.headers = {"X-Connector-Token": token}
        self.phone, self.audio, self.gate = phone, audio, gate
        self.clock = clock or Clock()
        self.host = host or socket.gethostname()
        self.audio_route = audio_route
        self.version = version
        self.log = log or (lambda *a: None)
        self.collect_logs = collect_logs      # () -> redacted log text (main.py: logsend.collect); None = the hub's request is ignored
        self.stopped = False
        self.stop_reason: Optional[str] = None     # replaced | outdated | stopped
        self._ws = None
        self._call: Optional[_Call] = None
        self._tasks: set = set()
        self._welcomed = False
        self.token_rejected = False                # the hub closed with 4401 (bad / rotated token); cleared by the next welcome
        self.last_ready: Optional[dict] = None     # the ready dict of the latest heartbeat (keep-awake reads it)
        self._replaced_at: list = []
        self.home = Path(home) if home else None
        # Server-pushed config (welcome["config"]): persisted to HOME/remote_config.json, loaded at start, handed to on_config callbacks.
        self.remote_config: dict = _read_json_dict(self.home / "remote_config.json") if self.home else {}
        self._config_cbs: list = []
        # Last good resolved IP per hostname (HOME/dns_cache.json): used only when name resolution fails.
        self._dns_cache: dict = _read_json_dict(self.home / "dns_cache.json") if self.home else {}
        self._last_dns_fail = False

    def on_config(self, fn, replay: bool = True):
        """Register fn(config: dict), called on every welcome that carries a config. With replay (default) a config loaded from
        disk is delivered right away, so callbacks need no special start-up path."""
        self._config_cbs.append(fn)
        if replay and self.remote_config:
            self._call_config_cb(fn, self.remote_config)

    def _call_config_cb(self, fn, cfg: dict):
        try:
            fn(dict(cfg))
        except Exception as e:  # noqa: BLE001
            self.log("on_config callback failed:", repr(e))

    def _apply_config(self, cfg):
        if not isinstance(cfg, dict):
            return
        changed = cfg != self.remote_config
        self.remote_config = dict(cfg)
        if changed and self.home:
            try:
                _write_json_atomic(self.home / "remote_config.json", self.remote_config)
            except OSError as e:
                self.log("remote_config persist failed:", repr(e))
        for fn in list(self._config_cbs):
            self._call_config_cb(fn, self.remote_config)

    # ---- websocket open with DNS fallback ---------------------------------------------------------------------------
    async def _open(self, url: str, **kw):
        """Open a websocket (`async with await self._open(...) as ws`). Normal path = websockets.connect(url). When the hostname cannot
        be resolved (gaierror) and a previous connection to that host succeeded, retry once through the remembered IP: TCP goes to
        the IP while TLS SNI / certificate check / Host header still use the hostname from the URL."""
        host = urlsplit(url).hostname or ""
        proxied = _proxy_for(url) is not None   # through a proxy: the gaierror is the proxy's name, remote_address the proxy's IP
        try:
            ws = await connect(url, **kw)
        except OSError as e:
            if not _is_dns_error(e):
                raise
            self._last_dns_fail = True
            ip = None if proxied else _valid_ip(self._dns_cache.get(host))
            _heal_report("net_dns", host=host, cached_ip=bool(ip))
            self.log("dns failed for", host, "-", "via proxy: no ip fallback" if proxied else
                     "retrying via last good ip" if ip else "no cached ip")
            if not ip:
                raise
            ws = await connect(url, host=ip, proxy=None, **kw)
        else:
            if not proxied:
                self._remember_ip(host, ws)
        return ws

    def _remember_ip(self, host: str, ws):
        try:
            ip = ws.remote_address[0]
            socket.inet_pton(socket.AF_INET6 if ":" in ip else socket.AF_INET, ip)   # a real address, not a name
        except Exception:  # noqa: BLE001
            return
        if not host or host == ip or self._dns_cache.get(host) == ip:
            return
        try:
            socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
            return                                                                   # the URL already carries an IP literal
        except OSError:
            pass
        self._dns_cache[host] = ip
        if self.home:
            try:
                _write_json_atomic(self.home / "dns_cache.json", self._dns_cache)
            except OSError as e:
                self.log("dns_cache persist failed:", repr(e))

    def stop(self):
        self.stopped = True
        self.stop_reason = self.stop_reason or "stopped"

    # ---- control client ---------------------------------------------------------------------------------------------
    async def run(self):
        idx = 0
        while not self.stopped:
            self._welcomed = False
            ws = None
            err = None
            self._last_dns_fail = False
            try:
                async with await self._open(self.base + "/connector/ws", additional_headers=self.headers, open_timeout=10,
                                            max_size=2 ** 22, ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT,
                                            close_timeout=WS_CLOSE_TIMEOUT) as ws:
                    await self._session(ws)
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                err = e
                self.log("control connect/session failed:", repr(e))
            code = getattr(ws, "close_code", None) if ws is not None else None
            if self.stopped:
                return
            if code == 4001:
                now = self.clock.now()
                self._replaced_at = [t for t in self._replaced_at if now - t < REPLACED_WINDOW] + [now]
                if len(self._replaced_at) >= REPLACED_MAX:
                    self.stop_reason = "replaced"
                    return
                self.log("replaced by another connector; retrying in", REPLACED_WAIT, "s")
                await self.clock.sleep(REPLACED_WAIT)
                continue
            if code == 4401 or self._rejected_http(err):
                self.token_rejected = True
                self.log("token rejected by the hub; retrying in", TOKEN_RETRY, "s")
                await self.clock.sleep(TOKEN_RETRY)
                continue
            if self._welcomed:
                idx = 0
            delay = BACKOFF[min(idx, len(BACKOFF) - 1)]
            if self._last_dns_fail and not self._welcomed:
                delay *= random.uniform(*DNS_JITTER)
            await self.clock.sleep(delay)
            idx += 1

    @staticmethod
    def _rejected_http(err) -> bool:
        """True for a handshake refused with HTTP 401/403 (a hub or proxy that rejects the token before the upgrade)."""
        return getattr(getattr(err, "response", None), "status_code", None) in (401, 403)

    async def _send(self, obj: dict, ws=None) -> bool:
        ws = ws or self._ws
        try:
            await ws.send(json.dumps(obj))
            return True
        except Exception:
            return False

    async def _session(self, ws):
        self._ws = ws
        await self._send({"type": "hello", "connector_version": self.version, "host": self.host,
                          "audio_route": self.audio_route}, ws)
        hb = asyncio.create_task(self._hb_loop(ws))
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await self._on_control(msg)
                if self.stopped:
                    break
        finally:
            hb.cancel()
            self._ws = None
            call = self._call
            if call is not None and call.reason is None:   # control lost mid-call: do not leave a live call dangling
                call.finish("audio_error")

    async def send_logs(self, text: str) -> bool:
        """The "send logs" action: one {"type": "logs", "text"} message on the authenticated control socket (False when offline)."""
        return await self._send({"type": "logs", "text": text}) if self._ws is not None else False

    async def _hb_loop(self, ws):
        while True:
            try:
                ready = await asyncio.to_thread(self.phone.ready)
            except Exception as e:
                reason = f"ready() failed: {e!r}"
                ar, en = _reason_texts(reason)
                ready = {"ok": False, "reason": reason, "reason_ar": ar, "reason_en": en, "warnings": []}
            self.last_ready = ready
            if not await self._send({"type": "hb", "ready": ready}, ws):
                return
            await self.clock.sleep(HB_EVERY)

    async def _on_control(self, m: dict):
        t = m.get("type")
        if t == "welcome":
            self._welcomed = True
            self.token_rejected = False
            if "config" in m:
                self._apply_config(m.get("config"))
            if _vtuple(self.version) < _vtuple(m.get("min_version", "0")):
                self.stop_reason = "outdated"
                self.stopped = True
        elif t == "dial":
            self._spawn(self._run_call(str(m.get("call_id")), str(m.get("phone", ""))))
        elif t == "answered_ack":
            if self._call and self._call.id == m.get("call_id"):
                self._call.ack.set()
        elif t == "hangup":
            if self._call and self._call.id == m.get("call_id"):
                self._call.finish("hangup_cmd")
        elif t == "send_logs":          # the hub's "send logs" request (the dashboard button): same upload as the status page's
            if self.collect_logs is not None:
                self._spawn(self._send_logs_requested())

    async def _send_logs_requested(self):
        if self.in_call():
            await self._send({"type": "logs_busy"})
            return
        text = await asyncio.to_thread(self.collect_logs)
        if not await self.send_logs(text):
            self.log("send_logs requested: not sent (control socket closed)")

    def in_call(self) -> bool:
        return self._call is not None and self._call.reason is None

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- one call ---------------------------------------------------------------------------------------------------
    async def _until(self, call: _Call, aw):
        """Await `aw` unless the call finishes first -> _ABORT."""
        t = asyncio.ensure_future(aw)
        d = asyncio.ensure_future(call.done.wait())
        try:
            await asyncio.wait({t, d}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            d.cancel()
        if t.done():
            return t.result()
        t.cancel()
        return _ABORT

    async def _run_call(self, call_id: str, number: str):
        if self._call is not None:
            await self._send({"type": "dial_ack", "call_id": call_id, "ok": False, "error": "connector busy"})
            return
        loop = asyncio.get_running_loop()
        call = _Call(call_id, loop)
        self._call = call
        mon = None
        try:
            try:
                ok, err = bool(await asyncio.to_thread(self.phone.dial, number)), ""
            except Exception as e:
                ok, err = False, repr(e)
            await self._send({"type": "dial_ack", "call_id": call_id, "ok": ok, "error": "" if ok else (err or "dial failed")})
            if not ok:
                call.finish("dial_failed")
                return
            mon = asyncio.create_task(self._monitor(call))
            await self._drive(call)
        except asyncio.CancelledError:
            call.finish("audio_error")
            raise
        finally:
            if mon:
                mon.cancel()
            await self._cleanup(call)

    async def _monitor(self, call: _Call):
        t0 = self.clock.now()
        sent_ringing = False
        while not call.done.is_set():
            try:
                st = await asyncio.to_thread(self.phone.call_state)
            except Exception:
                st = "idle"
            now = self.clock.now()
            if st == "ringing":
                if not sent_ringing:
                    sent_ringing = True
                    call.ring_at = now
                    await self._send({"type": "ringing", "call_id": call.id})
            elif st == "active":
                call.active.set()
            elif st == "ended":
                si = getattr(call.source, "inner", call.source) if call.source is not None else None
                lv = list(getattr(si, "levels", []) or [])
                self.log("call end: answered", call.answered_at is not None, "source_error", repr(getattr(si, "error", None)),
                         "frames", len(lv), "max_dbfs", max(lv) if lv else None, "last", lv[-8:], "speech", level_summary(lv))
                call.finish(self._end_reason(call, now))
                return
            if call.answered_at is None and now - t0 > DIAL_WINDOW:
                call.finish("no_answer")
                return
            if call.source is not None and call.answered_at is None and not call.source.alive() and not getattr(call, "_dead_logged", False):
                call._dead_logged = True
                si = getattr(call.source, "inner", call.source)
                self.log("audio source died before answer:", repr(getattr(si, "error", None)),
                         "levels", list(getattr(si, "levels", []))[-10:])
            if call.source is not None and call.answered_at is not None and not call.source.alive():
                src_in = getattr(call.source, "inner", call.source)
                pl_in = getattr(getattr(call.player, "inner", None), "inner", None) or getattr(call.player, "inner", call.player)
                self.log("audio source stopped: source", repr(getattr(src_in, "error", None)),
                         "alive", getattr(getattr(src_in, "thread", None), "is_alive", lambda: None)(),
                         "player", repr(getattr(pl_in, "error", None)),
                         "alive", getattr(getattr(pl_in, "thread", None), "is_alive", lambda: None)())
                call.finish("audio_error")
                return
            await self.clock.sleep(POLL)

    def _end_reason(self, call: _Call, now: float) -> str:
        hint = None
        try:
            hint = self.phone.end_hint()
        except Exception:
            pass
        if call.answered_at is not None:
            return hint if hint in REASONS else "callee_hangup"
        if hint in REASONS:
            return hint
        if call.ring_at is None:
            return "dial_failed"
        return "busy" if now - call.ring_at < BUSY_RING_MAX else "no_answer"

    async def _drive(self, call: _Call):
        t0 = self.clock.now()
        if await self._until(call, call.active.wait()) is _ABORT:
            return
        call.source = self.audio.source()
        call.source.start(call.tap.push)
        remaining = max(1.0, DIAL_WINDOW - (self.clock.now() - t0))
        ok = await self._until(call, self.gate.wait_answer(call.tap.gate_frames(), remaining))
        if ok is _ABORT:
            return
        if not ok:
            call.finish("no_answer")
            return
        call.answered_at = self.clock.now()
        preroll = call.tap.start_streaming()
        await self._send({"type": "answered", "call_id": call.id})
        call.player = self.audio.player()
        call.player.start()
        await self._until(call, self._wait_ack(call))
        if call.reason is not None:
            return
        try:
            async with await self._open(f"{self.base}/call/{call.id}/ws", additional_headers=self.headers,
                                        open_timeout=ACK_TIMEOUT, max_size=2 ** 22,
                                        ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT,
                                        close_timeout=WS_CLOSE_TIMEOUT) as cws:
                call.ws = cws
                await self._call_socket(call, cws, preroll)
        except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
            self.log("call socket failed:", repr(e))
            call.finish("audio_error")
        if call.draining:   # hub closed the socket right after `end`: let the drain finish with agent_end
            await call.done.wait()
        call.finish("audio_error")   # socket closed without a verdict: a dropped call socket mid-call

    async def _wait_ack(self, call: _Call):
        try:
            await asyncio.wait_for(call.ack.wait(), ACK_TIMEOUT / self.clock.speed)
        except asyncio.TimeoutError:
            call.finish("audio_error")

    async def _call_socket(self, call: _Call, cws, preroll):
        out_rs = Resampler(SRC_RATE, NET_RATE)
        in_rs = Resampler(NET_RATE, SRC_RATE)

        async def sender():
            buf = b""
            pending = preroll
            while True:
                frame = pending.pop(0) if pending else await call.tap.out_q.get()
                buf += out_rs.process(frame)
                while len(buf) >= NET_FRAME_BYTES:
                    await cws.send(buf[:NET_FRAME_BYTES])
                    call.frames_sent += 1
                    buf = buf[NET_FRAME_BYTES:]

        async def receiver():
            nonlocal in_rs
            async for raw in cws:
                if isinstance(raw, bytes):
                    pcm = in_rs.process(raw)
                    if pcm:
                        call.player.feed(pcm)
                    continue
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                t = m.get("type")
                if t == "flush":
                    call.player.flush()
                    in_rs.reset()
                elif t == "end" and not call.draining:
                    call.draining = True
                    self._spawn(self._drain_then_end(call))
                elif t == "error":
                    self.log("hub error:", m)

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        done = asyncio.create_task(call.done.wait())
        try:
            await asyncio.wait(tasks + [done], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks + [done]:
                t.cancel()

    async def _drain_then_end(self, call: _Call):
        t0 = self.clock.now()
        while call.reason is None and call.player.pending() > 0 and self.clock.now() - t0 < DRAIN_MAX:
            await self.clock.sleep(0.1)
        call.finish("agent_end")

    def _audio_stats(self, call: _Call):
        """One audio_stats line per call: frames sent plus whatever the audio adapters report. Telemetry only, never raises."""
        stats = {"frames_sent": call.frames_sent}
        try:
            for obj in (call.source, call.player):
                fn = getattr(obj, "stats", None)
                if fn is not None:
                    stats.update(fn() or {})
        except Exception as e:  # noqa: BLE001
            stats["error"] = repr(e)[:120]
        self.log("audio_stats", json.dumps(stats, default=str))

    async def _cleanup(self, call: _Call):
        reason = call.reason or "audio_error"
        if reason in ("agent_end", "hangup_cmd", "no_answer", "audio_error"):
            try:
                await asyncio.to_thread(self.phone.hangup)
            except Exception as e:
                self.log("hangup failed:", repr(e))
        self._audio_stats(call)   # before the stops: the adapters' telemetry is read while their owners are still set
        for obj, m in ((call.source, "stop"), (call.player, "stop")):
            if obj is not None:
                try:
                    getattr(obj, m)()
                except Exception:
                    pass
        if call.ws is not None:
            try:
                await call.ws.close()
            except Exception:
                pass
        talk = round(self.clock.now() - call.answered_at, 1) if call.answered_at is not None else 0.0
        await self._send({"type": "ended", "call_id": call.id, "reason": reason, "talk_s": talk})
        if self._call is call:
            self._call = None
