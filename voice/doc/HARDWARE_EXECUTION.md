# Executing on real hardware: what exists, what does not

*Created: 2026-07-14 05:13 +08 (SGT).*

Written after an attempt to connect the console to a physical Opentrons OT-2 did not work. The short version: **it did not fail, it was never built.** There is no code path from this system to a robot, so there was nothing to connect. This doc says exactly what is missing, what it would take, and the one safety property that must not be lost on the way.

## What exists today

The adapter contract (`app/adapters/base.py`) has three methods:

```python
def check_capabilities(self, wf: Workflow) -> list[ValidationIssue]
def compile(self, wf: Workflow) -> str
def simulate(self, wf: Workflow) -> tuple[bool, str]
```

- `check_capabilities` reconciles a workflow against the platform's declared limits. This is what makes a refusal meaningful ("an ELISA with incubations is not an acoustic job", "a 400 uL transfer exceeds a 300 uL well").
- `compile` turns the platform-independent Workflow into the target's own artifact. For the Opentrons adapter that is **Opentrons protocol Python source**; for the acoustic adapter it is a picklist CSV.
- `simulate` runs `opentrons.simulate` (the **software** simulator) in a subprocess, and falls back to a dry summary when that package is not installed. It is not installed by default: `requirements.txt` lists `opentrons>=7.0` as an optional, commented-out line.

So the pipeline today is complete and honest, and it ends one step short of a machine:

    speech -> transcript -> Plan -> Workflow -> validation -> COMPILED PROTOCOL -> simulation

## What does not exist

**There is no `execute()`.** No adapter method, no API route, no session state, and no client of any kind that talks to a robot:

- no robot address anywhere in the codebase,
- no HTTP client for the Opentrons robot server (it listens on port **31950**),
- no upload, no run creation, no play/pause/stop,
- no run-status polling, no way to observe or abort a run in progress,
- no state in the session machine after `executed`, which today means "simulated", not "ran".

This is why pointing the console at an OT-2 cannot work. Nothing in the system opens a socket to a robot, so there is no configuration, no hostname, and no port that would make it start.

## What real execution would require

Roughly in order:

1. **An `execute()` on the adapter contract**, distinct from `simulate()`, returning a run handle rather than a log string. `simulate()` must survive alongside it: the ability to dry-run a protocol before committing it to glassware is a feature, not scaffolding.
2. **An Opentrons robot client.** The OT-2 exposes an HTTP API on port 31950. The flow is: upload the compiled protocol (`POST /protocols`), create a run from it (`POST /runs`), then start it (`POST /runs/{id}/actions`, `{"actionType": "play"}`). Status is polled from `GET /runs/{id}`. `pause` and `stop` are the same actions endpoint, and they are not optional: see the safety section below.
3. **A new session state.** The state machine currently ends at `executed`. A real run is not instantaneous, so it needs at least `executing` (a run is in progress, with a run id) and a terminal state that distinguishes **completed**, **failed**, and **stopped by a human**. A protocol that a scientist aborted is not a protocol that finished.
4. **A capability declaration for "can actually run"**, so the validator can refuse to send a workflow to an adapter that only simulates. Today every adapter is, in effect, a simulator, and nothing in the system distinguishes them. That distinction has to be explicit before one of them can move liquid.
5. **The `opentrons` package installed**, and its version pinned against the robot's software version. The protocol API is versioned, and a protocol compiled for one API version is not guaranteed to run on another.

## The deployment constraint this forces

**Whatever calls the robot must be able to reach the robot.** The OT-2 is on a lab LAN, so:

- The **Lab Agent must run on a machine on that LAN**, next to the robot. It cannot be hosted remotely and still reach a robot behind someone else's router.
- The **console must be served from that machine too**, or at least be able to call the Lab Agent (the two must be same-origin, or the API base must be pointed at it explicitly).
- The **speech services (ASR, TTS, the safety gates, the verification model) do NOT need to be there.** They are GPU-bound and location-independent. The console reaches them over a WebSocket.

That is why the console takes both as parameters rather than assuming one machine:

    ?api=http://localhost:8000        the Lab Agent, running next to the robot
    ?voice=<speech-host>              the speech service, wherever the GPU is

**Audio crosses the network; the protocol does not.** The robot is only ever spoken to by the machine standing beside it. That happens to be both the only workable topology and the right security posture.

## The safety property that must not be lost

Right now, every safety mechanism in this system guards a **simulator**:

- the ASR confidence floor, which refuses to forward a confirmation it did not hear clearly,
- the "did you mean X?" verification turn, which will not act on a transcript the recognizer may have misheard,
- the spoken confirmation gate, which reads the plan back and waits for a human,
- the deterministic validator, which refuses an unsafe volume before anything is built.

Each one currently stands between a scientist and a **subprocess**. The moment `execute()` exists, the same gates stand between a scientist and a **machine that moves liquid**, and the cost of a gap changes completely.

So when execution lands, these are not nice-to-haves:

- **The confirmation gate must be provably in the execute path**, not merely in the simulate path, and there must be a test that asserts it. "Nothing executes until the scientist confirms" is currently true because nothing executes at all. That sentence has to stay true for a different reason.
- **Stop must be real.** Today an emergency stop cancels a reply and clears a pending action. Against hardware it has to reach the robot's `stop` action, and it has to work while a run is in progress, which is the one moment nobody will be reading a screen.
- **A run in progress must be observable.** A system that can start a machine and cannot tell you what it is doing, or stop it, is worse than one that cannot start it at all.

## Where this leaves the demo

Two honest options, and they are genuinely different projects:

**Demo the system as built.** Spoken intent, a validated protocol, an unsafe volume refused out loud before anything is built, a spoken confirmation, and a simulated run with a full audit trail. Everything in that sentence is real today, and the refusal is the part worth watching.

**Build real execution.** The work above, on a machine on the robot's LAN, with the gates proven to be in the path. This is not a configuration change, and it should not be attempted the night before a recording.

The system does not currently claim to run on hardware, and nothing in it is dishonest about that. The gap is worth closing deliberately, not quietly.
