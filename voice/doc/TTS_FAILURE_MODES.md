# TTS Failure Modes

*Created: 2026-07-09 +08.*

gepard-1.0 is an autoregressive model: a language model predicts audio codec tokens one at
a time, then a codec decodes the token sequence to a waveform. Autoregressive TTS (AR-TTS)
has two well-known instability modes at the sequence-length boundary: sampling an
end-of-sequence (EOS) token too early, or failing to sample one at all. A third mode,
unrelated to sequence length, comes from generating without a reference speaker at all:
the model is free to invent a different voice on every call.
Three modes are documented below. Modes 1 and 3 were observed directly in this project
(mode 3 has since been fixed and deployed); mode 2 is reported and expected on general
AR-TTS grounds but has not yet been reproduced here. These are model behaviors, not
defects in the ASR, LLM, or TTS pipeline code.

## Failure mode 1: early stop / short-clipped audio (early EOS)

- **Symptom**: synthesized audio is far shorter than the text warrants; the reply is
  clipped or truncated to a fraction of a second.
- **When observed**: 2026-07-09 SGT, on the live GPU deployment, gepard-1.0. The fixed
  smoke sentence "Hello, this is a test of the gepard text to speech voice." (11 words)
  produced roughly 0.05 s (about 1024 samples) in the smoke and about 0.42 s via a direct
  synth call, versus about 7.2 s for a different sentence of similar length on the same
  running instance. Repeating the exact same sentence gave byte-identical output
  (deterministic within one process), and a different process synthesized that same
  sentence normally (about 3.9 s). So the failure tracks the text plus that process's
  sampling state, not a transient glitch.
- **Mechanism**: the autoregressive language model samples an EOS token too early for a
  particular phrasing and RNG state, so generation halts before the sentence is fully
  spoken.
- **Mitigations (with tradeoffs)**:
  - **Detect-and-retry**: if the produced audio is anomalously short for the text length
    (below a chars-per-second floor), re-synthesize with a nudged sampling state
    (different seed or slightly higher temperature). Note that a naive retry with
    identical parameters reproduces the same output within a process, so the retry MUST
    vary the RNG or temperature. Least intrusive option.
  - **A minimum-frames floor**: force generation to continue past an early stop. Tradeoff:
    forcing past a natural stop can append babble or artifacts.
  - **Tune temperature / cfg_scale** for the deployment.
  - Recommended: detect-and-retry.

## Failure mode 2: runaway / long audio with silence (late or missing EOS)

- **Symptom**: synthesized audio is much longer than the text warrants, with long
  stretches of silence (or repetition and babble); the model does not stop cleanly.
- **When observed**: reported by the project owner as an intermittent behavior of AR-TTS
  in this project; not directly captured with a reproduction case in this session.
- **Mechanism**: the opposite of mode 1, the autoregressive language model fails to emit
  EOS in time and keeps generating (silence frames, repeats) until it hits the hard frame
  cap.
- **Mitigations (with tradeoffs)**:
  - **A max-length cap** (already present): gepard honors `max_frames` (env
    `LA_TTS_MAXFRAMES`, default 1075). This bounds the worst case, but the capped clip can
    still carry trailing silence.
  - **A per-synth timeout / max-duration guard** (the owner's suggestion): abort or bound
    a synth call that runs too long. The orchestrator's TTS HTTP client currently uses a
    60 s httpx timeout; a tighter, per-synth budget could cut off a runaway sooner.
  - **Trailing / internal silence trim**: post-process the waveform to strip long silence
    via an energy threshold.
  - **Detect abnormally long audio** relative to text length and retry or trim.
  - Recommended: a max-duration cap plus a trailing-silence trim, with a synth timeout as
    a backstop.

## Failure mode 3: unconditioned speaker / per-sentence voice drift (gepard "default", ref_codes=None)

- **Symptom**: within a single assistant reply, each spoken sentence comes out in a
  different speaker's voice; the voice changes at every sentence boundary instead of
  staying consistent across the reply.
- **When observed**: 2026-07-09 SGT, live GPU demo, reported by the project owner and
  confirmed from the code.
- **Mechanism**: gepard-1.0's `default` voice passes `ref_codes=None` to the language
  model, so the generation is not conditioned on any speaker. With temperature > 0
  sampling, the autoregressive LM invents a fresh speaker identity on every `generate()`
  call. Because the orchestrator streams TTS one sentence at a time (each sentence is a
  separate synth call), each call draws its own speaker, so the voice drifts at every
  sentence boundary. Single-shot (whole-reply) synthesis would hide this behind one
  random speaker per reply; sentence-streaming exposes it per sentence. Unlike modes 1
  and 2 above, which are about audio length, this one is about speaker identity.
- **Fix (implemented, live-verified this session)**: pin a reference voice so `default`
  is always conditioned. `GepardBackend._load_voices()` loads any `tts/voices/*.pt` file
  as a named voice, and a file named `default.pt` now overrides the `None` default with
  real `ref_codes`, so every `default` synth uses one fixed, conditioned speaker. The
  reference is generated on the GPU by the new `tts/make_gepard_voice.py`, which supports
  two methods: `--method capture` (freezes the codes from one generation and
  self-validates the `[1, T, C]` layout; recommended, and what we used) and `--method
  encode` (round-trips a reference clip through the NanoCodec; a fallback). An optional
  `LA_TTS_GEPARD_DEFAULT_VOICE` env can instead alias `default` to another named voice.
  After deploy, the gepard service now logs "default voice is CONDITIONED (stable
  reference loaded; no per-sentence speaker drift)"; a two-sentence pin-test synth came
  out at 3.44 s and 2.83 s (healthy).
- **Caveat**: the reference captured this session is short, 19 codec frames (under a
  second), because the seed generation stopped early. It works and synths are healthy,
  but a longer reference would be a more robust speaker anchor; a candidate refinement is
  a minimum-frames floor on the capture step.

## Detection + architectural notes

- A detection signal already exists in the pipeline: every synth logs `tts.audio_s` and
  `tts.rtf` to the latency log (`data/latency.jsonl`), so "audio_s near zero" (mode 1) or
  "audio_s much greater than expected for the character count" (mode 2) are both
  detectable from telemetry already being collected. Any mitigation above would threshold
  on this same signal. Mode 3 does not show up in this telemetry at all, since it is a
  speaker-identity problem, not a length problem; its fix instead removes the failure at
  the source, by never running `default` unconditioned.
- Sentence-streaming TTS partially contains modes 1 and 2: each sentence is synthesized
  as its own chunk, so a single bad sentence only affects its own `reply_audio` chunk,
  not the entire reply. For mode 3, sentence-streaming is what made the bug *visible*
  (a single-shot synth would have hidden the drift inside one random per-reply voice).
- gepard-1.0 is autoregressive and therefore susceptible to modes 1 and 2 in principle.
  As of this writing, its early-stop mode (1) has been directly observed and reproduced
  here, and its unconditioned-default mode (3) has been directly observed, fixed, and
  live-verified; the runaway mode (2) is a general AR-TTS risk the project owner has seen,
  not yet captured with a specific reproduction in this repo. Mode 3 was specific to the
  `default` voice, which is the one voice that shipped with no reference conditioning.

## Demo guidance

- Keep replies short: the assistant's system prompt already caps replies at 1 to 3 spoken
  sentences, which reduces the exposure window for both failure modes.
- If a reply clips short or drags on with silence during a live demo, just re-speak the
  turn; barge-in already lets the presenter cut off a runaway clip mid-playback.
- Mode 3's fix (a pinned `default` reference voice) is implemented and deployed, so a
  live demo should no longer show mid-reply voice drift on the default voice. The
  mitigations listed under modes 1 and 2 above are not implemented yet; this doc records
  them as candidates for if and when the lead schedules the work.
