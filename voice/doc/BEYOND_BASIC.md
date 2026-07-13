# Beyond a Basic Voice Agent

*Created: 2026-07-13 (SGT)*

A basic voice agent hears you, transcribes, asks a model, and speaks the answer. This project does that too (VAD -> STT -> Claude -> TTS), and then adds six things, because a lab is not a chat room: hands are busy, words trigger physical actions, and the microphone hears the whole room.

For the full detail and how to verify each item, see `doc/FEATURES.md`. This page is the short version.

## 1. One orchestrator that overlaps the whole pipeline

A basic agent runs the four steps in order and waits at each one: you finish talking, it transcribes, it thinks, it speaks, then it starts listening again. This one runs them concurrently. It starts the language model on your words before you have finished the turn, and holds that result until you actually commit, so a half-heard command can never fire early. It speaks each sentence of the reply as soon as that sentence is ready, instead of waiting for the whole answer. It gathers several spoken fragments into a single turn rather than replying to every pause. And it keeps listening while it talks, running each reply as a task it can cancel, so you can interrupt it mid-sentence. The result feels like a conversation, not a walkie-talkie, and every stage is timed, so the latency is measured, not guessed.

## 2. STT knows how sure it is

STT normally gives you text with no confidence. We tap its decoder so every transcribed segment carries per-token confidence scores. Real numbers from real audio: clean speech scores around 0.95, background-noise hallucinations score 0.04 to 0.37. That one signal powers most of what follows.

## 3. The mic filters the world

- A noise gate drops segments the STT was not confident about, calibrated on real clips the operator labeled from their own phone. Dropped segments show as one small "N ignored" line, so they are visible but never in the way.
- An optional classifier judges whether speech was addressed to the assistant at all, so colleagues talking nearby are ignored.
- An opt-in capture mode saves segments with their confidence and lets the tester label them, which is how the gate got its thresholds: measured, not guessed.

## 4. Commands are safety-gated, not just executed

Spoken lab commands pass a gate that weighs how risky the action is against how clearly it was heard:

- Safe commands just run ("read the temperature sensor").
- Risky ones are read back with careful digits ("dispense five zero, that is 50, microliters into well A three") and need a spoken confirmation that names the action: "confirm dispense". A bystander's stray "yes" cannot fire it.
- Unclear, confidence-less, or stale confirmations re-prompt instead of executing.
- "Cancel" always works. "Stop" halts everything immediately, ahead of the LLM.

## 5. The assistant speaks up on its own

Lab events talk back: a finished centrifuge run or a reached temperature target is announced out loud without being asked. Urgent alerts interrupt the assistant mid-sentence (with a warning beep first); routine news politely waits its turn.

## 6. Hands-busy modes

A protocol walkthrough you drive entirely by voice: next, back, repeat, what step am I on. Steps are read verbatim (the model is forbidden from improvising them), and a timed step announces its own completion, so you can start an incubation, walk away, and be told when it is done.
