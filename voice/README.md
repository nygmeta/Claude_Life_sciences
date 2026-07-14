# voice: real-time voice interface

A self-contained, real-time **voice assistant**: you speak, it transcribes, thinks, and
replies **out loud**. The full pipeline is **VAD → ASR → LLM → TTS**, split across a dev
machine and a self-hosted GPU host, plus hosted Claude. Browser-side VAD feeds FunASR-Nano
for speech-to-text, **Claude Haiku** generates the reply, and **gepard-1.0** speaks it back.

English-only (gepard synthesizes EN/ES-MX/PT-BR/NL, not Chinese).

This directory is self-contained: every command below is run with `voice/` as the working
directory.

## Two pages. Pick the right one.

There are **two** front ends here, and opening the wrong one is the single most common way
to lose an hour, because it fails silently rather than loudly.

| | `web/index.html` | `web/console.html` |
|---|---|---|
| what it is | the **standalone** voice assistant | the **Lab Agent console**, voice + the lab UI |
| talks to | its own orchestrator, same origin | a Lab Agent API **and** a speech service, which may be on different machines |
| use it for | testing the voice stack alone | **the lab demo, and anything near a robot** |
| served by | `deploy/run-web-local.sh` (:8765) | `deploy/start-lab-console.sh` (:8090) |

The rest of this README documents the **standalone** page. If you want the lab console,
which is almost certainly what you want, go to
**[doc/HOST_THE_CONSOLE.md](doc/HOST_THE_CONSOLE.md)** and run:

```bash
bash deploy/start-lab-console.sh --voice <voice-host>
```

That starts the Lab Agent locally and serves the console, borrowing ASR, TTS, the safety
gates and the "did you mean X?" check from a remote speech service over `wss://`. **No
GPU is needed on that machine.** It is the setup to use when the console must sit on a
robot's LAN while the GPUs cannot.

Two traps worth naming, because both look like broken buttons rather than a wrong page:

- **Do not open a bare `http://localhost:8090`.** It used to serve `index.html`, which
  hardcodes its WebSocket to its own origin, finds nothing there, and leaves the mic
  greyed out forever with no error. `/` now redirects to the console, but open the URL the
  script prints and you can't get this wrong.
- **The mic only arms on the Live tab.** It is deliberately greyed on **Demo**, which
  takes no input.

**None of this drives a robot yet.** The adapter can compile and simulate an Opentrons
protocol; there is no `execute()`. See
**[doc/HARDWARE_EXECUTION.md](doc/HARDWARE_EXECUTION.md)** for what exists, what does not,
and what real execution would require.

## Architecture (split: dev machine + self-hosted GPU host)

```
Dev machine
  Browser (index.html, OmniVAD WASM in-browser VAD)
    http://localhost:8765  (loopback, a secure context, so the mic works with no
                             HTTPS, no tunnel, and no public URL; the app has no auth)
    mic → 16 kHz PCM16 speech segment ──WS──▶ web/orchestrator :8765
                                               1. POST segment → ASR :8030 → user text
                                               2. Claude Haiku (HTTPS, direct), streamed → reply text
                                               3. each sentence → TTS :8040 → wav chunk
                                               ◀── transcript / reply_delta / reply_audio ×N / reply_audio_end (WS)
  Anthropic API key and data/sessions/*.json transcripts stay here; neither is ever sent
  to the GPU host.
        │
        │  ssh -L 8030:127.0.0.1:8030 -L 8040:127.0.0.1:8040   (deploy/dev-forward.sh)
        ▼
Self-hosted GPU host (<gpu-host>)
  asr/server.py   127.0.0.1:8030   FunASR-Nano
  tts/server.py   127.0.0.1:8040   gepard-1.0
  Both bind to loopback only and hold no secrets, just code and model weights; the SSH
  forward above is the only path in.
```

Three processes: one on the dev machine, two on the GPU host, each in its own venv
(model weights cached under a single `HF_HOME` on the GPU host):

| Service | Where | Port | venv | Stack |
|---|---|---|---|---|
| `web/` orchestrator + page | dev machine | 8765 | `venv-web` (no torch) | websockets, anthropic, openai, httpx |
| `asr/` FunASR-Nano | GPU host | 8030 | `lab-assistant-envs/asr` | funasr (OpenAI-compatible `/v1/audio/transcriptions`) |
| `tts/` gepard-1.0 | GPU host | 8040 | `lab-assistant-envs/tts` | nemo-toolkit 2.4.0, transformers 5.3.0, gepard_inference |

gepard runs via the **reference PyTorch `gepard_inference`** path, not `gepard-vllm`
(vLLM targets CUDA 13; the deploy pack targets Blackwell sm_120 via the cu128 torch
index instead, see `deploy/env.sh`).

Every ASR response also carries a nullable per-token confidence block, scraped from the
decoder at no extra latency cost; see the "Lab command gate" note below for what consumes
it.

The orchestrator (`web/server.py`) can address more than one TTS backend: `LA_TTS_MODELS`
(`model-id=url,model-id=url,...`, first entry is the default) maps model ids to backend
URLs, and the assistant path and the TTS panel's trial synth each route to whichever model
is selected. The current `deploy/` pack stands up gepard-1.0 only, so that mapping is
unset, the single-model `LA_TTS_URL` fallback applies, and the UI hides the model selector.

## Layout

```
web/     server.py (orchestrator+WS), index.html, requirements.txt, vendor/omnivad/
asr/     server.py (FunASR-Nano), requirements.txt
tts/     server.py (gepard backend), make_gepard_voice.py (captures/encodes the pinned
         `default` reference voice), fetch_voices.py (downloads the 18 preset voices),
         requirements.txt, voices/
deploy/  env.sh (host contract: conda env dir, python 3.12 floor, cu128 torch pin,
         LA_* vars) · sync-to-host.sh (local, rsync) · setup-{asr,tts}.sh (order-
         sensitive TTS install) · detached.sh (transient systemd units for long
         steps) · smoke-tts.sh (on-GPU validation gate) · install-services.sh +
         units/*.in (persistent systemd --user units) · run-services.sh (quick
         detached alternative) · health.sh · stop-services.sh · dev-forward.sh
         (local, SSH tunnel) · run-web-local.sh (local, orchestrator)
scripts/ preflight_gpu.py (GPU host, fail-fast CUDA + compute-capability check) ·
         smoke_tts.py (GPU host) · smoke_ws.py + run_local_smoke.sh (local)
credentials/  anthropic_key.txt, host.env (LA_SSH_TARGET, LA_APP_DIR, ...)   (gitignored)
```

## Local verification (no GPU)

Runs the orchestrator against mock ASR/TTS while calling the **real** Claude Haiku API,
so it validates the WS protocol, the ASR/TTS contracts, and the LLM key + streaming:

```
bash scripts/run_local_smoke.sh      # expects: SMOKE: PASS
```

## Deploy to a self-hosted GPU host

Isolation: only `asr/` and `tts/` run on the GPU host, and they need no secrets. The
Anthropic API key, `web/`, and `data/` (session transcripts) stay on the dev machine and
are never synced to the host. The GPU host's coordinates are never committed; they live
in the gitignored `credentials/host.env`:
```
LA_SSH_TARGET=<ssh alias or user@host>
LA_APP_DIR=<absolute path on the GPU host, e.g. /home/<user>/lab-assistant>
```

1. **Fill `credentials/host.env`** as above (create it if missing). Every `deploy/`
   script that touches the host sources it.
2. **Sync code to the host**: `bash deploy/sync-to-host.sh`. Pushes `asr/`, `tts/`
   (including the voice `.pt` files), `deploy/`, and the two scripts the host needs
   (`preflight_gpu.py`, `smoke_tts.py`); see the script's header for the full list of
   what is deliberately left out.
3. **Build the environments on the host**, wrapped in `deploy/detached.sh` so a dropped
   SSH session cannot kill a long install:
   ```
   ssh -o ForwardX11=no <gpu-host> 'bash -lc "cd <app-dir> && bash deploy/detached.sh start setup-asr"'
   ssh -o ForwardX11=no <gpu-host> 'bash -lc "cd <app-dir> && bash deploy/detached.sh wait setup-asr"'
   ssh -o ForwardX11=no <gpu-host> 'bash -lc "cd <app-dir> && bash deploy/detached.sh start setup-tts"'
   ssh -o ForwardX11=no <gpu-host> 'bash -lc "cd <app-dir> && bash deploy/detached.sh wait setup-tts"'
   ```
   Both scripts install torch explicitly from the cu128 index (Blackwell sm_120) before
   anything else, and run `scripts/preflight_gpu.py` (a real CUDA matmul, not just an
   import check) both before and after the dependency install. `setup-tts.sh`'s install
   order is load-bearing: NeMo pulls transformers <= 4.52, gepard needs transformers 5.x,
   so transformers is force-reinstalled after NeMo and `gepard_inference` installs last;
   do not reorder it.
4. **Validate TTS on the GPU before starting the service** (the deploy gate):
   ```
   ssh -o ForwardX11=no <gpu-host> 'bash -lc "cd <app-dir> && bash deploy/smoke-tts.sh"'
   ```
   Loads the model and codec, synthesizes one sentence to `out.wav`, and warms the HF
   cache so the service's first real start is fast.
5. **Install the services as persistent systemd `--user` units** (requires
   `loginctl enable-linger` once for the host user):
   ```
   ssh -o ForwardX11=no <gpu-host> 'bash -lc "cd <app-dir> && bash deploy/install-services.sh"'
   ssh -o ForwardX11=no <gpu-host> 'bash -lc "cd <app-dir> && bash deploy/health.sh"'
   ```
   The units survive SSH disconnect, logout, and an unattended reboot, and restart on
   crash (`Restart=on-failure`). `deploy/run-services.sh` is a lighter, non-persistent
   alternative for a quick throwaway test only.
6. **Locally: open the tunnel, then serve the page**:
   ```
   bash deploy/dev-forward.sh --bg      # SSH port-forward, :8030 and :8040 only
   bash deploy/run-web-local.sh         # starts web/server.py on http://localhost:8765
   open http://localhost:8765
   ```
   `dev-forward.sh` is the only path into the GPU host's services: both bind to
   `127.0.0.1` there and are exposed to nothing else. `run-web-local.sh` refuses to start
   if it cannot reach the forwarded ports, so a broken tunnel fails fast instead of at the
   first ASR call.
7. **Verify**: click Start, allow the mic, speak an English sentence. Expect the
   transcript, a streamed reply, and gepard's voice playing it back. `data/latency.jsonl`
   and `data/sessions/*.json` land on the dev machine, never on the GPU host.

Stop the services with `bash deploy/stop-services.sh` when the card is needed for
something else (they are enabled units, so they come back on the next reboot unless
disabled).

### Models

No upstream model code and no checkpoints are committed here: `asr/server.py` and
`tts/server.py` are original thin HTTP wrappers, and every weight is downloaded at deploy
time into the single `HF_HOME` cache defined in `deploy/env.sh`.

- **ASR**: the FunASR toolkit (https://github.com/modelscope/FunASR), installed by
  `deploy/setup-asr.sh`. `asr/server.py` loads `FunAudioLLM/Fun-ASR-Nano-2512` from
  Hugging Face on first use (`FUNASR_MODEL` overrides it).
- **TTS**: gepard-1.0 (`nineninesix/gepard-1.0`, plus the NeMo codec
  `nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps`), run through
  https://github.com/nineninesix-ai/gepard-inference, which `deploy/setup-tts.sh` installs
  pinned to a fixed commit (`fa19f579`); `deploy/smoke-tts.sh` warms the weight cache. The
  preset voices are fetched from the public `nineninesix/gepard` Hugging Face Space by
  `tts/fetch_voices.py` (no token). The only committed voice file is `tts/voices/default.pt`,
  the pinned reference speaker: regenerating it re-rolls the voice rather than reproducing it.
- **VAD**: OmniVAD, vendored in-tree under `web/vendor/omnivad/`. It runs in the browser,
  so there is nothing to download.

## Notes

- **Turn batching**: the browser gathers speech segments and only fires the reply after
  the whole turn ends (extra silence past a segment, "Turn pause" in the VAD panel).
  Multiple sentences become one message and one reply, not one reply per pause. Server
  side: `handle_segment` accumulates transcripts, `handle_end_turn` runs LLM -> TTS once.
- **Header buttons**: `STT`, `VAD`, `TTS`, and `History` each toggle their own config
  panel (below); `History` is mutually exclusive with the other three, opening it closes
  whichever of `STT`/`VAD`/`TTS` is open, and vice versa. `?` toggles the "Configure" help
  card on its own, independent of whether a panel is open; it folds by default on narrow
  or portrait viewports and shows by default on wide landscape viewports (768px or wider),
  a session-only preference that is not persisted. `New session` (renamed from `Reset`)
  finalizes the current session into history and starts a fresh, separately numbered one;
  see "Session history" below.
- **VAD tuning panel** ("VAD" button, applied hot): Speech threshold (VAD sensitivity),
  Segment pause ms (silence that ends a speech segment -> ASR), and Turn pause ms (silence
  before the reply fires). Threshold + segment-pause hot-swap a fresh OmniVAD instance while
  the mic keeps running (no re-prompt); turn-pause updates instantly. All browser-side.
- **STT panel** ("STT" button, applied hot; renamed from "Hints", same ASR hotwords +
  replacements config): **Hotwords** bias FunASR toward domain terms (sent as the ASR
  `prompt` -> `model.generate(hotwords=[...])`); **Replacements** are `from = to` fixes
  applied to every transcript (correct recurring mis-transcriptions). Managed over WS
  `get_hints`/`set_hints` (shape unchanged), now **scoped per user** (one file under
  `data/hints/<scope>.json`, no cross-scope access even for an operator, since hints
  rewrite what a user hears the assistant respond to), with new defaults for a scope
  with nothing saved yet: hotwords `["Claude"]`, replacements `{"cloud code": "Claude
  Code"}`. See `doc/INTEGRATION.md` for the scoping details.
- **TTS model switching**: when more than one TTS backend is configured
  (`LA_TTS_MODELS`), the TTS panel's first control, ahead of "Voice", is a "Model"
  selector; picking a model there sends `set_tts_model`, which sets the model for both the
  assistant's next reply and the panel's own trial synth (one shared selector, not a
  dropdown per panel). A single-model deploy, which is what `deploy/` stands up, hides the
  selector automatically, and the header itself carries no TTS-model control. Populated by
  `list_tts_models`, which reports the configured models, the default, and the session's
  current pick.
- **TTS panel**: the "TTS" button opens a single panel that replaces the former separate
  "Voice" panel and TTS playground. One shared set of controls, a voice dropdown plus
  temperature, cfg_scale, top_k, and max_frames, drives two actions from the same values:
  **Synthesize** previews them against custom text via the existing `tts_test` message
  (preview only, never touches the assistant's live config), and **Confirm change** sends
  `set_tts_params` to apply the same voice and params to the assistant's own reply path.
  Assistant params no longer auto-apply on every control edit: edits are staged in the
  panel, and Confirm is dirty-gated, disabled until the controls differ from the server's
  last-applied config. That baseline comes from the `tts_params` message received on
  connect, and refreshes again after every confirmed apply. `list_voices`, `tts_params`,
  and `tts_test` are otherwise unchanged (the trial's `tts_test` call now also carries
  `max_frames`, already accepted server-side). See `doc/INTEGRATION.md` for the exact
  message shapes.
- **Session history**: the "History" button opens a panel listing every session, newest
  first: number, name, and start time, with the live session tagged "Live". Any session,
  live or a past one, can be renamed inline (pencil icon; click to edit, Enter or blur
  commits, Escape cancels). A past session (not the live one) can be deleted via a
  two-click arm/confirm button on its row (no native browser confirm dialog); the live
  session has no delete control in the UI, and the server refuses a delete for it as a
  second guard. Clicking a past session swaps the main transcript pane to a read-only view
  of that session's saved messages, with a "Viewing Session N: <name>" banner and a "Back
  to live" button; the live session itself keeps running completely undisturbed in the
  background, only the client's displayed view changes. `New session` (the renamed former
  `Reset` button) finalizes the current session, which stays in History, and starts a
  fresh, separately numbered one. Every session is persisted to `data/sessions/<id>.json`
  as it happens (gitignored, same directory pattern as `data/latency.jsonl` and
  `data/asr_hints.json`), so history survives a server restart, and the transcript kept
  there is the full, never-truncated conversation, separate from the bounded window
  (`LA_HISTORY_TURNS` pairs) the LLM call actually sees. Managed over WS: client sends
  `list_sessions`, `get_session`, `rename_session`, `delete_session`, or `new_session`;
  server sends `session_started`, `sessions`, `session_data`, `session_renamed`, or
  `session_deleted`. The old `reset` message type no longer exists at all; `new_session`
  replaces it entirely, not as an alias alongside it. See `doc/INTEGRATION.md` for the
  exact message shapes.
- **Voices (gepard)**: 18 named presets in `tts/voices/` (US/UK English, Mexican
  Spanish, Brazilian Portuguese, Dutch, and novelty voices), plus `default.pt`, a pinned
  reference voice generated by `tts/make_gepard_voice.py` (`--method capture`,
  recommended, or `--method encode` as a fallback). `deploy/sync-to-host.sh` ships
  whatever `.pt` files already exist in `tts/voices/` (gitignored) to the GPU host, so
  provisioning is normally a no-op; `deploy/setup-tts.sh` only pulls the 18 presets from
  the public HuggingFace Space `nineninesix/gepard` (via `tts/fetch_voices.py`, no token
  needed) if the directory arrives empty, and never generates `default.pt` itself, since
  regenerating it re-rolls the pinned speaker rather than reproducing it; that stays a
  deliberate, GPU-requiring, one-time step whose output is then synced like any other
  voice file. Before the original fix, gepard's `default` passed `ref_codes=None` to the
  LM, meaning it was unconditioned and could drift to a different speaker every
  sentence; `default.pt` overrides that with one fixed, conditioned reference. See
  `doc/TTS_FAILURE_MODES.md` (failure mode 3) for the full story. An optional
  `LA_TTS_GEPARD_DEFAULT_VOICE` env can instead alias `default` to another named voice
  (the currently served default is `en_oak`, see the served-defaults note below). Any
  `*.pt` reference-code file dropped in `tts/voices/` becomes a selectable voice on the
  next TTS restart.
- **venv-tts install order is load-bearing**: NeMo pulls transformers ≤4.52, but gepard
  needs the Qwen3.5 backbone from transformers 5.x: `setup-tts.sh` force-reinstalls
  `transformers==5.3.0` after NeMo. Torch is not inherited from any base image; there is
  none. It is installed explicitly, first, from the cu128 index (Blackwell sm_120), per
  `deploy/env.sh`'s `LA_TORCH_SPEC`/`LA_TORCH_INDEX`.
- **Sentence-streaming TTS**: the reply is synthesized per sentence as it streams from
  Claude, not once after the full reply. Each completed sentence gets its own TTS call and
  its own `reply_audio` message (`{type, seq, text, audio_b64, sample_rate:22050,
  format:"wav"}`), sent in order, followed by a terminal `reply_audio_end` (`{type,
  chunks}`) once the last one is sent. The browser can start playing the first sentence
  while later sentences are still generating, cutting perceived (time-to-first-audio)
  latency. Toggle with `LA_TTS_STREAM` (default on; set to `0` to fall back to the
  original single-synth-after-full-reply path on the same client contract, useful as a
  demo safety valve if streaming misbehaves).
- **Latency logging**: every assistant turn and every playground synth is written as
  one JSON line to `data/latency.jsonl` (override with `LA_LOG_FILE`) with per-component
  timings, so total latency is reconstructable. An assistant record carries `asr`
  (per VAD segment + total), `llm` (`ttft_ms` time-to-first-token, total `ms`, token
  usage), `tts` (`ms`, audio seconds, real-time factor, chunk count when streaming), plus
  `reply_latency_ms` (end_turn → last reply_audio), `total_ms` (first speech → last
  reply_audio), `first_audio_ms` (end_turn → FIRST reply_audio, the perceived-latency
  metric), and `stream` (bool, whether sentence-streaming was used for the turn). A
  concise `[lat] ...` line is also echoed to stdout (`logs/web.log`). Summarize with
  `python3 scripts/latency_report.py [path]`, count + min/mean/p50/p90/p95/max per stage.
- **Latency**: full-turn (ASR→LLM→TTS sequential), first speech to last audio, ~1-3 s.
  With sentence-streaming on, the audience hears the first sentence well before that,
  around the LLM's time-to-first-token plus one short synth, not the full reply's
  generation and synthesis time. Reference figures (not a fresh-pod baseline): gepard
  synthesized 2.136 s of audio in 753.9 ms (real-time factor 2.83); Claude Haiku's
  time-to-first-token was about 0.9-1.0 s in local smoke runs.
- **Barge-in**: speaking again hushes the assistant's current audio.
- **Theme toggle**: a header toggle (`#themeToggle`) switches the page between the
  default light palette and a dark palette, via a CSS-variable theming system keyed on a
  `data-theme` attribute on the root element. The choice persists in `localStorage`; on a
  first visit it defaults to the light look unless the OS reports `prefers-color-scheme:
  dark`, in which case it starts dark. An inline snippet in `<head>` resolves the theme
  before first paint, so there is no flash of the wrong theme on load.
- **Speculative LLM start**: the reply pipeline now fires the Claude call at the segment
  boundary instead of waiting for the turn boundary, generating silently server-side (no
  client messages, no TTS) and releasing the buffered output only once the turn commits.
  A new segment silently aborts and refires the speculation; a discarded speculation is
  invisible to the client. The WS contract does not change, only the timing of
  `reply_start`/`reply_delta`/`reply_audio` moves earlier. Toggle with `LA_SPEC_START`
  (default on); `LA_SPEC_MAX_TURN_S` (default `12`) caps how long a single turn keeps
  refiring speculative calls. See `doc/SPECULATIVE_START.md`.
- **TTS served defaults**: gepard-1.0 serves voice `en_oak` at temperature `0.15` unless
  overridden, via envs `LA_TTS_VOICE` and `LA_TTS_TEMP` (the `LA_TTS_TEMP` default changed
  from `0.3` to `0.15`).
- **Lab command gate** (`LA_LAB_MODE`, default on): assistant turns gain a single Claude
  tool, `lab_command`, for a demo lab-bench command catalog (read a sensor, set
  temperature, start/stop the stirrer, dispense, add a reagent, run the centrifuge). Each
  command is tagged safe, reversible, irreversible, or hazardous, and a gate fuses that
  severity with the turn's weakest ASR confidence (`FUNASR_CONFIDENCE`, see the ASR note
  above) to decide whether to run it, ask for a spoken confirmation, or refuse it outright.
  A confirmation is a grounded, digit-by-digit readback ("dispense five zero, that is 50,
  microliters into well A three") that the user confirms or cancels by voice, no LLM call
  needed to resolve it; a short standalone "stop" halts an in-flight reply and the demo lab
  state immediately, as a supervisory stop, not a substitute for a real hardware e-stop.
  Thresholds are env-tunable: `LA_CONF_LOW` (default `0.75`) and `LA_CONF_VERYLOW` (default
  `0.50`). An irreversible or hazardous confirmation is intent-bound: the user must say
  the exact phrase ("say confirm dispense", not a bare "confirm" or "yes"), so a stray
  affirmation can never fire the wrong physical action. An unclear or confidence-less
  confirmation is heard but re-prompted rather than executed, and a stale confirmation
  (`LA_PENDING_TTL_S`, default `120` seconds) expires and must be repeated while an
  unrelated follow-up is simply answered normally, so neither a low-confidence "confirm"
  nor one heard long after the fact can fire an action on its own. Verify the confidence shim
  on its own with `asr/smoke_confidence.py`. See `doc/INTEGRATION.md` for the exact WS
  message shapes and the full confirm-floor/expiry rules.
- **Proactive announcements** (`LA_EVENTS`, default on): the assistant can now speak
  unprompted, not only in reply to a turn, when the lab produces an event. An `alert`
  preempts an in-flight reply (or a pending lab-command confirmation) and plays after a
  short earcon, so it is never buried; an `info` waits its turn and plays once the
  assistant falls quiet. Events come from the operator (`inject_event`, gated on the
  existing operator allowlist, optionally broadcast to every connected client) or from a
  timed automation completion (a centrifuge run finishing, a target temperature reached).
  Announcements render as EVENT/ALERT rows in the transcript but are not added to session
  history or the LLM's context, so the assistant has no memory of what it already
  announced. See `doc/INTEGRATION.md` for the exact WS message shapes.
- **Protocol walkthrough**: the assistant can walk an operator through a written
  protocol (a demo six-step plasmid miniprep) hands-free, reading each step back
  verbatim and tracking where the operator is. It rides the existing `lab_command` tool
  via five new SAFE intents (`protocol_start`, `protocol_next`, `protocol_back`,
  `protocol_repeat`, `protocol_status`), no new tool needed; navigating a protocol moves
  no hardware, so it proceeds without confirmation except at very low ASR confidence,
  like any SAFE command. A timed step (an incubation, a spin) schedules a spoken
  completion announcement through the proactive-announcement channel above when its
  countdown ends, and that timer disarms if the operator navigates away, halts, or
  disconnects, so an abandoned step never announces. `LA_PROTOCOL_TIMER_SCALE` (default
  `1.0`) compresses only the actual wait, never the spoken duration, so a demo can set
  it around `0.01` and still hear "5 minutes" spoken while observing the announcement in
  about a second. See `doc/INTEGRATION.md` for the exact WS shapes.
- **Addressed-speech detection** (`LA_ADDRESSED`, default `0`, off): an open mic on a
  lab bench also hears colleagues talking to each other, so this classifier decides
  whether each transcribed segment was actually said to the assistant before it becomes
  part of a turn. The `transcript` WS message gains an additive `addressed` boolean;
  side speech is still shown to the user (greyed) but never accumulates into a turn,
  never speculates, and never gets a reply. Deterministic fast paths (a pending
  confirm/cancel, a stop utterance, a wake form, a standalone filler) decide most
  utterances with no model call; the genuinely ambiguous rest costs one bounded Claude
  Haiku call forced through a typed tool result. The feature fails OPEN: any classifier
  error, timeout, or malformed answer is treated as addressed, so a hiccup can only cost
  one unnecessary reply, never a dropped turn. It defaults off and stays opt-in because
  it is the only feature here that can discard real user speech: a false negative costs
  the user their turn, and that risk should not go live until the classifier is tuned
  against real lab audio. See `doc/INTEGRATION.md` for the exact WS shape.
- **Noise gate** (`LA_CONF_FLOOR`, default `0.40`): on an open mic (iOS especially),
  background noise sometimes gets transcribed into fluent-looking words. Before a
  segment can enter a turn, it is dropped if its ASR confidence (`prob_mean`, not
  `prob_min`, see below) falls below the floor, or if its text is a runaway-repetition
  loop regardless of confidence. A dropped segment is still shown, rendered as a muted
  row (`transcript` gains an additive `discarded` reason), but never becomes part of a
  turn or earns a reply; a pending lab command's confirm/cancel and a would-halt stop
  are exempt so a quiet safety word is never gated. The `0.40` floor and the
  `prob_mean`-not-`prob_min` choice come from 29 operator-labeled clips (see the capture
  mode below); `LA_CONF_FLOOR=0` disables the confidence floor entirely. See
  `doc/INTEGRATION.md` for the exact rules and calibration numbers.
- **Segment capture** (`LA_CAPTURE`, default off, internal testing only): an opt-in
  debug mode that saves every uploaded speech segment as a WAV plus a JSONL record, and
  lets a tester label a transcript noise, another speaker, or real speech, building the
  calibration set the noise gate above was tuned against. `scripts/capture_report.py`
  summarizes it and answers whether a confidence threshold separates noise from speech.
  See `doc/INTEGRATION.md` for the full contract, storage layout, and the privacy note.
- **Identity and access** (`LA_ALLOWLIST`, `LA_OPERATOR_EMAILS`): a connecting client
  sends an email as a `?email=` query parameter, which scopes it to its own private
  session history. `LA_ALLOWLIST` (comma-separated; empty/unset by default, no
  enforcement) is a server-side gate: when set, only a listed email may connect at
  all, everything else gets a clean rejection. `LA_OPERATOR_EMAILS` (also
  comma-separated) marks an email as an operator, with a see-all view across every
  user's session history; an operator email must also be allowlisted to connect when
  the allowlist is enforced. The email is self-asserted, not verified by anything
  (there is no login, no OTP): this gates casual access and keeps each user's history
  private from every other user, but it is not authentication, and the operator email
  functions as a shared secret rather than a login. See `doc/MULTI_CLIENT.md` for the
  full identity, scoping, and trust model.
