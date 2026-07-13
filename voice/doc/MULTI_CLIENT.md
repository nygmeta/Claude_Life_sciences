# Multi-Client Support: Implementation Spec

Make one orchestrator serve multiple independent visitors without them seeing or
disturbing each other. Concurrent WS connections already work (each gets its own
Session); what is NOT isolated today is the state around them: the History panel
lists every visitor's sessions (and allows cross-visitor rename/delete), STT hints
are one global set, and there is no queueing discipline when two clients synthesize
at once. This spec fixes those three things and nothing else.

*Created: 2026-07-10 04:14 +08. Status: SPECCED; implement on the `multi-client`
worktree branch, local mock gate only. DO NOT DEPLOY: a live tester is using the
deployed demo. Deploy happens only on an explicit go-ahead later.*

## Decisions (pinned)

1. **Scoping model: anonymous per-browser client id.** No accounts, no rooms, no
   auth. The browser generates a UUID hex once, persists it in localStorage
   (`la.client.v1`), and presents it on every connect. Clearing site data means a
   fresh identity; that is acceptable demo semantics.
2. **Back-compat by default, no feature flag.** A connection with no client id
   (old page still deployed, curl probes, existing smoke scripts) gets the
   legacy "public" scope, which maps to today's exact on-disk layout. Existing
   sessions and hints stay where they are and remain visible to legacy clients;
   nothing migrates. This is what makes the branch deployable later with zero
   ceremony and lets most existing smoke suites run unchanged.
3. **GPU fairness lives in the orchestrator.** A single global
   `asyncio.Semaphore(1)` around TTS `synthesize()` HTTP calls serializes synths
   across ALL clients and the playground. Per-connection behavior is unchanged
   (turns already synthesize sequentially); this only prevents cross-client
   overlap on the GPU. ASR calls stay as they are (the ASR service handles its
   own queueing; short calls, low risk). If contention shows up in metrics later,
   tune then.
4. **WS contract change is connection-level only.** The client id travels as a
   query parameter on the WS URL (`/?cid=<hex>`). No new message types, no field
   changes on existing messages, one additive echo (see contract below).

## WS contract delta

- Client -> server: WS connect URL gains `?cid=<hex>` (8-64 lowercase hex chars).
  Absent or invalid cid -> the connection is assigned the "public" legacy scope
  (invalid means: wrong charset/length; NEVER trust it as a path fragment).
- Server -> client: `session_started` gains a `cid` field echoing the scope the
  server actually assigned (the validated cid, or null for the public scope).
  Purely informational; the client does not need to act on it.

## Server changes (web/server.py, web-core)

1. **Scope resolution at connect.** Parse `cid` from the WS request path
   (websockets exposes the request URI). Validate with a strict regex
   `^[0-9a-f]{8,64}$`. Valid -> scope = cid; else scope = None (public/legacy).
   Store on the connection (alongside `sess`), and on each Session (new
   `__slots__` entry `cid`), so autoname/persistence paths can reach it.
2. **Sessions scoping.** Today: `SESSIONS_DIR / f"{sid}.json"` with SESSIONS_DIR
   = data/sessions. New: scope None -> unchanged legacy path; scope cid ->
   `SESSIONS_DIR / cid / f"{sid}.json"`. Every session op (save, list, get,
   rename, delete, autoname, numbering/ranks) resolves through one helper that
   takes the connection's scope, so cross-scope access is structurally
   impossible: ids are only ever resolved inside the scope directory. The
   existing session-id validation (path-traversal hardening + its committed
   regression tests) stays exactly as is and applies within the scope dir; the
   cid segment is validated by the connect regex before any filesystem use.
   Session numbering: per scope (a fresh visitor starts at 1), derived the same
   way it is today but from the scope directory listing.
3. **Hints scoping.** Today: one global HINTS dict + data/asr_hints.json. New:
   per-scope hints, lazily loaded into a dict keyed by scope, persisted to
   `data/hints/<cid>.json`; public scope keeps data/asr_hints.json (legacy
   location, seeded/served exactly as today). A NEW cid scope starts from a COPY
   of the public hints (sensible defaults) the first time it is read, then
   diverges on its own set_hints. get_hints/set_hints and the per-segment
   apply_replacements/hotword use all resolve through the connection's scope.
4. **TTS semaphore.** Module-level `TTS_GPU_SEM = asyncio.Semaphore(1)`;
   `synthesize()` acquires it around the HTTP POST. Playground (`tts_test`) and
   assistant paths both go through synthesize(), so both are covered. The
   consumer's per-sentence latency metric should measure synth time INSIDE the
   semaphore acquire (queue wait excluded from `ms`, so rtf stays honest), but
   first_ms stays wall-clock from t_perceived (the user's perceived wait,
   including any queueing).
5. **Metrics.** Assistant/playground latency records gain `"cid": <first 8 chars
   or None>` so contention and per-visitor behavior are attributable.
6. **Docstring/Env.** Update the module docstring WS-contract block (cid query
   param, session_started.cid) and document the scoped data layout. Also
   parameterize the data root: new `LA_DATA_DIR` env (default: the current
   `STATIC_DIR.parent / "data"`) that SESSIONS_DIR, HINTS paths, and the
   LOG_FILE default all derive from. This is what lets a future dev-instance on
   the pod run against its own data directory; LA_LOG_FILE keeps working as an
   explicit override.

## Client changes (web/index.html, voice-ui)

1. Generate/persist the client id: localStorage key `la.client.v1`, value =
   32-char lowercase hex (crypto.getRandomValues). Wrapped reads/writes like the
   other storage helpers; if localStorage is unavailable, connect without a cid
   (public scope) rather than failing.
2. Append `?cid=<id>` to the WS URL in autoWsUrl()/connect().
3. Nothing else: History and STT panels automatically show scoped data because
   the server scopes what it returns. No UI for identity (deliberate).

## Smoke additions (scripts/smoke_multi.py, wired into run_local_smoke.sh)

1. Isolation: client A (cid a...) and client B (cid b...) each run a turn; A's
   list_sessions shows only A's session, B's only B's.
2. Hints isolation: A set_hints a replacement; B's get_hints unchanged; a new
   cid C first-read equals the public defaults.
3. Legacy scope: a no-cid connection lists exactly the sessions a pre-change
   server would have listed (root-level files), proving back-compat.
4. Cross-scope denial: B issuing get/rename/delete with A's session id gets the
   same not-found behavior as a bogus id.
5. Invalid cid: `?cid=../../etc` and over-length values are treated as public
   scope (and never touch the filesystem as a path).
6. Concurrency + fairness: A and B fire turns simultaneously against the mock
   TTS (instrumented with an active-request counter); assert the counter never
   exceeds 1 (semaphore works) and both turns complete correctly with their own
   audio.
7. All existing suites stay green unchanged (speculative, cancellation,
   sessions, hints, batching, model-routing). The sessions suite runs in the
   public scope by construction (no cid), which is exactly the back-compat
   guarantee.

## Sequencing

1. Worktree branch `multi-client` off main (this spec is committed to main first
   so the worktree contains it).
2. web-core implements the server per this spec; voice-ui adds the client id
   (tiny, parallel-safe: different files, same worktree).
3. Full local gate in the worktree (all suites + smoke_multi). Commit on the
   branch. NO merge to main, NO deploy, NO pod access of any kind: a live
   tester is on the deployed demo. The branch waits, like speculative-start
   did, for an explicit merge+deploy go-ahead.
4. At deploy time (later): merge, re-gate on main, deploy with a web-only
   restart. Optionally validate first via a second orchestrator instance on
   :8766 with its own LA_DATA_DIR (the parameterization added here), reached by
   SSH port-forward; the demo instance and its tunnel stay untouched.

## Out of scope

- Auth, named rooms, session sharing between browsers, admin views.
- ASR queueing (revisit if metrics show contention).
- Cross-scope migration tooling (legacy sessions stay in the public scope).

---
## 2026-07-11 09:52:56 +08 - Session: identity by verified Access email; operator "see all" view

This session SUPERSEDES the anonymous-cid identity model above and brings the operator
view IN scope (the original "admin views" out-of-scope line no longer holds). Trigger:
the feature request "each client sees only their own history, but the operator (me)
sees all history tagged by client." That second half needs a verified, human-readable
identity, which an anonymous self-issued cid cannot provide.

### Decision (supersedes Decision 1 above)

**Identity is the Cloudflare-Access-verified email, not an anonymous cid.**
- The app is served only behind Cloudflare Access (the same gate that fixes the
  zero-auth exposure). Access authenticates at the edge and injects
  `Cf-Access-Authenticated-User-Email` (plus a signed `Cf-Access-Jwt-Assertion`) on
  every request, including the WS upgrade. websockets 16.1 exposes it at
  `ws.request.headers`, the same object the old spec used to read `?cid=`, so it is
  mechanically just as available.
- Why email over token/cid: (a) an anonymous cid tags the operator view with opaque
  UUIDs, so you cannot tell who is who; (b) a self-issued token cannot enforce the
  operator/client trust boundary, being client-supplied and spoofable; (c) email via
  Access is verified, human-readable, and needs zero app-side auth code because Access
  is deployed anyway. Token would only win if we did NOT use Access and wanted anonymous
  frictionless clients, which this feature (tagging plus an operator gate) rules out.
- The `?cid=` query-param contract is DROPPED. Identity comes from the header
  server-side; the client sends no identity.

### Trust model (load-bearing)

- Trusting `Cf-Access-Authenticated-User-Email` is safe ONLY because the origin binds
  `127.0.0.1` and is reachable exclusively through cloudflared, which enforces Access.
  No external request can set the header while bypassing Access. Never expose the
  orchestrator on a public interface directly.
- Hardening (optional, recommended): verify the `Cf-Access-Jwt-Assertion` signature
  against the team Access certs (`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`)
  and check `aud`, so header-trust becomes defense-in-depth rather than sole reliance.
  Gated by `LA_ACCESS_JWT_VERIFY` + `LA_ACCESS_TEAM_DOMAIN` + `LA_ACCESS_AUD`.

### Scope + operator model

- **scope** = a filesystem-safe token derived from the email: `sha256(lower(email))`
  first 16 hex chars. Hashing (not the raw email) keeps the address out of directory
  names and makes path traversal structurally impossible (`^[0-9a-f]{16}$`). The display
  email is stored inside each session json as `owner_email` (and a per-scope `meta.json`)
  so the operator view can render the human address.
- No Access header (local dev, smoke, direct loopback) maps to the **"public"** scope =
  today's legacy flat layout, exactly the original back-compat rule. All existing smoke
  suites stay green (they connect with no header).
- **Operator** = the connection's email is in `LA_OPERATOR_EMAILS` (comma-separated,
  lowercased). Operator connections: `list_sessions` aggregates across ALL scope dirs,
  each entry tagged `owner` (owner_email, or "public"); get/rename/delete may address any
  scope. Non-operator (client) connections resolve every op through their OWN scope dir
  only, so cross-client read/rename/delete is structurally impossible and returns the
  same not-found as a bogus id. This also fixes today's real bug: get/rename/delete
  currently operate on any sid with no ownership check.

### Carried over UNCHANGED from the original spec

Per-scope directory isolation (`SESSIONS_DIR/<scope>/{sid}.json`, public = legacy flat),
the session-id path-traversal regex plus its regression tests (applied inside the scope
dir), per-scope hints (now keyed by email-scope), the single global `TTS_GPU_SEM`
fairness semaphore, `LA_DATA_DIR` parameterization, and per-record scope tagging in
metrics. Only the SCOPE KEY changes (cid to email-hash) and the OPERATOR aggregation is
added.

### WS contract delta (replaces the cid delta above)

- Client to server: no identity in the URL (a stray `?cid=` is ignored).
- Server to client: `session_started` gains `scope` (opaque hash or "public") and
  `is_operator` (bool). For an operator connection, `list_sessions` entries gain `owner`
  (owner_email or "public"). Clients get no `owner` (every row is theirs).

### Client (web/index.html, voice-ui)

- Sends no identity (Access supplies it). Reads `is_operator` from `session_started`.
- When `is_operator`, the History panel shows an `owner` label per session and,
  optionally, a client filter. The non-operator view is unchanged.

### Smoke additions (scripts/smoke_multi.py)

Local smoke has no real Access, so it SIMULATES identity by setting the
`Cf-Access-Authenticated-User-Email` header on the WS connect (websockets
`additional_headers`). Cases: (1) email A and email B each run a turn; A lists only A's,
B only B's; (2) an operator email (in `LA_OPERATOR_EMAILS`) lists BOTH, each tagged with
its owner; (3) a client cannot get/rename/delete another scope's sid (not-found); (4) no
header maps to the public scope == legacy layout (back-compat); (5) a non-operator email
is NOT treated as operator even if it supplies another scope's hash. Plus every existing
suite stays green (they send no header = public scope).

### Sequencing / deploy caution

Build and local-gate now. DEPLOY (a web-only orchestrator restart, plus turning on
Cloudflare Access with `LA_OPERATOR_EMAILS` set) only on an explicit go-ahead, and only
after Access is confirmed gating the hostname: without Access in front, the email header
is attacker-settable and the operator boundary is void.

---
## 2026-07-13 - Session: Cloudflare Access removed; server-side email allowlist replaces it

This session SUPERSEDES the identity and trust model of the 2026-07-11 session above.
Cloudflare Access has been removed from in front of the app entirely (the operator
deleted the Access application), so the `Cf-Access-Authenticated-User-Email` header
described above will never arrive again, and everything in that session's "Trust
model" and "WS contract delta" sections that depends on it no longer holds.

### What changed

Identity now comes from the CLIENT, not a verified edge header: the connection sends
its email as a `?email=<addr>` query parameter on the WS connect URL, over `wss`
(TLS-encrypted in flight, but not verified by anything upstream of the server). The
server-side `_scope_for_email` logic, the per-user scope hash
(`sha256(lower(email))[:16]`), and the operator concept via `LA_OPERATOR_EMAILS` are
all UNCHANGED from the 2026-07-11 session; only the source of the email changed, from
a trusted header to a client-supplied query parameter.

A new server-side gate, `LA_ALLOWLIST` (comma-separated, lowercased + stripped,
parsed the same way as `LA_OPERATOR_EMAILS`), decides who may connect at all:
- **Non-empty (the production posture)**: a connection must supply an `?email=` that
  is on the list, or the server sends exactly one `{type: "auth_error", reason:
  "email_required" | "not_allowlisted"}` message and closes the socket with code
  `4001`. No session is ever created for a rejected connection, and there is no
  fallback to the public scope.
- **Empty or unset (the default, matching dev and every existing smoke)**: no
  enforcement at all. An email still scopes the connection to its own per-user
  directory exactly as before; a missing email still maps to the legacy "public"
  scope.
- An operator email must ALSO be on the allowlist to connect at all when the
  allowlist is enforced; being in `LA_OPERATOR_EMAILS` does not bypass it.

### The honest new security posture (state this plainly, no softer framing)

**The email is self-asserted: there is no OTP, no verification, and no proof the
connecting client actually controls that address.** This is a materially different,
and materially weaker, trust model than the Cloudflare-Access-verified header the
2026-07-11 session relied on. Concretely:
- The allowlist gates casual, accidental access (a stranger landing on the URL with
  no idea what email to use) and gives each real user their own scoped history. It
  does **not** authenticate anyone: any party who learns or guesses an allowlisted
  address can type it in and connect as that identity, with that identity's full
  scope of past sessions.
- **The operator email functions as a shared secret, not a login.** Knowing the
  operator's address is sufficient to obtain the operator's see-all view; there is no
  password, token, or second factor behind it.
- This tradeoff was made deliberately: the operator wanted no OTP/email-verification
  friction for testers, and accepted spoofability-by-known-address in exchange. A
  one-click identity-provider option (reinstating a verified login) was offered and
  declined; see `doc/DECISIONS.md` for the record of that choice.
- Never present this allowlist as authentication in any user-facing copy. It is
  access control against strangers and a scoping key for cooperative users, nothing
  more.

### WS contract delta (replaces the 2026-07-11 delta above)

- Client to server: the WS connect URL carries `?email=<addr>` (URL-encoded). Absent
  or empty resolves to no email (public scope, or `email_required` if the allowlist
  is enforced).
- Server to client (new): `{type: "auth_error", reason: "email_required" |
  "not_allowlisted"}`, sent only when the allowlist rejects a connection, immediately
  followed by the server closing the socket with code `4001`. This is the ONLY
  message such a connection ever receives; no session-related message follows it.
- `session_started`'s `scope` and `is_operator` fields, and `list_sessions`'s
  operator-only `owner` field, are unchanged from the 2026-07-11 session.

### Client (web/index.html, voice-ui)

A full-screen, opaque email-gate overlay covers the app until a successful connect,
so nothing behind it is reachable before the user provides an email; there is no
"continue without email" path (an earlier version had one; it was removed, since
typing a throwaway address costs a dev nothing and the skip path bypassed nothing on
an enforcing server anyway). The chosen email persists in `localStorage`
(`la.email.v1`); a header control shows "signed in as `<address>`" and a "Sign out"
button that clears the stored email and reloads to a fresh gate, so a user can
explicitly switch identity. A server-sent `auth_error`, or the socket closing with a
code in the 4000-4099 range, shows the gate again with an inline reason and does NOT
auto-reconnect (which would otherwise loop pointlessly against a rejecting server).
Email validity is checked only superficially client-side (a non-empty local part, an
`@`, a dot in the domain); the server's allowlist is the only real check.

### Carried over UNCHANGED from the 2026-07-11 session

Per-scope directory isolation, the session-id path-traversal regex and its
regression tests, per-scope hints, the `TTS_GPU_SEM` fairness semaphore, `LA_DATA_DIR`
parameterization, per-record scope tagging in metrics, and the operator's `owner`-
tagged aggregated `list_sessions` view. Only the identity SOURCE changed (a verified
edge header to a client-supplied, allowlist-gated query parameter); the scoping and
operator logic built on top of that identity did not change shape.
