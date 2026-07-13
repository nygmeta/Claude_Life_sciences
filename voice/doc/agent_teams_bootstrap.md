# Agent Teams Bootstrap

*Created: 2026-07-09 +08.*

How to run the agent-team operating model on this repo from a cold start. The per-role
static contracts (scope, toolchain, self-checks) live as selectable agent profiles in
`.claude/agents/`. This doc holds the ORCHESTRATION (the parts a per-agent profile cannot
hold): the standing constraints, the tokens, the smoke gate, the lead conductor rules, the
dispatch protocol, the usage-log cadence, and the cold-start checklist. The lead is the
main session and is NOT a profile, so these rules are its contract.

Context: this repo is the voice half (VAD, ASR, LLM, TTS) of a hackathon lab-automation
assistant. A teammate builds the lab-automation half separately; the two merge days into
the hackathon. 25% of the judging is HOW Claude was used, so logging Claude usage is a
first-class lane here, not an afterthought.

---

## Operating principles (the 3 standing constraints)

1. **No git worktrees:** one shared working tree.
2. **Lead/orchestrator is read-only.** It explores, monitors, and orchestrates; it never
   edits files or mutates the pod; it delegates ALL mutating work. The lead MAY run
   read-only status queries (`git status`, `git diff`, curl a health endpoint, read logs).
3. **Each worker has a disjoint file scope.** The scope lives in the worker's profile; the
   lead ENFORCES it by reading `git diff` before marking any task done. Profiles are
   advisory, not a sandbox: every worker tool flows through Bash, so the lead's diff check
   is the real guard.

---

## Roster (1 unprofiled lead + 6 profiled workers)

The lead is the main session (no profile). Spawn a worker by its profile name and its
standing contract loads automatically. Standing (usually warm): web-core plus whichever
lane the day's work lives in. On-demand: the rest. usage-log is dispatched fire-and-forget
at every milestone.

- `.claude/agents/web-core.md`: SOLE writer of `web/server.py` (orchestrator + WS) and
  owner of the WS message contract. Owns the mock services and the local smoke + latency
  scripts. Inherits the session model.
- `.claude/agents/voice-ui.md`: SOLE writer of `web/index.html` (VAD, panels, playback,
  barge-in). `web/vendor/**` is read-only for everyone. Inherits the session model.
- `.claude/agents/speech-svc.md`: SOLE writer of `asr/**` + `tts/**` (FunASR-Nano, gepard,
  voices) and their smoke/fetch scripts. Writes locally; pod-ops deploys. Inherits the
  session model.
- `.claude/agents/pod-ops.md`: SOLE pod mutator (ssh/rsync/restarts/tunnel), executes the
  `deploy/` recipe pack. Pinned `sonnet` (deterministic recipe execution).
- `.claude/agents/docs.md`: single writer of contested markdown (README, `doc/**`). Pinned
  `sonnet` (turnaround over prose depth).
- `.claude/agents/usage-log.md`: SOLE writer of `CLAUDE_USAGE.md`. Pinned `haiku` for
  appends; the lead overrides to `sonnet` for ROLLUP dispatches. Rationale below.

**Model routing rationale (also a judge-facing story):** the lead runs the strongest
available model because orchestration, review, and integration judgment concentrate there.
Code-writing lanes inherit the session model. Recipe-execution (pod-ops) and fast-docs
lanes pin sonnet: their hard thinking is done upstream. The logger pins haiku: it formats
and appends a payload the lead already composed; entry quality depends on the dispatch
payload, not the logger's intelligence. Synthesis (ROLLUP) needs narrative judgment, so
those dispatches override to sonnet.

---

## Serialization rules (the non-obvious core)

### Hot files are single-writer
`web/server.py` and `web/index.html` are each one file touched by most features. One
web-core and one voice-ui at a time, ever. Parallelism comes from running lanes ALONGSIDE
each other (web-core + voice-ui + speech-svc + docs), never two agents in one lane.

### Concurrent writers
A worker's silence is not evidence that it has stopped. If a warm worker's mailbox seems
unreachable (garbled, truncated, or missing replies), do NOT spawn a replacement for its
lane until the incumbent has explicitly acknowledged a stand-down. A respawn while the
original may still be alive puts two agents in one lane, which is exactly what the
single-writer rule exists to prevent, and nothing in the harness detects it for you.

If a worker itself suspects a twin (another live process touching its files, unexplained
commits landing underneath it), it must halt its own writes and report immediately rather
than race to land first. Self-detection and voluntary stand-down, not the lead's
oversight, is what limits the damage once a collision is already underway.

Two side effects of a collision worth knowing, both seen for real: a red smoke gate may be
port contention rather than a code regression (the smokes bind hardcoded ports, so a
second stack silently corrupts the first instead of failing loudly), and HEAD can end up
importing a module that was never committed (one worker's commit sweeping in another's
in-progress edits). Add a HEAD-self-contained import check to the gate; nothing else
catches that. See doc/STATUS.md's "concurrent-writer collision" entry for the case that
motivated this rule.

### The WS contract changes server-first
web-core owns the message contract. A contract change = web-core lands handler + mock +
smoke in one change and reports the exact schema delta; the lead then dispatches voice-ui
to follow on the client side. Never dispatch both ends of a contract change concurrently.

Amendment (2026-07-12, tested twice): both ends MAY be dispatched concurrently when the
lead freezes the exact contract (every message type and field list) in both briefs BEFORE
either worker starts. Done this way for the action_* and announce_* message families, and
the client consumed the server's fields 1:1 on first contact with zero rework. The rule
still holds whenever the contract is being discovered rather than decided up front: if the
lead cannot write the field list in the brief, serialize server-first.

### The smoke gate
No `web/server.py` change deploys to the host before web-core reports a literal
`SMOKE: PASS` from `bash scripts/run_local_smoke.sh` (mock ASR/TTS + real Claude API, no
GPU needed). pod-ops is instructed to refuse gate-skipping dispatches. speech-svc changes
have no local gate (they need the GPU); their verification is on-host post-deploy
(`health.sh` out of `loading`, plus the TTS smoke for `tts/**` changes).

### Host mutations serialize through pod-ops
One host mutation window at a time under the `host` token (sync through health-verified).
Read-only host ssh (log tails, health curls) is allowed to any worker without the token,
but never concurrently with a service-restart window.

### status-log
`doc/STATUS.md` / `CLAUDE.md` are shared-append: one writer at a time under the
`status-log` token, append-only dated tail entries. docs is the primary prose writer of
`doc/STATUS.md`; every agent appends its own tail entries.

### Host discipline
The GPU host is self-hosted (a borrowed personal machine), not a rented pod: there is no
billing to stop. Both services run as persistent systemd `--user` units (survive SSH
disconnect, logout, and an unattended reboot; restart on crash), installed by
`deploy/install-services.sh`.

---

## Tokens (2, brokered by the lead)

1. **host**: held by pod-ops for a whole mutation window (sync, setup, restart, verify).
2. **status-log**: short-lived append lock on `doc/STATUS.md` / `CLAUDE.md`. Acquire for
   one append, release immediately.

(`CLAUDE_USAGE.md` needs no token: usage-log is its only writer, and the lead serializes
by dispatching one usage-log at a time.)

---

## Usage-log cadence (the 25% criterion)

The lead dispatches usage-log with a composed payload (actor, what, claude, artifacts)
after EVERY milestone:
- a feature lands (post diff-review, pre- or post-commit),
- a deploy or demo run happens,
- a notable Claude capability gets exercised (agent-team wave, a skill, a hook catching
  something, plan mode, workflow, memory, model routing decision),
- a design decision Claude drove.

Batch small related events into one entry rather than spamming; aim for entry-per-
milestone, not entry-per-tool-call. At the end of each working session, dispatch
`ROLLUP` with the model override set to sonnet to refresh the judge-facing summary block.
The log must never contain secrets, host coordinates, or tunnel hostnames.

---

## Lead conductor rules (cross-agent; the lead owns these)

- **Do NOT double-dispatch.** When re-tasking a warm named worker via SendMessage, do not
  also reassign a task's owner in the same beat: the owner-change fires a second
  assignment and the worker does the work twice. Pick ONE channel: SendMessage for warm
  workers; owner-assignment only for cold queue pickup.
- **Address workers by the EXACT spawn-result name/agentId**, never the bare lane name: a
  bare name can route to a stale prior-session agent of the same name.
- **Verify before marking done.** Read the actual `git diff` (scope compliance + quality)
  or confirm the artifact before marking a task completed. Trust but verify every worker
  self-report; require literal command output (SMOKE line, health output), never
  characterizations.
- **The lead owns all wakeups for long waits** (model downloads, first-load warmups,
  long host installs). Never let a subagent detach a long job and self-monitor in the
  background: its monitor is orphaned when the subagent completes. Keep long jobs as the
  subagent's FOREGROUND work, or the lead polls directly.
- **Demo protection.** Before any host restart window, check whether a live demo or the
  user's browser session may be active. Restarting a GPU service (`lab-assistant-asr` /
  `lab-assistant-tts`) drops any assistant turn in flight on it; restarting the LOCAL
  orchestrator (`run-web-local.sh`) is what kills the WS session itself, since that
  session lives between the browser and the orchestrator, not the GPU host.
- **Commit batches** with `/procommit`; the lead runs git. Before every commit run the
  standalone-repo scan on the staged diff: no secrets (`sk-`, `hf_`, JWT-shaped), no
  private hostnames or host coordinates, no references to other projects, `credentials/`
  and `data/` still gitignored.

---

## Dispatch protocol

- **Spawn named background agents** (Agent tool with `name` + `run_in_background`, using
  the worker profile's `subagent_type`). Re-task a warm agent via SendMessage; do not
  re-spawn it.
- **Deploy handoff:** web-core (or speech-svc) finishes + reports what changed and which
  services need a restart; lead verifies the smoke gate; lead dispatches pod-ops; pod-ops
  takes the `host` token, queries host state, syncs, restarts, health-verifies, reports
  literal output, releases.
- **Contract handoff:** web-core lands server-side first; lead relays the schema delta to
  voice-ui verbatim from web-core's done-report.
- **UI verification handoff:** voice-ui changes end with a concrete manual browser
  checklist; the lead runs it or asks the user to, before marking done.
- **After the done-verification of any milestone: dispatch usage-log.**

---

## Integration outlook (mid-hackathon merge)

The voice pipeline meets the teammate's lab-automation system at exactly one seam: the
LLM step inside `web/server.py` (user text in, reply text out). Integration shape: the
LLM call gains tool-use / command dispatch to the robot side, and robot results flow back
as text for TTS. Standing preparation:
- web-core keeps the LLM call behind one clean function (its profile mandates this).
- Lanes are directory-scoped, so the team ports into the merged repo unchanged; expect to
  add a robot-side lane owned by the teammate (human or agent) and treat the seam function
  as a contract file (single writer: web-core, changes announced like WS contract deltas).
- `CLAUDE_USAGE.md` continues in the merged repo, same file and template; add a
  `Workspace:` line to entries if the judges need the two halves distinguished.

---

## Quick start for a new session

1. **Read:** this file and `doc/STATUS.md` (the backlog).
2. **Query state:** `git status`; if the GPU host is up (coordinates in
   `credentials/host.env`), curl its health through pod-ops or read-only ssh.
3. **Spawn the lanes the day's items need**; each loads its own contract from its profile.
4. **Pick STATUS.md open items**, map each to a lane, run a parallel wave (one agent per
   lane, hot files single-writer).
5. **Log milestones** to usage-log as they land; ROLLUP at session end.
6. **Commit batches** with `/procommit` after the standalone-repo scan.

---

## Pointers

- Per-role contracts: `.claude/agents/{web-core,voice-ui,speech-svc,pod-ops,docs,usage-log}.md`
- Backlog: `doc/STATUS.md`
- Judging evidence: `CLAUDE_USAGE.md` (usage-log's file; everyone else read-only)
- Architecture + deploy steps: `README.md`
- Style reference for UI work: `doc/DESIGN.md`
