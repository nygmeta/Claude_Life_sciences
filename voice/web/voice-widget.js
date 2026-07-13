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
//
// TUNE AND OBSERVE (the settings-drawer API)
// A host that wants a settings panel gets three groups of additive methods, none of which a
// host has to use: the widget with no new options behaves exactly as it did before they existed.
//   tune the client:  getVadOptions / setVadOptions / listInputDevices / setInputDevice
//   watch it work:    onVadLevel (per frame, throttled) and onMetrics (once per turn)
//   tune the server:  setHints / listVoices / setTtsModel / setTtsParams / testVoice
// The server-side ones are thin passthroughs: they send a message the orchestrator already
// accepts and resolve with its reply. They add no protocol of their own.
//
// What they may NOT do is touch the safety path. transcript_refused is not a tunable: a panel
// may render its numbers, and there is deliberately no option anywhere below that suppresses a
// refusal or downgrades it to a transcript. See onRefused above for why.

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

// ---- constants for the additive tune/observe API (they change nothing above) ----
const FRAME_MS = 10;                  // FRAME samples at SAMPLE_RATE is 10ms, restated in ms for the ms-facing API
// OmniStreamVAD's OWN default for its (correctly spelled) minSilenceFrame, i.e. the segment-end
// silence that is actually in force today, since the key the widget passes is the ignored one.
// getVadOptions() reports THIS, not the 60 in DEFAULTS, because it must report what is running.
const LIB_MIN_SILENCE_FRAMES = 20;
// onVadLevel throttle. One VAD frame is 10ms, so a callback per frame would be 100 Hz into the
// host's render path. Emit one in 5 (20 Hz), counted in FRAMES rather than wall-clock: the frame
// count is the audio's own clock, so the rate cannot drift with block size or a busy main thread.
const VAD_LEVEL_EVERY_FRAMES = 5;
const BARGE_IN_MODES = ["transcript", "vad"];
// The server's own TTS param set, copied field for field from its handler. Anything not on this
// list is not a param the server reads, so sending it would be inventing protocol.
const TTS_PARAM_KEYS = ["voice", "temperature", "cfg_scale", "top_k", "max_frames"];
// tts_test additionally picks its model per request (the assistant's model is a session setting;
// an audition's is not). `voice` is missing here on purpose: testVoice takes it as an argument.
const TTS_TEST_KEYS = ["model", "temperature", "cfg_scale", "top_k", "max_frames"];
// A request whose reply never comes must reject, not hang a host's await forever. Synthesis is
// the slow one (a GPU cold start, then a whole sentence), so it gets its own, much longer budget.
const REQUEST_TIMEOUT_MS = 15000;
const TTS_TEST_TIMEOUT_MS = 60000;

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
    // Telemetry, both optional. onVadLevel is the only callback on the audio hot path, so a host
    // that did not ask for it must not pay for it: the emit is skipped entirely, not called into
    // a no-op 20 times a second.
    this.onVadLevel   = opts.onVadLevel   || null;
    this.onMetrics    = opts.onMetrics    || null;
    // Fired when the server reports the gate's numbers (armed + confirmFloor). connect()
    // resolves when the SOCKET opens, strictly before that message lands, so a host that
    // reads confirmFloor right after connect() reads null. Push it instead.
    this.onLabState   = opts.onLabState   || null;
    // Fired when the server proposes a correction ({raw, proposed}) instead of vouching for
    // the transcript. The host RENDERS it and does nothing else: acting on a proposal would
    // defeat the point, which is that a human confirms the reading before anything is sent.
    this.onVerify     = opts.onVerify     || null;

    this.ws = null;
    this.closing = false;       // disconnect() was called: do not auto-reconnect
    this.reconnectTimer = null;
    this.status = "idle";

    // The lab state is re-sent on every (re)connect. The server builds a fresh session per
    // connection that starts at its defaults, so a state set before a drop would silently
    // disarm the safety gate on reconnect. Remembering it here is what keeps the gate armed.
    this.labState = null;

    // The gate's own numbers, as the SERVER reports them (see the confirmFloor getter below).
    // Written only from an inbound lab_state message, never from the host.
    this._confirmFloor = null;
    this._armed = false;

    // capture
    this.ctx = null; this.stream = null; this.proc = null; this.src = null; this.sink = null;
    this.vad = null;
    this.micOn = false;
    this.turnTimer = null;
    this.hwSampleRate = null;
    this.capture = null;         // control handle into the capture closure (see createCapture)
    this.inputDeviceId = null;   // null = whatever the browser picks, which is the pre-existing behavior
    this.vadGen = 0;             // bumped per VAD rebuild; a build resolving for an old gen is discarded

    // telemetry state
    this.levelFrames = 0;        // frames since the last onVadLevel emit
    this.levelPeak = 0;          // highest probability seen inside the current throttle window
    this.lastSegSentAt = null;   // when the newest audio_segment went out, for the ASR round trip
    this.turn = newTurnMetrics();

    // in-flight request/reply waiters (setHints, listVoices, setTtsModel, setTtsParams, testVoice)
    this.reqSeq = 0;
    this.pendingReqs = new Map();
    this.testSource = null;      // the voice-audition clip, kept OUT of the conversation queue
    this.testGen = 0;            // same guard as playGen, for the audition's own decode

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
          // The replies these were waiting for died with the socket. Reject now rather than let
          // each one sit out its timeout, and reject BEFORE any reconnect: a request is bound to
          // the connection it was sent on, and the new one starts from the server's defaults.
          this.rejectAllReqs("voice socket closed");
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
    this.stopTestVoice();
    this.settleSpeak("disconnected");
    this.rejectAllReqs("voice widget disconnected");   // the handlers are dropped below, so onclose will not do it
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
  // `context` is optional: {questions, reply}, the backend's last question and reply. The
  // verification layer needs it to resolve a mishearing ("i am six") against what was
  // actually asked ("which analyte?"). A host that passes nothing behaves exactly as before.
  setLabState(state, context) {
    this.labState = state;
    this.labContext = context || null;
    const msg = { type: "set_lab_state", state };
    if (context && Array.isArray(context.questions)) msg.questions = context.questions;
    if (context && typeof context.reply === "string") msg.reply = context.reply;
    this.send(msg);
  }

  // The confidence floor a spoken confirmation must clear before the server will vouch for it,
  // and whether the server currently considers the host armed. Both come from the server's
  // lab_state reply, and both are for DISPLAY: a panel can finally show the floor next to the
  // prob_mean the recognizer actually reported, instead of "n/a".
  //
  // THERE IS NO SETTER, AND THERE MUST NEVER BE ONE. The floor travels one way, server to page.
  // A page that could set it could set it to 0, and a floor of 0 refuses nothing: the gate that
  // exists to stop a misheard "yes" from starting a machine would still be there, still be armed,
  // and still pass everything. That is worse than having no gate, because the panel would go on
  // displaying a floor while enforcing nothing. The browser is the side of this connection an
  // attacker (or a well-meaning "let me just turn this down for testing" patch) can reach, which
  // is precisely why the value is not reachable from here. If the floor needs to change, it
  // changes on the server, where it is enforced.
  //
  // These are getters, not fields, so `vw.confirmFloor = 0` THROWS rather than silently sticking
  // a lie onto the widget for a panel to read back.
  // Is the speech service actually reachable RIGHT NOW? A host needs this to decide
  // whether it can synthesize on demand or must fall back to something it already has.
  // readyState 1 is OPEN: not "connecting", not "closing". A socket that is merely
  // trying to reconnect cannot speak, and saying it can would mean silence at the moment
  // the words were due.
  get connected() { return !!this.ws && this.ws.readyState === 1; }

  get confirmFloor() { return this._confirmFloor; }   // number, or null until the first lab_state
  // The server's TTS defaults ({voice, temperature, cfg_scale, top_k, max_frames}), or null
  // until the first tts_params message. A host should seed its voice picker from this rather
  // than from the first option in the list, which is a different voice.
  get ttsDefaults() { return this._ttsDefaults || null; }
  get armed() { return this._armed; }                 // boolean

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
      const audio = { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true };
      // deviceId is added ONLY when the host has chosen one, so the default constraint set is
      // byte-for-byte the one that shipped. `exact` is deliberate: a device the user explicitly
      // picked and that is now gone must fail loudly (setInputDevice falls back), not silently
      // record from a different microphone than the panel says it is recording from.
      if (this.inputDeviceId) audio.deviceId = { exact: this.inputDeviceId };
      stream = await navigator.mediaDevices.getUserMedia({ audio });
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
    this.capture = null;
    this.vadGen++;              // void a rebuild that is mid-flight: it must not install into a stopped mic
    this.levelFrames = 0; this.levelPeak = 0;
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
    const cfg = {
      modelUrl: VAD_MODEL_URL,
      threshold: this.opts.vadThreshold,
      minSilenceFrames: this.opts.minSilenceFrames,
    };
    // The ONE way to reach the key the library actually reads, and it is opt-in: only a host that
    // explicitly sets segMs (constructor or setVadOptions) gets it. Leave segMs unset and this
    // config is exactly the two keys above, so the effective 200ms the constants were calibrated
    // against is untouched. Setting segMs is therefore a deliberate act of recalibration, which is
    // what the warning above asks for, rather than a silent correction of the bug for everyone.
    if (this.opts.segMs != null) {
      cfg.minSilenceFrame = Math.max(1, Math.round(this.opts.segMs / FRAME_MS));
    }
    return OmniStreamVAD.create(cfg);
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
      this.lastSegSentAt = nowMs();   // starts the clock the per-turn asr_ms in onMetrics reads
      this.send({ type: "audio_segment", audio_b64: arrayBufferToBase64(buffer), sample_rate: SAMPLE_RATE });
    };

    // End the in-flight segment the way an isSpeechEnd would: send it, then arm the turn timer.
    // Only the VAD swap in rebuildVad() needs this, and it needs it badly: the replacement VAD
    // starts in the silence state with no idea that speech is already under way, so it would
    // never fire the isSpeechEnd that closes this segment. Without the flush, segChunks would
    // keep growing until some LATER speech ended, and the user's sentence would be sent as one
    // long clip with a swap-length hole of silence buried in it.
    this.capture = {
      flushSegment: () => {
        if (!collecting) return;
        collecting = false;
        sendSeg();
        clearTimeout(this.turnTimer);
        this.turnTimer = setTimeout(() => { this.turnTimer = null; this.send({ type: "end_turn" }); }, this.opts.turnSilenceMs);
      },
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
        if (r) this.noteVadLevel(r);
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

  // ---- telemetry ----

  // One VAD frame. Reports smoothedProb, NOT the raw confidence: smoothedProb is the number the
  // library compares against `threshold`, so it is the only one a meter can draw a threshold line
  // across and have the line mean what it looks like it means.
  //
  // The throttle keeps the PEAK of the window rather than its last frame. A level meter exists to
  // show the moment speech crossed the line, and that moment can be a single 10ms frame: sampling
  // 1 frame in 5 would silently drop 4 out of every 5 chances to see it.
  noteVadLevel(r) {
    if (!this.onVadLevel) return;
    const p = typeof r.smoothedProb === "number" ? r.smoothedProb : r.confidence;
    if (typeof p !== "number") return;
    if (p > this.levelPeak) this.levelPeak = p;
    if (++this.levelFrames < VAD_LEVEL_EVERY_FRAMES) return;
    const peak = this.levelPeak;
    this.levelFrames = 0; this.levelPeak = 0;
    this.onSafe(this.onVadLevel, peak);
  }

  // One segment's transcript arrived. Accumulate only what the SERVER folded into the turn: a
  // segment it discarded (below the noise floor) or judged side speech is not part of the turn,
  // so counting its confidence here would report a turn the server never assembled.
  noteSegment(m) {
    const sentAt = this.lastSegSentAt;
    this.lastSegSentAt = null;   // cleared for a dropped segment too, or its send time would be
                                 // charged to whichever segment reported next
    if (m.discarded || m.addressed === false) return;
    if (sentAt != null) { this.turn.asrMs += nowMs() - sentAt; this.turn.measured++; }
    this.turn.segments++;
    const c = m.confidence;
    if (c) {
      if (typeof c.prob_mean === "number") this.turn.probMeans.push(c.prob_mean);
      if (typeof c.prob_min === "number") this.turn.probMins.push(c.prob_min);
    }
  }

  // The turn is over (accepted or refused): emit once, then reset. Always resets, even with no
  // onMetrics host, so attaching a panel mid-conversation cannot inherit a half-finished turn.
  //
  // prob_mean / prob_min are the SERVER's turn-level numbers, the same ones its gates keyed on.
  // The client-side aggregate is only a fallback for a field the server did not send (a refusal
  // carries prob_mean but no prob_min), and it aggregates the way the server does: min of the
  // segment minima, mean of the segment means.
  //
  // asr_ms is CLIENT-measured (audio_segment sent -> its transcript back) and is not the server's
  // own ASR timing: the server logs that to its latency records but does not put it on the wire.
  // So it is a round trip, network and queueing included, and it is a floor on what the user
  // waited, not a measurement of the recognizer alone. Do not present it as model latency.
  emitTurnMetrics(refused, serverConf) {
    const t = this.turn;
    this.turn = newTurnMetrics();
    if (!this.onMetrics) return;
    const conf = serverConf || {};
    const pick = (v, fallback) => (typeof v === "number" ? v : fallback);
    this.onSafe(this.onMetrics, {
      prob_mean: pick(conf.prob_mean, mean(t.probMeans)),
      prob_min:  pick(conf.prob_min,  min(t.probMins)),
      // null, not 0, when no segment of this turn was actually timed (a turn assembled across a
      // reconnect can report a transcript for a segment this widget never saw go out). A zero
      // would read as an instantaneous ASR, which is the one thing it certainly was not.
      asr_ms: t.measured ? Math.round(t.asrMs) : null,
      segments: t.segments,
      refused,
    });
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
        this.noteSegment(m);
        this.onSafe(this.onPartial, (m.text || "").trim());
        break;

      case "transcript_final":
        // The server accepted this turn. This is the ONLY message the host may act on, and (in
        // the default mode) the moment the assistant gets hushed: an accepted transcript is proof
        // that what interrupted was real speech addressed to us, not room noise.
        if (this.opts.bargeInMode !== "vad") this.interruptSpeech();
        // `raw` is present only when the server's normalizer rewrote what the recognizer
        // wrote ("i am six" -> "IL-6"). Pass it through as a third argument, so a host that
        // wants to show its work can, and a host that ignores it is unaffected.
        this.onSafe(this.onTranscript, (m.text || "").trim(), m.confidence,
                    { raw: m.raw || null, verified: m.verified === true });
        this.emitTurnMetrics(false, m.confidence);
        break;

      case "transcript_verify":
        // The server believes the recognizer misheard, and is ASKING rather than assuming.
        // It has already spoken "Did you mean: X?"; arm the audio path so that line is not
        // swallowed by a barge-in that just muted us. The host MUST NOT act on this: it is
        // a proposal, not a transcript. The scientist's next utterance is the answer, and
        // only then does a transcript_final arrive.
        this.armAudio();
        if (this.onVerify) {
          try { this.onVerify({ raw: m.raw, proposed: m.proposed }); }
          catch (e) { /* a host callback must never break the socket loop */ }
        }
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
        // A refusal is a completed turn, so it closes the metrics turn like any other. It reports
        // the refusal, it does not soften it: onRefused above has already fired, and nothing here
        // or downstream can turn these numbers back into an authorization to act.
        this.emitTurnMetrics(true, { prob_mean: m.prob_mean });
        break;

      case "lab_state":
        // The server's acknowledgement of set_lab_state, carrying the gate's own numbers. Record
        // them for display. This is the ONLY writer of these two, and it is inbound-only: nothing
        // the page does can move the floor, it can only learn what the floor is. Note that the
        // server is the authority on `armed` too: it derives that from the state itself, so a
        // state string it does not recognize comes back unarmed rather than assumed safe.
        if (typeof m.confirm_floor === "number") this._confirmFloor = m.confirm_floor;
        this._armed = m.armed === true;
        // Tell the host it arrived, rather than making it poll or guess a delay.
        if (this.onLabState) {
          try { this.onLabState({ armed: this._armed, confirmFloor: this._confirmFloor }); }
          catch (e) { /* a host callback must never break the socket loop */ }
        }
        break;

      // ---- replies to the config requests below. Each resolves its waiter and nothing else:
      // none of them touch capture, playback, or the gate.
      case "hints":
        this.resolveReq("hints", { hotwords: m.hotwords, replacements: m.replacements });
        break;

      case "voices":
        this.resolveReq("voices", m.voices || [], m.tag);
        break;

      case "tts_models":
        this.resolveReq("tts_models", { models: m.models, default: m.default, current: m.current });
        break;

      case "tts_params":
        // Remember the server's DEFAULTS (voice, temperature, cfg, top_k). A host that
        // wants to honour them, rather than impose its own, needs to know what they are:
        // the picker's first option is not the default voice, and selecting it would
        // silently change how the assistant sounds.
        if (m.defaults) this._ttsDefaults = m.defaults;
        this.resolveReq("tts_params", { params: m.params, defaults: m.defaults });
        break;

      case "tts_test_audio":
        this.playTestAudio(m.audio_b64);
        this.resolveReq("tts_test_audio", {
          audio_b64: m.audio_b64, sample_rate: m.sample_rate, format: m.format,
        });
        break;

      case "tts_test_error":
        // The server sends this ONE type for two different failures, with no tag to tell them
        // apart, so route on the text it does send. If it is ever neither, fail both waiters
        // rather than leave a host awaiting a reply that is not coming.
        this.failTtsTestError(m.text || "TTS request failed");
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
        // `error` is the server's general-purpose failure (a failed transcription is one), so it
        // must NOT be treated as a blanket rejection: doing that would fail an unrelated in-flight
        // request every time one segment failed to transcribe. Only the one error that is provably
        // the answer to a request rejects it. Anything else is left to its timeout.
        if (typeof m.text === "string" && m.text.startsWith("unknown TTS model")) {
          this.rejectReq("tts_models", m.text);
        }
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

  // ---- tuning: the VAD and the microphone ----

  // What the capture path is ACTUALLY running, which is not the same as what DEFAULTS says.
  // segMs reports the effective segment-end silence (200ms, the library's own default), not the
  // 600ms the ignored minSilenceFrames constant implies. A panel that showed 600 while the VAD
  // ended segments at 200 would be a lie, and the user would tune against the lie. See makeVad().
  getVadOptions() {
    return {
      threshold: this.opts.vadThreshold,
      segMs: this.opts.segMs != null ? this.opts.segMs : LIB_MIN_SILENCE_FRAMES * FRAME_MS,
      turnMs: this.opts.turnSilenceMs,
      bargeIn: this.opts.bargeInMode,
    };
  }

  // Partial: an omitted field is left alone. Applies live, with no mic restart and no dropped
  // audio, and resolves with the new effective options.
  //
  // threshold and segMs are baked into the VAD instance at create() time, so they need a new
  // instance (rebuildVad swaps it in without touching the mic stream or the audio graph).
  // turnMs and bargeIn are read at the moment they are used (arming the turn timer, deciding a
  // barge-in), so assigning them IS the live update.
  //
  // Ranges are enforced and a bad value THROWS: a slider that quietly sent threshold=5 would
  // wedge the VAD into never hearing speech, and the failure would look like a broken microphone.
  async setVadOptions(o = {}) {
    let rebuild = false;
    if (o.threshold != null) {
      this.opts.vadThreshold = inRange(o.threshold, "threshold", 0, 1);
      rebuild = true;
    }
    if (o.segMs != null) {
      this.opts.segMs = inRange(o.segMs, "segMs", FRAME_MS, 5000);
      rebuild = true;
    }
    if (o.turnMs != null) this.opts.turnSilenceMs = inRange(o.turnMs, "turnMs", 0, 30000);
    if (o.bargeIn != null) {
      if (!BARGE_IN_MODES.includes(o.bargeIn)) {
        throw new TypeError(`bargeIn must be one of ${BARGE_IN_MODES.join(" | ")}, got ${JSON.stringify(o.bargeIn)}`);
      }
      this.opts.bargeInMode = o.bargeIn;
    }
    if (rebuild) await this.rebuildVad();
    return this.getVadOptions();
  }

  // Swap in a VAD built with the current options. The old one keeps running until the new one is
  // ready, so no frame is ever handed to a null vad and no audio is lost while the WASM instance
  // is being built. The mic stream, the audio graph and the socket are untouched.
  async rebuildVad() {
    if (!this.micOn || !this.vad) return;   // nothing running: the next startMic() builds with the new values anyway
    const gen = ++this.vadGen;
    let next;
    try {
      next = await this.makeVad();
    } catch (e) {
      this.fail("VAD reload failed: " + errText(e));   // the old VAD is still installed and still working
      return;
    }
    // stopMic, or a second setVadOptions, happened while this one was building. Its VAD is the
    // current one now, so this instance is stale: dispose it instead of clobbering the live one.
    if (gen !== this.vadGen || !this.micOn) { try { next.dispose(); } catch {} return; }
    if (this.capture) this.capture.flushSegment();   // see flushSegment: the new VAD cannot end a segment it never saw start
    const old = this.vad;
    this.vad = next;                                 // no await between the flush and here, so no frame lands in between
    if (old) { try { old.dispose(); } catch {} }
  }

  // Labels are EMPTY until the user has granted mic permission at least once (browsers withhold
  // them until then, as a fingerprinting defence). Call this after startMic() if you want a menu
  // a human can read; before it, you get deviceIds and blank labels.
  async listInputDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return [];
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices
      .filter((d) => d.kind === "audioinput")
      .map((d) => ({ deviceId: d.deviceId, label: d.label }));
  }

  // Re-open the mic on another device. A null/empty id hands the choice back to the browser.
  //
  // While the mic is off this only records the choice: the next startMic() opens that device.
  // While it is ON, this restarts capture, so it must be called from a user gesture for the same
  // iOS reason startMic() must be (the AudioContext is rebuilt, and a suspended one never runs).
  // stopMic() flushes the in-flight turn on its way out, so a half-spoken sentence is still sent
  // rather than lost to the device switch.
  async setInputDevice(deviceId) {
    const prev = this.inputDeviceId;
    const next = deviceId || null;
    if (next === prev) return;
    this.inputDeviceId = next;
    if (!this.micOn) return;
    this.stopMic();
    await this.startMic();
    // The device would not open (unplugged, or seized by another app). startMic has already
    // reported the error; do not also leave the user with a dead microphone because a menu
    // offered a device that is no longer there.
    if (!this.micOn) {
      this.inputDeviceId = prev;
      await this.startMic();
    }
  }

  // ---- server-side config (thin passthroughs over messages the orchestrator already accepts) ----

  // HOTWORDS ARE A SCALPEL, NOT A GLOSSARY. Read this before you seed a vocabulary list.
  //
  // FunASR does not take hotwords as a dictionary of terms to prefer. It takes them as a DECODING
  // PROMPT, which conditions the decode itself. Seeding a 14-term lab vocabulary here was tried on
  // the real stack and measured, and it did not bias recognition, it derailed it: a clean utterance
  // came back as unrelated text ("the top card on the top was for the sample photo..."), and
  // prob_mean collapsed from 0.91 to 0.23. Every downstream confidence gate keys on prob_mean, so
  // the cost was not just a bad transcript: it was a turn the safety gate then refused, for a
  // sentence the user had spoken perfectly clearly.
  //
  // One or two terms, or none. To teach the system domain vocabulary, use `replacements` (a
  // post-ASR text fix, applied after recognition, which cannot corrupt the decode) or normalize it
  // server-side. Do not reach for hotwords because the list is long. The longer the list, the worse
  // it gets.
  //
  // Partial: an omitted field keeps the server's current value; an explicit empty list or object
  // CLEARS that field. Resolves with the hints the server now holds.
  setHints({ hotwords, replacements } = {}) {
    const msg = { type: "set_hints" };
    if (hotwords != null) msg.hotwords = hotwords;
    if (replacements != null) msg.replacements = replacements;
    return this.request(msg, { type: "hints" });
  }

  // Resolves with the voice array for `model`, or for the session's current TTS model when it is
  // omitted. An unknown model is not an error: the server falls back to the session's model and
  // says so in the reply, which is why the reply carries the model it actually queried.
  listVoices(model) {
    const tag = "vw-" + (++this.reqSeq);   // the server echoes `tag` back, which is what pairs a reply to its request
    const msg = { type: "list_voices", tag };
    if (model) msg.model = model;
    return this.request(msg, { type: "voices", tag });
  }

  // Resolves with {models, default, current}. Rejects on an unknown model id.
  setTtsModel(model) {
    return this.request({ type: "set_tts_model", model }, { type: "tts_models" });
  }

  // The assistant path's generation params. Per-field, and the distinction matters: a key you do
  // not pass is left UNCHANGED, while a key you pass as null RESETS it to the backend's own
  // default. `undefined` reads as "not passed" (JSON drops it), so reset with null, not undefined.
  // Resolves with {params, defaults}.
  setTtsParams(params = {}) {
    const msg = { type: "set_tts_params" };
    for (const k of TTS_PARAM_KEYS) if (k in params) msg[k] = params[k];
    return this.request(msg, { type: "tts_params" });
  }

  // Audition a voice. This is NOT a conversation turn: no ASR, no LLM, no history, and the audio
  // comes back on its own message rather than through the reply stream, so it is played through a
  // separate source that the conversation's playback state machine never sees. It cannot be barged
  // in on, it does not set the "speaking" status, and it cannot settle a pending speak().
  //
  // `opts` carries the rest of what the server's tts_test accepts (model, temperature, cfg_scale,
  // top_k, max_frames). Pass them to audition a voice under the same params the assistant would
  // use: tts_test reads ONLY what this message carries, never the session's params, so an audition
  // with none of them set is an audition of the backend defaults, not of your current settings.
  //
  // Resolves with {audio_b64, sample_rate, format} once the clip has arrived (it is already
  // playing by then). Rejects if synthesis fails.
  testVoice(text, voice, opts = {}) {
    const t = (text || "").trim();
    // The server drops an empty tts_test on the floor and answers nothing at all, so a request for
    // one would hang until it timed out. Reject it here, now, with the real reason.
    if (!t) return Promise.reject(new Error("testVoice needs some text to say"));
    const msg = { type: "tts_test", text: t };
    if (voice) msg.voice = voice;
    for (const k of TTS_TEST_KEYS) if (k in opts) msg[k] = opts[k];
    return this.request(msg, {
      type: "tts_test_audio",
      timeoutMs: this.opts.ttsTestTimeoutMs || TTS_TEST_TIMEOUT_MS,   // a cold GPU plus a whole sentence
    });
  }

  // Stop an audition that is playing. Safe to call when nothing is auditioning.
  stopTestVoice() {
    this.testGen++;   // voids a decode still in flight, so it cannot start a source after this
    if (this.testSource) {
      try { this.testSource.onended = null; this.testSource.stop(); } catch {}
      this.testSource = null;
    }
  }

  // Deliberately separate from playNext(): an audition must not enter audioQueue, where it would
  // be mistaken for the assistant talking (it would set the speaking status, arm barge-in, and
  // settle whatever speak() was pending).
  playTestAudio(b64) {
    if (!b64) return;
    const ctx = this.ensurePlayCtx();   // resumes it if it can; a context that was never unlocked by a
    if (!ctx) return;                   // gesture stays silent, exactly as the conversation path would
    this.stopTestVoice();               // one audition at a time: a new one replaces the one playing
    const gen = ++this.testGen;
    decodeAudio(ctx, b64).then((buf) => {
      if (gen !== this.testGen || !buf) return;
      try {
        const node = ctx.createBufferSource();
        node.buffer = buf; node.connect(ctx.destination);
        node.onended = () => { if (gen === this.testGen) this.testSource = null; };
        this.testSource = node;
        node.start();
      } catch { this.testSource = null; }
    });
  }

  // ---- request/reply plumbing for the config messages above ----

  // Send a message and resolve when its reply lands. The socket carries no request ids, so a reply
  // is matched by TYPE (plus the echoed tag, where the server has one), and every waiter carries a
  // timeout: a reply that never comes must reject, not hang the host's await forever.
  request(msg, spec) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== 1) {
        // send() silently drops on a closed socket, which would leave this waiting for a reply to
        // a message that was never sent. Say so instead.
        reject(new Error("voice socket is not open: call connect() first"));
        return;
      }
      const id = ++this.reqSeq;
      const timeoutMs = spec.timeoutMs || this.opts.requestTimeoutMs || REQUEST_TIMEOUT_MS;
      const timer = setTimeout(() => {
        this.pendingReqs.delete(id);
        reject(new Error(`no ${spec.type} reply from the server within ${timeoutMs}ms`));
      }, timeoutMs);
      this.pendingReqs.set(id, { type: spec.type, tag: spec.tag || null, resolve, reject, timer });
      this.send(msg);
    });
  }

  resolveReq(type, value, tag) {
    const w = this.takeReq(type, tag);
    if (w) w.resolve(value);
  }

  rejectReq(type, text) {
    const w = this.takeReq(type, null);
    if (w) w.reject(new Error(text));
  }

  // Claim the waiter a reply belongs to. Prefer an exact tag match; otherwise take the OLDEST
  // waiter of that type (a Map iterates in insertion order, and same-type replies come back in the
  // order they were asked for). The fallback is what keeps a reply that carries no tag from
  // stranding its waiter.
  takeReq(type, tag) {
    let hitId = null;
    if (tag != null) {
      for (const [id, w] of this.pendingReqs) if (w.type === type && w.tag === tag) { hitId = id; break; }
    }
    if (hitId == null) {
      for (const [id, w] of this.pendingReqs) if (w.type === type) { hitId = id; break; }
    }
    if (hitId == null) return null;
    const w = this.pendingReqs.get(hitId);
    clearTimeout(w.timer);
    this.pendingReqs.delete(hitId);
    return w;
  }

  // tts_test_error is the reply to BOTH list_voices and tts_test, and it carries no tag to tell
  // which. Its text does: the server writes "voices unavailable: ..." for one and "synth failed:
  // ..." for the other. Route on that, and if it is ever neither, fail both kinds rather than
  // leave a host awaiting a reply that is not coming.
  //
  // It does NOT call fail(): a voice audition that could not be synthesized is a failed request,
  // not a broken widget, and it must not put the whole conversation into the error status.
  failTtsTestError(text) {
    if (text.startsWith("voices unavailable")) { this.rejectReq("voices", text); return; }
    if (text.startsWith("synth failed"))       { this.rejectReq("tts_test_audio", text); return; }
    this.rejectReq("voices", text);
    this.rejectReq("tts_test_audio", text);
  }

  rejectAllReqs(text) {
    const waiters = [...this.pendingReqs.values()];
    this.pendingReqs.clear();
    for (const w of waiters) { clearTimeout(w.timer); w.reject(new Error(text)); }
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

// performance.now() is monotonic, so a clock change (NTP, a laptop waking) cannot make a measured
// ASR round trip come out negative. Date.now() is the fallback for a host without it.
function nowMs() {
  return (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
}

function newTurnMetrics() {
  return { segments: 0, measured: 0, asrMs: 0, probMeans: [], probMins: [] };
}

function mean(xs) { return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null; }
function min(xs)  { return xs.length ? Math.min(...xs) : null; }

function inRange(v, name, lo, hi) {
  const n = Number(v);
  if (!Number.isFinite(n) || n < lo || n > hi) {
    throw new RangeError(`${name} must be a number in [${lo}, ${hi}], got ${JSON.stringify(v)}`);
  }
  return n;
}

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
