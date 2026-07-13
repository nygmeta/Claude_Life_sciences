# Hosting the console next to the robot, with the voice coming from elsewhere

*Created: 2026-07-14 (SGT).*

The robot is on a lab LAN. The GPUs that do speech are not, and cannot be moved there. So the system has to split, and the split is not arbitrary: it falls out of where the *physical* things are.

| must run on the machine beside the robot | can run anywhere (and does) |
|---|---|
| the console page | ASR (Fun-ASR-Nano, GPU) |
| the Lab Agent API (`app/`, the planner and validator) | TTS (gepard-1.0, GPU) |
| whatever eventually drives the robot | the safety gates and the "did you mean X?" check |

**Audio crosses the network. The protocol does not.** The machine that will one day talk to the robot is the machine standing beside it. That happens to be both the only workable topology and the right security posture: a remote speech service never needs to know a robot exists, and never needs a route into the lab.

## Step by step

Everything below happens on **the machine that sits on the robot's LAN**. Nothing needs to be installed on the GPU side.

### Before you start

- **Python 3.10 or newer.** On Debian or Ubuntu, `python3-venv` as well (`sudo apt install python3-venv`), because the stock Python there cannot create a virtualenv without it.
- **An Anthropic API key.** The **planner runs on this machine**, so this machine needs its own key. The speech service holds a separate key for the verification turn; the two are not shared, and neither is ever committed.
- **No GPU, no CUDA, no model download.** Speech is borrowed over a WebSocket.
- **The speech host**, which the operator of the speech service will give you. It is a hostname, nothing more.

### 1. Get the code

```bash
git clone <repo-url>
cd <repo>
```

`main` is the right branch. The voice stack is merged into it, so there is nothing to check out.

### 2. Give it the key

Either export it:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

or write it to `voice/credentials/anthropic_key.txt`, which is gitignored:

```bash
mkdir -p voice/credentials
printf '%s' 'sk-ant-...' > voice/credentials/anthropic_key.txt
```

The script looks in both places, environment first. If it finds neither it stops and says so, rather than starting and failing on the first spoken word.

### 3. Start it

```bash
bash voice/deploy/start-lab-console.sh --voice <voice-host>
```

The first run takes a minute: it creates a virtualenv at `voice/venv-console` and installs four packages (`fastapi`, `uvicorn`, `pydantic`, `anthropic`). Then it starts the Lab Agent on `127.0.0.1:8000`, serves the console on `127.0.0.1:8090`, checks that the speech service is answering, and opens a browser.

You should see:

```
==> speech service reachable at <voice-host>
==> Lab Agent up on 127.0.0.1:8000 (planner, validator, adapters)
==> console up on 127.0.0.1:8090

    OPEN:  http://localhost:8090/console.html?voice=<voice-host>&api=http://localhost:8000
```

The speech host is remembered in `voice/credentials/console.env` (gitignored), so **every run after the first is just**:

```bash
bash voice/deploy/start-lab-console.sh
```

### 4. Open the page

Use the printed URL. Open it as **`localhost`, not as an IP address**: `localhost` is a secure context, so the microphone works with no HTTPS certificate on this machine, and `http://<lan-ip>` is not, so the mic will silently never start.

Allow the microphone when the browser asks.

### 5. Check it is really wired up

Two things tell you the split is working:

- The **voice selector fills in** (roughly 19 voices). That list comes from the remote speech service, so if it populates, the WebSocket reached it. If it says "Loading voices..." forever, the page cannot reach the speech host.
- **Switch to the Live tab** and the mic button becomes clickable. On the Demo tab it is deliberately greyed out, because Demo takes no input.

### 6. Speak

Click the mic and try the scripted path. The third line is the one worth watching, because the unsafe volume is refused out loud before anything is built:

```
"Run an ELISA on today's plasma samples."
"IL-6, 24 samples, 400 microliters per well."     <- 400 is deliberately unsafe
"Make it 100 microliters per well."
"Yes, go ahead."
```

### 7. Stop it

```bash
bash voice/deploy/stop-lab-console.sh
```

It checks that the ports actually came free and tells you if something is still holding one, rather than printing "stopped" and leaving a process behind.

## What the start script does, and deliberately does not do

- creates a virtualenv and installs four pure-Python packages,
- starts the Lab Agent API on loopback **only**: it has no authentication of its own, so nothing but this machine should be able to reach it,
- serves the console as static files,
- warns, but does not abort, if the speech service is unreachable, because the console is still usable by typing and Demo mode still speaks,
- installs **no GPU dependency, no torch, and no speech model.**

## Why the URL has two parameters

```
http://localhost:8090/console.html?voice=<voice-host>&api=http://localhost:8000
                                   ^^^^^                ^^^
                                   speech, remote       Lab Agent, local
```

The console used to assume that the page, the API, and the voice all lived on one machine. They do not, and cannot, so both are now parameters:

- `api=` is where the planner and validator live. It defaults to `http://localhost:8000` when the page is served from a local dev port, which is why the parameter is technically redundant here. It is passed explicitly anyway, because a default that silently resolves to *the viewer's own laptop* is exactly the kind of thing that wastes an afternoon.
- `voice=` is the speech service. Absent, the page looks for a voice service at its own origin and will not find one.

Two properties make this work, and both were checked rather than assumed:

- **`localhost` is a secure context.** The microphone works over plain `http://localhost` with no certificate and no tunnel on the local machine. It does **not** work over `http://<lan-ip>`, so open it as `localhost`, not as an IP address.
- **A `wss://` connection from an `http://localhost` page is allowed.** Browsers block the insecure direction (`ws://` from an HTTPS page, which is what broke the first public deployment) but never the secure one. WebSockets are also not subject to CORS, so nothing has to be configured on the speech side per client.

The Lab Agent already sends `Access-Control-Allow-Origin: *`, so the page on `:8090` can call the API on `:8000` cross-origin with no change. That permissive header is fine for a loopback-only service and would not be fine for an exposed one. It is noted as P4 in [LAB_AGENT_FINDINGS.md](LAB_AGENT_FINDINGS.md).

## What this gets you, honestly

Everything up to and including a **simulated** run: spoken intent, clarifying questions, a compiled Opentrons protocol, an unsafe volume refused out loud before anything is built, a spoken confirmation gate, and an audit trail.

It does **not** move liquid. Running the console on the robot's LAN does not connect it to the robot, because nothing in this repo opens a socket to a robot: there is no `execute()`, no robot client, and no run state. That gap, and what closing it requires, is written up in [HARDWARE_EXECUTION.md](HARDWARE_EXECUTION.md).

This script is the necessary first half of closing that gap. It puts the planner, the validator, and the confirmation gate on the machine that can reach the robot, which is the precondition for an `execute()` that has somewhere to send a protocol. The second half is the adapter work, and it is not a configuration change.

## If something does not work

- **"Loading voices..." forever, and the mic will not arm.** The console cannot reach the speech service. Check that the `voice=` host is up. The script warns about this at startup rather than letting the page fail silently.
- **The page loads but every command errors.** The Lab Agent is not up, or `api=` points somewhere else. Check `voice/logs/lab-agent.log`.
- **The mic never turns on and the browser never asks for permission.** The page was opened as an IP address rather than as `localhost`. Only `localhost` (and HTTPS) are secure contexts.
- **Demo mode still works when everything else is down.** It plays pre-rendered audio from `voice/web/demo-audio/`, so a dead network cannot mute a demo. If the speech service *is* reachable, Demo synthesizes live instead.
