#!/usr/bin/env python3
"""Operator-facing feature verification against the LIVE stack.

LIVE-RUN STATUS: accepted. First full live run passed on 2026-07-13, both the fast
set (9/9 deterministic scenarios, F9 INFO) and --slow (10/10, F10 expiry included),
against real FunASR + Claude + gepard. Because real ASR is variable (it correctly
rejects a low-confidence irreversible command about a third of the time, and pads a
1-word "stop" with a spurious word), the command scenarios retry a mis-heard turn a
few times; that is ASR variance, not a feature that half-works.

This drives the deployed system end to end with REAL synthesized speech: for each
scenario it synthesizes the utterance on the live gepard TTS, resamples the returned
22050 Hz mono WAV to 16 kHz PCM16 with a stdlib resampler, and sends it as a real
audio_segment over the WebSocket. Real FunASR transcribes it on the GPU and real
Claude answers. Everything is real except the human voice. This is deliberately
DIFFERENT from the mock-based smokes (scripts/run_local_smoke.sh): those validate
the code with mock ASR/TTS; this validates the running deployment.

Interpreter: run with the project's Python, which has `websockets`:
    python3 scripts/verify_features.py [--slow]
Only stdlib + `websockets` are used (no numpy / scipy / httpx needed), so the
system python works too as long as `websockets` is importable.

Prereqs (checked at startup; the run fails fast with instructions if any is down):
  - the orchestrator on http://localhost:8765
  - the gepard TTS on http://localhost:8040   (via deploy/dev-forward.sh)
  - the FunASR ASR on http://localhost:8030    (via deploy/dev-forward.sh)

The run creates its own sessions in the PUBLIC scope and takes a few minutes (real
GPU ASR + Claude per turn). Use --slow to also run F10 (pending expiry), which waits
out the full LA_PENDING_TTL_S.

Output: one line per scenario "FEAT <id> <label>: PASS|FAIL|INFO|SKIP" with a
one-line detail, then a summary and "VERIFY: PASS (n/m deterministic scenarios)".
Exit 0 iff every deterministic (non-INFO, non-SKIP) scenario passed.
"""
import argparse
import array
import asyncio
import base64
import io
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
import wave

try:
    import websockets
except ImportError:  # pragma: no cover
    print("FATAL: the `websockets` package is required. Run with the project's Python "
          "(or `pip install websockets`).", file=sys.stderr)
    sys.exit(2)

ORCH_URL = os.environ.get("LA_VERIFY_ORCH", "http://localhost:8765")
WS_URL = os.environ.get("LA_VERIFY_WS", "ws://localhost:8765/")
TTS_URL = os.environ.get("LA_VERIFY_TTS", "http://localhost:8040")
ASR_URL = os.environ.get("LA_VERIFY_ASR", "http://localhost:8030")
VOICE = os.environ.get("LA_TTS_VOICE", "en_oak")
SEG_RATE = 16000                 # the segment rate the client / VAD path uses
TURN_TIMEOUT = 90.0              # real GPU ASR + Claude per turn
SYNTH_TIMEOUT = 60.0
ANNOUNCE_TIMEOUT = 20.0
WS_MAX = 32 * 1024 * 1024


# --------------------------------------------------------------------- audio (pure)
def _box_filter(samples, width):
    """A centered moving-average low-pass over an int sample array, the anti-alias
    step before downsampling. width <= 1 is a no-op. Uses a prefix sum so it stays
    linear in the sample count."""
    if width <= 1:
        return samples
    n = len(samples)
    pref = [0] * (n + 1)
    for i in range(n):
        pref[i + 1] = pref[i] + samples[i]
    out = array.array("h", bytes(2 * n))
    half = width // 2
    for i in range(n):
        a = max(0, i - half)
        b = min(n, i + half + 1)
        out[i] = int((pref[b] - pref[a]) // (b - a))
    return out


def resample_pcm16(pcm, src_rate, dst_rate):
    """Resample little-endian PCM16 from src_rate to dst_rate with a box low-pass
    (anti-alias, only when downsampling) plus linear interpolation. Scipy-free, the
    same shape of resampler the browser client uses to feed 16 kHz to the stack.
    Identity when the rates match."""
    if src_rate == dst_rate:
        return pcm
    s = array.array("h")
    s.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":
        s.byteswap()
    n = len(s)
    if n == 0:
        return b""
    ratio = src_rate / float(dst_rate)
    filt = _box_filter(s, int(math.ceil(ratio))) if ratio > 1 else s
    out_n = max(1, int(round(n * dst_rate / float(src_rate))))
    out = array.array("h", bytes(2 * out_n))
    for i in range(out_n):
        pos = i * (n - 1) / float(max(out_n - 1, 1))
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        val = filt[lo] * (1.0 - frac) + filt[hi] * frac
        out[i] = int(max(-32768, min(32767, round(val))))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def wav_to_pcm16_mono(wav_bytes):
    """Parse a WAV byte string into (pcm16_bytes, sample_rate). Requires mono
    PCM16 (what gepard returns). Raises ValueError otherwise."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"expected mono PCM16, got ch={w.getnchannels()} "
                             f"width={w.getsampwidth()}")
        return w.readframes(w.getnframes()), w.getframerate()


def white_noise_pcm16(dur_s, rate, rms):
    """`dur_s` seconds of Gaussian white noise as PCM16 at `rate`, scaled so its
    RMS is `rms` of full scale (a speech-like loudness). Seeded per call for a
    fresh sample each run."""
    n = int(dur_s * rate)
    amp = rms * 32768.0
    rnd = random.Random()
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        out[i] = int(max(-32768, min(32767, rnd.gauss(0.0, amp))))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


# --------------------------------------------------------------------------- http
def _http_get(url, timeout=6.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except Exception:  # noqa: BLE001  # any failure = not reachable
        return None, None


def synth_speech_16k(text, voice=VOICE):
    """Synthesize `text` on the live gepard TTS and return 16 kHz PCM16 bytes. One
    retry, since a real GPU service can drop a connection under load."""
    body = json.dumps({"text": text, "voice": voice}).encode("utf-8")
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(TTS_URL + "/synthesize", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=SYNTH_TIMEOUT) as r:
                wav = r.read()
            pcm, rate = wav_to_pcm16_mono(wav)
            return resample_pcm16(pcm, rate, SEG_RATE)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0)
    raise RuntimeError(f"TTS synth failed for {text!r}: {type(last).__name__}: {last}")


def check_prereqs():
    """True iff the orchestrator, TTS, and ASR are all reachable. On failure prints
    exactly what to start and returns False (the caller exits 2)."""
    orch = _http_get(ORCH_URL + "/", timeout=6)[0] is not None
    tts = _http_get(TTS_URL + "/health", timeout=6)[0] is not None
    asr = _http_get(ASR_URL + "/v1/models", timeout=6)[0] is not None
    if orch and tts and asr:
        return True
    print("PREREQ FAIL: the live stack is not fully up.", file=sys.stderr)
    print(f"  orchestrator :8765  {'OK' if orch else 'DOWN'}", file=sys.stderr)
    print(f"  gepard TTS   :8040  {'OK' if tts else 'DOWN'}", file=sys.stderr)
    print(f"  FunASR ASR   :8030  {'OK' if asr else 'DOWN'}", file=sys.stderr)
    print("\nTo start it:", file=sys.stderr)
    if not (tts and asr):
        print("  bash deploy/dev-forward.sh --bg     # SSH forward to the GPU host "
              "(:8030 + :8040); the ASR/TTS services must be running there "
              "(deploy/install-services.sh installs them as systemd --user units).",
              file=sys.stderr)
    if not orch:
        print("  bash deploy/run-web-local.sh        # the orchestrator on :8765",
              file=sys.stderr)
    return False


# --------------------------------------------------------------------------- ws
class Stack:
    """One live WS connection to the orchestrator. Each turn returns its own list of
    received messages, so a scenario can inspect exactly what its utterance drew."""

    def __init__(self, ws):
        self.ws = ws

    async def close(self):
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    async def drain_handshake(self, timeout=10.0):
        # the server opens with status / tts_params / capture_state / session_started
        await self.collect(lambda m: m.get("type") == "session_started", timeout)

    async def collect(self, pred, timeout):
        """Collect messages until `pred` is true or `timeout` elapses. Returns
        (messages, matched)."""
        got = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return got, False
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return got, False
            except Exception:  # noqa: BLE001  # closed socket
                return got, False
            try:
                m = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            got.append(m)
            if pred(m):
                return got, True

    async def _send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def turn(self, pcm, terminal=("reply_audio_end",), timeout=TURN_TIMEOUT):
        """Send one speech segment + end_turn, collect until a terminal message."""
        await self._send({"type": "audio_segment", "sample_rate": SEG_RATE,
                          "audio_b64": base64.b64encode(pcm).decode("ascii")})
        await self._send({"type": "end_turn"})
        return await self.collect(lambda m: m.get("type") in terminal, timeout)

    async def segment(self, pcm, terminal, timeout=TURN_TIMEOUT):
        """Send one speech segment WITHOUT end_turn (for the fast-path stop, which is
        intercepted in handle_segment before a turn ends)."""
        await self._send({"type": "audio_segment", "sample_rate": SEG_RATE,
                          "audio_b64": base64.b64encode(pcm).decode("ascii")})
        return await self.collect(lambda m: m.get("type") in terminal, timeout)

    async def request(self, obj, want_type, timeout=15.0):
        """Send a JSON control message and return the first reply of `want_type`."""
        await self._send(obj)
        msgs, ok = await self.collect(lambda m: m.get("type") == want_type, timeout)
        return msgs[-1] if ok else None


async def open_stack():
    ws = await websockets.connect(WS_URL, max_size=WS_MAX)
    st = Stack(ws)
    await st.drain_handshake()
    return st


def first(msgs, t):
    return next((m for m in msgs if m.get("type") == t), None)


def find(msgs, t):
    return [m for m in msgs if m.get("type") == t]


def _tx(msgs):
    """The text of the last transcript in a batch, for detail lines."""
    tr = None
    for m in msgs:
        if m.get("type") == "transcript":
            tr = m
    return (tr or {}).get("text")


def _step_of(action_executed):
    """result.state.protocol.step off an action_executed message, or None."""
    res = (action_executed or {}).get("result") or {}
    return ((res.get("state") or {}).get("protocol") or {}).get("step")


async def turn_until(st, text, pred, tries=4, timeout=TURN_TIMEOUT):
    """Retry a full turn (a fresh synth each time) until pred(messages) holds. This
    absorbs real-ASR variance where the FEATURE works but a given transcription may
    mis-hear or land just under a confidence threshold; a stale pending from a missed
    attempt is superseded by the next turn's utterance. Returns (messages, matched,
    attempts_used)."""
    msgs = []
    for i in range(tries):
        msgs, _ = await st.turn(synth_speech_16k(text), timeout=timeout)
        if pred(msgs):
            return msgs, True, i + 1
        await asyncio.sleep(0.3)
    return msgs, False, tries


async def arm_pending(st, text, intent, tries=6):
    """Arm a confirmation pending for `intent`, retrying on the real-ASR failure
    modes that do NOT indicate a broken feature: a mis-transcription that never
    reaches the tool, or a genuine low-confidence rejection (an irreversible command
    with prob_min below the gate's floor is CORRECTLY rejected, not armed: live ASR
    rejects it about a third of the time). Each retry re-synthesizes. Returns
    (action_pending_msg_or_None, last_messages, attempts_used)."""
    msgs = []
    for i in range(tries):
        msgs, _ = await st.turn(synth_speech_16k(text))
        ap = first(msgs, "action_pending")
        if ap is not None and ap.get("intent") == intent:
            return ap, msgs, i + 1
        await asyncio.sleep(0.3)
    return None, msgs, tries


# --------------------------------------------------------------------- scenarios
async def feat_f1(state):
    st = await open_stack()
    try:
        msgs, _ = await st.turn(synth_speech_16k("what is two plus two"))
        tr = first(msgs, "transcript")
        state["f1_transcript"] = tr
        rd = first(msgs, "reply_done")
        ra = find(msgs, "reply_audio")
        rae = first(msgs, "reply_audio_end")
        ok = (bool(tr and (tr.get("text") or "").strip()) and rd is not None
              and len(ra) >= 1 and rae is not None)
        detail = (f"heard {(tr or {}).get('text')!r}; reply "
                  f"{((rd or {}).get('text') or '')[:60]!r}; reply_audio x{len(ra)}")
        return ("PASS" if ok else "FAIL", detail)
    finally:
        await st.close()


async def feat_f2(state):
    tr = state.get("f1_transcript")
    if tr is None:
        return ("FAIL", "no transcript captured in F1 to inspect")
    conf = tr.get("confidence")
    if not conf:
        return ("FAIL", "F1 transcript carried no confidence block")
    pm, pmin = conf.get("prob_mean"), conf.get("prob_min")
    ok = (isinstance(pm, (int, float)) and isinstance(pmin, (int, float))
          and 0.0 <= pm <= 1.0 and 0.0 <= pmin <= 1.0)
    return ("PASS" if ok else "FAIL", f"prob_mean={pm} prob_min={pmin}")


async def feat_f3():
    st = await open_stack()
    try:
        # A SAFE command auto-proceeds; a rare low-confidence transcription instead
        # holds it for confirmation, so retry until it executes (the next utterance
        # supersedes any stale confirm).
        def executed_safe(ms):
            ae = first(ms, "action_executed")
            return ae is not None and ae.get("intent") == "read_sensor" and ae.get("confirmed") is False
        msgs, ok, tries = await turn_until(st, "read the temperature sensor", executed_safe)
        ae = first(msgs, "action_executed")
        note = f" (in {tries} tries)" if tries > 1 else ""
        detail = (f"intent={(ae or {}).get('intent')} confirmed={(ae or {}).get('confirmed')}"
                  f"{note} (heard {_tx(msgs)!r})")
        return ("PASS" if ok else "FAIL", detail)
    finally:
        await st.close()


async def feat_f4():
    st = await open_stack()
    try:
        ap, m1, tries = await arm_pending(st, "dispense 50 microliters into well A3", "dispense")
        if ap is None:
            return ("FAIL", f"no dispense pending armed in {tries} tries (last heard {_tx(m1)!r})")
        phrase_ok = ap.get("confirm_phrase") == "confirm dispense"
        note = f" (armed in {tries} tries)" if tries > 1 else ""

        # a bare affirmation must NOT execute a bound (irreversible) pending
        m2, _ = await st.turn(synth_speech_16k("yes please"))
        yes_executed = first(m2, "action_executed") is not None
        superseded = any(m.get("type") == "action_cancelled" and m.get("reason") == "superseded"
                         for m in m2)
        if superseded:
            # the ASR mangled "yes please" into a non-confirm/cancel command, which
            # supersedes and clears the pending: re-arm before the real confirm.
            ap2, mre, _ = await arm_pending(st, "dispense 50 microliters into well A3", "dispense")
            if ap2 is None:
                return ("FAIL", f"re-arm after supersede failed (heard {_tx(mre)!r})")
            note += " (yes was superseded by ASR; re-armed the pending)"

        # the exact bound phrase executes; retry in case ASR drops "dispense" (the
        # pending is kept on a reprompt_unbound, so a retry re-attempts it)
        m3, executed, ctries = await turn_until(
            st, "confirm dispense",
            lambda ms: (first(ms, "action_executed") or {}).get("confirmed") is True, tries=3)
        if ctries > 1:
            note += f" (confirm took {ctries} tries)"
        ok = phrase_ok and not yes_executed and executed
        detail = (f"confirm_phrase={ap.get('confirm_phrase')!r} yes_executed={yes_executed} "
                  f"bound_executed={executed}{note}")
        if not executed:
            detail += f" (confirm heard {_tx(m3)!r})"
        return ("PASS" if ok else "FAIL", detail)
    finally:
        await st.close()


async def feat_f5():
    st = await open_stack()
    try:
        ap, m1, tries = await arm_pending(st, "dispense 10 microliters into well B2", "dispense")
        if ap is None:
            return ("FAIL", f"no dispense pending armed in {tries} tries (last heard {_tx(m1)!r})")
        m2, _ = await st.turn(synth_speech_16k("cancel"))
        ac = first(m2, "action_cancelled")
        executed = first(m2, "action_executed") is not None
        ok = ac is not None and ac.get("reason") == "user" and not executed
        note = f" (armed in {tries} tries)" if tries > 1 else ""
        return ("PASS" if ok else "FAIL",
                f"cancelled_reason={(ac or {}).get('reason')} executed={executed}{note}")
    finally:
        await st.close()


async def feat_f6():
    st = await open_stack()
    try:
        ap, m1, tries = await arm_pending(st, "dispense 20 microliters into well C1", "dispense")
        if ap is None:
            return ("FAIL", f"no dispense pending armed in {tries} tries (last heard {_tx(m1)!r})")
        # An emergency stop is a bare segment (no end_turn): the fast path intercepts
        # it in handle_segment. "stop now" is used over a bare "stop" because FunASR
        # pads a 1-word "stop" with a spurious content word (~"stop that") that fails
        # the is_stop guard, while "stop now" transcribes as a clean stop keyword +
        # filler. The pending stays armed until it halts, so we retry the utterance
        # until an is_stop-valid transcription lands (a real ASR variance, not a
        # feature failure). A non-stop transcription just accumulates harmlessly (no
        # end_turn is ever sent on this connection).
        heard = []
        for _ in range(6):
            m2, _ = await st.segment(synth_speech_16k("stop now"),
                                     terminal=("action_halted",), timeout=20)
            ah = first(m2, "action_halted")
            if ah is not None:
                arm_note = f" (armed in {tries} tries)" if tries > 1 else ""
                return ("PASS", f"halted={ah.get('halted')!r}{arm_note}")
            heard.append(_tx(m2))
            await asyncio.sleep(0.3)
        return ("FAIL", f"no action_halted after 6 stop attempts (heard {heard})")
    finally:
        await st.close()


async def feat_f7():
    """F7's target is the PROACTIVE EVENT CHANNEL: after a temperature set, the
    assistant should speak the completion announcement unprompted. At real-ASR
    confidence the REVERSIBLE set_temperature usually arms a confirmation (prob_min
    below the auto-proceed floor) rather than proceeding, so we confirm it (a
    reversible pending takes a loose confirm) and then assert the announce triple."""
    st = await open_stack()
    try:
        m1, _ = await st.turn(synth_speech_16k("set the temperature to 30 degrees"))
        ae = first(m1, "action_executed")
        note = ""
        if ae is None and first(m1, "action_pending") is not None:
            note = " (armed a pending first: ASR prob_min below the auto-proceed floor; confirmed it)"
            # a reversible pending takes a loose confirm; retry in case a confirm
            # lands under the confirm floor and re-prompts
            mb, ok_c, _ = await turn_until(
                st, "confirm", lambda ms: first(ms, "action_executed") is not None, tries=3)
            ae = first(mb, "action_executed")
        executed = ae is not None
        # the temperature must actually be set to 30 (regression guard for the
        # temperature-vs-celsius arg-name bug this scenario originally exposed).
        temp = (((ae or {}).get("result") or {}).get("state") or {}).get("temperature")
        temp_ok = temp == 30 or temp == 30.0
        # the stub completion timer fires ~2 s after execute: wait for the triple
        m2, _ = await st.collect(lambda m: m.get("type") == "announce_end", ANNOUNCE_TIMEOUT)
        ann = first(m2, "announce")
        aa = first(m2, "announce_audio")
        aend = first(m2, "announce_end")
        ann_text = (ann or {}).get("text") or ""
        # the announce must carry the number and never say "None degrees"
        ann_ok = ann is not None and "30" in ann_text and "None" not in ann_text
        ok = executed and temp_ok and ann_ok and aend is not None
        detail = (f"executed={executed} temp={temp} announce={ann is not None} "
                  f"announce_audio={aa is not None} announce_end={aend is not None}{note}")
        if ann is not None:
            detail += f" text={ann_text!r}"
        return ("PASS" if ok else "FAIL", detail)
    finally:
        await st.close()


async def feat_f8():
    st = await open_stack()
    try:
        # "start the protocol" over "start the plasmid miniprep protocol": FunASR
        # mangles "plasmid miniprep" to a ~0.02 prob_min transcription, which the
        # SAFE gate then correctly holds for confirmation instead of proceeding. The
        # short form starts the same (and only) protocol and transcribes cleanly.
        # Retry until it actually executes step 1 (a low-confidence run may hold it).
        s1 = None
        for _ in range(4):
            m1, _ = await st.turn(synth_speech_16k("start the protocol"))
            s1 = _step_of(first(m1, "action_executed"))
            if s1 == 1:
                break
            await asyncio.sleep(0.3)
        # advance one step; retry only while it has NOT advanced (retrying a
        # successful "next" would over-advance to step 3).
        s2 = None
        for _ in range(3):
            m2, _ = await st.turn(synth_speech_16k("next step"))
            s2 = _step_of(first(m2, "action_executed"))
            if s2 == 2:
                break
            await asyncio.sleep(0.3)
        # status must report the current step (2) without changing it. The model
        # sometimes answers "you're on step 2" from context without calling the tool,
        # so retry until protocol_status actually runs (it is idempotent).
        def status_2(ms):
            ae = first(ms, "action_executed")
            return ae is not None and ae.get("intent") == "protocol_status" and _step_of(ae) == 2
        m3, ok3, s3tries = await turn_until(st, "what step am I on", status_2, tries=4)
        s3 = _step_of(first(m3, "action_executed"))
        ok = s1 == 1 and s2 == 2 and ok3
        note = f" (status took {s3tries} tries)" if s3tries > 1 else ""
        detail = (f"steps: start={s1} next={s2} status={s3}{note}; the step-2 incubation "
                  f"timer will announce after its real duration (not waited)")
        return ("PASS" if ok else "FAIL", detail)
    finally:
        await st.close()


async def feat_f9():
    st = await open_stack()
    try:
        noise = white_noise_pcm16(1.2, SEG_RATE, rms=0.08)
        # send segment + end_turn; wait first for a transcript (fast), then, if the
        # segment was accepted, drain its reply so nothing is left mid-flight.
        await st._send({"type": "audio_segment", "sample_rate": SEG_RATE,
                        "audio_b64": base64.b64encode(noise).decode("ascii")})
        await st._send({"type": "end_turn"})
        msgs, got = await st.collect(lambda m: m.get("type") == "transcript", timeout=30)
        tr = first(msgs, "transcript")
        if tr is None:
            return ("INFO", "ASR returned nothing for white noise (empty result: the "
                            "gate had nothing to reject, the desired outcome)")
        disc = tr.get("discarded")
        if disc:
            return ("INFO", f"gate fired: transcript discarded={disc!r} text={tr.get('text')!r}")
        # accepted (a hallucination passed the floor): drain the reply it triggered
        await st.collect(lambda m: m.get("type") == "reply_audio_end", timeout=60)
        conf = (tr.get("confidence") or {}).get("prob_mean")
        return ("INFO", f"accepted noise as speech (possible hallucination): "
                        f"text={tr.get('text')!r} prob_mean={conf}")
    finally:
        await st.close()


async def feat_f10(slow):
    if not slow:
        return ("SKIP", "run with --slow to exercise it (waits out LA_PENDING_TTL_S ~120 s)")
    st = await open_stack()
    try:
        m1, _ = await st.turn(synth_speech_16k("dispense 5 microliters into well D4"))
        if first(m1, "action_pending") is None:
            return ("FAIL", f"no pending armed (heard {_tx(m1)!r})")
        ttl = float(os.environ.get("LA_PENDING_TTL_S", "120"))
        wait = ttl + 8.0
        print(f"      F10: pending armed; waiting {wait:.0f}s for it to expire ...", flush=True)
        waited = 0.0
        while waited < wait:
            chunk = min(15.0, wait - waited)
            await asyncio.sleep(chunk)
            waited += chunk
            print(f"      F10: {wait - waited:.0f}s remaining ...", flush=True)
        m2, _ = await st.turn(synth_speech_16k("confirm dispense"))
        ac = first(m2, "action_cancelled")
        executed = first(m2, "action_executed") is not None
        ok = ac is not None and ac.get("reason") == "expired" and not executed
        return ("PASS" if ok else "FAIL",
                f"cancelled_reason={(ac or {}).get('reason')} executed={executed} (ttl={ttl:.0f}s)")
    finally:
        await st.close()


async def feat_f11():
    st = await open_stack()
    original = None
    try:
        cur = await st.request({"type": "get_hints"}, "hints")
        if cur is None:
            return ("FAIL", "get_hints returned nothing")
        original = {"hotwords": list(cur.get("hotwords") or []),
                    "replacements": dict(cur.get("replacements") or {})}
        sk, sv = "verifyfeatures_sentinel_src", "verifyfeatures_sentinel_dst"
        new_repl = dict(original["replacements"])
        new_repl[sk] = sv
        await st.request({"type": "set_hints", "hotwords": original["hotwords"],
                          "replacements": new_repl}, "hints")
        check = await st.request({"type": "get_hints"}, "hints")
        got = (check or {}).get("replacements") or {}
        ok = got.get(sk) == sv
        return ("PASS" if ok else "FAIL", f"sentinel_persisted={ok}")
    finally:
        # ALWAYS restore the original hints exactly, even on failure: never leave the
        # sentinel behind for a real user.
        if original is not None:
            try:
                await st.request({"type": "set_hints", "hotwords": original["hotwords"],
                                  "replacements": original["replacements"]}, "hints", timeout=15)
            except Exception:  # noqa: BLE001
                print("      F11 WARNING: could not restore original hints", flush=True)
        await st.close()


SCENARIOS = [
    ("F1", "chat-turn", "feat_f1"),
    ("F2", "asr-confidence", "feat_f2"),
    ("F3", "safe-command", "feat_f3"),
    ("F4", "bound-confirm", "feat_f4"),
    ("F5", "cancel", "feat_f5"),
    ("F6", "fast-stop", "feat_f6"),
    ("F7", "proactive-event", "feat_f7"),
    ("F8", "protocol", "feat_f8"),
    ("F9", "noise-gate", "feat_f9"),
    ("F10", "expiry", "feat_f10"),
    ("F11", "hints-roundtrip", "feat_f11"),
]


async def _dispatch(fid, state, slow):
    if fid == "F1":
        return await feat_f1(state)
    if fid == "F2":
        return await feat_f2(state)
    if fid == "F3":
        return await feat_f3()
    if fid == "F4":
        return await feat_f4()
    if fid == "F5":
        return await feat_f5()
    if fid == "F6":
        return await feat_f6()
    if fid == "F7":
        return await feat_f7()
    if fid == "F8":
        return await feat_f8()
    if fid == "F9":
        return await feat_f9()
    if fid == "F10":
        return await feat_f10(slow)
    if fid == "F11":
        return await feat_f11()
    return ("FAIL", "unknown scenario")


async def run(slow):
    state = {}
    results = []
    for fid, label, _ in SCENARIOS:
        try:
            status, detail = await _dispatch(fid, state, slow)
        except asyncio.TimeoutError:
            status, detail = "FAIL", "timed out waiting for the live stack"
        except Exception as e:  # noqa: BLE001  # a crashed scenario is a FAIL, not a run-abort
            status, detail = "FAIL", f"{type(e).__name__}: {e}"
        results.append((fid, label, status, detail))
        print(f"FEAT {fid} {label}: {status} - {detail}", flush=True)
    return results


def main():
    ap = argparse.ArgumentParser(description="Live-stack feature verification.")
    ap.add_argument("--slow", action="store_true",
                    help="also run F10 (pending expiry), which waits out LA_PENDING_TTL_S")
    args = ap.parse_args()

    print("=" * 72)
    print("lab-assistant live feature verification")
    print("This drives the DEPLOYED stack with real synthesized speech (real ASR +")
    print("Claude + TTS). It creates its own sessions in the PUBLIC scope and takes a")
    print("few minutes. Nothing here is mocked except the human voice.")
    print("=" * 72, flush=True)   # flush so this header precedes any stderr prereq output

    if not check_prereqs():
        return 2

    results = asyncio.run(run(args.slow))

    print("\n" + "-" * 72)
    print("SUMMARY")
    for fid, label, status, detail in results:
        print(f"  {fid:<4} {label:<16} {status}")
    deterministic = [r for r in results if r[2] in ("PASS", "FAIL")]
    passed = [r for r in deterministic if r[2] == "PASS"]
    info = [r for r in results if r[2] == "INFO"]
    skipped = [r for r in results if r[2] == "SKIP"]
    n, m = len(passed), len(deterministic)
    overall = (n == m)
    print("-" * 72)
    print(f"  {len(info)} INFO (observational), {len(skipped)} SKIP")
    print(f"VERIFY: {'PASS' if overall else 'FAIL'} ({n}/{m} deterministic scenarios)")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
