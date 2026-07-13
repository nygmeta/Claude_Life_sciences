# Demo Script

*Created: 2026-07-09 +08.*

One-line what-it-is: lab-assistant is a real-time, self-hosted voice assistant, you speak,
it transcribes, thinks, and replies out loud. Pipeline: browser VAD -> FunASR-Nano (ASR) ->
Claude Haiku (LLM) -> gepard-1.0 (TTS), all served by one orchestrator (`web/server.py`)
over a WebSocket.

## Pre-flight checklist

- Pod health: run `bash deploy/health.sh` (or ask pod-ops) and confirm all three services
  (asr, tts, web) report ready, none stuck in `loading`.
- Tunnel: open `https://<your-tunnel-host>` in the demo browser and confirm the page loads
  (masthead + status square visible, WS connects on Start).
- Mic permission: the browser prompts on the first "Start" click; grant it in a dry run
  beforehand so the live demo does not stall on a permission dialog.
- English only: gepard synthesizes EN/ES-MX/PT-BR/NL, not Chinese; keep every spoken line
  in English.
- A quiet room: browser VAD reacts to ambient noise; background chatter or music can
  trigger false speech segments.
- Confirm `LA_TTS_STREAM` is on (the default), so the sentence-streaming behavior below is
  what actually plays.

## Spoken script (about 2 minutes)

1. **Click Start, allow the mic.** (Skip if already granted in the pre-flight dry run.)
2. **Say, with one short pause in the middle, then a longer pause to end the turn:**
   - "Hey, quick question." *(short pause, under the turn-pause threshold)*
   - "What are three ways to speed up experiments on a shared GPU cluster?" *(longer
     pause, ends the turn)*
   - **What the audience sees:** two transcript chips appear as you speak, one per VAD
     segment, but only ONE reply fires, after the longer pause, because the orchestrator
     batches segments into a single turn before calling the LLM.
   - **What the audience hears:** Claude's reply streams in as text, sentence by sentence.
     As soon as the first sentence is complete, its audio starts playing, while the second
     and third sentences are still being written to the transcript. This is the
     sentence-streaming TTS feature: audio arrives one message per sentence, in order,
     followed by a terminal end-of-stream marker after the last one.
3. **Interrupt it.** While the assistant is still speaking, say "Actually, one more
   thing." **What the audience hears:** the current playback is hushed immediately
   (barge-in), and a new turn starts.
4. **Ask a short, single-sentence question**, e.g. "In one sentence, what's the fastest
   way to check if a GPU is idle?" **What the audience sees:** a one-sentence reply still
   uses the same per-sentence audio path, just with one chunk instead of several, showing
   the mechanism degrades gracefully for short replies.
5. **Show session history.** Click "New session". **What the audience sees:** the
   transcript clears for a fresh, separately numbered session, while everything asked so
   far is now saved, not discarded. Open the "History" panel: the previous session is
   listed by number, name, and start time; the new one is tagged "Live". Click the
   previous session in the list. **What the audience sees:** the transcript pane switches
   to a read-only view of that earlier conversation with a "Viewing Session N: <name>"
   banner, while the live session keeps running untouched underneath. Rename it inline
   (click the pencil icon, type a short label, press Enter) to show the rename works on a
   past session too, then click "Back to live" to return.

## Optional extras (if time allows)

- **Assistant voice controls**: open the "TTS" panel, adjust temperature, cfg_scale,
  top_k, or the voice dropdown, then click **Confirm change** (it lights up only once
  something changed) before asking one more question. This is the same panel used for the
  "Synthesize" trial, it merges what used to be a separate "Voice" panel and TTS
  playground, so you can steer how the assistant itself sounds without leaving the main
  conversation.
- **Theme toggle**: click the header's theme toggle to switch the page from light to dark
  and back. Purely cosmetic, but a quick, no-restart-needed show of the CSS-variable
  theming underneath.
- **Help toggle**: the "?" button in the header shows or hides the "Configure" guidance
  card, independent of any panel being open. It starts folded on a narrow or portrait
  window and shown on a wide landscape one, so if the card is missing on a narrow demo
  window, click "?" rather than assume something is broken.

## Expected latency (framing, not a live-pod baseline)

The feature being demoed cuts *perceived* latency, time to first audio, not the total time
to finish speaking the whole reply. Reference figures from earlier measurements (no pod is
up as of this writing, so treat these as grounding, not a guarantee for the night):

- gepard synthesizes fast relative to audio length: 2.136 s of audio in 753.9 ms of compute
  (real-time factor 2.83), so a short first sentence's TTS call adds well under a second.
- Claude Haiku's time-to-first-token was about 0.9 to 1.0 s in local smoke runs.

So the first sentence's audio should start well before the full reply has finished
generating, versus the old single-synth-after-full-reply path where the audience waited for
the entire reply plus one TTS call before hearing anything. Exact end-to-end numbers on the
night depend on the live pod, the network to the tunnel, and reply length; describe the
effect to the audience ("you'll hear the answer start before it's finished being written")
rather than quoting a fixed millisecond figure.

## Fallback plan (calm, specific, in order of severity)

1. **Tunnel or pod is flaky but partially reachable:** switch to the "TTS" panel and use
   "Synthesize". It calls the TTS backend directly over the same WebSocket with no ASR or
   LLM in the path, so it demonstrates the voice and preset options even if the ASR or LLM
   leg is having trouble.
2. **Streaming glitches** (audio out of order, choppy overlap, or similar): the fallback is
   `LA_TTS_STREAM=0`, which restores the original single-synth-after-full-reply path on the
   same client contract. This needs a `web` service restart, so decide before the demo
   starts whether to keep a second browser tab pointed at a pre-configured fallback
   instance, rather than trying to flip the flag live between questions.
3. **Pod is down entirely:** run `bash scripts/run_local_smoke.sh` on the laptop. It
   exercises the orchestrator against mock ASR/TTS while calling the real Claude Haiku API,
   so it still proves the pipeline logic and the LLM key are working, and gives something
   concrete to show and narrate from the terminal.

## If asked how Claude was used

The assistant's actual spoken replies are Claude Haiku, live, streamed sentence by
sentence; that is the product, not a canned response. Separately, the whole codebase was
built with a Claude Code agent team: single-writer lanes per component (web, UI, speech
services, pod ops, docs, usage logging) coordinated by a read-only lead, plus custom
skills. Point to `CLAUDE_USAGE.md` for the detailed, dated log of how Claude was used at
each milestone.
