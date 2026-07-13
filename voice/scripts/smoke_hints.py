"""Local check for ASR hints, which are PER-USER (per scope), not global.

Public scope (no Access header, the legacy/local path):
  set_hints round-trips, and a replacement is applied to the transcript.

Per-scope cases (identity via the ?email= connect query param, exactly as
smoke_multi.py does it):
  a. a scope with no saved hints sees the DEFAULTS (both A and B, on first connect)
  b. A sets its hotwords + replacements (its own scope only)
  c. B still sees ITS OWN values (the defaults), unaffected by A's write
  d. A reconnects and still sees its saved values (per-scope persistence)
  e. an operator does NOT see or affect another scope's hints: a client-supplied
     `scope` on get_hints/set_hints is IGNORED for everyone, operator included, so
     the operator's get returns its own hints and its set lands in its own scope,
     leaving A's untouched.

A + B use per-run-FRESH scopes: any hints file left by a previous run is moved
aside (house rule: move, never delete) to a fixed name under deprecated/, so each
run genuinely exercises the unsaved -> defaults path and nothing accumulates.

Sends EXACTLY ONE audio segment (the public-scope replacement check), same as
before: the mock ASR alternates two canned segments off a process-global counter
that the other smokes' parity depends on. The scope cases send no audio at all.

Runs against web/server.py + the mock ASR (no LLM call: we don't end the turn)."""
import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import websockets

# No arg: local mode (mock ASR) does the full set/replace/get + per-scope test.
# With a URL arg: remote read-only mode (get_hints only) so we don't mutate a live pod.
PORT = os.environ.get("LA_WS_PORT", "8765")
URL = sys.argv[1] if len(sys.argv) > 1 else f"ws://localhost:{PORT}/"
REMOTE = len(sys.argv) > 1
SEG = json.dumps({"type": "audio_segment",
                  "audio_b64": base64.b64encode(b"\x00\x00" * 8000).decode(),
                  "sample_rate": 16000})

APP = Path(__file__).resolve().parents[1]
HINTS_DIR = APP / "data" / "hints"
GRAVEYARD = APP / "deprecated"

# Must match web/server.py's DEFAULT_HINTS.
DEFAULT_HOTWORDS = ["Claude"]
DEFAULT_REPLACEMENTS = {"cloud code": "Claude Code"}

A_EMAIL = "hints.alice.smoke@example.com"
B_EMAIL = "hints.bob.smoke@example.com"
_ops = [e.strip() for e in os.environ.get("LA_OPERATOR_EMAILS", "").split(",") if e.strip()]
OP_EMAIL = _ops[0] if _ops else "operator.smoke@example.com"

A_HOTWORDS = ["Claude", "gepard", "OmniVAD"]
A_REPLACEMENTS = {"cloud code": "Claude Code", "fun as are": "FunASR"}
OP_HOTWORDS = ["operator-own-scope"]


def scope_of(email: str) -> str:
    """Mirror the server: sha256(lower(email))[:16]."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def reset_scope(email: str) -> None:
    """Guarantee an UNSAVED scope for this run by moving any file a previous run
    left behind out of the live hints dir. Fixed destination name, so repeated runs
    overwrite one graveyard file instead of piling up."""
    src = HINTS_DIR / f"{scope_of(email)}.json"
    if src.is_file():
        GRAVEYARD.mkdir(parents=True, exist_ok=True)
        os.replace(src, GRAVEYARD / f"smoke_hints_{scope_of(email)}.json")


def connect(email=None):
    # identity via the ?email= connect query param (Cloudflare Access is gone)
    url = URL + (f"?email={quote(email)}" if email else "")
    return websockets.connect(url, max_size=16 * 1024 * 1024)


async def recv_until(ws, want, timeout=15):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if m.get("type") == want:
            return m


async def get_hints(email, **extra):
    """get_hints as `email`; `extra` lets a case smuggle in a `scope` field that the
    server must ignore."""
    async with connect(email) as ws:
        await ws.send(json.dumps({"type": "get_hints", **extra}))
        return await recv_until(ws, "hints")


async def set_hints(email, hotwords, replacements, **extra):
    async with connect(email) as ws:
        await ws.send(json.dumps({"type": "set_hints", "hotwords": hotwords,
                                  "replacements": replacements, **extra}))
        return await recv_until(ws, "hints")


def is_defaults(m) -> bool:
    return m.get("hotwords") == DEFAULT_HOTWORDS and m.get("replacements") == DEFAULT_REPLACEMENTS


async def public_scope_case() -> dict:
    """The original local test, now explicitly scoped to "public" (no header): set,
    transcribe one segment, get. This is the ONLY audio segment the script sends."""
    async with connect(None) as ws:
        await ws.send(json.dumps({"type": "set_hints",
                                  "hotwords": ["gepard", "OmniVAD"],
                                  "replacements": {"weather": "WEATHER"}}))
        h = await recv_until(ws, "hints")
        print("public set -> hints:", h.get("hotwords"), h.get("replacements"), flush=True)
        set_ok = (h.get("hotwords") == ["gepard", "OmniVAD"]
                  and h.get("replacements") == {"weather": "WEATHER"})

        await ws.send(SEG)   # mock ASR returns "What is the weather"; replacement -> "WEATHER"
        t = await recv_until(ws, "transcript")
        print("public transcript:", repr(t.get("text")), flush=True)
        rep_ok = "WEATHER" in (t.get("text") or "")

        await ws.send(json.dumps({"type": "get_hints"}))
        g = await recv_until(ws, "hints")
        get_ok = g.get("replacements") == {"weather": "WEATHER"}
    return {"public_set": set_ok, "public_replace": rep_ok, "public_get": get_ok}


async def scope_cases() -> dict:
    checks = {}
    reset_scope(A_EMAIL)
    reset_scope(B_EMAIL)

    # a. an unsaved scope sees the DEFAULTS, and each scope sees them independently
    a0 = await get_hints(A_EMAIL)
    b0 = await get_hints(B_EMAIL)
    checks["a_defaults"] = is_defaults(a0) and is_defaults(b0)
    print(f"a. defaults: A={a0.get('hotwords')} {a0.get('replacements')} | "
          f"B={b0.get('hotwords')} {b0.get('replacements')}:",
          "PASS" if checks["a_defaults"] else "FAIL", flush=True)

    # b. A sets its own hints
    a1 = await set_hints(A_EMAIL, A_HOTWORDS, A_REPLACEMENTS)
    checks["b_set_own"] = (a1.get("hotwords") == A_HOTWORDS
                           and a1.get("replacements") == A_REPLACEMENTS)
    print(f"b. A set -> {a1.get('hotwords')} {a1.get('replacements')}:",
          "PASS" if checks["b_set_own"] else "FAIL", flush=True)

    # c. B is untouched by A's write (still its own values: the defaults)
    b1 = await get_hints(B_EMAIL)
    checks["c_other_unaffected"] = is_defaults(b1) and b1.get("hotwords") != A_HOTWORDS
    print(f"c. B after A's write: {b1.get('hotwords')} {b1.get('replacements')}:",
          "PASS" if checks["c_other_unaffected"] else "FAIL", flush=True)

    # d. A's hints persist across a reconnect (a new connection, a new Session)
    a2 = await get_hints(A_EMAIL)
    checks["d_persist"] = (a2.get("hotwords") == A_HOTWORDS
                           and a2.get("replacements") == A_REPLACEMENTS)
    print(f"d. A reconnect: {a2.get('hotwords')} {a2.get('replacements')}:",
          "PASS" if checks["d_persist"] else "FAIL", flush=True)

    # e. an operator supplying A's scope is IGNORED on BOTH get and set: hints have
    #    no cross-scope addressing for anyone. The operator reads/writes only its own.
    op_get = await get_hints(OP_EMAIL, scope=scope_of(A_EMAIL))
    op_sees_a = op_get.get("hotwords") == A_HOTWORDS and op_get.get("replacements") == A_REPLACEMENTS
    op_set = await set_hints(OP_EMAIL, OP_HOTWORDS, {}, scope=scope_of(A_EMAIL))
    op_set_own = op_set.get("hotwords") == OP_HOTWORDS and op_set.get("replacements") == {}
    a3 = await get_hints(A_EMAIL)   # A must be exactly as A left it
    a_intact = a3.get("hotwords") == A_HOTWORDS and a3.get("replacements") == A_REPLACEMENTS
    a_file = HINTS_DIR / f"{scope_of(A_EMAIL)}.json"
    file_intact = (a_file.is_file()
                   and json.loads(a_file.read_text(encoding="utf-8")).get("hotwords") == A_HOTWORDS)
    checks["e_operator_no_cross_scope"] = (not op_sees_a) and op_set_own and a_intact and file_intact
    print(f"e. operator w/ scope=A: get sees A's? {op_sees_a} (must be False); "
          f"set landed in own scope? {op_set_own}; A intact (ws/file)? {a_intact}/{file_intact}:",
          "PASS" if checks["e_operator_no_cross_scope"] else "FAIL", flush=True)
    return checks


async def main() -> int:
    if REMOTE:
        async with connect(None) as ws:
            # read-only: just confirm the deployed handler answers get_hints
            await ws.send(json.dumps({"type": "get_hints"}))
            g = await recv_until(ws, "hints")
            print("get -> hints:", g.get("hotwords"), g.get("replacements"), flush=True)
            ok = "hotwords" in g and "replacements" in g
            print("HINTS(remote):", "PASS" if ok else "FAIL", flush=True)
            return 0 if ok else 1

    checks = await public_scope_case()
    checks.update(await scope_cases())

    ok = all(checks.values())
    print("HINTS:", "PASS" if ok else "FAIL", checks, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
