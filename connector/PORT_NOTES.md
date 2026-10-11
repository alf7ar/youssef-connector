# Windows port notes (5p-B)

Sources: the private dialer's `dialer/native/{pipeline,calls,phone,micguard}.py` and `plcall.ps1`; interfaces from
`dialer/connector_core.py` (now `core.py`). "Identical" was checked by comparing `ast.get_source_segment` of each function or
class (and `cmp` for whole files) against the source. Not copied anywhere: agent IDs, keys, hostnames, the Phone Link
call-history reader, prompts, remote-access scripts, pipecat, the hosted-agent client, the campaign link, settings loading.

## Files

| File | What |
|---|---|
| `phone.py` | whole file, byte-identical to `native/phone.py` (it had no legacy paths to rename) |
| `plcall.ps1` | whole file, byte-identical to `native/plcall.ps1` |
| `micguard.py` | `native/micguard.py` with ONE line changed (line 13, `HOME`, see below) |
| `win_audio.py` | verbatim blocks from `pipeline.py` (below) behind a hand-written header |
| `vad.py` | new: Silero VAD through onnxruntime, written for this repo |
| `silero_vad.onnx`, `LICENSE-silero-vad.txt` | vendored official model + MIT license |
| `readiness.py` | `calls.py` readiness helpers; `auto_ready` adapted |
| `win_adapters.py` | glue: `WinPhone`, `WinAudio`, `SileroGate`, `make_adapters`, `build` |

## Copied verbatim (identical = yes)

`win_audio.py` <- `native/pipeline.py` (header lines 1-11 of win_audio.py are hand-written: docstring, `sys`/`threading`/`time`/`numpy` imports)

| Item | pipeline.py | win_audio.py | Identical |
|---|---|---|---|
| constants `RATE`, `CHUNK`, `TTS_RATE`, `OUT_*`, `HP_HZ`, `PRESENCE_*` | 90-105 | 13-28 | yes, except the trailing comment on `HP_HZ`, where a persona name was removed |
| `Biquad`, `FirLowpass`, `LookaheadLimiter`, `Loudness`, `LoudBlocks` | 108-251 | 31-174 | yes |
| `HF_MATCH`, `_PA_LOCK`, `AUDIO_BUSY`, `_PA_EXEC`, `pa_call`, `find_hf_devices`, `_find_hf_devices`, `_find_hf`, `_stream_args` | 517-579 | 177-239 | yes |
| `SdSource`, `SdPlayer` | 582-754 | 242-414 | yes |
| `_com_init`, `_com_mta`, `_sc` | 757-792 | 417-452 | yes |
| `IdSource`, `IdPlayer` | 795-903 | 455-563 | yes |
| `CABLE_CAPTURE/PLAYBACK`, `find_cable_devices` | 906-930 | 566-590 | yes |

Not taken from pipeline.py: everything else (lines 1-89, 253-516, 931-end: pipecat transports, STT/LLM/TTS, `Player`, WAV test
sources, `CallInput/Output`).

`readiness.py` <- `native/calls.py`: `resumed` (72-75), `RESUME_GAP`/`HF_SETTLE` (48-49), `HF_REFRESH_EVERY`/`_last_hf_refresh`
(640-641), `_phone_link_running` (597-604), `_tasklist_has` (608-616), `session_locked` (619-621), `battery_saver_on` (624-637):
all identical. Only `_sibling()` and the module-level `phone = _sibling("phone")` are new at the top.

`win_adapters.py` <- `native/calls.py`: `NUM_RE` (43), `_not_room_mics` (524-530), `_switch_to_cables` (533-539),
`restore_defaults` (542-558): identical. They use module globals `phone`, `json`, `DEFAULTS_BACKUP`.

## Changed or adapted

- `micguard.py` line 13: the legacy ProgramData path and its env override became
  `HOME = Path(os.environ.get("YOUSSEF_HOME") or os.environ.get("YC_HOME") or (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "YoussefConnector"))`
  (same default and same `YC_HOME` override as `main.app_home()`). Every other line is identical (`diff` shows only line 13). `win_adapters.HOME = micguard.HOME` is the one data dir: it holds
  `mic-mute.json` (crash-safety state) and `default-devices.json` (cable route), and `WinPhone.__init__` creates it.
  `build()` also reads `HOME/config.json` for its optional keys, so call `build(cfg=self.cfg, log=self.log)` from main to avoid
  any path coupling and to get the adapter log lines (`build()` without `log` prints to stdout).
- `readiness.auto_ready` (calls.py 644-674): synchronous (the core calls `ready()` through `asyncio.to_thread`; the winrt
  coroutines run with `asyncio.run`), no `openai_key_missing` check, no `pipeline.load_settings`, MAC from the `mac`
  argument else `find_iphone_mac()` once (cached in `readiness._mac`). Same check order and same reason strings
  (`call_active`, `call_incoming`, `call_state_unknown`, `hf_link_active`, `session_locked`, `battery_saver`,
  `phone_link_not_running`, `bluetooth_off`, `iphone_not_connected`, `hf_endpoints`). New reason `hf_refreshing`.
- Hands-Free refresh (`Bridge._hf_check`, calls.py 452-469, plus the 30 s settle in `watch_loop` and the 15 min throttle in
  `auto_ready`): `readiness._hf_check` / `_maybe_refresh` run `phone.refresh_hf_service` (about 2 minutes) in a daemon thread,
  so the heartbeat never stalls. Due at app start + 30 s and 30 s after a resume from sleep (`resumed`), otherwise at most
  every 900 s; reports `hf_refreshing` while it runs, `hf_endpoints` otherwise.
- `readiness.check(mac, log) -> dict` is what `PhoneAdapter.ready()` returns: `ok, reason, bt_radio, iphone_connected,
  hfp_audio_device, phone_link_running, session_locked, call_active, hf_link (bool), hf_state (str)`; values not probed
  before an early exit are `None`. While a call is in progress `WinPhone.ready()` answers `{ok: False, reason: "call_active"}`
  without probing.
- Dial/flow logic (`WinPhone`) re-implements `Bridge._flow`, `_bridge.watch`, `dial`, `end_phone_call` against the core's
  polling model:
  - `dial()`: same number check as `/api/call`; refuses when `hf_link()=="active"` or `call_active`; then
    `asyncio.run(phone.phone_link("dial", n))`.
  - `call_state()`: `idle` / `ringing` (live call in the Phone Service, Hands-Free link not up) / `active` (both Hands-Free
    endpoints state 1) / `ended`. Before the audio is on the laptop: `audio_to_laptop` after 2.5 s "talking", up to 3
    attempts, 5 s apart (calls.py 226-238); "no live call for 1.2 s and `call_active is not True`" = ended; the
    `hf_link_stuck` / `phone_state_gap` log lines. After it: `call_active` False for 1.5 s (after having been True) =
    `callee_hangup`, Hands-Free link not active for 1.5 s = `call_ended` (watch(), calls.py 320-329, 3 x 0.5 s ticks).
  - `end_hint()` returns the reason only after `SileroGate` saw the callee speak (`mark_answered`), or `audio_error`; before the
    answer it returns `None`, so the core tells busy / no_answer / dial_failed by the ring time.
  - `hangup()`: `phone.end_calls()` (PhoneEnd), then restores the microphone / cable defaults.
  - Differences from the old flow: the old 75 s "no call audio on the laptop" error is now 30 s after the Phone Service says
    "talking" (`NO_AUDIO_S`; an unanswered ring is left to the core's 90 s `no_answer`), ends as `audio_error`. The old
    per-5 s `levels` log, the `phone_link_ui` dump and the "auto-join" mode are not ported.
  - Room-microphone mute (`micguard.mute(_not_room_mics())`) and the cable default switch happen when the call goes `active`
    (inside `call_state`, worker thread). `micguard.restore` and `restore_defaults` run on every end path (`_end`, `hangup`,
    source/player `stop`) and are idempotent.
- Audio (`WinAudio`): `hf_direct` -> `IdSource(capture id)` / `IdPlayer(render id)` (the IDs `WinPhone` took from the MMDevices
  registry); `cable` -> `SdSource` / `SdPlayer` on the PortAudio indices from `find_cable_devices`. `AUDIO_BUSY` is set while
  any stream is open. **Loudness is applied** in the player wrapper (`_Player`): `Loudness.process()` per `feed()` chunk and
  `Loudness.clear()` after `flush()`, exactly what `eleven.run_agent` did, because the hub sends the agent audio unprocessed
  (`loudness=False` in the config turns it off). `LoudBlocks` is copied but unused. The source's `alive()` is also False
  when the player's stream died (the old `watch` checked both).
- `vad.py` implements `eleven.wait_for_speech`'s parameters: 512-sample windows with a 64-sample context, confidence >= 0.7
  for 0.3 s in a row, model state reset every 5 s. The pre-roll is not ported (core's `_Tap` keeps 1 s). The loop is a plain
  `async for` with a monotonic deadline per frame (cancelling an async generator's `__anext__` would close it). The code is
  written from the algorithm in pipecat's `SileroVADAnalyzer` (BSD-2, Daily) rather than copied; add that notice to the
  third-party list only if you want to be conservative.
- Model: `silero_vad.onnx` = snakers4/silero-vad tag `v6.0`, `src/silero_vad/data/silero_vad.onnx`, sha256
  `597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004` (same bytes as the model pipecat bundles).
  License: MIT, Copyright (c) 2020-present Silero Team, in `LICENSE-silero-vad.txt` (same text at v5.1.2 and v6.0).

## Public surface for main.py / packaging

```python
from connector import win_adapters
phone, audio, gate, audio_route = win_adapters.build(cfg=None, log=None)   # what main.build_adapters expects
#   cfg keys (default: HOME/config.json): audio_route "hf_direct"|"cable", phone_mac, in_gain, loudness
#   calls WinPhone.startup_cleanup(): unmute a mic / restore cable defaults a crashed call left behind
win_adapters.make_adapters(route="hf_direct", mac="", gain=1.0, loudness=True, log=None) -> (WinPhone, WinAudio, SileroGate)
core.Connector(base_url, token, phone, audio, gate, audio_route=audio_route, log=log)   # log(*args) as the core expects
vad.SileroVAD().voice_confidence(b"\0" * vad.WINDOW_BYTES)   # model load check, no devices (vad.WINDOW = 512 samples)
```

- `win_audio`, `vad`, `micguard`, `readiness` import on any OS (numpy only at import; onnxruntime / sounddevice / soundcard /
  pycaw / comtypes load lazily). `phone` needs winrt, so off Windows `win_adapters.phone` and `readiness.phone` are `None`;
  `WinPhone()` then raises, `readiness.check()` returns `phone_module_unavailable`. Tests can assign stubs to those attributes.
- Selftest with zero devices: `vad.SileroVAD()`, `import win_audio` (and, on Windows, `win_audio._sc()` for soundcard/WASAPI).
- PyInstaller: ship `plcall.ps1` as data NEXT TO `phone.py` (`phone.PLCALL = Path(__file__).with_name("plcall.ps1")`),
  `silero_vad.onnx` next to `vad.py` (`vad.model_path()` also looks in `sys._MEIPASS/connector/` and `sys._MEIPASS/`; env
  `YOUSSEF_VAD_MODEL` overrides). Hidden imports the spec needs: `winrt.windows.applicationmodel.calls`,
  `winrt.windows.devices.bluetooth`, `winrt.windows.devices.radios`, `winrt.windows.devices.enumeration`, `comtypes`,
  `comtypes.client`, `pycaw.pycaw`, `pycaw.api.mmdeviceapi`, `pycaw.constants`, `soundcard.mediafoundation`, `sounddevice`.
  The private `packaging/requirements-app.txt` lists the proven set (sounddevice, soundcard, psutil, pycaw, soxr, winrt-runtime,
  winrt-Windows.ApplicationModel.Calls / Devices.Bluetooth / Devices.Enumeration / Devices.Radios / Foundation /
  Foundation.Collections); add numpy, onnxruntime, websockets, comtypes.
- Import order matters on the real machine (from the private `calls.py`): import `vad`/onnxruntime (and `win_audio`) before
  `phone` (winrt), so onnxruntime's DLLs load before winrt's msvcp140. `win_adapters` imports `phone` first; if the
  frozen app shows a DLL error, import `vad` (or `onnxruntime`) earlier in `main.py`.

## Could not be ported / not verified here

- Nothing was run on Windows: Phone Link UI automation, PhoneOm.dll calls, WASAPI via soundcard, pycaw/comtypes. The glue was
  tested on Linux (scratch, not in the repo) with a scripted phone stub, fake IdSource/IdPlayer, the REAL Silero gate on
  ringback tone + a speech wav, the real core and `mock_hub`: answer + hub-ended call, busy, dial_failed, callee_hangup,
  call_ended (Hands-Free drop), no-call-audio audio_error, readiness reasons and the background Hands-Free refresh, the
  Loudness wrapper (10 tests green).
- The cable route (`SdSource` / `SdPlayer` on PortAudio) is copied but untested end to end.
- Auto-join (attach to any active call), the dial page / call history, `levels` logging, and the Phone Link `dump` log
  are intentionally not part of the connector.
