# Deploying the integrated console on a public hostname

*Created: 2026-07-14 (SGT).*

Goal: someone on another machine opens one URL and can drive the whole thing by voice, without needing the GPU box, the API key, or a checkout of this repo.

Target hostname in this write-up: `lab-auto.<your-domain>`.

## The one thing that makes this non-trivial

The console page and the Lab Agent API must arrive on the **same origin**.

The console fetches the API at `http://localhost:8000`. That is correct when the page is opened on the machine that runs the backend, and wrong for everyone else: a browser resolves `localhost` to the **viewer's** machine, so a remote viewer would be asking their own laptop for the API. An HTTPS page calling `http://localhost` is blocked as mixed content regardless. Live mode would be dead for every remote viewer, and the failure looks like "the console just doesn't respond", which is a miserable thing to debug on a deadline.

`voice/web/console.html` therefore uses a **relative** API base whenever it was served from anywhere other than a local dev port. The edge is what puts the two services back together under one origin.

The Lab Agent API keeps binding to **127.0.0.1**. It has no authentication of its own, so it must never be exposed directly. It reaches the internet only through the paths the edge explicitly routes.

## Local processes (all on the dev machine)

| service | port | what it is |
|---|---|---|
| Lab Agent API | 8000 (loopback) | `app/main.py`, the planner and validator |
| orchestrator | 8766 | serves `console.html`, the voice WebSocket, ASR/TTS glue |
| ASR (Fun-ASR-Nano) | 8030 (loopback) | on the GPU host, via SSH forward |
| TTS (gepard) | 8040 (loopback) | on the GPU host, via SSH forward |

Bring them up with:

```bash
cd voice
bash deploy/dev-forward.sh --bg          # GPU services
bash deploy/run-integration-local.sh     # backend + orchestrator
```

## Cloudflare Tunnel: the routing that has to exist

The tunnel is token-managed, so its ingress lives in the Zero Trust dashboard, not in a local config file. Add **three public hostnames on the existing tunnel**, in this order. Order matters: the specific paths must come before the catch-all, or the catch-all swallows them.

| # | hostname | path | service |
|---|---|---|---|
| 1 | `lab-auto.<domain>` | `/session/*` | `http://localhost:8000` |
| 2 | `lab-auto.<domain>` | `/adapters` | `http://localhost:8000` |
| 3 | `lab-auto.<domain>` | (empty: catch-all) | `http://localhost:8766` |

That is what makes the page and the API one origin. Everything else (the page, the WebSocket, the VAD assets, the demo audio) is served by the orchestrator.

If the console later calls another backend route, it needs its own entry here. The ones it uses today are `POST /session/message`, `POST /session/reset`, and `GET /adapters`.

## Access control: do NOT skip this

The orchestrator holds an Anthropic API key and can drive a GPU. The Lab Agent API has no auth at all. An open URL is an open invitation, and the app's own email allowlist is not usable here (the console has no place to type an email, unlike the standalone voice page).

Put **Cloudflare Access** in front of the hostname:

1. Zero Trust -> Access -> Applications -> Add a self-hosted application.
2. Domain: `lab-auto.<domain>`.
3. Policy: Allow, and match on **email**, listing exactly the operator and the teammate.
4. Identity: one-time PIN is enough. No IdP needed.

**Confirm Access is enforcing BEFORE the tunnel is pointed at anything.** An unauthenticated request must answer a redirect to the Cloudflare Access login. If it answers with the app, stop and fix the policy: at that moment the key and the GPU are open to anyone with the URL.

```bash
curl -sI https://lab-auto.<domain>/ | head -3     # expect a 302 to cloudflareaccess.com
```

## Order of operations

1. GPU forward up, then `run-integration-local.sh`, and check `http://127.0.0.1:8766/console.html` locally.
2. Add the Access application and policy.
3. Add the three public hostnames.
4. Probe unauthenticated (above). Only when it redirects, share the URL.
5. Have the remote user open it, sign in with the PIN, and run one spoken turn end to end before any recording starts.

## What the remote user gets

- **Demo mode**: works with no backend and no network. The replies are pre-rendered (`voice/web/demo-audio/`), so a bad connection cannot mute the demo. If the speech service IS reachable, Demo synthesizes live instead, in whatever voice is selected.
- **Live mode**: speech goes to the real ASR, the real planner, and the real validator, and the reply is spoken back. This is the one that needs everything above to be up.

## Known sharp edges

- **The mic needs a secure context.** Over the tunnel that is satisfied (HTTPS). On plain `http://<lan-ip>` it is not, and the mic will silently never start. Use the hostname.
- **The GPU box and the dev machine must both be awake.** This deployment is a chain of personal machines, not a service. If the demo has to survive a laptop lid closing, that is a different deployment, not this one.
- **The confirmation floor was calibrated on synthesized speech** (clear confirmations at prob_mean 0.92 to 0.95). A real microphone in a noisy room will score lower. If a good "confirm" starts being refused, that number (`LA_CONFIRM_FLOOR`, default 0.40) is the one to revisit, and the settings drawer shows the live confidence next to it so it can be judged rather than guessed.
