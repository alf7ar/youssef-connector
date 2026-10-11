"""Minimal protocol server for the connector (control + call sockets), scripted from tests. Stdlib + websockets only,
no pytest import, so the public repo's Windows CI can reuse it.

    hub = MockHub(token="t"); await hub.start()          # hub.url -> ws://127.0.0.1:<port>
    cid = await hub.dial("+2010...")                      # sends `dial`
    await hub.wait_msg("ended", cid)                      # waits for a connector message
    hub.log                                               # ordered [(kind, detail)] for ordering assertions

Rules mirrored from the hub: header X-Connector-Token only (bad -> close 4401), a new hello replaces the old connector
(old closed 4001), a call socket before `answered_ack` is rejected (close 4403), call sockets are single use.
`on_call(call)` is an async callback run when a call socket opens (scripted audio / flush / end / drop).
"""
import asyncio
import json
import time

import websockets
from websockets.asyncio.server import serve


class MockCall:
    def __init__(self, call_id, ws):
        self.id, self.ws = call_id, ws
        self.audio = []           # binary frames received (bytes)
        self.closed = asyncio.Event()

    async def send_audio(self, pcm24: bytes, chunk: int = 1920, pace_s: float = 0.0):
        for i in range(0, len(pcm24), chunk):
            await self.ws.send(pcm24[i:i + chunk])
            if pace_s:
                await asyncio.sleep(pace_s)

    async def send(self, **msg):
        await self.ws.send(json.dumps(msg))

    async def flush(self):
        await self.send(type="flush")

    async def end(self, reason="agent_end"):
        await self.send(type="end", reason=reason)

    async def drop(self):
        await self.ws.close(1011)


class MockHub:
    def __init__(self, token="tok", min_version="0.0.0", auto_ack=True, on_call=None, welcome=True):
        self.token, self.min_version, self.auto_ack, self.on_call, self.welcome = token, min_version, auto_ack, on_call, welcome
        self.server = None
        self.port = None
        self.control = None
        self.msgs = []                 # (type, msg, t) from the connector's control socket
        self.log = []                  # ordered events: ctl:<type>, ack_sent, call_open, audio, call_rejected, call_closed
        self.calls = {}                # call_id -> MockCall
        self.headers_seen = []         # X-Connector-Token values seen (control + call sockets)
        self.early_call_attempts = 0
        self.acked = set()
        self._cond = asyncio.Condition()
        self._used = set()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    async def start(self):
        self.server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, ws):
        path = ws.request.path
        tok = ws.request.headers.get("X-Connector-Token")
        self.headers_seen.append(tok)
        if tok != self.token:
            await ws.close(4401)
            return
        if path == "/connector/ws":
            await self._control(ws)
        elif path.startswith("/call/") and path.endswith("/ws"):
            await self._call(ws, path.split("/")[2])
        else:
            await ws.close(4404)

    async def _control(self, ws):
        old, self.control = self.control, ws
        if old is not None:
            await old.close(4001)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                self.log.append((f"ctl:{t}", msg.get("call_id")))
                if t == "hello" and self.welcome:
                    await ws.send(json.dumps({"type": "welcome", "min_version": self.min_version}))
                async with self._cond:
                    self.msgs.append((t, msg, time.monotonic()))
                    self._cond.notify_all()
                if t == "answered" and self.auto_ack:
                    await self.ack(msg["call_id"])
        finally:
            if self.control is ws:
                self.control = None

    async def _call(self, ws, call_id):
        if call_id not in self.acked or call_id in self._used:
            self.early_call_attempts += 1
            self.log.append(("call_rejected", call_id))
            await ws.close(4403)
            return
        self._used.add(call_id)
        call = MockCall(call_id, ws)
        self.calls[call_id] = call
        self.log.append(("call_open", call_id))
        task = asyncio.create_task(self.on_call(call)) if self.on_call else None
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    call.audio.append(raw)
                    self.log.append(("audio", call_id))
        finally:
            call.closed.set()
            self.log.append(("call_closed", call_id))
            if task:
                task.cancel()

    async def _send(self, **msg):
        await self.control.send(json.dumps(msg))

    async def ack(self, call_id):
        self.acked.add(call_id)
        self.log.append(("ack_sent", call_id))
        await self._send(type="answered_ack", call_id=call_id)

    async def dial(self, phone, call_id="c1"):
        await self._send(type="dial", call_id=call_id, phone=phone)
        return call_id

    async def hangup(self, call_id, reason="operator"):
        await self._send(type="hangup", call_id=call_id, reason=reason)

    async def wait_msg(self, mtype, call_id=None, timeout=10.0):
        async def _w():
            async with self._cond:
                while True:
                    for t, m, _ in self.msgs:
                        if t == mtype and (call_id is None or m.get("call_id") == call_id):
                            return m
                    await self._cond.wait()
        return await asyncio.wait_for(_w(), timeout)

    def of_type(self, mtype):
        return [m for t, m, _ in self.msgs if t == mtype]

    async def wait_call(self, call_id, timeout=10.0):
        async def _w():
            while call_id not in self.calls:
                await asyncio.sleep(0.005)
            return self.calls[call_id]
        return await asyncio.wait_for(_w(), timeout)
