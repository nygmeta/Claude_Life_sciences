"""lab-assistant orchestrator + web server.

One port serves the browser front-end (index.html + vendored OmniVAD WASM) AND a
WebSocket. The browser runs VAD locally and streams speech segments; for each one
we run the full pipeline:

    PCM16 segment --> ASR (FunASR-Nano, :8030) --> transcript
                  --> Claude Haiku (HTTPS)      --> reply text (streamed to UI)
                  --> TTS (gepard-1.0, :8040)   --> reply audio (WAV)

and stream the transcript, the reply text (token deltas), and the reply audio back
over the same socket. Reply audio is synthesized and sent one sentence at a time
as the reply streams (LA_TTS_STREAM=1, default), so the first sentence reaches
the client while later ones are still being generated. TTS can run as more than
one backend instance (LA_TTS_MODELS); the assistant path and the TTS playground
each pick a model, and the reply's actual WAV sample rate is read back from the
audio rather than assumed. Single origin so one Cloudflare Tunnel
(your tunnel host -> :8765) gives a remote browser both the HTTPS page
(mic-capable secure context) and the socket.

WS message contract (kept in sync with the handler below):
  client -> server:
    binary frame (one 16 kHz PCM16 speech segment), audio_segment (b64 fallback),
    end_turn, cancel_turn, list_voices, tts_test, list_tts_models, set_tts_model,
    set_tts_params, get_hints, set_hints, list_sessions, get_session,
    rename_session, delete_session, new_session, client_info, label_segment, ping.
  server -> client:
    status, transcript, reply_start, reply_delta, reply_done, reply_audio,
    reply_audio_end, reply_cancelled, capture_state, segment_labeled, error, plus
    hints / voices / tts_* / session responses. When LA_ALLOWLIST rejects a connect
    the ONLY message sent is auth_error {reason: "email_required"|"not_allowlisted"},
    followed by a close (4001).
    Lab mode (LA_LAB_MODE, PART B) adds: action_executed, action_pending,
    action_rejected, action_cancelled, action_halted. `transcript` also carries
    an additive nullable `confidence` block (PART A). `action_pending` carries an
    additive `confirm_phrase` (the exact spoken phrase "confirm <keyword>" an
    IRREVERSIBLE / HAZARDOUS command needs, or null for a reversible pending); a
    bound pending executes ONLY on that phrase, so a stray "yes" cannot fire it.
    Event channel (LA_EVENTS, Phase 3) adds server -> client: announce,
    announce_audio, announce_end (one triple per event); and client -> server:
    inject_event {severity, text, broadcast} (OPERATOR-only; a non-operator gets
    an error).
    Addressed-speech detection (LA_ADDRESSED, Phase 4) adds an additive boolean
    `addressed` to `transcript`: true = spoken to the assistant (the turn proceeds
    exactly as before), false = overheard side speech (shown to the user, but NOT
    accumulated into the turn, so it draws no reply). The field is present ONLY
    when LA_ADDRESSED is on; when off, `transcript` is byte-identical to before.
    Segment capture (LA_CAPTURE, debug/calibration) adds client -> server:
    client_info (device + VAD metadata, resent whenever it changes) and
    label_segment {id, label}; and server -> client: capture_state {on} (sent on
    connect) and segment_labeled {id, label}. See "segment capture" below.
    Noise gate (LA_CONF_FLOOR) adds an additive nullable `discarded`
    ("low_confidence" | "degenerate") to `transcript`: present when the incoming
    segment was dropped as noise (shown to the user greyed, never accumulated into
    a turn, no reply). Absent on an accepted segment. See "noise gate" below.
    Speech-service mode (`?mode=speech` on the connect URL) is PER-CONNECTION and
    purely additive. It adds client -> server: set_lab_state {state}, speak
    {text, voice?}, cancel_speak; and server -> client: transcript_final
    {text, confidence} and transcript_refused {reason, prob_mean, reprompt}. In that
    mode `end_turn` commits the turn WITHOUT any reply pipeline, so reply_start /
    reply_delta / reply_done are never sent and audio comes only from `speak`. A
    connection that does not ask for it is entirely unaffected. See
    "speech-service mode" below.

Identity + scope (multi-client isolation): a connection's identity is the `email`
query param on the WS connect URL (`<ws base>/?email=<addr>`), lowercased + stripped.
Cloudflare Access has been removed; identity comes from the client over wss (TLS).
LA_ALLOWLIST gates who may connect: when it is non-empty, a connection whose email is
missing or not on the list gets one `auth_error` and a clean close (4001), no session.
When it is empty (local dev, direct loopback, curl, every existing smoke) there is no
enforcement, and no email maps to the legacy "public" scope, whose on-disk layout is
unchanged. An email maps to an opaque scope = sha256(lower(email))[:16], and its
sessions live under a per-scope directory; a client only ever sees / renames /
deletes sessions in its OWN scope. An email listed in LA_OPERATOR_EMAILS is an
operator: it can list sessions across ALL scopes and address any scope (an operator
must also be allowlisted to connect when the allowlist is active).
Contract deltas from this feature:
  - session_started gains `scope` (opaque hash or "public") and `is_operator` (bool).
  - list_sessions rows gain `owner` (the owner email, or "public") and `scope` ONLY
    for an operator connection; a non-operator's rows are unchanged (all theirs).
  - get_session / rename_session / delete_session accept an OPTIONAL `scope`
    ("public" or a 16-hex token), honored ONLY for an operator connection. For a
    non-operator it is ignored: every op resolves in the connection's own scope, so
    a cross-scope id returns the same not-found as a bogus id (no existence oracle).
  - ASR hints (hotwords + replacements) are per-scope too, and their messages
    (get_hints / set_hints) keep their exact shapes: only the values are now the
    connection's own. Hints have NO cross-scope operator addressing at all (see
    handle_get_hints), so every connection reads and writes only its own.

Barge-in cancellation: the reply for a turn runs as a background task, so a
cancel_turn (sent when the user talks over the reply) is read and handled while
the reply is still in flight. cancel_turn is safe at any time (a no-op with
nothing in flight); it cancels the reply and emits reply_cancelled with the
partial text streamed so far. reply_cancelled is the TERMINAL message of a
cancelled turn: no reply_audio_end follows (unless the consumer already sent one
before the cancel landed, which the client tolerates). A new end_turn arriving
while a reply is still running supersedes it (same cancel + cleanup) so at most
one reply task runs per session.

Speech-service mode (`?mode=speech`): the orchestrator stops owning the turn and
becomes a pure SPEECH I/O SERVICE for another app (a console that owns the
conversation and the session). Mic -> ASR -> the SAFETY GATES still run here, and
the accepted transcript is handed to that app instead of to an LLM; the app sends
its reply text back as `speak` and we synthesize it. No LLM call and no Lab Agent
call is ever made on this path.

The gates are the entire reason the voice half exists, so they all still apply,
and one of them is load-bearing: the CONFIRMATION FLOOR. The console tells us its
backend's state with set_lab_state, and when that state is armed
(lab_backend.armed, i.e. awaiting_confirmation) its next affirmative EXECUTES A
PHYSICAL PROTOCOL. If ASR is not confident about what it heard, the transcript is
NOT handed over at all (the console would POST it, and the backend would act on
it): the turn is refused with transcript_refused, and we speak the reprompt
ourselves so the user hears WHY nothing happened. Silence after saying "yes" is
precisely the failure mode being avoided. A misheard "yes" must never be able to
start a machine.

Being HEARD and being OBEYED stay different things: while the console is armed, a
low-confidence confirm/cancel is exempt from the noise gate (so it reaches
end_turn and can be refused OUT LOUD) but is still never handed to the console.

Speculative LLM start (LA_SPEC_START, default on): the reply's Claude call is
fired at the SEGMENT boundary (end of handle_segment) instead of waiting for
end_turn, generated silently, and released only when end_turn commits it. The WS
contract is UNCHANGED: the client still sees reply_start / reply_delta /
reply_audio only after its end_turn, exactly as before, just sooner. Until commit
the speculative turn sends nothing and synthesizes nothing; a discarded
speculation is fully invisible (no reply_cancelled, no history, no per-turn log).
A committed speculation behaves identically to a normal committed reply for
barge-in. LA_SPEC_START=0 restores the turn-boundary flow exactly.

Env:
  LA_WS_HOST        0.0.0.0
  LA_WS_PORT        8765
  LA_FUNASR_URL     http://localhost:8030/v1
  LA_FUNASR_MODEL   fun-asr-nano
  LA_ASR_LANG       en
  LA_TTS_URL        http://localhost:8040   (single-model fallback; see LA_TTS_MODELS)
  LA_TTS_MODELS     model-id=url,model-id=url,...  e.g. "gepard-1.0=http://localhost:8040,
                    gepard-1.0-alt=http://localhost:8050". First entry is the default
                    assistant model. Unset -> falls back to {"gepard-1.0": LA_TTS_URL}, so
                    a single-TTS deploy needs no change.
  LA_LLM_MODEL      claude-haiku-4-5
  LA_LLM_MAXTOK     256
  LA_HISTORY_TURNS  6
  LA_TTS_STREAM     1   (per-sentence TTS as the reply streams; "0"/"false"/"" = off)
  LA_SPEC_START     1   (speculative LLM start at the segment boundary; "0"/"false"/""
                        = off, restoring the turn-boundary reply flow exactly)
  LA_SPEC_MAX_TURN_S 12  (skip speculation once the turn has run this long: a dictation
                        guard, so a long monologue does not refire the LLM per segment)
  LA_TTS_TEMP       0.15  (default TTS generation params. Sent to the client as
  LA_TTS_CFG        1.0    the tts_params `defaults` so its controls can seed;
  LA_TTS_TOPK       0      a per-session set_tts_params overrides them, and any
  LA_TTS_MAXFRAMES  1075   field left unset falls through to the TTS backend default.)
  LA_TTS_VOICE      en_oak  (default assistant reference voice. Substituted at synth
                        time when the session's voice is unset AND the turn uses the
                        default model (gepard-1.0); other models keep their own
                        default voice, since this name is a gepard speaker.)
  LA_ADDRESSED      0   (addressed-speech detection; "1"/"true" = on. When on, a
                        segment classified as overheard side speech is transcribed
                        with addressed:false and dropped from the turn.)
  LA_ADDRESSED_TIMEOUT_S 2.0  (cap on the classifier's Claude call; on timeout it
                        fails OPEN, i.e. treats the speech as addressed)
  LA_CAPTURE        0   (debug segment capture; "1"/"true" = on. Saves every
                        uploaded speech segment as a 16 kHz mono WAV plus one JSONL
                        record, so noise that gets transcribed into words can be
                        collected and studied. OFF by default: no disk writes at all.)
  LA_CAPTURE_DIR    ../data/captures   (where capture WAVs + captures.jsonl land)
  LA_CONF_FLOOR     0.40  (incoming-segment noise gate: drop a segment whose ASR
                        prob_mean is below this, or whose text is runaway
                        repetition. "0" disables the confidence floor. Data-tuned
                        on labeled captures; keys on prob_mean, not prob_min.)
  LA_CONFIRM_FLOOR  0.40  (execution floor for a spoken "confirm": below this the
                        confirm is re-prompted, not executed (pending kept). Keys
                        on prob_mean. "0" disables. Missing confidence fails open.)
  LA_PENDING_TTL_S  120  (a pending confirmation older than this is cancelled,
                        not confirmed, on the next turn. "0" disables expiry.)
  LA_ANTHROPIC_API_KEY   (falls back to ../credentials/anthropic_key.txt)
  LA_LOG_FILE       ../data/latency.jsonl   (per-turn / per-synth latency records)
  LA_OPERATOR_EMAILS   comma-separated emails treated as operators (see-all view).
                    Lowercased + stripped. Empty/unset -> no operators.
  LA_ALLOWLIST      comma-separated emails permitted to connect (server-side gate,
                    replaces Cloudflare Access). Lowercased + stripped. Non-empty ->
                    a connection must carry an allowlisted ?email= or it is refused
                    (auth_error + close 4001). Empty/unset -> no enforcement (dev).
"""
import array
import asyncio
import base64
import collections
import datetime
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
import wave
import zlib
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import httpx
from openai import AsyncOpenAI
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

try:                       # imported as the `web.server` module (tests)
    from web import addressed as addressed_mod
    from web import lab_backend
    from web import lab_gate
except ImportError:        # run directly as web/server.py (local smoke)
    import addressed as addressed_mod
    import lab_backend
    import lab_gate

WS_HOST = os.environ.get("LA_WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("LA_WS_PORT", "8765"))
FUNASR_URL = os.environ.get("LA_FUNASR_URL", "http://localhost:8030/v1")
FUNASR_MODEL = os.environ.get("LA_FUNASR_MODEL", "fun-asr-nano")
ASR_LANG = os.environ.get("LA_ASR_LANG", "en")
TTS_URL = os.environ.get("LA_TTS_URL", "http://localhost:8040").rstrip("/")


def _parse_tts_models() -> dict:
    """Parse LA_TTS_MODELS ("model-id=url,model-id=url,...") into an ORDERED
    {model_id: base_url} map; the first entry is the default assistant model.
    Falls back to a single-entry map built from LA_TTS_URL when unset, so an
    existing single-TTS deploy keeps working unchanged."""
    raw = os.environ.get("LA_TTS_MODELS", "").strip()
    if not raw:
        return {"gepard-1.0": TTS_URL}
    models = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        model_id, url = pair.split("=", 1)
        model_id, url = model_id.strip(), url.strip().rstrip("/")
        if model_id and url:
            models[model_id] = url
    return models or {"gepard-1.0": TTS_URL}


TTS_MODELS = _parse_tts_models()
DEFAULT_TTS_MODEL = next(iter(TTS_MODELS))
LLM_MODEL = os.environ.get("LA_LLM_MODEL", "claude-haiku-4-5")
LLM_MAXTOK = int(os.environ.get("LA_LLM_MAXTOK", "256"))
HISTORY_TURNS = int(os.environ.get("LA_HISTORY_TURNS", "6"))
# per-sentence TTS as the reply streams (default on); "0"/"false"/"" = one synth
# after the full reply, for an A/B safety valve during the demo.
TTS_STREAM = os.environ.get("LA_TTS_STREAM", "1").strip().lower() not in ("0", "false", "")
# Speculative LLM start: fire the reply's Claude call at the segment boundary and
# gate ALL client output (and TTS) until end_turn commits it. "0"/"false"/"" = off,
# restoring the turn-boundary flow exactly. SPEC_MAX_TURN_S is a dictation guard:
# once the turn has run this long, stop refiring the LLM per segment.
SPEC_START = os.environ.get("LA_SPEC_START", "1").strip().lower() not in ("0", "false", "")
SPEC_MAX_TURN_S = float(os.environ.get("LA_SPEC_MAX_TURN_S", "12"))
SAMPLE_RATE = 16000
# Default TTS generation params. Sent to the client as `defaults` (the tts_params
# message) so its controls can seed. A per-session set_tts_params overrides these,
# and any param left unset (None) is omitted at synth time so the TTS backend
# applies its own default. Same env names/defaults the TTS service reads.
DEF_TEMP = float(os.environ.get("LA_TTS_TEMP", "0.15"))
DEF_CFG = float(os.environ.get("LA_TTS_CFG", "1.0"))
DEF_TOPK = int(os.environ.get("LA_TTS_TOPK", "0"))
DEF_MAXFRAMES = int(os.environ.get("LA_TTS_MAXFRAMES", "1075"))
# Default assistant reference voice, substituted at synth time only when the
# session left the voice unset AND the turn uses the default model (gepard-1.0);
# DEF_VOICE is a gepard speaker name, so other models keep their own default.
DEF_VOICE = (os.environ.get("LA_TTS_VOICE") or "en_oak").strip()

SYSTEM_PROMPT = (
    "You are a helpful, friendly voice assistant. The user speaks to you and hears "
    "your reply read aloud, so keep replies to 1-3 short spoken sentences. Use plain "
    "text only: no markdown, no bullet lists, no code blocks, no emoji. If a question "
    "needs a long answer, give the short version and offer to go deeper."
)
# Lab-command mode (PART B). When on (default), assistant turns get the lab_command
# tool and the lab-assistant system paragraph, and the confirm/execute gate is live.
# When "0"/"false"/"" everything lab-related is inert and behavior is exactly
# pre-change: no tools passed, base system prompt, no gate, no fast-path stop.
LAB_MODE = os.environ.get("LA_LAB_MODE", "1").strip().lower() not in ("0", "false", "")

# Never let TTS speak text the confirmation gate has not cleared (see the buffer in
# stream_llm's tool loop). Costs nothing on a tool-calling turn, because the model
# emits no text before the tool call anyway. On a turn that calls NO tool (ordinary
# chat while lab mode is on) it does cost the sentence-by-sentence stream: that
# reply is released when generation completes rather than as it is written, so first
# audio waits for the last token. Set to 0 to trade the guarantee back for the
# stream.
STRICT_GATE_AUDIO = os.environ.get("LA_STRICT_GATE_AUDIO", "1").strip().lower() not in ("0", "false", "")
LAB_SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + lab_gate.LAB_SYSTEM_SUFFIX
# Max streaming tool-use round trips per turn, so a mis-behaving loop cannot spin.
LAB_TOOL_MAX_ITERS = 4
# Proactive event channel (Phase 3): the assistant speaks unprompted when the lab
# produces an event (an operator inject or a stub timed completion). LA_EVENTS
# gates the whole channel; the stub completion timers additionally require
# LAB_MODE. When off, no announce message is ever sent and inject_event no-ops.
EVENTS_ENABLED = os.environ.get("LA_EVENTS", "1").strip().lower() not in ("0", "false", "")
# Addressed-speech detection (Phase 4). An open mic in a lab hears colleagues
# talking to each other; when this is on, a segment classified as side speech is
# transcribed and shown (transcript carries addressed:false) but never becomes part
# of a turn: no accumulation, no speculation, no reply. Default OFF: the classifier
# adds a Haiku call in front of the turn (about 1 s when no fast path fires), so it
# is opt-in until it has been tuned on live mic audio. Off = byte-identical to
# pre-change behavior, down to the absence of the `addressed` transcript field.
ADDRESSED_ENABLED = os.environ.get("LA_ADDRESSED", "0").strip().lower() not in ("0", "false", "")
# Debug segment capture (calibration set for a planned server-side VAD/AED gate).
# Default OFF, and OFF means OFF: not one byte is written to disk, capture_state
# reports on=false, and label_segment is a no-op. Internal testing only.
CAPTURE_ENABLED = os.environ.get("LA_CAPTURE", "0").strip().lower() not in ("0", "false", "")
# Incoming-segment noise gate (data-calibrated: see the gate_segment comment). A
# segment whose transcript is runaway-repetition ("degenerate") OR whose ASR
# prob_mean is below this floor is dropped before it can enter a turn. Default
# 0.40; "0" disables the confidence floor entirely (the degenerate check has no
# floor and is unaffected). Safety/control words are exempt (see _gate_exempt).
CONF_FLOOR = float(os.environ.get("LA_CONF_FLOOR", "0.40"))
# Confirmation-execution confidence floor (review fix F2). A spoken "confirm" whose
# turn prob_mean is below this is HEARD but not clearly enough to EXECUTE: the
# pending stays armed and the user is re-prompted (the word is never dropped or
# auto-cancelled). Keys on prob_mean, matching the noise gate, because the
# calibration showed prob_min overlaps between speech and noise. Default 0.40; "0"
# disables. Missing confidence fails OPEN (executes): the mock and a degraded ASR
# supply no confidence, and must not be locked out of confirming.
CONFIRM_FLOOR = float(os.environ.get("LA_CONFIRM_FLOOR", "0.40"))
# Pending-confirmation expiry (review fix F4). A pending lab command older than this
# (by its created_ts) is cancelled instead of confirmed, so a "confirm" heard long
# after the readback cannot fire an action the user has moved on from. Default
# 120 s; "0" disables.
PENDING_TTL_S = float(os.environ.get("LA_PENDING_TTL_S", "120"))

STATIC_DIR = Path(__file__).resolve().parent
asr_client = AsyncOpenAI(base_url=FUNASR_URL, api_key="unused")
tts_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
try:
    SGT = ZoneInfo("Asia/Singapore")
except Exception:  # noqa: BLE001  # tzdata may be absent in a minimal container
    SGT = datetime.timezone(datetime.timedelta(hours=8))
LOG_FILE = Path(os.environ.get("LA_LOG_FILE", str(STATIC_DIR.parent / "data" / "latency.jsonl")))


def _parse_emails(env_name) -> frozenset:
    """Parse a comma-separated email env ("a@x.com,b@y.com") into a frozenset of
    lowercased, stripped addresses."""
    raw = os.environ.get(env_name, "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


# Operators get the see-all view (orthogonal to the allowlist: an operator must ALSO
# be allowlisted to connect when the allowlist is active).
OPERATOR_EMAILS = _parse_emails("LA_OPERATOR_EMAILS")
# Server-side email allowlist (replaces Cloudflare Access). NON-EMPTY = enforce: a
# connection must supply an `?email=` that is on the list or it is rejected with an
# auth_error and the socket is closed; no session, no public fallback. EMPTY/unset
# (default, dev + smokes) = no enforcement, identity is still sourced from `?email=`
# (per-user scope) and its absence maps to the public scope, exactly as before.
ALLOWLIST = _parse_emails("LA_ALLOWLIST")


def _load_anthropic_key() -> str | None:
    key = os.environ.get("LA_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    cred = STATIC_DIR.parent / "credentials" / "anthropic_key.txt"
    if cred.is_file():
        return cred.read_text().strip()
    return None


_anthropic_key = _load_anthropic_key()
llm_client = anthropic.AsyncAnthropic(api_key=_anthropic_key) if _anthropic_key else None


# ----------------------------------------------------------------------------- ASR hints
# Hotwords bias FunASR toward domain terms (sent as the OpenAI `prompt`, split on
# commas by the ASR server into model.generate(hotwords=[...])). Replacements are
# post-ASR text fixes {from: to} applied to every transcript. Both are hot (edited
# live via the WS get_hints/set_hints messages).
#
# Hints are PER-SCOPE, using the same identity model as sessions (see below): one
# file per scope under HINTS_DIR, named for the scope ("public" or a 16-hex token).
# They deliberately do NOT reuse the sessions layout: the public scope's session
# directory is FLAT and globbed for *.json, so a hints file living there would sit
# in the middle of a session listing. A separate directory keeps the two trees from
# ever aliasing. There is no legacy hints layout to preserve: the old single global
# data/asr_hints.json is retired (moved aside, never migrated), so every scope,
# public included, starts from DEFAULT_HINTS.
HINTS_DIR = STATIC_DIR.parent / "data" / "hints"
DEFAULT_HINTS = {"hotwords": ["Claude"], "replacements": {"cloud code": "Claude Code"}}
# NOTE: seeding the lab vocabulary as hotwords here was TRIED and REVERTED. FunASR takes
# hotwords as a decoding prompt, and a 14-term domain list did not bias the decode, it
# derailed it: on the real stack a clean utterance came back as unrelated text ("the top
# card on the top was for the sample photo...") and prob_mean collapsed from 0.91 to
# 0.23. Hotwords are a scalpel (one or two terms), not a glossary. Domain vocabulary is
# fixed AFTER recognition instead, in lab_backend.normalize_transcript.
# scope -> its hints dict. transcribe() runs on EVERY VAD segment, so the hot path
# must never stat/parse a file: a scope is read from disk (or defaulted) once, on
# first use, and the entry is REPLACED on every set_hints, which is the only
# invalidation this cache needs (a scope's file is written by nothing else).
_HINTS_CACHE = {}


def _default_hints() -> dict:
    """A fresh COPY of the defaults. Never hand out DEFAULT_HINTS itself, or one
    scope's edit (or an in-place mutation anywhere) would leak into every other
    scope that has not saved hints yet."""
    return {"hotwords": list(DEFAULT_HINTS["hotwords"]),
            "replacements": dict(DEFAULT_HINTS["replacements"])}


def _normalize_hints(data) -> dict:
    """Coerce a raw hints payload (from disk or from a client) into the stored
    shape: non-empty stripped hotword strings, string->string replacements."""
    data = data if isinstance(data, dict) else {}
    return {"hotwords": [str(w).strip() for w in (data.get("hotwords") or []) if str(w).strip()],
            "replacements": {str(k): str(v) for k, v in (data.get("replacements") or {}).items()
                             if str(k).strip()}}


def _hints_path(scope):
    """The hints file for a scope, or None if the scope is not "public" or a 16-hex
    token. Same guard as _session_path: an untrusted string never becomes a path
    fragment. (Scopes are always server-derived, so None means a bug, not a client.)"""
    if scope != "public" and not _SCOPE_RE.match(scope or ""):
        return None
    return HINTS_DIR / f"{scope}.json"


def load_hints(scope):
    """One scope's SAVED hints, or None if it has never saved any. None (no file)
    and {"hotwords": [], "replacements": {}} (a saved empty list) are different:
    an explicit empty list is a user choice and must NOT fall back to the defaults."""
    path = _hints_path(scope)
    if path is None:
        return None
    try:
        return _normalize_hints(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[hints] load failed ({scope}): {e}", flush=True)
        return None


def hints_for(scope) -> dict:
    """The live hints for one scope: a cached dict lookup on the hot path, filled
    from disk (or the defaults) on first use."""
    hints = _HINTS_CACHE.get(scope)
    if hints is None:
        hints = load_hints(scope)
        if hints is None:
            hints = _default_hints()
        _HINTS_CACHE[scope] = hints
    return hints


def save_hints(scope, hints) -> dict:
    """Persist one scope's hints and refresh its cache entry. The cache is updated
    even if the write fails, so the connection's own view stays consistent with what
    it just set."""
    _HINTS_CACHE[scope] = hints
    path = _hints_path(scope)
    if path is None:
        return hints
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hints, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[hints] save failed ({scope}): {e}", flush=True)
    return hints


def apply_replacements(text: str, hints: dict) -> str:
    for src, dst in hints["replacements"].items():
        if src:
            text = text.replace(src, dst)
    return text.strip()


# ----------------------------------------------------------------------------- session persistence
# Each Session gets its own JSON file under its scope directory: identity fields
# (name, owner_email, timestamps) plus the FULL, never-truncated transcript, so a
# session list and a resumable transcript view can be built without touching the
# live pipeline's bounded LLM context (Session.history). The file is written on
# the FIRST message (Session.add -> save_session), not on connect, so a
# zero-message session (an auto-reconnect blip, a page reload) never litters the
# directory. `number` is NOT stored: it is a positional rank derived at read time
# from creation order (see _ranked_sessions), so deleting a session renumbers the
# rest instead of leaving a permanent hole in the sequence.
#
# Scoping (multi-client isolation): the "public" scope keeps the legacy FLAT
# layout (SESSIONS_DIR/{sid}.json), unchanged for back-compat; a verified email
# maps to a 16-hex scope whose sessions live under SESSIONS_DIR/{scope}/{sid}.json.
# _scope_dir + _session_path are the ONLY path builders and are the sole guard
# between a WS client and arbitrary file access.
SESSIONS_DIR = STATIC_DIR.parent / "data" / "sessions"

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}$")
# A scope is either the literal "public" (legacy flat layout) or 16 lowercase hex
# chars (sha256(lower(email))[:16]). Any other value is untrusted and must never
# be turned into a path fragment.
_SCOPE_RE = re.compile(r"^[0-9a-f]{16}$")
# A pre-rework auto-name baked the old sequence number into the name
# ("Session 7"). On read that is treated as never-named, so such a session can
# be titled or renamed like any other instead of showing a stale ordinal.
_LEGACY_NAME_RE = re.compile(r"^Session \d+$")


def _read_client_email(ws):
    """The client-supplied email from the WS connect query string (`?email=`),
    lowercased + stripped, or None when absent/blank.

    Identity now comes from the CLIENT, not a Cloudflare-Access header (Access has
    been removed at the edge; that header will never arrive again). This value is
    therefore NOT proof of identity on its own: the LA_ALLOWLIST check in handler()
    is what gates who may connect, and the transport is wss (TLS), so the query
    string is encrypted in flight. _scope_for_email turns the email into an opaque
    per-user scope hash, so a user only ever sees their own sessions."""
    try:
        path = ws.request.path
    except Exception:  # noqa: BLE001  # no request/path (shouldn't happen post-handshake)
        return None
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(path or "").query).get("email")
    except Exception:  # noqa: BLE001
        return None
    if not values:
        return None
    email = (values[0] or "").strip().lower()
    return email or None


def _read_client_mode(ws):
    """The `mode` query param on the WS connect URL (`<ws base>/?mode=speech`),
    lowercased + stripped, or None when absent/blank. Same parse as the email above:
    the connect URL is where a client declares what KIND of connection it wants, and
    the choice is fixed for the life of the connection.

    Only "speech" is meaningful (see speech_mode below). Anything else, including a
    missing param, leaves the connection on the default conversational path, so this
    is invisible to every existing client and every smoke."""
    try:
        path = ws.request.path
    except Exception:  # noqa: BLE001  # no request/path (shouldn't happen post-handshake)
        return None
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(path or "").query).get("mode")
    except Exception:  # noqa: BLE001
        return None
    if not values:
        return None
    mode = (values[0] or "").strip().lower()
    return mode or None


def _scope_for_email(email):
    """Map a verified email to (scope, owner_email, is_operator). No/empty email
    -> the public legacy scope (None owner, not operator). Otherwise the scope is
    an opaque sha256(lower(email))[:16] so the raw address never becomes a
    directory name and path traversal is structurally impossible."""
    if not email:
        return "public", None, False
    owner = email.strip().lower()
    scope = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    return scope, owner, owner in OPERATOR_EMAILS


def _scope_dir(scope):
    """The directory holding a scope's session files. "public" -> the legacy FLAT
    SESSIONS_DIR (on-disk layout unchanged); a 16-hex scope -> SESSIONS_DIR/scope.
    Any other value is untrusted: raise rather than build a path from it."""
    if scope == "public":
        return SESSIONS_DIR
    if _SCOPE_RE.match(scope or ""):
        return SESSIONS_DIR / scope
    raise ValueError(f"invalid scope: {scope!r}")


def _session_path(scope, sid):
    """Resolve a (scope, client-supplied sid) to its file path, or None if either
    fails validation. This stays THE ONLY thing between a WS client and arbitrary
    file read/write/delete: sid must match the server's own id shape (path-traversal
    guard, unchanged) AND scope must be "public" or a 16-hex token."""
    if not _SESSION_ID_RE.match(sid or ""):
        return None
    try:
        return _scope_dir(scope) / f"{sid}.json"
    except ValueError:
        return None


def _is_legacy_auto_name(name) -> bool:
    """True for a pre-rework auto-name ("Session N"), which is equivalent to
    never-named. Applied on every read path so a legacy file is titled and
    displayed like an unnamed session, not treated as a user-chosen name."""
    return bool(name) and bool(_LEGACY_NAME_RE.match(name))


def _started_key(value):
    """Sort key for session ordering: parse an ISO started_at into a comparable,
    timezone-aware datetime. Both the old second-precision and the new
    microsecond-precision values parse, so existing files keep sorting
    correctly. A missing/unparseable value sorts oldest."""
    try:
        dt = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=SGT)


def _ranked_sessions(scope, sess=None) -> list:
    """Sessions in ONE scope, oldest first, each tagged with its 1-based `number`
    rank. Lists _scope_dir(scope)/*.json only (glob does not recurse, so the public
    scope never picks up scoped subdirs' files), maps a legacy "Session N" name to
    None, and merges in the given live session ONLY when it shares this scope and
    has no file yet (lazy persistence) so a just-started conversation still gets a
    rank. Rank is positional and computed here, never stored.

    The rank is RELATIVE TO THIS `sess` view: a message-less live session has no
    file, so it only appears (and only pushes later sessions down a rank) when it
    is merged in here. Every message that reports a rank (sessions / session_data
    / session_started) must therefore pass the SAME per-connection `sess`, or the
    banner and the picker can disagree once a second client's newer session lands
    after this client's un-persisted live one."""
    rows = []
    seen = set()
    try:
        sdir = _scope_dir(scope)
    except ValueError:
        sdir = None
    if sdir is not None and sdir.is_dir():
        for path in sdir.glob("*.json"):
            sid = path.stem
            if _session_path(scope, sid) is None:   # same path-traversal guard as every read
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            name = data.get("name")
            rows.append({"id": sid, "name": None if _is_legacy_auto_name(name) else name,
                         "started_at": data.get("started_at")})
            seen.add(sid)
    if sess is not None and sess.scope == scope and sess.sid not in seen:
        # live session with no file yet: surface it from memory so an in-progress
        # conversation appears in the list and holds a stable rank.
        rows.append({"id": sess.sid, "name": sess.name, "started_at": sess.started_at})
    rows.sort(key=lambda r: (_started_key(r["started_at"]), r["id"]))   # id tiebreak = deterministic
    for i, r in enumerate(rows, start=1):
        r["number"] = i
    return rows


def _ranked_all_scopes(sess=None) -> list:
    """Operator-only aggregate across ALL scopes, oldest first, positionally ranked.
    Rows carry `owner` (the file's owner_email, or "public") and `scope` (the scope
    the row came from). Public sessions are the flat SESSIONS_DIR/*.json (glob does
    not recurse); scoped sessions are each SESSIONS_DIR/<16hex>/*.json, so public
    and scoped are never double-counted. The operator's own live session is merged
    in even before it has a file, exactly like _ranked_sessions."""
    rows = []
    seen = set()   # (scope, sid)

    def _collect(scope, sdir):
        for path in sdir.glob("*.json"):
            sid = path.stem
            if _session_path(scope, sid) is None:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            name = data.get("name")
            rows.append({"id": sid, "name": None if _is_legacy_auto_name(name) else name,
                         "started_at": data.get("started_at"),
                         "owner": data.get("owner_email") or ("public" if scope == "public" else scope),
                         "scope": scope})
            seen.add((scope, sid))

    if SESSIONS_DIR.is_dir():
        _collect("public", SESSIONS_DIR)          # flat public files
        for sub in SESSIONS_DIR.iterdir():        # each scoped subdir
            if sub.is_dir() and _SCOPE_RE.match(sub.name):
                _collect(sub.name, sub)
    if sess is not None and (sess.scope, sess.sid) not in seen:
        rows.append({"id": sess.sid, "name": sess.name, "started_at": sess.started_at,
                     "owner": sess.owner_email or ("public" if sess.scope == "public" else sess.scope),
                     "scope": sess.scope})
    rows.sort(key=lambda r: (_started_key(r["started_at"]), r["id"]))
    for i, r in enumerate(rows, start=1):
        r["number"] = i
    return rows


def _effective_scope(sess, msg) -> str:
    """The scope a get/rename/delete op resolves in. THE KEY SECURITY RULE: a
    non-operator is ALWAYS pinned to its own connection scope, so any client-supplied
    `scope` field is ignored and it can never reach another client's data. An
    operator MAY target any scope via an optional `scope` field ("public" or a
    16-hex token); anything else falls back to the operator's own scope."""
    if not sess.is_operator:
        return sess.scope
    req = msg.get("scope")
    if req == "public" or (isinstance(req, str) and _SCOPE_RE.match(req)):
        return req
    return sess.scope


def _session_rank(sess) -> int:
    """The live session's current 1-based rank WITHIN ITS OWN SCOPE, derived
    exactly like the picker, so session_started always agrees with the sessions
    list. Ranked in the connection's own scope even for an operator: session_started
    is about the connection's live session, not the aggregate view."""
    for r in _ranked_sessions(sess.scope, sess):
        if r["id"] == sess.sid:
            return r["number"]
    return 1   # unreachable: the live session is always merged in


# ----------------------------------------------------------------------------- audio
def pcm16_to_wav(pcm_bytes: bytes, filename: str = "audio.wav") -> io.BytesIO:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    buf.seek(0)
    buf.name = filename
    return buf


# Local-smoke seam only: a segment that is exactly an "utter;text=...;pmin=...;
# pmean=....wav" token is uploaded under that filename so the mock ASR can return a
# scripted transcript + confidence (see web/mock_asr_tts.py). Real 16 kHz PCM never
# starts with this ASCII prefix, so this is inert in production.
_ASR_OVERRIDE_PREFIX = b"utter;text="


def _upload_filename(pcm_bytes: bytes) -> str:
    if pcm_bytes[:len(_ASR_OVERRIDE_PREFIX)] == _ASR_OVERRIDE_PREFIX:
        try:
            return pcm_bytes.decode("ascii")
        except UnicodeDecodeError:
            pass
    return "audio.wav"


def _read_confidence(resp):
    """Pull the additive nullable ASR confidence block off a Transcription. The
    SDK parses it into the pydantic model's extras, reachable either as an
    attribute (extra='allow') or via model_extra; fall back robustly to both so a
    future SDK that drops one path still works."""
    conf = getattr(resp, "confidence", None)
    if conf is None:
        conf = (getattr(resp, "model_extra", None) or {}).get("confidence")
    return conf


async def transcribe(pcm_bytes: bytes, language: str, hints: dict):
    """Transcribe one segment with the CALLER'S hints (per-scope: never a global
    read, so one client's hotwords can never bias another's audio). Returns
    (text, confidence): confidence is the ASR service's additive block
    {logprob_mean, logprob_min, prob_mean, prob_min, tokens} or None when the
    service did not supply one."""
    wav = pcm16_to_wav(pcm_bytes, filename=_upload_filename(pcm_bytes))
    hotwords = ",".join(hints["hotwords"]) or None   # biases FunASR toward these terms
    resp = await asr_client.audio.transcriptions.create(
        model=FUNASR_MODEL, file=wav, language=language,
        prompt=hotwords, response_format="json",
    )
    return (resp.text or "").strip(), _read_confidence(resp)


def _tts_base_url(model: str | None) -> str:
    """Resolve a model-id to its backend base URL; unknown/None -> default model."""
    return TTS_MODELS.get(model) or TTS_MODELS[DEFAULT_TTS_MODEL]


async def synthesize(text: str, model: str | None = None, **params) -> bytes:
    body = {"text": text}
    body.update({k: v for k, v in params.items() if v is not None})
    r = await tts_client.post(f"{_tts_base_url(model)}/synthesize", json=body)
    r.raise_for_status()
    return r.content


async def list_voices(model: str | None = None) -> list:
    r = await tts_client.get(f"{_tts_base_url(model)}/voices")
    r.raise_for_status()
    return r.json().get("voices", [])


# ----------------------------------------------------------------------------- latency logging
# Every assistant turn and every playground synth is logged as one JSON line with
# per-component timings, so total latency can be reconstructed offline:
#   assistant  = asr (per VAD segment) + llm (ttft + total) + tts (per-sentence
#                sum + chunk count + model, the TTS backend the turn used),
#                plus first_audio_ms (end_turn -> FIRST reply_audio, the
#                perceived-latency headline), stream (whether TTS_STREAM was
#                on), reply_latency_ms (end_turn -> LAST reply_audio) and
#                total_ms (first speech -> LAST reply_audio)
#   playground = tts only (custom text, chosen model + voice + config)
# Durations use perf_counter (monotonic); the wall-clock ts is SGT.
def _ms(t0: float) -> float:
    """Elapsed milliseconds since a time.perf_counter() mark."""
    return round((time.perf_counter() - t0) * 1000, 1)


def _rtf(audio_s, ms):
    """TTS real-time factor: seconds of audio produced per second of compute
    (>1 = faster than real time). None when either input is missing."""
    if audio_s and ms:
        return round(audio_s / (ms / 1000.0), 2)
    return None


def wav_duration_s(wav_bytes: bytes):
    """Duration in seconds of a WAV byte string, or None if it cannot be parsed."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return round(wf.getnframes() / float(wf.getframerate()), 3)
    except Exception:  # noqa: BLE001
        return None


def wav_sample_rate(wav_bytes: bytes):
    """Sample rate (Hz) of a WAV byte string, or None if it cannot be parsed.
    Different TTS backends may emit different rates, so this keeps the
    reply_audio `sample_rate` field honest instead of a hardcoded guess."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return wf.getframerate()
    except Exception:  # noqa: BLE001
        return None


def log_event(event: dict) -> None:
    """Append one latency record to LOG_FILE (JSONL) and echo a concise stdout line.
    Never raises: telemetry must not break the pipeline."""
    event = {"ts": datetime.datetime.now(SGT).isoformat(timespec="milliseconds"), **event}
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"[lat] write failed: {e}", flush=True)
    try:
        if event.get("kind") == "assistant":
            a, l, t = event.get("asr") or {}, event.get("llm") or {}, event.get("tts") or {}
            print(f"[lat] assistant sid={event.get('session')} turn={event.get('turn')} "
                  f"status={event.get('status')} asr={a.get('total_ms')}ms(x{a.get('segments')}) "
                  f"llm={l.get('ms')}ms(ttft={l.get('ttft_ms')}) "
                  f"tts={t.get('ms')}ms(x{t.get('chunks')},model={t.get('model')}) "
                  f"first_audio={event.get('first_audio_ms')}ms "
                  f"reply={event.get('reply_latency_ms')}ms total={event.get('total_ms')}ms", flush=True)
        elif event.get("kind") == "playground":
            t = event.get("tts") or {}
            print(f"[lat] playground sid={event.get('session')} status={event.get('status')} "
                  f"model={t.get('model')} voice={t.get('voice')} chars={t.get('chars')} "
                  f"tts={t.get('ms')}ms audio={t.get('audio_s')}s rtf={t.get('rtf')}", flush=True)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------- segment capture
# WHY: on iOS, background noise sometimes gets transcribed into words (now and then
# even into a hotword). No filter can be tuned without the offending audio, so this
# is an opt-in (LA_CAPTURE) debug mode that saves every uploaded speech segment plus
# the metadata needed to correlate one device against another, and lets a tester
# LABEL a transcript as noise. The result is the calibration set for a planned
# server-side VAD/AED gate. Internal testing only, with the testers' consent.
#
# Layout, under CAPTURES_DIR (default ../data/captures, gitignored):
#   <YYYY-MM-DD>/<sid>-<seq>.wav   the EXACT 16 kHz PCM16 bytes sent to the ASR,
#                                  wrapped as a mono WAV (no re-encode). `seq` is
#                                  the transcript id the user sees, so the clip,
#                                  the JSONL record and the on-screen line all join.
#   captures.jsonl                 one record per segment (kind="segment"), plus
#                                  one record per label action (kind="label").
#
# Two invariants:
#   1. Capture NEVER costs the user a turn. Every disk touch is inside try/except,
#      and a full disk or a permissions error logs a warning and the turn proceeds.
#   2. Capture stays OFF the latency path. capture_segment() only builds a small
#      argument dict and spawns a task; the level math, the WAV encode and the
#      JSONL append all run in a worker thread (asyncio.to_thread), so the
#      speculative LLM call right behind it is never held up by disk I/O.
#
# The file is APPEND-ONLY. A label is therefore not an in-place edit (that would
# race with concurrent appends) but a new kind="label" record; readers fold the
# LAST label per (sid, seq). See scripts/capture_report.py, which is the consumer.
CAPTURES_DIR = Path(os.environ.get("LA_CAPTURE_DIR", str(STATIC_DIR.parent / "data" / "captures")))
CAPTURES_LOG = CAPTURES_DIR / "captures.jsonl"
CAPTURE_LABELS = ("noise", "other_speaker", "speech")
# One writer lock for captures.jsonl: the appends happen on worker threads, so a
# short lock keeps two records from interleaving mid-line.
_CAPTURE_LOCK = threading.Lock()
# Strong refs to in-flight capture tasks: a bare create_task result can be GC'd
# mid-flight. Discarded on completion.
_CAPTURE_TASKS = set()
# Caps on the client-supplied client_info, which is untrusted and goes to disk.
_CI_MAX_STR = 300
_CI_KEYS = ("ua", "platform", "hw_sample_rate", "resampled", "vad_threshold",
            "seg_pause_ms", "turn_pause_ms", "viewport")


def _pcm_levels(pcm: bytes):
    """(rms, peak) of a PCM16 buffer, each normalized to 0..1 (full scale = 1.0).
    Cheap, and they let the calibration set be sorted by loudness later: noise
    clips and speech clips separate partly on level alone. Runs on a worker
    thread, never on the event loop."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])   # PCM16: whole samples only
    if sys.byteorder == "big":
        samples.byteswap()   # the wire format is little-endian
    n = len(samples)
    if not n:
        return 0.0, 0.0
    peak = max(max(samples), -min(samples)) / 32768.0
    rms = (sum(s * s for s in samples) / n) ** 0.5 / 32768.0
    return round(rms, 5), round(min(peak, 1.0), 5)


def _capture_scalar(v, max_str: int = _CI_MAX_STR):
    """Coerce one client_info value into something safe to store: bools and finite
    numbers pass through, strings are truncated, a shallow dict/list of scalars is
    kept (viewport may arrive either as "390x844" or as {w, h}), anything else is
    dropped. client_info is CLIENT-SUPPLIED and is metadata only: it is never
    trusted for a decision, only recorded."""
    if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
        return v
    if isinstance(v, str):
        return v[:max_str]
    if isinstance(v, dict):
        return {str(k)[:40]: _capture_scalar(x, 80) for k, x in list(v.items())[:8]
                if isinstance(x, (str, int, float, bool)) or x is None}
    if isinstance(v, list):
        return [_capture_scalar(x, 80) for x in v[:8]
                if isinstance(x, (str, int, float, bool)) or x is None]
    return None


def sanitize_client_info(msg) -> dict:
    """The stored shape of a client_info message: the known keys only, each value
    coerced and capped. Unknown keys are dropped, so a client cannot grow the
    on-disk record."""
    msg = msg if isinstance(msg, dict) else {}
    return {k: _capture_scalar(msg.get(k)) for k in _CI_KEYS}


def build_capture_record(*, sid, scope, seq, cap_n, pcm, transcript, confidence,
                         hotwords, addressed, accepted, client_info, error=None,
                         reject_reason=None, now=None) -> dict:
    """The JSONL record for one captured segment. PURE: no I/O, no globals beyond
    the clock, so it is directly unit-testable.

    `seq` is the transcript id, and is None for a segment that never produced a
    transcript (ASR error, or an empty result). Those cannot be labeled (the user
    has no line to click), but they are exactly the clips worth keeping, so they
    are still captured, named from `cap_n` (the session's monotonic capture index)
    instead of the missing seq.
    """
    now = now or datetime.datetime.now(SGT)
    date = now.strftime("%Y-%m-%d")
    rms, peak = _pcm_levels(pcm)
    name = f"{sid}-{seq}.wav" if seq is not None else f"{sid}-n{cap_n}.wav"
    return {
        "kind": "segment",
        "ts": now.isoformat(timespec="milliseconds"),
        "date": date,
        "sid": sid,
        "scope": scope,
        "seq": seq,
        "wav": f"{date}/{name}",          # relative to CAPTURES_DIR (next to captures.jsonl)
        "dur_s": round(len(pcm) // 2 / float(SAMPLE_RATE), 3),
        "rms": rms,
        "peak": peak,
        "transcript": transcript,
        "confidence": confidence,          # the ASR block, or None
        "hotwords_active": list(hotwords or []),
        "addressed": addressed,            # the classifier verdict, or None when LA_ADDRESSED is off
        "accepted": bool(accepted),        # did this segment enter the turn?
        "reject_reason": reject_reason,    # why the noise gate dropped it, or None when accepted
        "client_info": client_info,        # the connection's latest, or None
        "label": None,                     # set later by a kind="label" record, never in place
        "error": error,                    # e.g. "TimeoutError" when the ASR call failed
    }


def _append_capture_line(rec: dict) -> None:
    """Append one record to captures.jsonl. Never raises: a capture failure must
    cost a warning, not the user's turn."""
    try:
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with _CAPTURE_LOCK:
            with CAPTURES_LOG.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:  # noqa: BLE001
        print(f"[capture] jsonl append failed: {e}", flush=True)


def _write_capture(pcm: bytes, rec: dict) -> None:
    """Worker-thread body: save the WAV, then append the record. The WAV wraps the
    EXACT PCM16 bytes that went to the ASR (a header, not a re-encode), so the clip
    is bit-for-bit what the model heard. A WAV failure still records the row (the
    metadata is worth having without the audio)."""
    try:
        path = CAPTURES_DIR / rec["wav"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pcm16_to_wav(pcm[:len(pcm) - (len(pcm) % 2)]).getvalue())
    except Exception as e:  # noqa: BLE001
        print(f"[capture] wav write failed ({rec.get('wav')}): {e}", flush=True)
    _append_capture_line(rec)


def _capture_worker(pcm: bytes, kwargs: dict) -> None:
    """Everything expensive about a capture, on a worker thread: the level math
    (a pass over every sample) and both disk writes."""
    try:
        rec = build_capture_record(pcm=pcm, **kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"[capture] record build failed: {e}", flush=True)
        return
    _write_capture(pcm, rec)


def capture_segment(sess, pcm: bytes, *, seq, transcript, confidence, hotwords,
                    addressed, accepted, error=None, reject_reason=None) -> None:
    """Capture one segment, if capture mode is on. Returns IMMEDIATELY: the caller
    is on the latency path (the speculative LLM call fires right behind it), so all
    this does is bump the session's capture index and hand the work to a thread.
    A no-op, with zero disk contact, when LA_CAPTURE is off."""
    if not CAPTURE_ENABLED:
        return
    sess.cap_n += 1
    if seq is not None:
        sess.captured.add(seq)   # only a captured seq can be labeled (see handle_label_segment)
    kwargs = {"sid": sess.sid, "scope": sess.scope, "seq": seq, "cap_n": sess.cap_n,
              "transcript": transcript, "confidence": confidence,
              "hotwords": list(hotwords or []), "addressed": addressed,
              "accepted": accepted, "client_info": sess.client_info, "error": error,
              "reject_reason": reject_reason}
    try:
        task = asyncio.create_task(asyncio.to_thread(_capture_worker, pcm, kwargs))
    except RuntimeError:   # no running loop (a direct call outside the server)
        _capture_worker(pcm, kwargs)
        return
    _CAPTURE_TASKS.add(task)
    task.add_done_callback(_CAPTURE_TASKS.discard)


def build_label_record(sid, seq, label, now=None) -> dict:
    """The append-only label record. captures.jsonl is never rewritten in place (a
    rewrite would race with the concurrent appends above), so a label is a new line
    and readers take the LAST label per (sid, seq)."""
    now = now or datetime.datetime.now(SGT)
    return {"kind": "label", "ts": now.isoformat(timespec="milliseconds"),
            "sid": sid, "seq": int(seq), "label": label}


# ----------------------------------------------------------------------------- ws
# Per-session assistant-path TTS generation params. None = unset -> omitted at
# synth time so the TTS backend applies its own default. voice None likewise
# means "let the backend resolve its default reference voice".
_TTS_PARAM_KEYS = ("voice", "temperature", "cfg_scale", "top_k", "max_frames")


class Session:
    __slots__ = ("history", "pending", "seq", "sid", "asr_ms", "asr_conf", "turn_t0",
                 "tts_model", "tts_params", "messages", "name", "started_at",
                 "reply_task", "reply_ctx", "spec_fired", "spec_discarded",
                 "scope", "owner_email", "is_operator", "pending_action", "lab_stub",
                 "client_info", "cap_n", "captured", "lab_backend",
                 "speech_mode", "lab_state", "speak_task")

    def __init__(self):
        self.history = []   # [{"role": "user"|"assistant", "content": str}, ...]
        self.pending = []   # transcript segments of the in-progress user turn
        self.seq = 0
        self.sid = uuid.uuid4().hex[:8]   # correlate a connection's log records
        self.asr_ms = []                  # per-segment ASR ms for the in-progress turn
        self.asr_conf = []                # per-segment ASR confidence block (or None), parallel to asr_ms
        self.turn_t0 = None               # perf_counter at the turn's first segment
        # In-flight reply for barge-in cancellation. At most one turn's reply
        # work runs at a time: `reply_task` is the background asyncio.Task (LLM
        # producer + TTS consumer) or None; `reply_ctx` carries what the cancel
        # path needs after the task is cancelled (the partial-text chunk list the
        # producer appends to, the asr record, and the turn's timing marks).
        self.reply_task = None
        self.reply_ctx = None
        # Speculative-start accounting, counted per committed turn (reset at commit).
        # spec_fired: speculative task spawns since the last commit; spec_discarded:
        # speculations aborted without committing (refires + abandoned turns). A
        # committed turn's log reports these, then clears them; abandoned discards
        # carry forward into the next committed turn (spec principle 5).
        self.spec_fired = 0
        self.spec_discarded = 0
        self.tts_model = DEFAULT_TTS_MODEL  # assistant-path TTS model; set_tts_model changes it
        self.tts_params = {k: None for k in _TTS_PARAM_KEYS}  # set_tts_params overrides; None = backend default
        # `messages` is the persisted transcript: it never gets trimmed, unlike
        # `history` above which stays bounded to what the LLM call needs as context.
        self.messages = []
        # name is None until the user renames it or Claude titles it on session
        # end (autoname_session); None is the "never named" sentinel. NOT persisted
        # here: persistence is lazy (first add() -> save_session), so a zero-message
        # session never reaches disk. microsecond precision so two sessions created
        # in the same second still order deterministically when ranked.
        self.name = None
        self.started_at = datetime.datetime.now(SGT).isoformat(timespec="microseconds")
        # Identity is per-CONNECTION, not per-Session: set by _apply_scope right
        # after the connect header is resolved and re-applied to every new_session
        # Session on the same connection. Defaults are the public legacy scope so a
        # Session used off the connect path (should not happen) never escapes it.
        self.scope = "public"
        self.owner_email = None
        self.is_operator = False
        # Segment capture (LA_CAPTURE). client_info is the connection's LATEST
        # device/VAD metadata (client-supplied, metadata only, never trusted for a
        # decision); like the scope it belongs to the CONNECTION, so it is carried
        # onto a new_session Session. cap_n is a monotonic capture index, used to
        # name the clips that never got a transcript id. `captured` is the set of
        # seqs captured in THIS session: it is what makes a label_segment
        # structurally own-scope-only (a connection can only name its own live
        # session's transcript ids).
        self.client_info = None
        self.cap_n = 0
        self.captured = set()
        # A lab command awaiting the user's spoken confirm/cancel (PART B). None
        # when no command is pending. Set by the lab-gate handler; consumed by the
        # next end_turn before the LLM path. Never persisted as its own record.
        self.pending_action = None
        # In-memory lab state for this connection (PART B). Physical state, not
        # conversation state, so new_session carries it over (like the TTS prefs).
        self.lab_stub = lab_gate.AutomationStub()
        # The integration seam (LA_LAB_BACKEND_URL). When configured, lab turns are
        # driven by the Lab Agent API instead of the local stub: it owns the planner,
        # the confirmation state machine, and execution. None keeps the previous
        # self-contained behavior. One backend conversation per voice session, so a
        # multi-turn clarification survives and two clients cannot stomp each other.
        self.lab_backend = lab_backend.LabBackend() if lab_backend.enabled() else None
        # Speech-service mode (?mode=speech). Like the scope, this belongs to the
        # CONNECTION, not the conversation: it is stamped at connect and carried onto
        # every new_session Session. False = the default conversational path, which is
        # what every existing client gets.
        self.speech_mode = False
        # The CONSOLE's backend state, as of its last set_lab_state ("gathering",
        # "awaiting_confirmation", "executed", ...). None until it tells us. This is
        # what arms the confirmation floor in speech mode: it is the speech-mode
        # counterpart of sess.lab_backend.state, and the only thing we know about a
        # conversation we do not own. Physical/robot state, not conversation state, so
        # it survives a new_session (like lab_stub).
        self.lab_state = None
        # The in-flight `speak` task (speech mode), or None. At most one at a time:
        # a new speak supersedes the previous one, and cancel_speak (barge-in) drops
        # it. Kept separate from reply_task, which is the LLM reply path this mode
        # never uses.
        self.speak_task = None

    def add(self, role: str, content: str):
        self.history.append({"role": role, "content": content})
        self.messages.append({"role": role, "content": content})
        # keep the last HISTORY_TURNS user+assistant pairs for the LLM call;
        # self.messages above is the full transcript and is never capped
        cap = HISTORY_TURNS * 2
        if len(self.history) > cap:
            self.history = self.history[-cap:]
        save_session(self)


def _apply_scope(sess, scope, owner_email, is_operator):
    """Stamp a connection's resolved identity onto a Session. Identity is
    per-CONNECTION, so this is called on the initial Session at connect AND on
    every new_session Session on the same connection (scope/owner/is_operator do
    not derive from the Session, they are carried from the connect header)."""
    sess.scope = scope
    sess.owner_email = owner_email
    sess.is_operator = is_operator
    return sess


def save_session(sess) -> bool:
    try:
        sdir = _scope_dir(sess.scope)
        sdir.mkdir(parents=True, exist_ok=True)
        # `number` is intentionally NOT written: it is a derived positional rank
        # (see _ranked_sessions), and persisting it would recreate the delete-hole
        # bug it replaces. name may be null (the never-named sentinel). owner_email
        # is null for the public scope; an absent key on pre-scoping files reads as
        # null too, so old flat files stay shape-compatible.
        data = {"id": sess.sid, "name": sess.name,
                "owner_email": sess.owner_email,
                "started_at": sess.started_at,
                "updated_at": datetime.datetime.now(SGT).isoformat(timespec="seconds"),
                "messages": sess.messages}
        (sdir / f"{sess.sid}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[sessions] save failed: {e}", flush=True)
        return False


def new_session_preserving(prev: Session) -> Session:
    """Start a fresh transcript while carrying the TTS voice / generation params /
    backend model over from `prev`. Those are a user preference, not conversation
    state, so a new session must not silently revert them to the backend defaults.
    Before session-history landed, `reset` mutated history/pending in place, so the
    TTS config survived implicitly; a plain `Session()` here would drop it. We copy
    `tts_params` so the new session never aliases the old dict."""
    nxt = Session()
    nxt.tts_model = prev.tts_model
    nxt.tts_params = dict(prev.tts_params)
    nxt.lab_stub = prev.lab_stub   # physical lab state persists across a new conversation
    nxt.client_info = prev.client_info   # device metadata belongs to the connection, not the session
    # Speech mode is a property of the CONNECTION (it was chosen on the connect URL),
    # and the console's backend state is physical, not conversational: a new transcript
    # does not un-arm a robot that is still awaiting confirmation.
    nxt.speech_mode = prev.speech_mode
    nxt.lab_state = prev.lab_state
    return nxt


# Claude titles an ended, never-named session from its transcript. This is a
# separate, self-contained Haiku call (NOT the conversation seam in stream_llm):
# transcript in, one short title out, best-effort. Every guard is cheap and runs
# before any API call, and every failure is soft: a session that cannot be titled
# just stays unnamed (name=None -> the client shows a placeholder).
_AUTONAME_SYSTEM = (
    "You write a short, plain-text title for a finished voice conversation. "
    "Reply with the title only: 3 to 6 words, no surrounding quotes, no trailing "
    "punctuation, no explanation."
)
# C0 and C1 control ranges: dropped from a model-generated title so it stays a
# safe single-line display/JSON string.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_title(raw: str) -> str:
    """Harden a model-generated title into a safe single-line display string:
    strip surrounding whitespace and quotes, collapse all whitespace (newlines
    included) to single spaces, drop control characters, and cap to 60 chars.
    Returns "" when nothing usable is left, so the caller leaves it unnamed. The
    transcript is the user's own speech (low injection risk), but the output is
    sanitized regardless since it becomes a display string and a JSON value."""
    t = (raw or "").strip().strip("\"'“”‘’").strip()
    t = re.sub(r"\s+", " ", t)          # collapse all whitespace to single spaces
    t = _CTRL_RE.sub("", t)             # drop control characters
    t = re.sub(r"\s+", " ", t).strip()  # re-collapse in case a drop left a gap
    return t[:60].strip()


def _autoname_transcript(sess) -> str:
    """Flatten the session transcript into a capped prompt body: the first 20
    messages and a few thousand characters, so a long conversation cannot blow
    the prompt or the token cost."""
    lines = []
    total = 0
    for m in sess.messages[:20]:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        line = f"{m.get('role', 'user')}: {content}"
        lines.append(line)
        total += len(line)
        if total >= 3000:
            break
    return "\n".join(lines)


async def autoname_session(sess) -> None:
    """Title an ended session from its transcript, when the user never named it.
    Guards first (all cheap, before any API call), then one Haiku call, sanitize
    the output hard, then persist. Never raises: this runs on teardown paths
    (connection close, new_session) where an exception would strand the
    connection, so every failure just leaves the session unnamed."""
    if sess.name:            # user renamed it, or it was already auto-named
        return
    if not sess.messages:    # nothing to title (and no file exists yet)
        return
    if llm_client is None:   # no API key configured
        return
    try:
        resp = await llm_client.messages.create(
            model=LLM_MODEL, max_tokens=24, system=_AUTONAME_SYSTEM,
            messages=[{"role": "user",
                       "content": "Title this conversation:\n\n" + _autoname_transcript(sess)}],
        )
        raw = resp.content[0].text if resp.content else ""
    except Exception as e:  # noqa: BLE001  # titling is best-effort, never fatal
        print(f"[sessions] autoname failed sid={sess.sid}: {type(e).__name__}", flush=True)
        return
    title = _sanitize_title(raw)
    if not title:            # model returned nothing usable: leave it unnamed
        return
    sess.name = title
    save_session(sess)


async def send(ws, **payload):
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def stream_llm(messages, out=None, tools_ctx=None):
    """The single integration seam: text in (messages) -> text out (yields reply
    chunks). No WebSocket coupling. `out` (optional dict) receives best-effort
    token usage after the stream ends.

    tools_ctx is the lab-automation seam (PART B). When None this is byte-identical
    to the pre-tool behavior: a single streamed Claude call with the base system
    prompt. When provided it runs the standard streaming tool-use loop: stream the
    assistant text as before, and on stop_reason == tool_use call
    `await tools_ctx.handle(name, input)` for each tool_use block, append the
    assistant content + tool_result blocks, and re-stream, up to
    LAB_TOOL_MAX_ITERS iterations. tools_ctx supplies the tool list and the
    extended system prompt so this stays WS-agnostic."""
    if tools_ctx is None:
        async with llm_client.messages.stream(
            model=LLM_MODEL, max_tokens=LLM_MAXTOK,
            system=SYSTEM_PROMPT, messages=messages,
        ) as stream:
            async for chunk in stream.text_stream:
                yield chunk
            if out is not None:
                try:   # token usage is best-effort: never let it break the reply
                    final = await stream.get_final_message()
                    out["in_tokens"] = final.usage.input_tokens
                    out["out_tokens"] = final.usage.output_tokens
                except Exception:  # noqa: BLE001
                    pass
        return

    convo = list(messages)
    in_tokens = 0
    out_tokens = 0
    for _ in range(LAB_TOOL_MAX_ITERS):
        async with llm_client.messages.stream(
            model=LLM_MODEL, max_tokens=LLM_MAXTOK,
            system=tools_ctx.system, messages=convo, tools=tools_ctx.tools,
        ) as stream:
            if STRICT_GATE_AUDIO:
                # STRUCTURAL confirmation-gate ordering. A pass that ends in a tool
                # call has not been gated yet: the gate runs below, in handle(). Any
                # text this pass produced is therefore PRE-GATE narration ("Okay,
                # starting that now..."), and speaking it would tell the user an
                # action is underway before the gate has decided whether it may
                # happen at all. So hold the pass's text until stop_reason is known,
                # and drop it entirely if a tool call is coming.
                #
                # Without this, the guarantee is only that the system prompt tells
                # the model not to claim an action happened. That held in every
                # probe (the model goes straight to the tool call and emits no
                # preamble, which is also why this buffer usually holds nothing and
                # costs nothing), but a demo that shows a hard safety stop should not
                # rest on the model choosing to behave.
                buffered = []
                async for chunk in stream.text_stream:
                    buffered.append(chunk)
                final = await stream.get_final_message()
                if final.stop_reason != "tool_use":
                    for chunk in buffered:      # gated pass done: safe to speak
                        yield chunk
                elif buffered:
                    print(f"[gate] suppressed {len(''.join(buffered))} chars of "
                          f"pre-gate narration", flush=True)
            else:
                async for chunk in stream.text_stream:
                    yield chunk
                final = await stream.get_final_message()
        try:
            in_tokens += final.usage.input_tokens or 0
            out_tokens += final.usage.output_tokens or 0
        except Exception:  # noqa: BLE001
            pass
        if final.stop_reason != "tool_use":
            break
        tool_uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
        convo.append({"role": "assistant", "content": final.content})
        results = []
        for b in tool_uses:
            content = await tools_ctx.handle(b.name, b.input)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
        convo.append({"role": "user", "content": results})
    if out is not None:
        out["in_tokens"] = in_tokens
        out["out_tokens"] = out_tokens


# A sentence is COMPLETE when a run of terminators is immediately followed by
# whitespace (lookahead: the whitespace itself is not consumed, so it is still
# there for the next match). Abbreviations that end in a period (Mr, Dr, e.g.,
# a.m., ...) suppress a split so a title or acronym never gets read as its own
# turn boundary.
_SENT_END_RE = re.compile(r"([.!?…]+)(?=\s)")
_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
           "e.g", "i.e", "a.m", "p.m", "u.s"}
_ABBREV_WORD_RE = re.compile(r"([A-Za-z]+(?:\.[A-Za-z]+)*)$")
# a lone ordinal/list marker ("1." "a.") isn't a sentence on its own, even
# though it clears the 2-char minimum; the abbreviation guard above only
# covers alpha abbreviations, so this catches single digits/letters too.
_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+|[A-Za-z])\.$")


def split_sentences(buf: str):
    """Pull complete sentences out of a streaming text buffer.

    A terminator run followed by whitespace ends a sentence; without the
    trailing-whitespace requirement, a decimal ("3.14") or a bare URL
    ("weather.com") would split mid-token, since there is no space after their
    internal dots. When the terminator run ends in "." and the word right
    before it is a known abbreviation, the split is suppressed too. Candidates
    shorter than 2 stripped characters are noise (stray punctuation) and are
    folded into whatever comes next. Returns (list_of_complete_sentences,
    remainder_tail) so the caller can keep feeding the remainder new chunks."""
    complete = []
    pos = 0
    for m in _SENT_END_RE.finditer(buf):
        term, end = m.group(1), m.end()
        if term.endswith("."):
            word = _ABBREV_WORD_RE.search(buf[pos:m.start()])
            if word and word.group(1).lower() in _ABBREV:
                continue
        candidate = buf[pos:end].strip()
        if len(candidate) < 2:
            continue
        if _LIST_MARKER_RE.match(candidate):
            continue   # e.g. "1." or "a.": fold into the following sentence
        complete.append(candidate)
        pos = end
    return complete, buf[pos:]


async def _llm_producer(ws, queue: "asyncio.Queue", commit: dict, partial_chunks: list,
                        llm_messages: list, tools_ctx=None):
    """Stream Claude's reply into `queue` (one completed sentence at a time when
    TTS_STREAM is on) and, GATED on the turn's commit event, send
    reply_start/reply_delta/reply_done to the client. `commit["event"]` is set the
    moment the turn commits (already set for a non-speculative turn, so this
    behaves exactly as before); a speculative turn buffers all client output until
    then. On the first post-commit iteration this flushes reply_start plus one
    catch-up reply_delta carrying every token accumulated so far, then streams live
    per token. A producer that finishes generating before commit waits for the
    event, then emits reply_start + the full text + reply_done. `partial_chunks` is
    a caller-owned list this appends each token to as it streams, so the cancel
    path can read the reply-so-far after this coroutine is cancelled (barge-in).
    ttft/ms are measured from `commit["fired_at"]` (the true LLM clock: fire ->
    first token for a speculation, end_turn -> first token otherwise). Always
    queues a None sentinel last (even on error), so the consumer never hangs.
    Returns (text, metrics) with the shape the latency log has always used."""
    event = commit["event"]
    fired_at = commit["fired_at"]
    tail = ""
    ttft_ms = None
    usage = {}
    text = ""
    started = False   # reply_start has been sent
    sent_upto = 0     # count of partial_chunks already flushed as reply_delta

    async def flush_pending():
        # send reply_start once, then any accumulated-but-unsent tokens as one
        # reply_delta. Called only when the commit event is set.
        nonlocal started, sent_upto
        if not started:
            await send(ws, type="reply_start")
            started = True
        if sent_upto < len(partial_chunks):
            catchup = "".join(partial_chunks[sent_upto:])
            sent_upto = len(partial_chunks)
            if catchup:
                await send(ws, type="reply_delta", text=catchup)

    try:
        async for chunk in stream_llm(llm_messages, out=usage, tools_ctx=tools_ctx):
            if ttft_ms is None:
                ttft_ms = _ms(fired_at)
            partial_chunks.append(chunk)
            if event.is_set():          # committed: stream to the client live
                await flush_pending()
            if TTS_STREAM:              # queue sentences regardless; consumer is gated
                tail += chunk
                done, tail = split_sentences(tail)
                for s in done:
                    await queue.put(s)
        # generation finished: if the turn has not committed yet (fast LLM, slow
        # talker), hold the terminal messages until it does.
        if not event.is_set():
            await event.wait()
        await flush_pending()           # reply_start + any remaining unsent text
        text = "".join(partial_chunks).strip()
        await send(ws, type="reply_done", text=text)
        if TTS_STREAM:
            tail = tail.strip()
            if len(tail) >= 2:
                await queue.put(tail)
        elif text:
            await queue.put(text)
    finally:
        await queue.put(None)   # sentinel: end-of-turn audio, even on error
    metrics = {"model": LLM_MODEL, "ttft_ms": ttft_ms, "ms": _ms(fired_at),
               "out_chars": len(text), "in_tokens": usage.get("in_tokens"),
               "out_tokens": usage.get("out_tokens")}
    return text, metrics


async def _backend_producer(ws, queue: "asyncio.Queue", commit: dict, partial_chunks: list,
                           sess: "Session", asr_rec):
    """The integration seam's producer: drive the Lab Agent API instead of Claude.

    Same contract as `_llm_producer` (client messages, a queue of sentences for the
    TTS consumer, a None sentinel last, returns (text, metrics)), so the consumer,
    barge-in, and latency logging are all unchanged. The difference is where the
    words come from: the Lab Agent owns the planner, the safety validation, the
    confirmation state machine, and execution, and hands back one `reply` string.

    The one thing this layer refuses to pass through is a confirmation it did not
    hear clearly. When the backend is awaiting_confirmation, the next affirmative
    starts a machine, so a low-confidence utterance is answered with a re-prompt
    and NEVER forwarded. The backend cannot apply this check itself: by design it
    trusts its transcript, and it has no idea the transcript came from a microphone
    in a room with a centrifuge running.

    There is no streaming here: the reply arrives whole. It is still split into
    sentences and queued as it goes, so TTS starts on sentence 1 rather than
    waiting for the full string to synthesize.
    """
    fired_at = commit["fired_at"]
    user_text = commit.get("user_text") or ""
    backend = sess.lab_backend
    ttft_ms = None
    text = ""
    blocked = False
    try:
        prob_mean = _turn_prob_mean(asr_rec)
        if lab_backend.blocks_confirmation(backend.state, prob_mean):
            # Do not POST. The backend is armed and we are not sure what was said.
            blocked = True
            text = lab_backend.REPROMPT
            await send(ws, type="action_rejected", intent="confirm",
                       reason=f"low_confidence_confirmation prob_mean={prob_mean}")
        else:
            try:
                data = await backend.send(user_text)
            except lab_backend.LabBackendError as e:
                text = ("I could not reach the lab agent, so nothing was run. "
                        "Please try again.")
                await send(ws, type="error", text=f"lab backend unreachable: {e}")
            else:
                ttft_ms = _ms(fired_at)
                text = (data.get("reply") or "").strip()
                # Give the UI the structured half of the turn: what the planner
                # understood, what the validator said, and where the state machine
                # now sits. The spoken reply is only the headline of all that.
                await send(ws, type="lab_backend", **lab_backend.summary(data))

        if not commit["event"].is_set():
            await commit["event"].wait()
        await send(ws, type="reply_start")
        if text:
            partial_chunks.append(text)
            await send(ws, type="reply_delta", text=text)
        await send(ws, type="reply_done", text=text)

        if text:
            if TTS_STREAM:
                done, tail = split_sentences(text + " ")
                for s in done:
                    await queue.put(s)
                tail = tail.strip()
                if len(tail) >= 2:
                    await queue.put(tail)
            else:
                await queue.put(text)
    finally:
        await queue.put(None)   # sentinel: the consumer must never hang
    metrics = {"model": f"lab-agent:{backend.adapter}", "ttft_ms": ttft_ms,
               "ms": _ms(fired_at), "out_chars": len(text),
               "in_tokens": None, "out_tokens": None,
               "backend_state": backend.state,
               "blocked_confirmation": blocked}
    return text, metrics


async def _tts_consumer(ws, queue: "asyncio.Queue", commit: dict, model: str, params: dict):
    """Pull sentences off `queue` as the producer forms them, synthesize with
    `model` (the session's current assistant TTS model) and the session's
    assistant TTS params (voice + generation config), and send each as its own
    reply_audio (seq 0-based, in order). None-valued params are omitted so the
    TTS backend applies its own default (and no voice = the backend's default
    reference voice). Synths run sequentially here (avoids GPU contention) but
    overlap with the producer still streaming later sentences, which is what
    cuts time-to-first-audio. On a synth error, stop synthesizing but still send
    reply_audio_end with the count so far, since the user already has the text.
    Returns the tts metrics block for the latency log, including an `error` field
    (the exception type name, or None) so the caller can log the turn's true
    status instead of "ok".

    Gated on the turn's commit event: nothing is synthesized until the turn
    commits (already set for a non-speculative turn). The queue buffers freely
    before commit, so sentences that completed during the turn-pause window are
    ready to synthesize the instant the user finishes. first_ms (and thus
    commit_to_first_audio_ms) is measured from `commit["t_perceived"]`, the
    end_turn moment, so it stays the perceived time-to-first-audio."""
    await commit["event"].wait()
    t0 = commit["t_perceived"]
    synth_kwargs = {k: v for k, v in params.items() if v is not None}   # drop unset -> backend default
    if "voice" not in synth_kwargs and model == DEFAULT_TTS_MODEL:
        synth_kwargs["voice"] = DEF_VOICE   # gepard: seed the served default reference voice
    chunks = 0
    ms_total = 0.0
    chars_total = 0
    bytes_total = 0
    audio_s_total = 0.0
    first_ms = None
    failed = False
    error_name = None   # surfaced in the metrics so the caller can log honestly
    while True:
        sentence = await queue.get()
        if sentence is None:
            break
        if failed:
            continue   # a synth already errored this turn: drain, don't retry
        t_synth = time.perf_counter()
        try:
            wav = await synthesize(sentence, model=model, **synth_kwargs)
        except Exception as e:  # noqa: BLE001
            await send(ws, type="error", text=f"TTS failed: {type(e).__name__}")
            failed = True
            error_name = type(e).__name__
            continue
        ms = _ms(t_synth)
        if first_ms is None:
            first_ms = _ms(t0)
        await send(ws, type="reply_audio", seq=chunks, text=sentence,
                   audio_b64=base64.b64encode(wav).decode("ascii"),
                   sample_rate=wav_sample_rate(wav) or 22050, format="wav")
        chunks += 1
        ms_total += ms
        chars_total += len(sentence)
        bytes_total += len(wav)
        dur = wav_duration_s(wav)
        if dur:
            audio_s_total += dur
    await send(ws, type="reply_audio_end", chunks=chunks)
    return {"ms": round(ms_total, 1), "chunks": chunks, "first_ms": first_ms,
            "chars": chars_total, "audio_bytes": bytes_total,
            "audio_s": round(audio_s_total, 3) if chunks else None,
            "rtf": _rtf(audio_s_total, ms_total) if chunks else None,
            "voice": synth_kwargs.get("voice") or "default", "model": model, "error": error_name}


async def _addressed_llm_call(system, messages, tools, tool_choice):
    """The Anthropic side of the addressed-speech classifier, injected into
    web/addressed.py so that module stays SDK-free and unit-testable. Returns the
    forced tool's input dict, or None when the model somehow answered without it
    (the classifier then fails open). Raising is fine: classify_addressed catches
    and fails open."""
    resp = await llm_client.messages.create(
        model=LLM_MODEL, max_tokens=128, system=system, messages=messages,
        tools=tools, tool_choice=tool_choice)
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    return None


async def _classify_addressed(sess: Session, text: str):
    """Is this segment addressed to the assistant, or overheard side speech?
    Returns (verdict_or_None, ms). None means the feature is off (or no API key),
    i.e. the caller must behave exactly as it did before this feature existed."""
    if not ADDRESSED_ENABLED:
        return None, None
    t0 = time.perf_counter()
    verdict = await addressed_mod.classify_addressed(
        text, sess.history[-addressed_mod.MAX_CONTEXT_TURNS:],
        sess.pending_action is not None,
        _addressed_llm_call if llm_client is not None else None)
    return verdict, _ms(t0)


# ----------------------------------------------------------------------- noise gate
# A VAD segment reaches here already transcribed. Some of them are not speech at
# all: on an open mic (iOS especially) background noise gets transcribed into
# words, now and then even into a hotword. This gate drops those before they enter
# a turn, and it keys on the ASR's own confidence, which is what separates the two
# classes in the labeled capture set (data/captures/captures.jsonl):
#
#   prob_MEAN separates:  noise 0.043-0.372, speech 0.501-0.956. A floor at 0.40
#     sits cleanly in the gap. prob_MIN does NOT separate (speech min 0.331 vs
#     noise max 0.295 overlap), so the floor keys on prob_mean, never prob_min.
#   the ONE exception is a degenerate repetition loop ("to the to the to the...")
#     that the ASR is very "confident" about (prob_mean 0.9195, above any sane
#     floor). Confidence cannot catch that; the text shape does. So the degenerate
#     check runs FIRST and independently of confidence.
def _is_degenerate(text: str) -> bool:
    """Runaway-repetition check: a high zlib compression ratio OR a single token
    dominating the output. A reimplementation of the ASR service's own guard, run
    here too so the orchestrator drops a repetition loop even when the upstream ASR
    did not. No confidence needed, so it applies even when confidence is absent."""
    if not text or len(text) < 24:
        return False
    raw = text.encode("utf-8")
    comp = zlib.compress(raw, 6)
    ratio = len(raw) / max(len(comp), 1)
    if ratio > 2.4:
        return True
    toks = re.findall(r"\w+", text)
    if len(toks) >= 8:
        top = collections.Counter(toks).most_common(1)[0][1]
        if top / len(toks) > 0.40:
            return True
    return False


def gate_segment(text: str, confidence, conf_floor: float):
    """The noise-gate decision, PURE (text + ASR confidence block + floor in, a
    reject reason or None out), so it is directly unit-testable. Conditions are
    checked in order:
      1. degenerate text  -> "degenerate"   (no confidence needed)
      2. prob_mean floor  -> "low_confidence"  (only when a floor is set AND the
         ASR supplied a confidence block; a missing block fails OPEN, since the
         floor cannot judge what it cannot see)
    Returns None to accept. Exemptions (a pending confirm/cancel, a safety stop)
    are the caller's job: they depend on session state this function does not see."""
    if _is_degenerate(text):
        return "degenerate"
    if conf_floor > 0 and confidence is not None:
        pm = confidence.get("prob_mean")
        if pm is not None and pm < conf_floor:
            return "low_confidence"
    return None


def _gate_exempt(sess: Session, text: str) -> bool:
    """Segments the noise gate must never reject, because they are control or
    safety utterances, not content: a low-confidence "confirm" or "stop" is exactly
    the case where the user most needs to be heard.
      - while a lab command is pending, its spoken confirm (strict per the pending)
        or cancel: a quiet "confirm" must still commit the action.
      - while the Lab Agent API is awaiting_confirmation, the same thing: see below.
      - a bare emergency stop that would actually halt something in flight. This
        mirrors the fast-path stop condition below (which runs after accumulation);
        exempting it here keeps a low-confidence stop from being gated before it
        ever reaches that path."""
    if sess.pending_action is not None and (
            lab_gate.is_confirm(text, strict=sess.pending_action.get("strict", False))
            or lab_gate.is_cancel(text)):
        return True
    # Same exemption for the integration seam. Without it, a mumbled "yes" while the
    # backend is awaiting_confirmation is dropped HERE, as noise, and the user hears
    # nothing back at all: they said yes, the room went silent, and they cannot tell
    # whether the protocol is running. Being HEARD and being OBEYED are different
    # things: let it through, and let the confirmation floor in _backend_producer
    # refuse it out loud ("I did not catch that clearly enough to act on it"). It is
    # still never forwarded to the backend, so a misheard yes still cannot execute.
    if (sess.lab_backend is not None
            and lab_backend.armed(sess.lab_backend.state)
            and (lab_gate.is_confirm(text, strict=False) or lab_gate.is_cancel(text))):
        return True
    # Speech mode: the SAME exemption, for the same reason, with the armed state
    # coming from the console (set_lab_state) instead of from a backend we drive
    # ourselves. Whoever owns the state machine, a confirm/cancel spoken while it is
    # armed must never be silently swallowed as noise: it has to reach end_turn, so
    # the confirmation floor can refuse it OUT LOUD (transcript_refused + a spoken
    # reprompt) rather than leaving the user to wonder whether their "yes" started
    # the protocol. Exempt from the NOISE gate, still refused by the CONFIRMATION
    # floor: heard, not obeyed.
    if (sess.speech_mode
            and lab_backend.armed(sess.lab_state)
            and (lab_gate.is_confirm(text, strict=False) or lab_gate.is_cancel(text))):
        return True
    if LAB_MODE and lab_gate.is_stop(text) and (
            (sess.reply_task is not None and not sess.reply_task.done())
            or sess.lab_stub.busy or sess.pending_action is not None):
        return True
    return False


async def handle_segment(ws, sess: Session, pcm: bytes):
    """Transcribe one VAD segment and accumulate it into the current turn. Does
    NOT call the LLM: the reply fires once the turn ends (handle_end_turn), so a
    multi-sentence message is gathered into one prompt instead of one reply per
    pause. Records per-segment ASR latency on the session for the turn log.

    With LA_ADDRESSED on, the transcript is first classified as addressed-to-the-
    assistant or overheard side speech (Phase 4). Side speech is still shown to the
    user (transcript, addressed:false) but is dropped from the turn: it never
    accumulates, never speculates, and never earns a reply.

    With LA_CAPTURE on, every segment is ALSO saved to disk (audio + a JSONL record)
    for VAD/ASR calibration. Each capture_segment() call below returns immediately
    and does its work on a thread, so nothing here waits on disk; and every exit
    path is captured, including the two that never reach the user (an ASR error and
    an empty transcript), because a clip that failed is exactly the clip we want."""
    first_of_turn = not sess.pending
    t0 = time.perf_counter()
    # One snapshot of THIS connection's scope hints for the whole segment, so the
    # hotwords that biased the ASR call are the same ones whose replacements are
    # applied to its result even if a set_hints lands mid-transcription.
    hints = hints_for(sess.scope)
    try:
        text, confidence = await transcribe(pcm, ASR_LANG, hints)
    except Exception as e:  # noqa: BLE001
        await send(ws, type="error", text=f"transcription failed: {type(e).__name__}")
        capture_segment(sess, pcm, seq=None, transcript=None, confidence=None,
                        hotwords=hints["hotwords"], addressed=None, accepted=False,
                        error=type(e).__name__)
        return
    asr_ms = _ms(t0)
    text = apply_replacements(text, hints)   # post-ASR fixes for recurring mis-transcriptions
    if not text:
        # The ASR heard nothing in it. No transcript id (nothing was shown), so this
        # clip cannot be labeled, but it is still captured: "VAD fired, ASR returned
        # empty" is the GOOD outcome for noise and the baseline the gate is tuned against.
        capture_segment(sess, pcm, seq=None, transcript="", confidence=confidence,
                        hotwords=hints["hotwords"], addressed=None, accepted=False)
        return
    # Noise gate (LA_CONF_FLOOR). Runs BEFORE accumulation, BEFORE speculation, and
    # BEFORE the addressed classifier, so a rejected segment never enters a turn and
    # never spends a Haiku call. Safety/control utterances (a pending confirm/cancel,
    # a would-halt stop) are exempt. A rejected segment still gets a transcript id and
    # is shown to the user greyed (discarded=<reason>), and is still captured (with a
    # reject_reason), but it does not accumulate, speculate, or reset any turn state.
    reject = None if _gate_exempt(sess, text) else gate_segment(text, confidence, CONF_FLOOR)
    if reject is not None:
        sess.seq += 1
        await send(ws, type="transcript", role="user", id=sess.seq, text=text,
                   ts=time.time(), confidence=confidence, discarded=reject)
        capture_segment(sess, pcm, seq=sess.seq, transcript=text, confidence=confidence,
                        hotwords=hints["hotwords"], addressed=None, accepted=False,
                        reject_reason=reject)
        pm = confidence.get("prob_mean") if confidence else None
        log_event({"kind": "segment_rejected", "session": sess.sid, "reason": reject,
                   "prob_mean": pm, "text_len": len(text)})
        return
    # Addressed-speech gate. Deterministic fast paths (confirm / cancel / stop /
    # wake form / bare filler) decide most utterances with no model call; only a
    # genuinely ambiguous one costs a Haiku round trip, and any fault fails OPEN
    # (addressed), so a classifier hiccup can never swallow the user's speech.
    verdict, addr_ms = await _classify_addressed(sess, text)
    if verdict is not None and not verdict["addressed"]:
        sess.seq += 1   # side speech still gets a transcript id: the client renders it greyed
        await send(ws, type="transcript", role="user", id=sess.seq, text=text,
                   ts=time.time(), confidence=confidence, addressed=False)
        capture_segment(sess, pcm, seq=sess.seq, transcript=text, confidence=confidence,
                        hotwords=hints["hotwords"], addressed=verdict, accepted=False)
        log_event({"kind": "sidespeech", "session": sess.sid, "turn": sess.seq,
                   "text": text, "confidence": verdict["confidence"],
                   "reason": verdict["reason"], "ms": addr_ms})
        return
    if first_of_turn:                 # the first speech that actually yields text
        sess.turn_t0 = t0
        sess.asr_ms = []
        sess.asr_conf = []
    sess.asr_ms.append(asr_ms)
    sess.asr_conf.append(confidence)   # parallel to asr_ms; may be None
    sess.pending.append(text)
    sess.seq += 1
    # `addressed` is additive and present ONLY when the feature is on, so a client
    # (and every existing smoke) sees the exact pre-change message when it is off.
    extra = {"addressed": True} if verdict is not None else {}
    await send(ws, type="transcript", role="user", id=sess.seq, text=text,
               ts=time.time(), confidence=confidence, **extra)
    # Captured AFTER the transcript is on the wire and BEFORE the speculative LLM
    # call below: the call itself only spawns a thread task, so the reply is not
    # delayed by the WAV write. accepted=True: this segment entered the turn.
    capture_segment(sess, pcm, seq=sess.seq, transcript=text, confidence=confidence,
                    hotwords=hints["hotwords"], addressed=verdict, accepted=True)
    # Fast-path emergency stop (PART B): a SHORT standalone "stop"/"halt"/"abort"
    # aborts an in-flight reply and halts the stub. It is a control utterance, not
    # content, so it is popped back off the turn (never accumulates) and skips
    # speculation. Only in lab mode, and only when there is something to stop: a
    # reply in flight, the stub busy, or a confirmation pending. ("stop the
    # stirrer" is NOT a bare stop: it routes through the LLM as stop_stirrer.)
    if LAB_MODE and lab_gate.is_stop(text) and (
            (sess.reply_task is not None and not sess.reply_task.done())
            or sess.lab_stub.busy or sess.pending_action is not None):
        sess.pending.pop()
        sess.asr_ms.pop()
        sess.asr_conf.pop()
        await _cancel_reply_task(ws, sess)   # aborts + reply_cancelled if one was live
        halted = sess.lab_stub.halt()
        sess.pending_action = None
        # `halted` is a display string (the client renders it as text); `state` is
        # the additive post-halt lab snapshot.
        halted_text = ", ".join(halted["halted"])
        await send(ws, type="action_halted", halted=halted_text, state=halted["state"])
        log_event({"kind": "assistant", "session": sess.sid, "turn": sess.seq,
                   "status": "halted", "action": {"decision": "halted"},
                   "halted": halted["halted"]})
        return
    # Speculative start: fire (or refire) the reply's LLM call now, at the segment
    # boundary, gated so nothing reaches the client until end_turn commits it.
    _maybe_speculate(ws, sess)


# get_hints / set_hints ALWAYS resolve in the connection's OWN scope, and unlike the
# session ops they have NO cross-scope operator addressing: a client-supplied `scope`
# field is ignored even for an operator. Hints silently rewrite the transcripts a user
# sees, so a cross-user write is a footgun with no demo value. (An operator keeps its
# session-level see-all; that is unchanged.)
async def handle_get_hints(ws, sess: Session):
    hints = hints_for(sess.scope)
    await send(ws, type="hints", hotwords=hints["hotwords"], replacements=hints["replacements"])


async def handle_set_hints(ws, sess: Session, msg):
    current = hints_for(sess.scope)
    hw, rp = msg.get("hotwords"), msg.get("replacements")
    # Build a NEW dict rather than mutating the cached one in place: an unset field
    # keeps the scope's current value, and a supplied empty list/dict is an explicit
    # user choice (it clears, and it persists as such rather than reading as "unset").
    updated = _normalize_hints({
        "hotwords": hw if isinstance(hw, list) else current["hotwords"],
        "replacements": rp if isinstance(rp, dict) else current["replacements"],
    })
    hints = save_hints(sess.scope, updated)   # writes the scope's file + refreshes its cache entry
    await send(ws, type="hints", hotwords=hints["hotwords"], replacements=hints["replacements"])


async def handle_client_info(sess: Session, msg):
    """Record the connection's device / VAD metadata (segment capture). Sent once
    after connect and again whenever it changes (the mic starting gives the
    AudioContext a real hw_sample_rate; the VAD panel changes the thresholds), so
    the LATEST one wins and is what every subsequent capture record carries.

    Stored even when capture is off: it costs one small dict, it touches no disk,
    and it means a tester who flips LA_CAPTURE on mid-session does not get records
    with a null client_info. It is CLIENT-SUPPLIED, so it is sanitized (known keys,
    capped values) and used as metadata only, never for a decision."""
    sess.client_info = sanitize_client_info(msg)


async def handle_label_segment(ws, sess: Session, msg):
    """Label one captured segment (noise / other_speaker / speech), so a tester can
    mark the clip that just got mis-transcribed. Append-only: this writes a new
    kind="label" record rather than rewriting the segment's line in place, and
    readers fold the LAST label per (sid, seq).

    Scope: `id` is a transcript id in THIS connection's live session, so a
    connection can only ever label its own segments (operator included: there is no
    cross-scope labeling). An id this session never captured is a silent no-op ack,
    which is also what an unknown id gets. No-op with no ack at all when capture is
    off: a client that never saw capture_state.on == true has no business here."""
    if not CAPTURE_ENABLED:
        return
    label = msg.get("label")
    if label not in CAPTURE_LABELS:
        await send(ws, type="error", text=f"unknown label: {label!r}")
        return
    try:
        seq = int(msg.get("id"))
    except (TypeError, ValueError):
        await send(ws, type="error", text="label_segment needs a numeric id.")
        return
    if seq in sess.captured:
        await asyncio.to_thread(_append_capture_line, build_label_record(sess.sid, seq, label))
    await send(ws, type="segment_labeled", id=seq, label=label)


async def handle_list_sessions(ws, sess):
    """Lightweight session picker list: identity fields only, never the full
    transcript (that's handle_get_session's job), so this stays cheap however
    many past sessions have piled up on disk. `number` is a derived positional
    rank, and the live `sess` is merged in even before it has a file, so a fresh
    conversation still shows up. Emitted newest-first.

    Scope: an operator connection gets the aggregate across ALL scopes, each row
    tagged `owner` + `scope`. A non-operator gets ONLY its own scope, unchanged
    shape (no owner/scope fields), so a client never even learns other scopes
    exist."""
    if sess.is_operator:
        ranked = _ranked_all_scopes(sess)
        sessions = [{"id": r["id"], "number": r["number"], "name": r["name"],
                     "started_at": r["started_at"], "owner": r["owner"], "scope": r["scope"]}
                    for r in reversed(ranked)]
    else:
        ranked = _ranked_sessions(sess.scope, sess)
        sessions = [{"id": r["id"], "number": r["number"], "name": r["name"],
                     "started_at": r["started_at"]} for r in reversed(ranked)]
    await send(ws, type="sessions", sessions=sessions)


async def handle_get_session(ws, sess, msg):
    sid = (msg.get("id") or "").strip()
    if not sid:
        await send(ws, type="error", text="get_session needs an id.")
        return
    scope = _effective_scope(sess, msg)   # non-operator: pinned to its own scope
    path = _session_path(scope, sid)
    if path is None:
        await send(ws, type="error", text="Invalid session id.")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        await send(ws, type="error", text="Session not found.")
        return
    # number is derived, not stored: look this id up in the SAME per-connection
    # ordering the picker uses, within the SAME scope the file was read from.
    # _ranked_sessions merges the live sess only when it shares this scope, so a
    # message-less live session still holds its rank in its own scope and does not
    # perturb ranks when an operator reads another scope. A legacy "Session N" name
    # reads as unnamed (null), same as the list.
    number = next((r["number"] for r in _ranked_sessions(scope, sess) if r["id"] == sid), None)
    name = data.get("name")
    await send(ws, type="session_data", id=sid, number=number,
               name=None if _is_legacy_auto_name(name) else name,
               started_at=data.get("started_at"),
               messages=data.get("messages") or [])


async def handle_rename_session(ws, sess: Session, msg):
    sid = (msg.get("id") or "").strip()
    name = (msg.get("name") or "").strip()
    if not sid or not name:
        await send(ws, type="error", text="Rename needs an id and a non-empty name.")
        return
    scope = _effective_scope(sess, msg)   # non-operator: pinned to its own scope
    # The live in-place path applies ONLY to the caller's own live session in its
    # own scope; an operator targeting a different scope with a matching id falls
    # through to the file path (and finds no file there), not this branch.
    if sid == sess.sid and scope == sess.scope:   # caller's own live session: update in place
        old_name = sess.name
        sess.name = name
        if not save_session(sess):
            sess.name = old_name
            await send(ws, type="error", text="Rename failed.")
            return
    else:                 # renaming a past, already-finalized session: patch its file
        path = _session_path(scope, sid)
        if path is None:
            await send(ws, type="error", text="Invalid session id.")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            await send(ws, type="error", text="Session not found.")
            return
        data["name"] = name
        data["updated_at"] = datetime.datetime.now(SGT).isoformat(timespec="seconds")
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"[sessions] rename failed: {e}", flush=True)
            await send(ws, type="error", text="Rename failed.")
            return
    await send(ws, type="session_renamed", id=sid, name=name)


async def handle_delete_session(ws, sess: Session, msg):
    sid = (msg.get("id") or "").strip()
    scope = _effective_scope(sess, msg)   # non-operator: pinned to its own scope
    if sid == sess.sid and scope == sess.scope:   # server-side invariant: the live session can never be deleted this way
        await send(ws, type="error", text="Can't delete the live session.")
        return
    path = _session_path(scope, sid)
    if path is None:
        await send(ws, type="error", text="Invalid session id.")
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass   # already gone (or a cross-scope id resolved into an empty own-scope):
               # treat as success so a client gets no existence oracle for other scopes
    except Exception:  # noqa: BLE001
        await send(ws, type="error", text="Delete failed.")
        return
    await send(ws, type="session_deleted", id=sid)
    # ranks are positional, so a delete renumbers the rest: push a fresh list so
    # an open picker re-renders with the new ranks instead of a stale cache.
    await handle_list_sessions(ws, sess)


async def handle_list_voices(ws, sess: Session, msg):
    """Voices are per-model: an explicit `model` in the request queries that
    backend (playground picking a model before synthesizing); otherwise use
    the session's current assistant model. The optional `tag`
    ("assistant"|"playground", default "playground" for back-compat) is echoed
    back so the client can route the response to the right control panel."""
    model = msg.get("model")
    model = model if model in TTS_MODELS else sess.tts_model
    tag = msg.get("tag") or "playground"
    try:
        v = await list_voices(model)
    except Exception as e:  # noqa: BLE001
        await send(ws, type="tts_test_error", text=f"voices unavailable: {type(e).__name__}")
        return
    await send(ws, type="voices", voices=v, model=model, tag=tag)


def _tts_models_payload(sess: Session) -> dict:
    return {"models": [{"id": mid, "label": mid} for mid in TTS_MODELS],
            "default": DEFAULT_TTS_MODEL, "current": sess.tts_model}


async def handle_list_tts_models(ws, sess: Session):
    await send(ws, type="tts_models", **_tts_models_payload(sess))


async def handle_set_tts_model(ws, sess: Session, msg):
    model = (msg.get("model") or "").strip()
    if model not in TTS_MODELS:
        await send(ws, type="error", text=f"unknown TTS model: {model!r}")
        return
    sess.tts_model = model
    await send(ws, type="tts_models", **_tts_models_payload(sess))


def _tts_params_payload(sess: Session) -> dict:
    """The tts_params message body: the session's current effective assistant
    TTS params (None where unset) plus the server's default generation params,
    so the client can seed its controls from the same values the TTS service
    would use by default. The advertised default voice is model-aware: DEF_VOICE
    is a gepard speaker and is only substituted at synth time for the default
    model, so it is advertised only when the session's current model is
    DEFAULT_TTS_MODEL; other models advertise no default voice (None), matching
    what the backend would actually use."""
    default_voice = DEF_VOICE if sess.tts_model == DEFAULT_TTS_MODEL else None
    return {"params": dict(sess.tts_params),
            "defaults": {"voice": default_voice, "temperature": DEF_TEMP, "cfg_scale": DEF_CFG,
                         "top_k": DEF_TOPK, "max_frames": DEF_MAXFRAMES}}


async def handle_set_tts_params(ws, sess: Session, msg):
    """Set the assistant-path TTS generation params for this session. Per-field
    semantics: an OMITTED field is left unchanged; a field explicitly set to
    null RESETS it to the backend default (None -> synthesize omits it, so the
    TTS service applies its own default). Voice None likewise means "let the
    backend resolve its default reference voice". Replies with a tts_params
    message (the session's current effective params + the server defaults)."""
    for k in _TTS_PARAM_KEYS:
        if k in msg:
            sess.tts_params[k] = msg[k]   # a value, or None when the client sent null
    await send(ws, type="tts_params", **_tts_params_payload(sess))


async def handle_tts_test(ws, sess: Session, msg):
    """TTS playground: synthesize custom text with a chosen model + voice +
    config, and return the audio. Separate from the conversation path (no
    ASR, no LLM). Logs a latency record so playground synths are measurable
    alongside assistant turns."""
    text = (msg.get("text") or "").strip()
    if not text:
        return
    model = msg.get("model")
    model = model if model in TTS_MODELS else DEFAULT_TTS_MODEL
    params = {k: msg.get(k) for k in ("voice", "temperature", "cfg_scale", "top_k", "max_frames")}
    if params.get("voice") is None and model == DEFAULT_TTS_MODEL:
        params["voice"] = DEF_VOICE   # gepard: same served default reference voice as the assistant path
    voice = params.get("voice") or "default"
    t0 = time.perf_counter()
    try:
        wav = await synthesize(text, model=model, **params)
    except Exception as e:  # noqa: BLE001
        log_event({"kind": "playground", "session": sess.sid, "status": f"error:{type(e).__name__}",
                   "tts": {"ms": _ms(t0), "chars": len(text), "voice": voice, "model": model,
                           "temperature": params.get("temperature"),
                           "cfg_scale": params.get("cfg_scale"), "top_k": params.get("top_k")}})
        await send(ws, type="tts_test_error", text=f"synth failed: {type(e).__name__}")
        return
    tts_ms = _ms(t0)
    dur = wav_duration_s(wav)
    log_event({"kind": "playground", "session": sess.sid, "status": "ok",
               "tts": {"ms": tts_ms, "chars": len(text), "audio_bytes": len(wav), "audio_s": dur,
                       "rtf": _rtf(dur, tts_ms), "voice": voice, "model": model,
                       "temperature": params.get("temperature"),
                       "cfg_scale": params.get("cfg_scale"), "top_k": params.get("top_k")}})
    await send(ws, type="tts_test_audio",
               audio_b64=base64.b64encode(wav).decode("ascii"),
               sample_rate=wav_sample_rate(wav) or 22050, format="wav")


def _agg_confidence(conf_list: list):
    """Aggregate a turn's per-segment ASR confidence blocks into one turn-level
    block: the min of the segment prob_min values (the weakest word anywhere in
    the turn, what the gate keys on) and the mean of the segment prob_mean values,
    plus the per-segment blocks verbatim (each block or None). Returns None when
    no segment carried a confidence block, so `confidence` stays cleanly nullable
    downstream (transcript WS message, data/latency.jsonl)."""
    present = [c for c in conf_list if c]
    if not present:
        return None
    prob_mins = [c["prob_min"] for c in present if c.get("prob_min") is not None]
    prob_means = [c["prob_mean"] for c in present if c.get("prob_mean") is not None]
    return {"prob_min": round(min(prob_mins), 4) if prob_mins else None,
            "prob_mean": round(sum(prob_means) / len(prob_means), 4) if prob_means else None,
            "segments": list(conf_list)}


def _asr_rec(asr_ms_list: list, conf_list: list, user_text: str) -> dict:
    """The per-turn ASR latency record, built from the segment ms list, the
    parallel per-segment confidence list, and the joined user text. Shared by the
    speculative and non-speculative paths so the log shape is identical either
    way. `confidence` is additive and nullable (see _agg_confidence)."""
    return {"segments": len(asr_ms_list), "ms": asr_ms_list,
            "total_ms": round(sum(asr_ms_list), 1), "chars": len(user_text),
            "confidence": _agg_confidence(conf_list)}


def _turn_prob_min(asr_rec):
    """The turn's weakest ASR confidence (prob_min), or None when confidence is
    unavailable. This is what the lab gate keys on."""
    conf = (asr_rec or {}).get("confidence")
    return conf.get("prob_min") if conf else None


def _turn_prob_mean(asr_rec):
    """The turn's mean ASR confidence (prob_mean), or None when unavailable. The
    confirmation-execution floor keys on this (not prob_min), matching the noise
    gate: the calibration showed prob_min overlaps between speech and noise."""
    conf = (asr_rec or {}).get("confidence")
    return conf.get("prob_mean") if conf else None


class _LabToolsCtx:
    """Per-turn, WS-aware handler for the lab_command tool. It carries what the
    tool loop in stream_llm needs (the tool list + extended system prompt) and,
    on each tool call, runs the confidence/severity gate and either executes
    against the session's AutomationStub, arms a pending confirmation, or rejects.
    Kept out of lab_gate on purpose: lab_gate stays pure/unit-testable, this side
    owns the WS sends and session mutation."""

    def __init__(self, ws, sess: Session, commit: dict, asr_rec):
        self.ws = ws
        self.sess = sess
        self.commit = commit
        self.stub = sess.lab_stub
        self.prob_min = _turn_prob_min(asr_rec)
        self.tools = [lab_gate.tool_schema()]
        self.system = LAB_SYSTEM_PROMPT
        # The last tool decision this turn, for the latency record (None if the
        # turn made no tool call).
        self.action = None

    async def handle(self, name, tool_input):
        # A speculative turn may PARSE a command but must never DISPATCH before the
        # turn commits: park here until commit. A discarded speculation is cancelled
        # while parked, so no WS send and no stub side effect ever happens for it.
        await self.commit["event"].wait()
        if name != "lab_command":
            return "ERROR: unknown tool; no action taken."
        tool_input = tool_input or {}
        intent = tool_input.get("intent")
        args = tool_input.get("args") or {}
        decision = lab_gate.gate(intent, args, self.prob_min)
        severity = lab_gate.severity_of(intent)
        self.action = {"intent": intent, "decision": decision["action"],
                       "severity": severity, "prob_min": self.prob_min}
        act = decision["action"]
        if act == "proceed":
            result = await self.stub.execute(intent, args)
            _schedule_stub_event(self.sess, intent, args)   # timed completion announce, if any
            await send(self.ws, type="action_executed", intent=intent, args=args,
                       result=result, confirmed=False)
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if act in ("confirm", "confirm_strict"):
            strict = act == "confirm_strict"
            rb = lab_gate.readback(intent, args)
            # Intent binding (F1): IRREVERSIBLE and HAZARDOUS pendings require the
            # user to say "confirm <keyword>", so a loose "yes" cannot fire the wrong
            # physical action. `bound` is the single source of truth for the
            # resolution logic; `strict` is kept only for backward compat (it now
            # always equals bound-and-hazardous) and is not consulted anymore.
            bound = severity in (lab_gate.IRREVERSIBLE, lab_gate.HAZARDOUS)
            keyword = lab_gate.keyword_of(intent)
            phrase = lab_gate.confirm_phrase(intent) if bound else None
            self.sess.pending_action = {"intent": intent, "args": args, "readback": rb,
                                        "strict": strict, "bound": bound, "keyword": keyword,
                                        "confirm_phrase": phrase, "severity": severity,
                                        "prob_min": self.prob_min, "created_ts": time.time()}
            await send(self.ws, type="action_pending", intent=intent, args=args,
                       readback=rb, severity=severity, confidence=self.prob_min,
                       confirm_phrase=phrase)   # additive: null for a reversible pending
            if bound:
                return ("CONFIRMATION REQUIRED. Do not claim any action happened. Say this "
                        "readback to the user, then tell them the EXACT phrase to proceed: "
                        f"'{phrase}', or to say cancel. Readback: " + rb)
            return ("CONFIRMATION REQUIRED. Do not claim any action happened. Say exactly "
                    "this readback to the user and ask them to say confirm or cancel: " + rb)
        # reject
        await send(self.ws, type="action_rejected", intent=intent, reason=decision["reason"])
        return "REJECTED: " + decision["reason"] + ". Ask the user to rephrase the command clearly."


def _spec_block(commit: dict, first_audio_ms) -> dict:
    """Per-turn speculative-start metrics for the latency log: whether the feature
    is enabled, how many speculations fired / were discarded since the last commit,
    whether the served reply was itself a committed speculation, and the two win
    axes (fire -> commit, and commit -> first audio). fire_to_commit_ms is only
    meaningful for a committed speculation."""
    spec = bool(commit.get("spec"))
    fire_to_commit = None
    if spec and commit.get("t_perceived") and commit.get("fired_at"):
        fire_to_commit = round((commit["t_perceived"] - commit["fired_at"]) * 1000, 1)
    return {"enabled": SPEC_START, "fired": commit.get("fired", 0),
            "committed": spec, "discarded": commit.get("discarded", 0),
            "fire_to_commit_ms": fire_to_commit,
            "commit_to_first_audio_ms": first_audio_ms}


def _start_reply_task(ws, sess: Session, asr_rec, turn_t0, commit: dict, llm_messages: list):
    """Spawn the background reply task for a turn (speculative or committed) and
    wire up the session's one-in-flight-task handles. `commit` carries the gate
    (an asyncio.Event set at commit; already set for a non-speculative turn) and
    the timing marks the producer / consumer read. The TTS model + params are
    snapshotted here so a later set_tts_params does not retarget an in-flight
    reply. partial_chunks is shared with the producer so the cancel path can read
    the reply-so-far after the task is cancelled."""
    partial_chunks: list = []
    sess.reply_ctx = {"partial": partial_chunks, "asr": asr_rec,
                      "turn_t0": turn_t0, "commit": commit}
    sess.reply_task = asyncio.create_task(
        _run_turn(ws, sess, asr_rec, turn_t0, commit, sess.tts_model,
                  dict(sess.tts_params), partial_chunks, llm_messages))
    sess.reply_task.add_done_callback(lambda t: _reply_task_done(sess, t))


def _maybe_speculate(ws, sess: Session) -> None:
    """Fire (or refire) the reply's LLM call at the segment boundary, gated so
    nothing reaches the client until end_turn commits it. No-op when the feature
    is off, no API key is configured, the turn has run past the dictation guard,
    or a committed reply is already in flight (barge-in owns that path). When a
    prior uncommitted speculation is still running, abort it and refire from the
    new, longer segment snapshot (Flux's TurnResumed). History is NOT mutated:
    the LLM sees a snapshot of `sess.history + [user message]`; the user and
    assistant messages are added only on/after commit."""
    if not SPEC_START or llm_client is None:
        return
    if sess.speech_mode:
        # NEVER speculate in speech mode. There is no reply for this server to
        # generate: the console owns the conversation, and this is the one place a
        # segment could otherwise still reach an LLM (speculation fires at the SEGMENT
        # boundary, ahead of end_turn). A speech-mode turn must cost zero LLM calls.
        return
    if sess.lab_backend is not None:
        # NEVER speculate against the Lab Agent API. Speculation fires the reply at
        # the SEGMENT boundary, before the user has finished the turn, and discards
        # it if the turn keeps going. That is safe for a stateless LLM call and
        # unsafe here: the backend is a state machine with an audit trail, so a
        # discarded speculation would still have advanced idle -> gathering, logged
        # a turn, and (worst case, when it was already awaiting_confirmation) taken a
        # half-heard "yes" as sign-off. A lab turn is committed or it does not happen.
        return
    if sess.pending_action is not None:
        return   # a confirm/cancel turn bypasses the LLM: speculating wastes a call
    if sess.turn_t0 is not None and (time.perf_counter() - sess.turn_t0) > SPEC_MAX_TURN_S:
        return
    task = sess.reply_task
    if task is not None and not task.done():
        commit = (sess.reply_ctx or {}).get("commit") or {}
        if commit.get("committed") or not commit.get("spec"):
            return   # a committed reply is running: leave it to the barge-in path
    n_segments = len(sess.pending)
    user_text = " ".join(s for s in sess.pending if s).strip()
    if not user_text:
        return
    _abort_speculation(sess)   # drop the prior uncommitted speculation (counts a discard)
    fired_at = time.perf_counter()
    commit = {"event": asyncio.Event(), "spec": True, "committed": False,
              "n_segments": n_segments, "user_text": user_text,
              "fired_at": fired_at, "t_perceived": None}
    asr_rec = _asr_rec(list(sess.asr_ms), list(sess.asr_conf), user_text)
    llm_messages = sess.history + [{"role": "user", "content": user_text}]
    _start_reply_task(ws, sess, asr_rec, sess.turn_t0, commit, llm_messages)
    sess.spec_fired += 1


def _abort_speculation(sess: Session) -> None:
    """Silently drop the in-flight uncommitted speculation: cancel the task, count
    a discard, clear the handles. No client sends, no history commit, no log (spec
    principle 5). No-op when nothing is running."""
    task = sess.reply_task
    if task is not None and not task.done():
        task.cancel()
        sess.spec_discarded += 1
    sess.reply_task = None
    sess.reply_ctx = None


def _try_commit_speculation(sess: Session) -> bool:
    """Commit the in-flight speculation in place when its segment snapshot still
    matches sess.pending: run the end_turn bookkeeping (clear pending, add the user
    message, freeze the spec counters) and release the commit gate. Returns True
    when it committed the running speculation. Returns False (silently aborting a
    stale one) otherwise, so the caller falls through to a normal turn.

    The commit test is append-only-transcript equality: sess.pending only grows
    within a turn, so a speculation fired from segments [0..n) is valid iff the
    segment count and the joined text both still match the snapshot."""
    task = sess.reply_task
    if task is None or task.done():
        return False
    ctx = sess.reply_ctx or {}
    commit = ctx.get("commit") or {}
    if not commit.get("spec") or commit.get("committed"):
        return False   # a committed reply or non-spec task: let the normal path handle it
    cur_text = " ".join(s for s in sess.pending if s).strip()
    if len(sess.pending) != commit.get("n_segments") or cur_text != commit.get("user_text"):
        _abort_speculation(sess)   # stale (should not happen given refire-on-segment)
        return False
    sess.pending = []
    sess.asr_ms = []
    sess.asr_conf = []
    sess.add("user", commit["user_text"])
    commit["t_perceived"] = time.perf_counter()   # end_turn == perceived-latency clock
    commit["fired"] = sess.spec_fired
    commit["discarded"] = sess.spec_discarded
    sess.spec_fired = 0
    sess.spec_discarded = 0
    commit["committed"] = True
    commit["event"].set()   # release the gated producer + consumer
    return True


async def _run_canned_reply(ws, sess: Session, user_text, reply, asr_rec, turn_t0,
                            status, action):
    """Deliver a fixed reply with NO LLM call: commit user+assistant to history,
    stream it as reply_start / reply_delta / reply_done, synthesize it through the
    normal TTS path (one synth), send reply_audio + reply_audio_end, and log one
    assistant latency record (llm None) with `status` and the given `action` block.
    Used by the confirm / cancel / re-prompt turns, which bypass the LLM."""
    if user_text:
        sess.add("user", user_text)
    t0 = time.perf_counter()
    await send(ws, type="reply_start")
    await send(ws, type="reply_delta", text=reply)
    await send(ws, type="reply_done", text=reply)
    sess.add("assistant", reply)
    model = sess.tts_model
    params = {k: v for k, v in sess.tts_params.items() if v is not None}
    if "voice" not in params and model == DEFAULT_TTS_MODEL:
        params["voice"] = DEF_VOICE
    tts_ms = 0.0
    first_ms = None
    audio_s = 0.0
    nbytes = 0
    chunks = 0
    error_name = None
    t_synth = time.perf_counter()
    try:
        wav = await synthesize(reply, model=model, **params)
    except Exception as e:  # noqa: BLE001
        await send(ws, type="error", text=f"TTS failed: {type(e).__name__}")
        error_name = type(e).__name__
        wav = None
    if wav is not None:
        tts_ms = _ms(t_synth)
        first_ms = _ms(t0)
        await send(ws, type="reply_audio", seq=0, text=reply,
                   audio_b64=base64.b64encode(wav).decode("ascii"),
                   sample_rate=wav_sample_rate(wav) or 22050, format="wav")
        chunks = 1
        nbytes = len(wav)
        audio_s = wav_duration_s(wav) or 0.0
    await send(ws, type="reply_audio_end", chunks=chunks)
    tts_rec = {"ms": round(tts_ms, 1), "chunks": chunks, "first_ms": first_ms,
               "chars": len(reply), "audio_bytes": nbytes,
               "audio_s": round(audio_s, 3) if chunks else None,
               "rtf": _rtf(audio_s, tts_ms) if chunks else None,
               "voice": params.get("voice") or "default", "model": model, "error": error_name}
    log_event({"kind": "assistant", "session": sess.sid, "turn": sess.seq, "status": status,
               "asr": asr_rec, "llm": None, "tts": tts_rec, "action": action,
               "first_audio_ms": first_ms, "stream": TTS_STREAM,
               "reply_latency_ms": _ms(t0),
               "total_ms": round((time.perf_counter() - turn_t0) * 1000, 1) if turn_t0 else None})


async def _handle_pending_action_turn(ws, sess: Session):
    """Resolve a turn while a lab command is pending confirmation, WITHOUT an LLM
    call. Order: expiry, cancel, confirmation, supersede.

      expired + a confirm/cancel attempt -> action_cancelled(expired), canned "That
                 request expired." and CONSUME the turn (decision "expired"): a
                 stale confirm must never fire.
      expired + anything else            -> drop the pending (action_cancelled
                 "expired", NO canned notice) and run the utterance as a NORMAL turn,
                 so an unrelated request is answered instead of eaten.
      cancel   -> clear pending, action_cancelled(user), canned "Cancelled." (never
                 confidence-gated: cancelling is reversible).
      confirm  -> a BOUND (IRREVERSIBLE / HAZARDOUS) pending executes ONLY on
                 "confirm <keyword>"; a bare confirm/yes re-prompts with the exact
                 phrase (decision "reprompt_unbound"), pending kept. A non-bound
                 (REVERSIBLE / SAFE low-confidence) pending executes on a loose
                 confirm. Either way EXECUTION is confidence-gated: no confidence
                 block re-prompts (decision "reprompt_noconf"), prob_mean below
                 LA_CONFIRM_FLOOR re-prompts (decision "reprompt_lowconf"); both keep
                 the pending and never drop the word.
      else     -> supersede: action_cancelled(superseded), run a normal turn.
    Speculation is a no-op while pending, so nothing is in flight to commit.

    Internal representation: `bound` (severity in {IRREVERSIBLE, HAZARDOUS}) is the
    single source of truth for how a confirmation is matched. The old `strict` field
    is kept on the pending dict for backward compat but is NO LONGER consulted here:
    a strict (HAZARDOUS) pending is just a bound pending, and IRREVERSIBLE is now
    bound too, so the two behave identically."""
    pending = sess.pending_action
    user_text = " ".join(s for s in sess.pending if s).strip()
    asr_ms_list = list(sess.asr_ms)
    asr_conf_list = list(sess.asr_conf)
    turn_t0 = sess.turn_t0
    sess.pending = []
    sess.asr_ms = []
    sess.asr_conf = []
    asr_rec = _asr_rec(asr_ms_list, asr_conf_list, user_text)
    intent, args = pending["intent"], pending["args"]
    severity = pending.get("severity")
    bound = pending.get("bound")
    keyword = pending.get("keyword")
    phrase = pending.get("confirm_phrase") or lab_gate.confirm_phrase(intent)
    prob_min = _turn_prob_min(asr_rec)
    prob_mean = _turn_prob_mean(asr_rec)

    async def _reprompt(reply, decision):
        # keep the pending and re-prompt; never drop the word, never cancel.
        act = {"intent": intent, "decision": decision, "severity": severity,
               "prob_mean": prob_mean}
        await _run_canned_reply(ws, sess, user_text, reply, asr_rec, turn_t0,
                                "action_" + decision, act)

    async def _passthrough(reason):
        # clear the pending (voice-ui clears the strip on action_cancelled), then run
        # the utterance as a NORMAL turn: pending_action is None, so the LLM path runs.
        sess.pending_action = None
        await send(ws, type="action_cancelled", reason=reason)
        sess.pending = [user_text] if user_text else []
        sess.asr_ms = asr_ms_list
        sess.asr_conf = asr_conf_list
        sess.turn_t0 = turn_t0
        await handle_end_turn(ws, sess)

    # Expiry (F4 + round-3). A pending older than the TTL never fires. A stale
    # confirm/cancel gets the expiry notice and consumes the turn; ANY OTHER
    # utterance drops the pending and is processed as a normal turn, so unrelated
    # speech is answered rather than swallowed by a "please repeat" it did not ask for.
    created_ts = pending.get("created_ts")
    if PENDING_TTL_S > 0 and created_ts is not None and (time.time() - created_ts) > PENDING_TTL_S:
        if lab_gate.is_confirm(user_text, strict=False) or lab_gate.is_cancel(user_text):
            sess.pending_action = None
            await send(ws, type="action_cancelled", reason="expired")
            action = {"intent": intent, "decision": "expired", "severity": severity,
                      "prob_min": prob_min}
            await _run_canned_reply(ws, sess, user_text,
                                    "That request expired. Please repeat the command.",
                                    asr_rec, turn_t0, "action_expired", action)
            return
        await _passthrough("expired")
        return

    if lab_gate.is_cancel(user_text):
        # Cancel is deliberately NOT confidence-gated: a spurious low-confidence
        # cancel only costs re-issuing the command (reversible), whereas a spurious
        # low-confidence confirm fires an irreversible action (not). The asymmetry
        # is the whole point: relax on the reversible side, tighten on the other.
        sess.pending_action = None
        await send(ws, type="action_cancelled", reason="user")
        action = {"intent": intent, "decision": "cancelled", "severity": severity, "prob_min": prob_min}
        await _run_canned_reply(ws, sess, user_text, "Cancelled.", asr_rec, turn_t0,
                                "action_cancelled", action)
        return

    # Is this utterance a FULL confirmation for THIS pending (one that would execute)?
    # A bound pending needs "confirm <keyword>"; a non-bound one accepts a loose confirm.
    full_confirm = (lab_gate.is_confirm_bound(user_text, keyword) if bound
                    else lab_gate.is_confirm(user_text, strict=False))
    if full_confirm:
        # Execution floors. The word was never dropped (it was exempt from the noise
        # gate); execution is gated on how clearly it was heard, since a spurious
        # confirm fires an irreversible action. NO confidence block re-prompts
        # (round-3: consistent with the command gate's escalate-on-None), and
        # prob_mean below LA_CONFIRM_FLOOR re-prompts (F2). Distinct decisions for the
        # log, same spoken text (the phrase was right, just not clearly heard).
        if prob_mean is None:
            await _reprompt("I heard confirm, but not clearly. Please say it again, or say cancel.",
                            "reprompt_noconf")
            return
        if CONFIRM_FLOOR > 0 and prob_mean < CONFIRM_FLOOR:
            await _reprompt("I heard confirm, but not clearly. Please say it again, or say cancel.",
                            "reprompt_lowconf")
            return
        sess.pending_action = None
        result = await sess.lab_stub.execute(intent, args)
        _schedule_stub_event(sess, intent, args)   # timed completion announce, if any
        await send(ws, type="action_executed", intent=intent, args=args,
                   result=result, confirmed=True)
        detail = result.get("detail") if isinstance(result, dict) else None
        reply = "Confirmed. " + ((detail[0].upper() + detail[1:] + ".") if detail else "Done.")
        action = {"intent": intent, "decision": "confirmed", "severity": severity, "prob_min": prob_min}
        await _run_canned_reply(ws, sess, user_text, reply, asr_rec, turn_t0,
                                "action_confirmed", action)
        return

    # A bound pending, but the user only gave a bare/loose affirmation ("yes",
    # "confirm") without the keyword: intent binding (F1). Tell them the exact phrase
    # and KEEP the pending; do not execute, do not supersede.
    if bound and lab_gate.is_confirm(user_text, strict=False):
        await _reprompt(f"To proceed, say {phrase}, or say cancel.", "reprompt_unbound")
        return

    # neither confirm nor cancel: supersede the pending action and run a normal turn.
    await _passthrough("superseded")


async def handle_end_turn(ws, sess: Session):
    """User finished speaking: assemble the accumulated segments into one message
    and run LLM -> TTS once per turn. When a speculation for this exact segment
    snapshot is already in flight (the common case with LA_SPEC_START on), commit
    it in place, which releases its gated reply immediately. Otherwise start a
    fresh turn. The reply work (LLM producer + TTS consumer) runs as a background
    task (see `_run_turn`) so the receive loop stays free to read the next message,
    including a `cancel_turn` barge-in, while the reply is still generating. If a
    committed reply for this session is still in flight when a new end_turn arrives
    (a raced barge-in), it is superseded: the old task is cancelled and cleaned up
    before the new turn starts, so there is at most one in-flight reply per
    session."""
    # A lab command is pending confirmation: this turn is the spoken confirm/cancel,
    # resolved without the LLM. Handled BEFORE the speculation/LLM path.
    if sess.pending_action is not None:
        await _handle_pending_action_turn(ws, sess)
        return
    # commit an in-flight speculation whose snapshot matches the accumulated
    # segments, releasing its gated reply instead of starting fresh.
    if _try_commit_speculation(sess):
        return
    # no matching speculation: supersede any still-running (committed) reply, then
    # run a normal turn. A stale speculation was already aborted silently above.
    await _cancel_reply_task(ws, sess)
    if not sess.pending:
        return
    user_text = " ".join(s for s in sess.pending if s).strip()
    asr_ms_list = list(sess.asr_ms)
    asr_conf_list = list(sess.asr_conf)
    turn_t0 = sess.turn_t0
    sess.pending = []
    sess.asr_ms = []
    sess.asr_conf = []
    if not user_text:
        return
    if llm_client is None and sess.lab_backend is None:
        # With the seam configured the Lab Agent owns the planner (and its own key),
        # so the voice half needs no Anthropic key of its own to run a lab turn.
        await send(ws, type="error", text="no Anthropic API key configured")
        return
    sess.add("user", user_text)
    asr_rec = _asr_rec(asr_ms_list, asr_conf_list, user_text)
    now = time.perf_counter()   # end_turn: both the LLM clock and the perceived clock
    # non-speculative turn: an already-set gate, so the producer + consumer behave
    # exactly as before (single code path, no fork of _run_turn). fired stays 0 for
    # a turn that never speculated; any carried spec counters are frozen here.
    commit = {"event": asyncio.Event(), "spec": False, "committed": True,
              "n_segments": len(asr_ms_list), "user_text": user_text,
              "fired_at": now, "t_perceived": now,
              "fired": sess.spec_fired, "discarded": sess.spec_discarded}
    commit["event"].set()
    sess.spec_fired = 0
    sess.spec_discarded = 0
    llm_messages = list(sess.history)   # history now includes this user message
    _start_reply_task(ws, sess, asr_rec, turn_t0, commit, llm_messages)


async def _run_turn(ws, sess: Session, asr_rec, turn_t0, commit, tts_model, tts_params,
                    partial_chunks: list, llm_messages: list):
    """Background body of one assistant turn: run the LLM producer and TTS
    consumer concurrently (a queue of completed sentences between them), commit
    the reply to history, and log the per-turn latency record. `commit` is the
    turn's gate (already set for a non-speculative turn; set at commit for a
    speculation), so a speculative turn does its LLM work here but sends nothing
    until released. Runs as a fire-and-forget task so `cancel_turn` can interrupt
    it; on cancellation a CancelledError propagates out of the gather (which
    cancels both children), and the cancelled-turn cleanup (partial-text commit,
    reply_cancelled, 'cancelled' log) is done by `_cancel_reply_task`, NOT here,
    since awaiting sends inside an already-cancelled task is fragile. A cancelled
    speculation unwinds silently (both children were parked on the gate).
    Non-cancellation exceptions are handled here so a background failure never
    vanishes."""
    queue: asyncio.Queue = asyncio.Queue()
    # Lab mode: give the LLM the lab_command tool + gate for this turn. The ctx is
    # WS-aware but the seam (stream_llm) stays WS-agnostic. tools_ctx None restores
    # the exact pre-change reply flow.
    tools_ctx = _LabToolsCtx(ws, sess, commit, asr_rec) if (LAB_MODE and sess.lab_stub) else None
    # The integration seam: when the Lab Agent API is configured it owns the turn
    # (planner, validation, confirmation, execution) and the local tool gate stands
    # down, so there is exactly one planner and one confirmation gate in the system.
    producer = (
        _backend_producer(ws, queue, commit, partial_chunks, sess, asr_rec)
        if sess.lab_backend is not None
        else _llm_producer(ws, queue, commit, partial_chunks, llm_messages, tools_ctx)
    )
    try:
        prod_result, tts_result = await asyncio.gather(
            producer,
            _tts_consumer(ws, queue, commit, tts_model, tts_params),
            return_exceptions=True,
        )
        if isinstance(prod_result, Exception):
            await send(ws, type="error", text=f"LLM failed: {type(prod_result).__name__}")
            log_event({"kind": "assistant", "session": sess.sid, "turn": sess.seq,
                       "status": f"llm_error:{type(prod_result).__name__}", "asr": asr_rec,
                       "spec": _spec_block(commit, None)})
            return
        reply, llm_rec = prod_result
        tts_rec = tts_result if not isinstance(tts_result, Exception) else {
            "ms": 0.0, "chunks": 0, "first_ms": None, "chars": 0, "audio_bytes": 0,
            "audio_s": None, "rtf": None, "voice": tts_params.get("voice") or "default",
            "model": tts_model, "error": type(tts_result).__name__}

        if not reply:
            log_event({"kind": "assistant", "session": sess.sid, "turn": sess.seq,
                       "status": "empty_reply", "asr": asr_rec, "llm": llm_rec,
                       "spec": _spec_block(commit, None)})
            return
        sess.add("assistant", reply)

        # a mid-turn synth failure still delivers the text (and any audio sent
        # before the failure), but the record should say so instead of "ok": the
        # client got an `error` message and a short reply_audio_end for this turn.
        tts_err = tts_rec.get("error")
        status = "ok" if not tts_err else (f"ok_partial:{tts_err}" if tts_rec.get("chunks") else f"tts_error:{tts_err}")

        t_perceived = commit.get("t_perceived")
        log_event({"kind": "assistant", "session": sess.sid, "turn": sess.seq, "status": status,
                   "asr": asr_rec, "llm": llm_rec, "tts": tts_rec,
                   "action": tools_ctx.action if tools_ctx else None,
                   "first_audio_ms": tts_rec.get("first_ms"), "stream": TTS_STREAM,
                   "spec": _spec_block(commit, tts_rec.get("first_ms")),
                   "reply_latency_ms": _ms(t_perceived) if t_perceived else None,
                   "total_ms": round((time.perf_counter() - turn_t0) * 1000, 1) if turn_t0 else None})
    except asyncio.CancelledError:
        # barge-in: cleanup is the cancel path's job, not ours. Re-raise so the
        # task ends cancelled and _cancel_reply_task's await sees CancelledError.
        raise
    except Exception as e:  # noqa: BLE001  # a background task's error must not vanish
        print(f"[!] reply task sid={sess.sid} turn={sess.seq}: {e}", flush=True)
        try:
            await send(ws, type="error", text=f"reply failed: {type(e).__name__}")
        except Exception:  # noqa: BLE001  # socket may already be gone
            pass
        log_event({"kind": "assistant", "session": sess.sid, "turn": sess.seq,
                   "status": f"error:{type(e).__name__}", "asr": asr_rec})


def _reply_task_done(sess: Session, task) -> None:
    """Done-callback safety net. A turn task is fire-and-forget on the normal
    path (no one awaits it), so retrieve any exception it ended with, both to
    avoid a 'Task exception was never retrieved' warning and to surface a bug in
    the turn runner. A cancelled task IS awaited by the cancel path, so skip it
    here (calling .exception() on it would raise)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[!] reply task sid={sess.sid} crashed: {exc!r}", flush=True)


async def _cancel_reply_task(ws, sess: Session) -> bool:
    """Cancel this session's in-flight reply task (barge-in / supersede) and run
    the cancelled-turn cleanup OUTSIDE the cancelled task: commit any partial
    reply text to history (the user already saw/heard it, so the LLM context
    must match), send the terminal reply_cancelled message, and log a 'cancelled'
    latency record. A no-op (returns False, sends nothing) when no reply task is
    running or it already finished on its own. Safe to call at any time."""
    task = sess.reply_task
    if task is None or task.done():
        sess.reply_task = None
        sess.reply_ctx = None
        return False
    # grab the context BEFORE cancelling: the task's own unwinding may clear the
    # session fields, but this local ref (and the partial list it holds, which
    # the producer mutates in place) stays valid.
    ctx = sess.reply_ctx or {}
    commit = ctx.get("commit") or {}
    if commit.get("spec") and not commit.get("committed"):
        # uncommitted speculation: the client never saw this turn, so drop it
        # silently (no reply_cancelled / history / log), same as _abort_speculation
        # but on an async path. In practice the client cannot barge in here (its
        # replyInFlight is false); this is teardown hygiene.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
        sess.reply_task = None
        sess.reply_ctx = None
        sess.spec_discarded += 1
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001  # the task body already reported its own error
        pass
    sess.reply_task = None
    sess.reply_ctx = None
    if not task.cancelled():
        # it completed (normally or with an error) before the cancel took effect:
        # not a real cancellation, so no reply_cancelled and no partial commit.
        return False
    partial = "".join(ctx.get("partial") or []).strip()
    if partial:
        sess.add("assistant", partial)   # keep history in sync with what was delivered
    await send(ws, type="reply_cancelled", text=partial)
    turn_t0 = ctx.get("turn_t0")
    t_reply = commit.get("t_perceived")
    log_event({"kind": "assistant", "session": sess.sid, "turn": sess.seq,
               "status": "cancelled", "asr": ctx.get("asr"),
               "llm": {"model": LLM_MODEL, "ttft_ms": None,
                       "ms": _ms(t_reply) if t_reply else None,
                       "out_chars": len(partial), "in_tokens": None, "out_tokens": None},
               "tts": None, "first_audio_ms": None, "stream": TTS_STREAM,
               "spec": _spec_block(commit, None),
               "reply_latency_ms": _ms(t_reply) if t_reply else None,
               "total_ms": round((time.perf_counter() - turn_t0) * 1000, 1) if turn_t0 else None})
    return True


def _abort_reply_task(sess: Session) -> None:
    """Best-effort cancel with NO client-facing cleanup, for teardown paths where
    reply_cancelled would be pointless or unsendable (connection closing, session
    switch). Just cancels the task and drops the session's handles."""
    task = sess.reply_task
    if task is not None and not task.done():
        task.cancel()
    sess.reply_task = None
    sess.reply_ctx = None


# ----------------------------------------------------------- speech-service mode
# The inversion: this server stops owning the turn and becomes a microphone and a
# speaker for a console that owns the conversation.
#
#   browser mic --audio--> [ASR + the safety gates] --accepted transcript--> console
#   console --reply text--> [TTS] --audio--> browser speaker
#
# Everything above this line still runs on the way in: the noise gate, the addressed
# -speech classifier, the segment accumulation. What changes is the way OUT: end_turn
# runs NO reply pipeline (no LLM, no Lab Agent, no _run_turn), it just publishes the
# turn's verdict, and audio is produced only when the console asks for it (`speak`).
#
# The confirmation floor is the reason this mode is not just "send the text". When
# the console's backend is armed, its next affirmative fires a physical protocol, and
# the console will POST whatever transcript we hand it. So a transcript we are not
# confident about is NOT HANDED OVER AT ALL. That refusal has to be audible, or the
# user's "yes" is met with silence and they cannot tell whether the machine started.
async def handle_set_lab_state(ws, sess: Session, msg):
    """The console tells us its backend's state string. This is the ONLY thing we
    know about a conversation we do not own, and it is what arms the confirmation
    floor (lab_backend.armed). Any string is accepted and an unknown one is simply
    not armed: a console that reports a state we have never heard of must fail
    SAFE-BY-DEFAULT-OPEN here (the floor does not bite), because the alternative is
    refusing every turn on a state string typo. The floor's job is to be exactly
    right about awaiting_confirmation, not to guess at the rest of a foreign state
    machine."""
    state = msg.get("state")
    sess.lab_state = state if isinstance(state, str) and state.strip() else None
    await send(ws, type="lab_state", state=sess.lab_state)


async def _speak_producer(queue: "asyncio.Queue", text: str):
    """Feed `text` to the TTS consumer one complete sentence at a time, so audio
    starts on sentence 1 instead of after the whole reply is synthesized. The text
    arrives whole (the console generated it), so there is nothing to stream: this is
    the same split the Lab Agent producer does. Always queues the None sentinel last,
    even on error, so the consumer can never hang."""
    try:
        done, tail = split_sentences(text + " ")
        for s in done:
            await queue.put(s)
        tail = tail.strip()
        if len(tail) >= 2:
            await queue.put(tail)
    finally:
        await queue.put(None)


async def _run_speak(ws, sess: Session, text: str, voice=None):
    """Body of one `speak`: synthesize `text` through the EXISTING TTS path and
    stream it as reply_audio chunks + a terminal reply_audio_end, exactly as an
    assistant reply's audio has always been sent (the client's player is unchanged).
    Runs as a background task so the receive loop stays free to read a cancel_speak
    barge-in while the audio is still being generated.

    `voice` (optional, per-call) overrides the session's voice for this utterance
    only; unset falls through to the session's voice, and unset-with-the-default-
    model falls through to DEF_VOICE inside _tts_consumer, same as everywhere else."""
    queue: asyncio.Queue = asyncio.Queue()
    # _tts_consumer is gated on a commit event and measures first-audio from
    # t_perceived. There is no speculation here, so the turn is committed the moment
    # it is asked for: set the gate and start the clock now.
    commit = {"event": asyncio.Event(), "t_perceived": time.perf_counter()}
    commit["event"].set()
    model = sess.tts_model
    params = dict(sess.tts_params)
    if voice:
        params["voice"] = voice
    t0 = time.perf_counter()
    _, tts_rec = await asyncio.gather(
        _speak_producer(queue, text),
        _tts_consumer(ws, queue, commit, model, params),
        return_exceptions=True,
    )
    if isinstance(tts_rec, Exception):   # the consumer swallows synth errors, so this
        log_event({"kind": "speak", "session": sess.sid,                 # is a real bug
                   "status": f"error:{type(tts_rec).__name__}",
                   "tts": {"ms": _ms(t0), "chars": len(text), "model": model}})
        return
    err = tts_rec.get("error")
    log_event({"kind": "speak", "session": sess.sid,
               "status": "ok" if not err else (f"ok_partial:{err}" if tts_rec.get("chunks")
                                               else f"tts_error:{err}"),
               "tts": tts_rec, "first_audio_ms": tts_rec.get("first_ms")})


def _speak_task_done(sess: Session, task) -> None:
    """Done-callback safety net for the fire-and-forget speak task: retrieve any
    exception so it neither vanishes nor warns. A cancelled task IS awaited by the
    cancel path, so skip it here (calling .exception() on it would raise)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[!] speak task sid={sess.sid} crashed: {exc!r}", flush=True)


async def _cancel_speak_task(sess: Session) -> bool:
    """Stop the in-flight `speak` (barge-in). Cancelling the gather cancels both the
    producer and the TTS consumer, so no further reply_audio is sent; the client asked
    for the stop, so there is no terminal message to send it. Returns False, sending
    nothing, when nothing is speaking: cancel_speak is safe to call at any time."""
    task = sess.speak_task
    sess.speak_task = None
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001  # the task body already reported its own error
        pass
    return True


async def _start_speak(ws, sess: Session, text: str, voice=None) -> None:
    """Speak `text`, superseding anything already being spoken (at most one utterance
    per session is ever in flight). Fire-and-forget on purpose: awaiting the synth
    here would block the receive loop, and a cancel_speak that cannot be READ is not a
    barge-in."""
    if not text:
        return
    await _cancel_speak_task(sess)
    sess.speak_task = asyncio.create_task(_run_speak(ws, sess, text, voice))
    sess.speak_task.add_done_callback(lambda t: _speak_task_done(sess, t))


async def handle_speak(ws, sess: Session, msg):
    """The console hands us its reply text; we are its speaker. No LLM, no history:
    we did not write these words and we are not part of the conversation they belong
    to."""
    text = (msg.get("text") or "").strip()
    if not text:
        return
    voice = msg.get("voice")
    await _start_speak(ws, sess, text, voice if isinstance(voice, str) and voice else None)


def _abort_speak_task(sess: Session) -> None:
    """Best-effort cancel with no client-facing cleanup, for teardown paths where the
    socket is going away anyway (connection close, session switch). A no-op on every
    non-speech connection, where speak_task is always None."""
    task = sess.speak_task
    if task is not None and not task.done():
        task.cancel()
    sess.speak_task = None


async def handle_end_turn_speech(ws, sess: Session):
    """Commit the turn WITHOUT replying to it: assemble the accumulated segments,
    apply the gates, and answer with exactly one of transcript_final (the console may
    POST this) or transcript_refused (the console must NOT).

    This is the whole safety argument of speech mode, so it is worth being explicit
    about what each branch means:

      transcript_final   we heard this clearly enough that the console may act on it.
                         The text is NORMALIZED first (lab_backend.normalize_transcript):
                         the console's backend parses slots with regexes written for
                         TYPED text ("IL-6", digits), and a real recognizer writes
                         "IL 6" and "hundred". Measured on the real stack: without
                         this the backend never fills the analyte slot, re-asks the
                         same question every turn, and the conversation DEADLOCKS
                         before it can ever reach a confirmation.
      transcript_refused we heard something, but the console's backend is armed and
                         we are not confident enough about WHAT we heard. Handing this
                         over would let a misheard "yes" start a machine, so it is not
                         handed over at all. We also SPEAK the reprompt, because the
                         user needs to hear why nothing happened: silence after saying
                         "yes" is the exact failure this mode exists to prevent.

    A missing confidence block fails OPEN (accepted), matching every other floor in
    this file: the mock and a degraded-but-working ASR supply no confidence, and must
    not be locked out of confirming (see lab_backend.blocks_confirmation).

    NO reply pipeline runs here: no LLM, no Lab Agent, no _run_turn, no history. The
    console owns all of that."""
    user_text = " ".join(s for s in sess.pending if s).strip()
    asr_ms_list = list(sess.asr_ms)
    asr_conf_list = list(sess.asr_conf)
    sess.pending = []
    sess.asr_ms = []
    sess.asr_conf = []
    if not user_text:
        return
    asr_rec = _asr_rec(asr_ms_list, asr_conf_list, user_text)
    prob_mean = _turn_prob_mean(asr_rec)
    prob_min = _turn_prob_min(asr_rec)

    if lab_backend.blocks_confirmation(sess.lab_state, prob_mean):
        await send(ws, type="transcript_refused", reason="low_confidence_confirmation",
                   prob_mean=prob_mean, reprompt=lab_backend.REPROMPT)
        await _start_speak(ws, sess, lab_backend.REPROMPT)   # say it out loud, not just on screen
        log_event({"kind": "speech_turn", "session": sess.sid, "turn": sess.seq,
                   "status": "refused", "reason": "low_confidence_confirmation",
                   "lab_state": sess.lab_state, "asr": asr_rec})
        return

    text = lab_backend.normalize_transcript(user_text)
    await send(ws, type="transcript_final", text=text,
               confidence={"prob_mean": prob_mean, "prob_min": prob_min})
    log_event({"kind": "speech_turn", "session": sess.sid, "turn": sess.seq,
               "status": "accepted", "lab_state": sess.lab_state, "asr": asr_rec,
               "normalized": text != user_text})


# --------------------------------------------------------------- event channel
# Live connections (Phase 3). A Conn wraps one WS + its CURRENT session (which is
# reassigned on new_session) + a per-connection AnnounceManager. The registry lets
# an operator inject_event broadcast to every live connection and lets a stub
# completion timer route to the connection that owns the stub.
LIVE_CONNS = set()


class AnnounceManager:
    """Per-connection serial delivery of proactive announcements with severity
    arbitration. Alerts preempt (cancel an in-flight committed reply, abort an
    invisible speculation, clear a pending confirmation) and jump the queue; infos
    defer until any committed reply finishes and never interrupt. One announcement
    is delivered at a time, FIFO within severity."""

    def __init__(self, conn):
        self.conn = conn
        self._alerts = collections.deque()
        self._infos = collections.deque()
        self._wake = asyncio.Event()
        self._worker = None
        self._closed = False

    def enqueue(self, ann):
        if self._closed:
            return
        (self._alerts if ann["severity"] == "alert" else self._infos).append(ann)
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def close(self):
        self._closed = True
        self._wake.set()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    def _committed_reply_in_flight(self):
        sess = self.conn.sess
        task = sess.reply_task
        if task is None or task.done():
            return False
        commit = (sess.reply_ctx or {}).get("commit") or {}
        return bool(commit.get("committed"))

    def _pick(self):
        """The next DELIVERABLE announcement (removed), or None. Alerts are always
        deliverable (they preempt); an info is deliverable only when no committed
        reply is in flight (else it defers)."""
        if self._alerts:
            return self._alerts.popleft()
        if self._infos and not self._committed_reply_in_flight():
            return self._infos.popleft()
        return None

    def _arm_reply_wake(self):
        """When an info is blocked by a committed reply, wake the worker the moment
        that reply finishes (event-driven, no busy-poll)."""
        if not self._infos:
            return
        task = self.conn.sess.reply_task
        if task is not None and not task.done():
            task.add_done_callback(lambda _t: self._wake.set())

    async def _run(self):
        try:
            while not self._closed:
                ann = self._pick()
                if ann is None:
                    self._wake.clear()
                    self._arm_reply_wake()
                    await self._wake.wait()
                    continue
                await self._deliver(ann)
        except asyncio.CancelledError:
            pass

    async def _deliver(self, ann):
        sess = self.conn.sess
        ws = self.conn.ws
        try:
            if ann["severity"] == "alert":
                await self._preempt(sess, ws)
            await self._announce(ws, sess, ann)
        except Exception:  # noqa: BLE001  # a closed socket must not kill the worker
            pass

    async def _preempt(self, sess, ws):
        """Alert arbitration: abort an invisible speculation, cancel a committed
        reply (client sees reply_cancelled), and clear any pending confirmation
        (client sees action_cancelled superseded)."""
        task = sess.reply_task
        if task is not None and not task.done():
            commit = (sess.reply_ctx or {}).get("commit") or {}
            if commit.get("spec") and not commit.get("committed"):
                _abort_speculation(sess)                 # invisible: drop silently
            else:
                await _cancel_reply_task(ws, sess)       # committed: reply_cancelled
        if sess.pending_action is not None:
            sess.pending_action = None
            await send(ws, type="action_cancelled", reason="superseded")

    async def _announce(self, ws, sess, ann):
        eid = ann["event_id"]
        wait_ms = round((time.perf_counter() - ann["enqueue_perf"]) * 1000, 1)
        await send(ws, type="announce", event_id=eid, severity=ann["severity"],
                   text=ann["text"], source=ann["source"], ts=time.time())
        model = sess.tts_model
        params = {k: v for k, v in sess.tts_params.items() if v is not None}
        if "voice" not in params and model == DEFAULT_TTS_MODEL:
            params["voice"] = DEF_VOICE
        tts_ms = 0.0
        error_name = None
        t0 = time.perf_counter()
        try:
            wav = await synthesize(ann["text"], model=model, **params)
        except Exception as e:  # noqa: BLE001  # on TTS failure skip audio, still announce_end
            error_name = type(e).__name__
            wav = None
        if wav is not None:
            tts_ms = _ms(t0)
            await send(ws, type="announce_audio", event_id=eid,
                       audio_b64=base64.b64encode(wav).decode("ascii"),
                       sample_rate=wav_sample_rate(wav) or 22050, format="wav")
        await send(ws, type="announce_end", event_id=eid)
        log_event({"kind": "announce", "event_id": eid, "severity": ann["severity"],
                   "source": ann["source"], "wait_ms": wait_ms, "session": sess.sid,
                   "tts": {"ms": round(tts_ms, 1), "error": error_name}})


class Conn:
    """One live WS connection: its current session (reassigned on new_session) and
    its announcement manager."""

    def __init__(self, ws, sess: Session):
        self.ws = ws
        self.sess = sess
        self.announce = AnnounceManager(self)

    def close(self):
        self.announce.close()


def _conn_for_sess(sess):
    """The live Conn whose current session is `sess`, or None (small linear scan;
    the live-connection count is tiny)."""
    for c in LIVE_CONNS:
        if c.sess is sess:
            return c
    return None


def _emit_event(conn, severity, text, source):
    """Enqueue one announcement onto a connection's serial announce channel."""
    eid = uuid.uuid4().hex[:12]
    conn.announce.enqueue({"event_id": eid, "severity": severity, "text": text,
                           "source": source, "enqueue_perf": time.perf_counter()})
    return eid



async def _stub_event_timer(conn, delay, text):
    """Fire a stub completion event after `delay` seconds, unless cancelled first
    (halt / disconnect cancel it, so no event fires). Routed to the owning
    connection's CURRENT session."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if conn in LIVE_CONNS:
        _emit_event(conn, "info", text, source="stub")


def _schedule_stub_event(sess, intent, args):
    """After a stub command with a delayed completion, schedule its info event to
    the owning connection. start_centrifuge fires after its real run duration;
    set_temperature fires after about 2 s. Gated on the event channel + lab mode;
    the timer task is registered on the stub so halt()/disconnect cancel it."""
    if not (EVENTS_ENABLED and LAB_MODE):
        return
    conn = _conn_for_sess(sess)
    if conn is None:
        return
    # The announce text (alias-tolerant + None-guarded) lives in lab_gate so it is
    # unit-testable; only the DELAY is computed here. The LLM's arg key names drift
    # from the schema (rpm/speed_rpm, minutes/duration_minutes, celsius/temperature),
    # so both read through lab_gate.arg.
    if intent == "start_centrifuge":
        minutes = float(lab_gate.arg(args, "minutes") or 0)
        delay = max(0.0, minutes * 60.0)
        text = lab_gate.completion_announce(intent, args)
    elif intent == "set_temperature":
        delay = 2.0
        text = lab_gate.completion_announce(intent, args)
    elif intent in ("protocol_start", "protocol_next", "protocol_back"):
        # Arriving at a timed protocol step starts its countdown; the announcement
        # fires when that incubation / spin is done. Moving on replaces (cancels)
        # the previous step's timer, so only the current step can announce.
        _schedule_protocol_timer(conn, sess.lab_stub)
        return
    else:
        return
    task = asyncio.create_task(_stub_event_timer(conn, delay, text))
    sess.lab_stub.register_timer(task)


def _schedule_protocol_timer(conn, stub):
    """Start (or replace) the completion timer for the protocol step the stub is now
    on. No-op when the protocol is not running, is already complete, or the current
    step has no timer. The step TEXT always states the real duration;
    LA_PROTOCOL_TIMER_SCALE only compresses the actual wait (lab_gate.step_timer_s)."""
    stub.cancel_protocol_timer()   # navigating away disarms the abandoned step's timer
    proto = stub.protocol
    step = proto.get("step") or 0
    if proto.get("done") or step < 1:
        return
    delay = lab_gate.step_timer_s(step)
    if not delay:
        return
    task = asyncio.create_task(_stub_event_timer(conn, delay, lab_gate.timer_done_text(step)))
    stub.set_protocol_timer(task)


async def handle_inject_event(conn, msg):
    """Operator-only proactive event injection. A non-operator connection gets an
    error and nothing else. broadcast (default true) delivers to ALL live
    connections (every scope); broadcast false only to the sender's connection."""
    if not EVENTS_ENABLED:
        return
    if not conn.sess.is_operator:
        await send(conn.ws, type="error", text="not authorized")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        await send(conn.ws, type="error", text="inject_event needs text.")
        return
    severity = "alert" if msg.get("severity") == "alert" else "info"
    broadcast = msg.get("broadcast", True)
    targets = list(LIVE_CONNS) if broadcast else [conn]
    for t in targets:
        _emit_event(t, severity, text, source="operator")


async def handler(ws):
    # Identity is resolved ONCE per connection from the `?email=` query param and
    # carried onto every Session on this connection (initial + each new_session).
    email = _read_client_email(ws)
    # Allowlist enforcement (when LA_ALLOWLIST is non-empty): the email must be
    # present AND on the list, or the connection is refused with a single auth_error
    # and a clean close (4001), with no session created and no public fallback. When
    # the allowlist is empty (dev + smokes) there is no enforcement: an email scopes
    # the connection and its absence maps to the public scope, exactly as before.
    if ALLOWLIST:
        reason = "email_required" if email is None else (
            None if email in ALLOWLIST else "not_allowlisted")
        if reason is not None:
            await send(ws, type="auth_error", reason=reason)
            await ws.close(code=4001, reason="auth failed")
            return
    conn_scope, conn_owner, conn_is_op = _scope_for_email(email)
    # Speech-service mode is chosen on the connect URL (`?mode=speech`) and is fixed
    # for the life of the connection, like the scope. Anything else (including no
    # param at all) is the default conversational path, unchanged.
    speech_mode = _read_client_mode(ws) == "speech"
    sess = _apply_scope(Session(), conn_scope, conn_owner, conn_is_op)
    sess.speech_mode = speech_mode
    # Register this connection so operator broadcasts and stub completion timers can
    # reach it (Phase 3). conn.sess tracks the CURRENT session across new_session.
    conn = Conn(ws, sess)
    LIVE_CONNS.add(conn)
    peer = getattr(ws, "remote_address", ("?",))[0]
    print(f"[+] client {peer} scope={conn_scope}{' operator' if conn_is_op else ''}"
          f"{' speech-service' if speech_mode else ''}", flush=True)
    ready = llm_client is not None
    await send(ws, type="status",
               text="Ready. Click start and speak." if ready else "No API key configured.")
    # initial handshake: seed the client's assistant TTS controls with the
    # session's current params (all unset on connect) and the server defaults.
    await send(ws, type="tts_params", **_tts_params_payload(sess))
    # Segment capture: tell the client whether the debug capture mode is on, so it
    # knows whether to show the label controls. Always sent (on=false is the normal
    # case); it is additive, so a client that ignores it is unaffected.
    await send(ws, type="capture_state", on=CAPTURE_ENABLED)
    await send(ws, type="session_started", id=sess.sid, number=_session_rank(sess),
               name=sess.name, started_at=sess.started_at,
               scope=sess.scope, is_operator=sess.is_operator)
    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                await handle_segment(ws, sess, bytes(message))
                continue
            try:
                msg = json.loads(message)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "audio_segment":
                b64 = (msg.get("audio_b64") or "").strip()
                if not b64:
                    continue
                try:
                    pcm = base64.b64decode(b64)
                except Exception:
                    await send(ws, type="error", text="audio decode failed")
                    continue
                await handle_segment(ws, sess, pcm)
            elif mtype == "end_turn":
                # The fork. In speech mode the turn is COMMITTED but never REPLIED to:
                # the accepted transcript goes to the console, which owns the
                # conversation, so no reply pipeline runs here at all.
                if sess.speech_mode:
                    await handle_end_turn_speech(ws, sess)
                else:
                    await handle_end_turn(ws, sess)
            # The speech-service messages. Each is gated on the connection having
            # asked for speech mode, so on any other connection they stay exactly what
            # they were before this feature existed: unknown types, silently ignored.
            elif mtype == "set_lab_state" and sess.speech_mode:
                await handle_set_lab_state(ws, sess, msg)
            elif mtype == "speak" and sess.speech_mode:
                await handle_speak(ws, sess, msg)
            elif mtype == "cancel_speak" and sess.speech_mode:
                await _cancel_speak_task(sess)   # barge-in; a no-op when nothing is speaking
            elif mtype == "cancel_turn":
                # barge-in: user started talking over the reply. Cancel the
                # in-flight reply task and send the terminal reply_cancelled;
                # a no-op if nothing is in flight.
                await _cancel_reply_task(ws, sess)
            elif mtype == "list_voices":
                await handle_list_voices(ws, sess, msg)
            elif mtype == "tts_test":
                await handle_tts_test(ws, sess, msg)
            elif mtype == "list_tts_models":
                await handle_list_tts_models(ws, sess)
            elif mtype == "set_tts_model":
                await handle_set_tts_model(ws, sess, msg)
            elif mtype == "set_tts_params":
                await handle_set_tts_params(ws, sess, msg)
            elif mtype == "get_hints":
                await handle_get_hints(ws, sess)
            elif mtype == "set_hints":
                await handle_set_hints(ws, sess, msg)
            elif mtype == "list_sessions":
                await handle_list_sessions(ws, sess)
            elif mtype == "get_session":
                await handle_get_session(ws, sess, msg)
            elif mtype == "rename_session":
                await handle_rename_session(ws, sess, msg)
            elif mtype == "delete_session":
                await handle_delete_session(ws, sess, msg)
            elif mtype == "inject_event":
                await handle_inject_event(conn, msg)
            elif mtype == "client_info":
                await handle_client_info(sess, msg)
            elif mtype == "label_segment":
                await handle_label_segment(ws, sess, msg)
            elif mtype == "new_session":
                # reassigns the function-local `sess` in this same handler() call
                # frame; no `nonlocal` needed since it's one scope, not a closure.
                # All later messages on this connection read the same name, so
                # they naturally operate on the new Session from here on.
                # Carry the TTS config over (see new_session_preserving): it's a
                # user preference, so the client's controls stay in sync without a
                # tts_params re-emit and the contract is unchanged.
                _abort_reply_task(sess)   # drop any reply still running on the old session
                _abort_speak_task(sess)   # ... and any speech-mode audio still going out
                # the outgoing session just ended: let Claude title it (when the
                # user never did) BEFORE we let go of it, and push session_renamed
                # with the OLD sid so an open History panel updates the row live.
                prev = sess
                prev_named = bool(prev.name)
                await autoname_session(prev)
                if not prev_named and prev.name:
                    await send(ws, type="session_renamed", id=prev.sid, name=prev.name)
                # identity is per-connection: re-stamp the SAME scope/owner/operator
                # onto the fresh session (new_session_preserving carries only the TTS
                # config, which is a user preference, not identity).
                sess = _apply_scope(new_session_preserving(prev), conn_scope, conn_owner, conn_is_op)
                conn.sess = sess   # keep the connection registry pointing at the live session
                await send(ws, type="session_started", id=sess.sid, number=_session_rank(sess),
                           name=sess.name, started_at=sess.started_at,
                           scope=sess.scope, is_operator=sess.is_operator)
            elif mtype == "ping":
                await send(ws, type="status", text="pong")
    except Exception as e:  # noqa: BLE001
        print(f"[!] handler {peer}: {e}", flush=True)
    finally:
        # Deregister first so no new broadcast / timer targets this connection, then
        # cancel its announce worker and any pending stub completion timers.
        LIVE_CONNS.discard(conn)
        conn.close()
        sess.lab_stub.cancel_timers()
        _abort_reply_task(sess)   # don't leave a turn task sending on a closed socket
        _abort_speak_task(sess)   # ... nor a speech-mode synth
        # the connection dropped == the session ended: title it if the user never
        # did. This runs after _abort_reply_task, and any earlier barge-in already
        # committed its partial assistant text via _cancel_reply_task, so whatever
        # is in sess.messages is what gets titled. Bounded so a hung API call
        # cannot stall teardown; fully soft (no session_renamed, the socket is
        # gone). The name appears on the next list_sessions.
        try:
            await asyncio.wait_for(autoname_session(sess), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        print(f"[-] client {peer}", flush=True)


# ----------------------------------------------------------------------------- static
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".cjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".omnivad": "application/octet-stream",
    ".bin": "application/octet-stream",
    ".data": "application/octet-stream",
}


def _static_response(raw_path):
    rel = raw_path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if rel == "":
        rel = "index.html"
    try:
        target = (STATIC_DIR / rel).resolve()
        target.relative_to(STATIC_DIR)            # path-traversal guard
    except (ValueError, OSError):
        target = None
    if target is None or not target.is_file():
        body = b"404 not found"
        h = Headers()
        h["Content-Type"] = "text/plain; charset=utf-8"
        h["Content-Length"] = str(len(body))
        return Response(404, "Not Found", h, body)
    body = target.read_bytes()
    h = Headers()
    h["Content-Type"] = _MIME.get(target.suffix, "application/octet-stream")
    h["Content-Length"] = str(len(body))
    h["Cache-Control"] = "no-store"
    return Response(200, "OK", h, body)


def process_request(connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    return _static_response(request.path)


async def main():
    key_state = "key OK" if llm_client is not None else "NO KEY"
    tts_desc = ",".join(f"{mid}@{url}" for mid, url in TTS_MODELS.items())
    print(f"lab-assistant  http+ws://{WS_HOST}:{WS_PORT}  "
          f"ASR={FUNASR_URL}  TTS=[{tts_desc}] (default={DEFAULT_TTS_MODEL})  "
          f"LLM={LLM_MODEL} ({key_state})", flush=True)
    if ALLOWLIST:
        print(f"[auth] email allowlist active: {len(ALLOWLIST)} addresses; connections "
              f"must supply an allowlisted ?email=", flush=True)
    async with serve(handler, WS_HOST, WS_PORT, max_size=16 * 1024 * 1024,
                     process_request=process_request):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
