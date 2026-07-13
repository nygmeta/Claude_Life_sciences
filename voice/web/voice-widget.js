// voice-widget.js: the browser half of the voice pipeline, as a reusable class.
//
// WHAT IT IS
// Mic capture -> in-browser VAD (OmniVAD WASM) -> turn segmentation -> WebSocket to a
// speech-mode server, plus streamed playback of the audio that server sends back. It is
// the plumbing lifted out of the voice assistant's own page so a different app can gain
// speech in and speech out without reimplementing any of it.
//
// WHAT IT DELIBERATELY DOES NOT DO
// No UI. It creates no elements, touches no CSS, and reads no DOM. No session, no history,
// no conversation, no transcript rendering, no hint/TTS panels: the HOST app owns all of
// that. The widget reports what it hears through callbacks and the host renders whatever
// it likes. It also does not decide what a transcript MEANS: it hands the host an accepted
// transcript and the host acts on it.
//
// THE SAFETY REFUSAL (why onRefused exists and why it is not just an error)
// The host tells the server its backend state via setLabState(). When the host is armed to
// execute something on the user's word (say "awaiting_confirmation"), the server holds the
// ASR to a higher bar. A turn the server heard but is NOT confident enough about, while the
// host is armed, comes back as `transcript_refused` instead of `transcript_final`. The turn
// is real, it just must not be acted on: mishearing "cancel" as "continue" in front of an
// armed action is the failure this prevents. The server speaks its own reprompt (the widget
// plays it, no host involvement). The host MUST NOT act on a refusal: it is not a transcript,
// it is the server declining to vouch for one. onTranscript is the only callback that
// authorizes action.
//
// PORTING NOTE
// This is a faithful lift, not a rewrite. The tuning constants below were calibrated on real
// speech and are reproduced exactly, including one library-config bug that is preserved on
// purpose (see minSilenceFrames). Two behaviors that look like bugs are load-bearing and
// commented as such: the audio context is built synchronously inside the start gesture (iOS),
// and barge-in fires on the accepted transcript rather than on VAD speech-start.

import { OmniStreamVAD } from "./vendor/omnivad/dist/index.js";

// Resolved against THIS module's URL, not the host page's. The host imports the widget from
// wherever it likes and does not need to know that a WASM model lives next to it. The WASM
// glue itself is self-locating the same way (vendor/omnivad/dist/index.js resolves its .wasm
// against its own import.meta.url), so only the model needs an explicit URL here.
const VAD_MODEL_URL = new URL("./vendor/omnivad/models/stream-vad.omnivad", import.meta.url).href;

// ---- tuning: carried over from the app's page unchanged ----
const FRAME = 160;                 // 10ms @ 16kHz, the frame size the streaming VAD expects
const DEFAULTS = {
  vadThreshold: 0.5,        // OmniVAD speech activation threshold [0,1]
  minSilenceFrames: 60,     // silence that ends one speech SEGMENT (10ms/frame). SEE THE WARNING IN makeVad().
  turnSilenceMs: 900,       // extra silence after a segment end before the whole TURN ends
  preRollFrames: 30,        // ~300ms of pre-speech audio prepended to a segment (onset recovery)
  minSegSeconds: 0.3,       // segments shorter than this are dropped, never sent
  bufferSize: 2048,         // ScriptProcessor block size
  reconnectMs: 2000,        // delay before retrying a dropped socket
  bargeInMode: "transcript",// "transcript" (faithful) or "vad". See handleBargeIn().
  reconnect: true,
};
const SAMPLE_RATE = 16000;   // the wire format is fixed: the server reads our bytes as 16 kHz PCM16

export class VoiceWidget {
  constructor(opts = {}) {
    const o = { ...DEFAULTS, ...opts };
    this.wsUrl = o.wsUrl || defaultWsUrl();
    this.opts = o;

    this.onTranscript = opts.onTranscript || (() => {});
    this.onRefused    = opts.onRefused    || (() => {});
    this.onPartial    = opts.onPartial    || (() => {});
    this.onStatus     = opts.onStatus     || (() => {});
    this.onError      = opts.onError      || (() => {});

    this.ws = null;
    this.closing = false;       // disconnect() was called: do not auto-reconnect
    this.reconnectTimer = null;
    this.status = "idle";

    // The lab state is re-sent on every (re)connect. The server builds a fresh session per
    // connection that starts at its defaults, so a state set before a drop would silently
    // disarm the safety gate on reconnect. Remembering it here is what keeps the gate armed.
    this.labState = null;

    // capture
    this.ctx = null; this.stream = null; this.proc = null; this.src = null; this.sink = null;
    this.vad = null;
    this.micOn = false;
    this.turnTimer = null;
    this.hwSampleRate = null;

    // playback. playCtx is SEPARATE from the capture context and is kept across stopMic/startMic:
    // on iOS a context only ever plays if it was resumed inside a user gesture, so it is created
    // in the same tap that starts the mic and then reused.
    this.playCtx = null;
    this.audioQueue = [];        // FIFO of base64 clips for the in-flight utterance
    this.curSource = null;       // the BufferSourceNode currently playing
    this.decoding = false;       // a clip is between decodeAudioData and start(); blocks double-play
    this.playGen = 0;            // bumped on every stop; a decode resolving for an old gen is discarded
    this.streamEnded = false;    // reply_audio_end arrived for the in-flight utterance
    this.acceptingAudio = false; // gates stale reply_audio from an utterance we cancelled
    this.speaking = false;       // the server is streaming us audio right now
    this.speakSettle = null;     // resolver for the pending speak() promise
  }

  // ---- connection ----

  // Resolves once the socket is open. A later drop reconnects on its own (opts.reconnect);
  // only a failure to EVER open rejects, so the host can show a real connection error.
  connect() {
    if (this.ws && this.ws.readyState <= 1) return Promise.resolve();
    this.closing = false;
    return new Promise((resolve, reject) => {
      let opened = false;
      const open = () => {
        this.ws = new WebSocket(this.wsUrl);
        this.ws.binaryType = "arraybuffer";
        this.ws.onopen = () => {
          opened = true;
          // Re-arm the server's view of the host: a fresh connection knows nothing about the
          // backend state, and an unset state means an unarmed safety gate.
          if (this.labState != null) this.send({ type: "set_lab_state", state: this.labState });
          this.setStatus(this.micOn ? "listening" : "idle");
          resolve();
        };
        this.ws.onmessage = (ev) => {
          let m; try { m = JSON.parse(ev.data); } catch { return; }
          this.handleServer(m);
        };
        this.ws.onerror = () => {
          if (!opened) { reject(new Error("voice socket failed to connect: " + this.wsUrl)); return; }
          this.fail("voice socket error");
        };
        this.ws.onclose = () => {
          this.settleSpeak("disconnected");
          if (this.closing || !this.opts.reconnect) return;
          this.clearReconnect();
          this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; open(); }, this.opts.reconnectMs);
        };
      };
      open();
    });
  }

  disconnect() {
    this.closing = true;
    this.clearReconnect();
    this.stopMic();
    this.stopPlayback();
    this.settleSpeak("disconnected");
    if (this.ws) {
      // Drop the handlers before closing: onclose would otherwise schedule a reconnect for a
      // socket the host has explicitly finished with.
      this.ws.onopen = this.ws.onmessage = this.ws.onerror = this.ws.onclose = null;
      try { this.ws.close(); } catch {}
      this.ws = null;
    }
    if (this.playCtx) { try { this.playCtx.close(); } catch {} this.playCtx = null; }
    this.setStatus("idle");
  }

  // ---- host-facing controls ----

  // The host calls this whenever its backend state changes. It ARMS the server's safety gate:
  // while the state says an action is awaiting the user's word, the server refuses transcripts
  // it is not confident about rather than handing the host a possible mishearing.
  setLabState(state) {
    this.labState = state;
    this.send({ type: "set_lab_state", state });
  }

  // Resolves when playback finishes. Also resolves (never rejects, never hangs) if the user
  // barges in, if the host cancels, or if the socket drops, so a host awaiting it cannot wedge.
  speak(text, voice) {
    this.settleSpeak("superseded");     // a new utterance replaces any still-pending one
    this.armAudio();
    const msg = { type: "speak", text };
    if (voice) msg.voice = voice;
    this.send(msg);
    return new Promise((resolve) => { this.speakSettle = resolve; });
  }

  cancelSpeak() {
    if (!this.speaking && !this.curSource && this.audioQueue.length === 0) return;
    this.stopPlayback();
    this.send({ type: "cancel_speak" });
    this.settleSpeak("cancelled");
  }

  // ---- microphone + VAD ----

  async startMic() {
    if (this.micOn) return;
    let ctx = null;
    // The AudioContext is constructed and resumed SYNCHRONOUSLY here, before the getUserMedia
    // await, because iOS creates every context suspended and only honours resume() from inside
    // a user gesture. Awaiting the mic first breaks the gesture chain and leaves a context that
    // never runs: the widget would report "listening" while no audio ever reached the VAD.
    // startMic() must therefore be called from a real user gesture (a click/tap handler).
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      ctx = new AC();                 // no sampleRate option: WebKit only allows the hardware rate
      ctx.resume().catch(() => {});
      this.ensurePlayCtx();           // create + resume the PLAYBACK context in the same gesture, or iOS will never let the assistant speak
    } catch (e) {
      this.fail("audio setup failed: " + errText(e));
      return;
    }
    // Mobile browsers hide getUserMedia entirely on an insecure origin, where it would otherwise
    // surface as a bare TypeError. Say so plainly instead.
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      try { ctx.close(); } catch {}
      this.fail("mic unavailable: this page must be served over https (or localhost)");
      return;
    }
    let stream = null;
    // Only getUserMedia is inside this try, so a setup failure cannot be mislabelled as a
    // denied permission.
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      try { ctx.close(); } catch {}
      this.fail(micErrText(e));
      return;
    }
    try {
      await this.createCapture(stream, ctx);
    } catch (e) {
      try { ctx.close(); } catch {}
      try { stream.getTracks().forEach(t => t.stop()); } catch {}
      this.fail("audio setup failed: " + errText(e));
      return;
    }
    this.ctx = ctx;
    this.stream = stream;
    this.hwSampleRate = ctx.sampleRate;
    this.micOn = true;
    this.setStatus(this.speaking ? "speaking" : "listening");
  }

  stopMic() {
    if (!this.micOn && !this.ctx) return;
    clearTimeout(this.turnTimer); this.turnTimer = null;
    this.send({ type: "end_turn" });      // flush a half-finished turn so it is still transcribed
    if (this.proc) { this.proc.onaudioprocess = null; try { this.proc.disconnect(); } catch {} }
    if (this.src)  { try { this.src.disconnect(); } catch {} }
    if (this.sink) { try { this.sink.disconnect(); } catch {} }
    if (this.stream) { try { this.stream.getTracks().forEach(t => t.stop()); } catch {} }
    if (this.ctx) { try { this.ctx.close(); } catch {} }
    // The page never disposed the VAD (it just dropped the reference), which leaks the WASM
    // handle across a stop/start cycle. A widget that can be torn down and rebuilt has to.
    if (this.vad) { try { this.vad.dispose(); } catch {} }
    this.proc = this.src = this.sink = this.ctx = this.stream = this.vad = null;
    this.micOn = false;
    // playCtx is deliberately NOT closed here: it is kept across stop/start so the assistant can
    // still speak after the mic is released, and so iOS does not need a fresh gesture to restart.
    this.setStatus(this.speaking ? "speaking" : "idle");
  }

  async makeVad() {
    // WARNING, PRESERVED BUG: OmniStreamVAD.create() reads `minSilenceFrame` (SINGULAR). The key
    // passed here is the plural one, which it ignores, so the segment-end silence actually runs at
    // the library default of 20 frames (200ms), NOT the 60 frames (600ms) this value implies. This
    // is reproduced exactly as the app has it, because every other constant here was calibrated on
    // real speech AGAINST that effective 200ms. Correcting the key without recalibrating the rest
    // would triple the segment-end pause and change turn-taking in the field. Fix it deliberately,
    // with fresh calibration, or not at all.
    return OmniStreamVAD.create({
      modelUrl: VAD_MODEL_URL,
      threshold: this.opts.vadThreshold,
      minSilenceFrames: this.opts.minSilenceFrames,
    });
  }

  async createCapture(stream, ctx) {
    this.vad = await this.makeVad();
    const resample = makeResampler(ctx.sampleRate, SAMPLE_RATE);
    const minSegBytes = SAMPLE_RATE * this.opts.minSegSeconds * 2;   // PCM16: 2 bytes/sample
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(this.opts.bufferSize, 1, 1);
    const sink = ctx.createGain(); sink.gain.value = 0;   // the graph must reach a destination to pull audio, but must not be audible

    let carry = new Float32Array(0);
    let collecting = false, segChunks = [], preRoll = [];

    const sendSeg = () => {
      const buffer = floatToInt16Buffer(segChunks);
      segChunks = [];
      if (buffer.byteLength < minSegBytes) return;   // too short to be speech: drop, do not bother the ASR
      this.send({ type: "audio_segment", audio_b64: arrayBufferToBase64(buffer), sample_rate: SAMPLE_RATE });
    };

    proc.onaudioprocess = (e) => {
      if (!this.vad) return;
      const inp = resample(e.inputBuffer.getChannelData(0));   // 16 kHz mono, whatever the hardware rate is
      const buf = new Float32Array(carry.length + inp.length);
      buf.set(carry, 0); buf.set(inp, carry.length);
      let i = 0;
      for (; i + FRAME <= buf.length; i += FRAME) {
        const frame = buf.subarray(i, i + FRAME);
        const r = this.vad.processFrame(frame);
        if (r && r.isSpeechStart && !collecting) {
          collecting = true; segChunks = preRoll.slice(); preRoll = [];
          clearTimeout(this.turnTimer); this.turnTimer = null;   // still talking: cancel a pending turn-end
          this.handleBargeIn();
        }
        const copy = frame.slice();
        if (collecting) segChunks.push(copy);
        else { preRoll.push(copy); if (preRoll.length > this.opts.preRollFrames) preRoll.shift(); }
        if (r && r.isSpeechEnd && collecting) {
          collecting = false; sendSeg();
          // End of a segment, not necessarily of the turn. Arm the turn-end so the host gets ONE
          // transcript for a sentence spoken with pauses in it, not one per pause.
          clearTimeout(this.turnTimer);
          this.turnTimer = setTimeout(() => { this.turnTimer = null; this.send({ type: "end_turn" }); }, this.opts.turnSilenceMs);
        }
      }
      carry = buf.slice(i);
    };

    src.connect(proc); proc.connect(sink); sink.connect(ctx.destination);
    if (ctx.state === "suspended") ctx.resume().catch(() => {});   // iOS can re-suspend while the graph is being wired
    this.src = src; this.proc = proc; this.sink = sink;
  }

  // ---- barge-in ----
  //
  // Two modes, and the DEFAULT IS NOT THE OBVIOUS ONE. Read this before changing it.
  //
  // "transcript" (default, and what the app actually ships): the assistant is hushed only when
  // the server ACCEPTS a transcript, i.e. when real, addressed speech has been recognized. Noise,
  // a cough, a door, a colleague talking across the bench: none of these interrupt the assistant,
  // because none of them survive the ASR's confidence gate. The cost is latency: barge-in waits
  // for ASR, so the assistant may talk over the first ~0.5-1.5s of genuine speech before stopping.
  // That tradeoff was made deliberately, in a noisy room, after the VAD-triggered version proved
  // too twitchy to use. Do not "improve" it back.
  //
  // "vad": hush the moment the VAD sees speech. Snappier, and correct in a quiet room with a
  // headset. Wrong in a shared lab: it is the version that got reverted.
  handleBargeIn() {
    if (this.opts.bargeInMode !== "vad") return;   // the transcript path handles it, in handleServer()
    this.interruptSpeech();
  }

  // Hush whatever the assistant is saying and tell the server to stop synthesizing it.
  interruptSpeech() {
    if (!this.speaking && !this.curSource && this.audioQueue.length === 0) return;
    this.stopPlayback();
    this.send({ type: "cancel_speak" });
    this.settleSpeak("barged-in");
  }

  // ---- server messages ----

  handleServer(m) {
    switch (m.type) {
      case "transcript":
        // A live caption for one segment. NOT an authorization to act: the turn is not finished
        // and the server has not gated it yet.
        this.onSafe(this.onPartial, (m.text || "").trim());
        break;

      case "transcript_final":
        // The server accepted this turn. This is the ONLY message the host may act on, and (in
        // the default mode) the moment the assistant gets hushed: an accepted transcript is proof
        // that what interrupted was real speech addressed to us, not room noise.
        if (this.opts.bargeInMode !== "vad") this.interruptSpeech();
        this.onSafe(this.onTranscript, (m.text || "").trim(), m.confidence);
        break;

      case "transcript_refused":
        // Heard, but not vouched for, while the host was armed to execute. The host must NOT act.
        // The server speaks its own reprompt, so arm the audio path for it: without this, a
        // reprompt landing right after a barge-in (which set acceptingAudio false) would be
        // dropped, and the user would be left in silence in front of an armed action.
        this.armAudio();
        this.onSafe(this.onRefused, {
          reason: m.reason,
          prob_mean: m.prob_mean,
          reprompt: m.reprompt,
        });
        break;

      case "reply_audio":
        // A stale chunk from an utterance the user barged in on still arrives (the server finishes
        // synthesizing what it already started), so an unarmed audio path must drop it.
        if (this.acceptingAudio) { this.audioQueue.push(m.audio_b64); this.playNext(); }
        break;

      case "reply_audio_end":
        this.streamEnded = true;
        this.playNext();          // drains the queue, then settles the speak() promise
        break;

      case "error":
        this.settleSpeak("error");
        this.fail(m.text || "server error");
        break;
    }
  }

  // ---- playback ----

  // Arm the audio path for an utterance that is about to stream in, from either direction: the
  // host called speak(), or the server is about to speak a refusal reprompt on its own.
  armAudio() {
    this.acceptingAudio = true;
    this.audioQueue = [];
    this.streamEnded = false;
    this.speaking = true;
    this.setStatus("speaking");
  }

  ensurePlayCtx() {
    if (!this.playCtx) {
      try { this.playCtx = new (window.AudioContext || window.webkitAudioContext)(); }
      catch { return null; }
    }
    if (this.playCtx.state === "suspended") this.playCtx.resume().catch(() => {});
    return this.playCtx;
  }

  // Hard-stop the current clip, drop everything queued, and start ignoring the rest of this
  // utterance's audio. Bumping playGen voids any decode that is mid-flight so it cannot start a
  // source after we have torn down.
  stopPlayback() {
    this.playGen++;
    this.decoding = false;
    if (this.curSource) { try { this.curSource.onended = null; this.curSource.stop(); } catch {} this.curSource = null; }
    this.audioQueue = [];
    this.streamEnded = false;
    this.acceptingAudio = false;
    this.speaking = false;
    this.setStatus(this.micOn ? "listening" : "idle");
  }

  // Clips arrive one per sentence, in order, over an ordered socket, so a plain FIFO preserves
  // order and the first sentence can play while later ones are still being synthesized. Each clip
  // is decoded then played as a BufferSourceNode whose onended chains the next.
  playNext() {
    if (this.curSource || this.decoding) return;   // a clip is playing or decoding; its completion chains the next
    if (this.audioQueue.length === 0) {
      if (this.streamEnded) {
        this.speaking = false;
        this.setStatus(this.micOn ? "listening" : "idle");
        this.settleSpeak("done");
      }
      return;
    }
    const ctx = this.ensurePlayCtx();
    if (!ctx) { this.audioQueue = []; this.fail("audio unavailable in this browser"); return; }
    if (ctx.state === "suspended") {
      // Starting a source on a suspended context never fires onended, so the queue would stall in
      // silence. Try to wake it once, and if it will not wake, say so rather than hanging.
      const gen = this.playGen;
      this.decoding = true;
      ctx.resume().catch(() => {}).then(() => {
        if (gen !== this.playGen) return;   // stopped while resuming: a newer owner holds the state
        this.decoding = false;
        if (ctx.state === "suspended") { this.audioQueue = []; this.fail("audio blocked: start the mic from a tap first"); return; }
        this.playNext();
      });
      return;
    }
    const b64 = this.audioQueue.shift();
    const gen = this.playGen;
    this.decoding = true;
    decodeAudio(ctx, b64).then((buf) => {
      if (gen !== this.playGen) return;      // barged in during decode: discard
      this.decoding = false;
      if (!buf) { this.playNext(); return; } // undecodable clip: skip it, never stall the queue
      try {
        const node = ctx.createBufferSource();
        node.buffer = buf; node.connect(ctx.destination);
        node.onended = () => { if (gen === this.playGen) { this.curSource = null; this.playNext(); } };
        this.curSource = node;
        node.start();
      } catch { this.curSource = null; this.playNext(); }
    });
  }

  // ---- plumbing ----

  send(p) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(p)); }

  settleSpeak(outcome) {
    const done = this.speakSettle;
    this.speakSettle = null;
    if (done) done(outcome);
  }

  setStatus(s) {
    if (s === this.status) return;
    this.status = s;
    this.onSafe(this.onStatus, s);
  }

  fail(msg) {
    this.setStatus("error");
    this.onSafe(this.onError, msg);
  }

  // A throwing host callback must not corrupt widget state (it would otherwise abort an
  // onaudioprocess block mid-segment, or leave a decode chain unterminated).
  onSafe(fn, ...args) {
    try { fn(...args); } catch (e) { try { this.onError("callback threw: " + errText(e)); } catch {} }
  }

  clearReconnect() {
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
  }
}

// ---- module-local helpers (pure, no widget state) ----

// Same origin as the page, in speech mode. The server serves both the page and the socket.
function defaultWsUrl() {
  if (typeof location === "undefined") return "ws://localhost:8765/?mode=speech";
  if (location.protocol === "https:") return `wss://${location.host}/?mode=speech`;
  if (location.protocol === "http:")  return `ws://${location.host}/?mode=speech`;
  return "ws://localhost:8765/?mode=speech";
}

function floatToInt16Buffer(chunks) {
  let n = 0; for (const c of chunks) n += c.length;
  const pcm = new Float32Array(n); let o = 0;
  for (const c of chunks) { pcm.set(c, o); o += c.length; }
  const i16 = new Int16Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) { let s = Math.max(-1, Math.min(1, pcm[i])); i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff; }
  return i16.buffer;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer); let bin = "";
  const chunk = 0x8000;   // chunked so a long segment cannot blow the argument limit of String.fromCharCode
  for (let i = 0; i < bytes.length; i += chunk) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  return btoa(bin);
}

// base64 -> ArrayBuffer for decodeAudioData. Throws on malformed input; the caller wraps it.
function b64ToArrayBuffer(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

// Decode one base64 wav, resolving null on any failure (never rejects, so a bad clip skips instead
// of stalling the queue). Supports both the promise and the older callback form of decodeAudioData,
// since iOS WebKit shipped the callback-only form for years.
function decodeAudio(ctx, b64) {
  return new Promise((resolve) => {
    let arr;
    try { arr = b64ToArrayBuffer(b64); } catch { resolve(null); return; }
    let done = false;
    const ok = (buf) => { if (!done) { done = true; resolve(buf || null); } };
    const bad = () => { if (!done) { done = true; resolve(null); } };
    try {
      const p = ctx.decodeAudioData(arr, ok, bad);
      if (p && typeof p.then === "function") p.then(ok, bad);
    } catch { bad(); }
  });
}

// ---- streaming resampler: the hardware rate -> 16 kHz ----
// We cannot just ask for a 16 kHz AudioContext: WebKit pins the context to the hardware rate
// (48000 on iOS) and refuses anything else, which is why the mic failed there and not on desktop
// Chrome. So the context runs native and the downsampling happens here, because the wire format is
// fixed: the server reads our bytes as 16 kHz PCM16.
//
// Design: a box (moving-average) lowpass of ceil(ratio) taps, then a linear-interpolated read at a
// fractional step of inRate/16000. The box is the anti-aliasing filter: without it, everything
// above 8 kHz folds back down into the speech band. Both the fractional read position and the
// filter's look-back tail live in closure state and survive across blocks, so a long signal cut
// into blocks resamples identically to the same signal in one piece: no clicks at the block seams
// and no drift of the 10ms VAD frame boundaries.
function makeResampler(inRate, outRate) {
  if (inRate === outRate) return (block) => block;      // native 16 kHz: identity, zero cost
  const ratio = inRate / outRate;
  const taps = Math.max(1, Math.ceil(ratio));           // box width: about one output sample period
  let hist = new Float32Array(0);                       // unconsumed input + the filter's look-back
  let pos = 0;                                          // fractional read position within hist+block
  const box = (x, i) => {
    let s = 0, n = 0;
    for (let k = i - taps + 1; k <= i; k++) if (k >= 0) { s += x[k]; n++; }
    return n ? s / n : 0;
  };
  return (block) => {
    const x = new Float32Array(hist.length + block.length);
    x.set(hist, 0); x.set(block, hist.length);
    const out = new Float32Array(Math.ceil(x.length / ratio) + 1);
    let n = 0, p = pos;
    while (Math.floor(p) + 1 < x.length) {              // need both interpolation neighbours
      const i0 = Math.floor(p), f = p - i0;
      const y0 = box(x, i0), y1 = box(x, i0 + 1);
      out[n++] = y0 + (y1 - y0) * f;
      p += ratio;
    }
    const keep = Math.max(0, Math.floor(p) - taps + 1); // carry exactly the tail the next block reads back into
    hist = x.slice(keep);
    pos = p - keep;
    return out.subarray(0, n);
  };
}

// e.name is the diagnostic field (NotAllowedError, NotReadableError, ...); never report the
// message alone.
function errText(e) {
  const name = (e && e.name) ? e.name : "Error";
  const msg = (e && e.message) ? e.message : String(e);
  return name + ": " + msg;
}

function micErrText(e) {
  const name = e && e.name;
  if (name === "NotAllowedError" || name === "SecurityError") return "mic permission denied";
  if (name === "NotFoundError") return "no microphone found";
  return "mic error: " + errText(e);
}

export default VoiceWidget;
