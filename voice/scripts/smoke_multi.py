"""Local check for the email-scoped multi-client isolation boundary and the
operator see-all view (web/server.py). This is a SECURITY test: a client must
never see, rename, or delete another client's history, and a non-operator must
never be treated as operator even if it supplies another scope's hash.

Identity is supplied via the ?email= connect query param (Cloudflare Access is
gone; the server now sources identity from the client). The operator email comes
from LA_OPERATOR_EMAILS, which run_local_smoke.sh exports into both the
orchestrator and this script. No LA_ALLOWLIST is set, so there is no enforcement:
the email simply scopes the connection.

Sessions are persisted WITHOUT the LLM/audio path: renaming the live session
calls save_session, which writes the scoped file. So this suite makes zero LLM
calls and sends zero audio segments (parity-neutral: it never touches the mock
ASR's shared canned-segment counter, so its position in the run is irrelevant).

Cases (each asserted):
  1. Isolation: email A and email B each persist a session; A's list_sessions
     shows only A's, B's only B's (and non-operator rows carry no owner/scope).
  2. Operator: the operator email lists BOTH, each row tagged with its owner+scope.
  3. Cross-client denial: a non-operator (B) get/rename/delete of A's sid returns
     not-found/error (or a no-oracle idempotent delete), and A's file is unchanged.
  4. Back-compat: NO header -> public scope == the legacy FLAT layout; a no-header
     connection lists the flat SESSIONS_DIR/*.json and never the scoped sessions.
  5. Ignored scope: a NON-operator that supplies scope=<A's hash> still resolves in
     ITS OWN scope (the supplied scope is ignored) -> not-found for A's sid.

Runs against web/server.py + the mock ASR/TTS (see scripts/run_local_smoke.sh)."""
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import websockets

PORT = os.environ.get("LA_WS_PORT", "8765")
URL = f"ws://localhost:{PORT}/"
SESSIONS_DIR = Path(__file__).resolve().parents[1] / "data" / "sessions"

A_EMAIL = "alice.smoke@example.com"
B_EMAIL = "bob.smoke@example.com"
_ops = [e.strip() for e in os.environ.get("LA_OPERATOR_EMAILS", "").split(",") if e.strip()]
OP_EMAIL = _ops[0] if _ops else "operator.smoke@example.com"


def scope_of(email: str) -> str:
    """Mirror the server: sha256(lower(email))[:16]."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def scoped_file(email: str, sid: str) -> Path:
    return SESSIONS_DIR / scope_of(email) / f"{sid}.json"


def connect(email=None):
    # Identity is now the ?email= connect query param (Cloudflare Access is gone), so
    # this smoke supplies users that way instead of the old Cf-Access header. The
    # runner sets no LA_ALLOWLIST, so there is no enforcement: the email just scopes.
    url = URL + (f"?email={quote(email)}" if email else "")
    return websockets.connect(url, max_size=16 * 1024 * 1024)


async def recv_until(ws, want, timeout=15):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if m.get("type") == want:
            return m


async def recv_any(ws, timeout=15):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def recv_delete_result(ws, timeout=15):
    """A successful delete emits session_deleted THEN a fresh sessions list (ranks
    renumber); drain the follow-up. A rejected delete emits only an error."""
    m = await recv_any(ws, timeout)
    if m.get("type") == "session_deleted":
        await recv_until(ws, "sessions", timeout)
    return m


async def persist(email, name):
    """Connect as `email`, persist the live session by renaming it, and return the
    (sid, scope, is_operator) the server reported on session_started."""
    async with connect(email) as ws:
        started = await recv_until(ws, "session_started")
        sid, scope, is_op = started.get("id"), started.get("scope"), started.get("is_operator")
        await ws.send(json.dumps({"type": "rename_session", "id": sid, "name": name}))
        r = await recv_until(ws, "session_renamed")
        assert r.get("id") == sid, f"rename echoed a different id: {r}"
    return sid, scope, is_op


async def list_ids(email):
    """Own-scope list for `email`: return (session_started, rows, ids)."""
    async with connect(email) as ws:
        started = await recv_until(ws, "session_started")
        await ws.send(json.dumps({"type": "list_sessions"}))
        s = await recv_until(ws, "sessions")
        rows = s.get("sessions", [])
        return started, rows, [r.get("id") for r in rows]


async def main() -> int:
    checks = {}

    # --- setup: persist one session for A and one for B ------------------------
    a_sid, a_scope, a_is_op = await persist(A_EMAIL, "Alice one")
    b_sid, b_scope, b_is_op = await persist(B_EMAIL, "Bob one")
    setup_ok = (a_scope == scope_of(A_EMAIL) and not a_is_op
                and b_scope == scope_of(B_EMAIL) and not b_is_op
                and a_sid != b_sid)
    checks["setup_scopes"] = setup_ok
    print(f"setup: A sid={a_sid} scope={a_scope} op={a_is_op}; "
          f"B sid={b_sid} scope={b_scope} op={b_is_op}:",
          "PASS" if setup_ok else "FAIL", flush=True)

    # --- case 1: per-client isolation -----------------------------------------
    _, a_rows, a_ids = await list_ids(A_EMAIL)
    _, b_rows, b_ids = await list_ids(B_EMAIL)
    a_no_owner = all("owner" not in r and "scope" not in r for r in a_rows)
    b_no_owner = all("owner" not in r and "scope" not in r for r in b_rows)
    case1 = (a_sid in a_ids and b_sid not in a_ids           # A sees only A's
             and b_sid in b_ids and a_sid not in b_ids       # B sees only B's
             and a_no_owner and b_no_owner)                  # client rows carry no owner/scope
    checks["case1_isolation"] = case1
    print(f"1. isolation: A has A not B ({a_sid in a_ids}/{b_sid not in a_ids}), "
          f"B has B not A ({b_sid in b_ids}/{a_sid not in b_ids}), "
          f"client rows unlabeled ({a_no_owner and b_no_owner}):",
          "PASS" if case1 else "FAIL", flush=True)

    # --- case 2: operator sees ALL, tagged by owner ----------------------------
    async with connect(OP_EMAIL) as ws:
        op_started = await recv_until(ws, "session_started")
        await ws.send(json.dumps({"type": "list_sessions"}))
        op_list = await recv_until(ws, "sessions")
    op_by_id = {r.get("id"): r for r in op_list.get("sessions", [])}
    a_row, b_row = op_by_id.get(a_sid), op_by_id.get(b_sid)
    case2 = (op_started.get("is_operator") is True
             and a_row is not None and a_row.get("owner") == A_EMAIL.lower()
             and a_row.get("scope") == scope_of(A_EMAIL)
             and b_row is not None and b_row.get("owner") == B_EMAIL.lower()
             and b_row.get("scope") == scope_of(B_EMAIL))
    checks["case2_operator_aggregate"] = case2
    print(f"2. operator: is_operator={op_started.get('is_operator')}, "
          f"A row={a_row and (a_row.get('owner'), a_row.get('scope'))}, "
          f"B row={b_row and (b_row.get('owner'), b_row.get('scope'))}:",
          "PASS" if case2 else "FAIL", flush=True)

    # --- case 3: non-operator B cannot get/rename/delete A's sid ----------------
    a_file = scoped_file(A_EMAIL, a_sid)
    before = a_file.read_text(encoding="utf-8") if a_file.is_file() else None
    async with connect(B_EMAIL) as ws:
        await recv_until(ws, "session_started")
        await ws.send(json.dumps({"type": "get_session", "id": a_sid}))
        g = await recv_any(ws)
        await ws.send(json.dumps({"type": "rename_session", "id": a_sid, "name": "HACKED"}))
        rn = await recv_any(ws)
        await ws.send(json.dumps({"type": "delete_session", "id": a_sid}))
        dl = await recv_delete_result(ws)
    after = a_file.read_text(encoding="utf-8") if a_file.is_file() else None
    get_denied = g.get("type") == "error"
    rename_denied = rn.get("type") == "error"
    delete_clean = dl.get("type") in ("error", "session_deleted")   # no oracle: either is fine
    a_unchanged = before is not None and after == before and "HACKED" not in (after or "")
    case3 = get_denied and rename_denied and delete_clean and a_unchanged
    checks["case3_cross_client_denied"] = case3
    print(f"3. B->A get={g.get('type')} rename={rn.get('type')} delete={dl.get('type')}, "
          f"A file unchanged={a_unchanged}:", "PASS" if case3 else "FAIL", flush=True)

    # --- case 4: no header -> public scope == legacy FLAT layout ----------------
    async with connect(None) as ws:
        pub_started = await recv_until(ws, "session_started")
        p_sid = pub_started.get("id")
        await ws.send(json.dumps({"type": "rename_session", "id": p_sid, "name": "Public one"}))
        await recv_until(ws, "session_renamed")
    flat_file = SESSIONS_DIR / f"{p_sid}.json"
    p_scope_ok = pub_started.get("scope") == "public" and pub_started.get("is_operator") is False
    flat_ok = flat_file.is_file()                       # written FLAT, not in a subdir
    _, pub_rows, pub_ids = await list_ids(None)
    pub_no_owner = all("owner" not in r and "scope" not in r for r in pub_rows)
    pub_isolation = p_sid in pub_ids and a_sid not in pub_ids and b_sid not in pub_ids
    case4 = p_scope_ok and flat_ok and pub_isolation and pub_no_owner
    checks["case4_public_backcompat"] = case4
    print(f"4. public: scope={pub_started.get('scope')}, flat_file={flat_ok}, "
          f"P listed & A/B absent={pub_isolation}, rows unlabeled={pub_no_owner}:",
          "PASS" if case4 else "FAIL", flush=True)

    # --- case 5: a non-operator's supplied scope is IGNORED ---------------------
    before5 = a_file.read_text(encoding="utf-8") if a_file.is_file() else None
    async with connect(B_EMAIL) as ws:
        await recv_until(ws, "session_started")
        # B supplies A's scope hash: it must be ignored, resolving in B's own scope.
        await ws.send(json.dumps({"type": "get_session", "id": a_sid, "scope": scope_of(A_EMAIL)}))
        g5 = await recv_any(ws)
        await ws.send(json.dumps({"type": "rename_session", "id": a_sid,
                                  "name": "HACKED2", "scope": scope_of(A_EMAIL)}))
        rn5 = await recv_any(ws)
    after5 = a_file.read_text(encoding="utf-8") if a_file.is_file() else None
    case5 = (g5.get("type") == "error" and rn5.get("type") == "error"
             and before5 is not None and after5 == before5 and "HACKED2" not in (after5 or ""))
    checks["case5_supplied_scope_ignored"] = case5
    print(f"5. B supplies A's scope: get={g5.get('type')} rename={rn5.get('type')}, "
          f"A file unchanged={after5 == before5}:", "PASS" if case5 else "FAIL", flush=True)

    # --- cleanup (best-effort, contract-based via the operator): remove this run's
    # three sessions so the scope dirs do not accumulate files across runs. Any
    # failure here is non-fatal and does not affect the result.
    try:
        async with connect(OP_EMAIL) as ws:
            await recv_until(ws, "session_started")
            for sid, scope in ((a_sid, scope_of(A_EMAIL)), (b_sid, scope_of(B_EMAIL)),
                               (p_sid, "public")):
                await ws.send(json.dumps({"type": "delete_session", "id": sid, "scope": scope}))
                await recv_delete_result(ws)
    except Exception as e:  # noqa: BLE001
        print(f"(cleanup best-effort skipped: {type(e).__name__})", flush=True)

    ok = all(checks.values())
    print(f"\n{checks}", flush=True)
    print("SMOKE: PASS (multi-client scope isolation + operator view)"
          if ok else "SMOKE: FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
