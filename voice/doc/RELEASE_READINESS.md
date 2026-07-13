# Public Release Readiness

Findings from auditing whether a stranger cloning this repo from GitHub could deploy it
on their own infrastructure.

**Status: PARTIALLY CLOSED.** The portability blockers have since been fixed by the
host-agnostic deploy rework; the licensing and packaging items below are still open and
still block a public release. This file exists so the audit is not re-derived from scratch
later.

*Created: 2026-07-09*

---

## 2026-07-09 21:46:32 +08 - Session

### Verdict, as of this audit

No. On a fresh `git clone` the documented deploy failed at step 2, and even if it had not,
the instructions only worked on a rented GPU pod created from one specific base template.

The gap was packaging and portability, not design. The architecture, the WS contract in
`doc/INTEGRATION.md`, the idempotent setup scripts, `health.sh`, the documented env-var
surface, and the load-bearing transformers install order were all in good shape.

### Closed since this audit

The deploy pack was rewritten to run on any Linux box with an NVIDIA GPU and conda, which
closes every portability finding this audit raised.

- **The host contract is one file, and it names no host.** `deploy/env.sh` holds it:
  `LA_APP_DIR`, `LA_ENV_DIR`, and `LA_HF_HOME` are env overrides with `$HOME`-relative
  defaults, so no user, hostname, or absolute home path is baked in anywhere. The old
  hardcoded `/root` venv and app paths are gone.
- **Torch is installed explicitly, not inherited.** `LA_TORCH_SPEC` is installed from the
  `cu128` index before any requirements file, so there is no invisible dependency on a base
  image's system torch, and `scripts/preflight_gpu.py` asserts the expected compute
  capability up front instead of letting the services fail late at import.
- **The container-disk versus network-volume split is gone.** There is exactly one Hugging
  Face cache (`LA_HF_HOME`). That split was specific to the rented pod, had no equivalent on
  a bare GPU box, and had already caused one real bug (the lost gepard preset voices).
- **The deploy step no longer dies on a fresh clone.** Code reaches the host via
  `deploy/sync-to-host.sh`, which deliberately does not send `credentials/` at all: the GPU
  host runs only ASR and TTS, which need no secrets. The old failure, an rsync of a
  gitignored directory aborting the user's very first command, cannot happen.
- **The verification scripts ship.** The smoke entry points were promoted out of the
  gitignored underscore namespace into tracked, permanent names (`scripts/run_local_smoke.sh`,
  `scripts/smoke_tts.py`, and the rest of the `scripts/smoke_*.py` suite), so a clone now
  carries the interface the docs actually point at.

### Still open

- **No LICENSE file.** By default this means all rights reserved: nobody may legally use the
  code. This alone blocks release.
- **Upstream licensing untouched.** gepard-1.0, FunASR-Nano, and the OmniVAD WASM binaries
  vendored into `web/vendor/` (including `.wasm` and `.omnivad` model blobs) each carry terms
  the repo never mentions. A third-party-notices section is needed.
- **The credential format is documented only inside the gitignored credential directory.**
  Whatever explains the format of each credential file is therefore the one doc guaranteed
  never to reach a user. A tracked `credentials/README.md` or `.env.example` would fix it.
- **The tunnel is unexplained.** `deploy/run-tunnel.sh` runs a Cloudflare Tunnel connector,
  but the public-hostname ingress is configured in the Cloudflare dashboard, and nothing
  tells a user to create a named tunnel or how. Worth stating plainly that it is optional:
  the orchestrator runs locally and is reachable at `localhost:8765` with no tunnel at all,
  so the tunnel is only needed to publish the demo.
- **`HF_TOKEN` is stated but not scoped.** The README exports it "to pull model weights"
  without saying which checkpoints are actually gated. The preset-voices Space needs none.
- **Internal docs ship as tracked files.** `doc/STATUS.md`, `doc/agent_teams_bootstrap.md`,
  and `CLAUDE.md` describe the agent team, single-writer file ownership, hackathon judging
  criteria, and the host credential file. Not harmful, but they signal to a reader that this
  was never meant for them.
- **No CI.** The suite under `tests/` has grown to ten files, but nothing runs them
  automatically on a push.

### Smallest set of changes that would make the claim true

Ordered by what actually stops a user.

1. Add a LICENSE, plus a third-party-notices section covering the models and the vendored
   OmniVAD binaries.
2. Add a tracked `credentials/README.md` (or `.env.example`) documenting each token and its
   file format, outside the gitignored payload.
3. Document the tunnel: what a named tunnel is, how to create one, and that it is optional.
4. Wire the existing test suite into CI.

Item 1 is what stops a lawful user. The rest are what stop a motivated one.

### Sequencing note

Agreed to defer the remaining items until after the integration work lands. The portability
findings were closed by the host-agnostic deploy rework; the ones still listed above were
verified against the repo as of the date of this entry, so re-check anything integration
touches (especially `deploy/env.sh`, the `scripts/` naming, and the `credentials/` layout)
before acting on them.
