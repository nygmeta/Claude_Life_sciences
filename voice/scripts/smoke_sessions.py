"""Local check for the session-history WS surface: list/get/rename/delete/new
session messages, including the malicious-id regression checks for the
path-traversal fix (get_session/rename_session/delete_session must reject any
id that isn't the server's own uuid4().hex[:8] shape before it ever touches a
filesystem path). Runs against web/server.py + the mock ASR/TTS and makes zero
LLM calls (no end_turn/audio path needed), so it belongs in the local
mock-only smoke gate without credentials."""
import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

PORT = os.environ.get("LA_WS_PORT", "8765")
URL = f"ws://localhost:{PORT}/"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HINTS_FILE = DATA_DIR / "asr_hints.json"

# Five ids with a shape that must never resolve to a real file (never even
# reach the filesystem with the fix in place); a sixth, well-formed-but-almost
# certainly-nonexistent id is checked separately below, since it exercises the
# "not found" path rather than the "invalid id" path.
MALICIOUS_IDS = [
    "../asr_hints",
    "../../credentials/anthropic_key",
    "",
    "not-8-hex-chars",
    "deadbeef00",   # 10 hex chars: right alphabet, wrong length
]
WELLFORMED_MISSING_ID = "00000000"   # 8 hex chars, essentially certain not to exist


async def recv_until(ws, want, timeout=15):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if m.get("type") == want:
            return m


async def recv_any(ws, timeout=15):
    """Receive exactly one message with no type filter. Used for the
    malicious-id checks, where the whole point is confirming we do NOT get
    the success type a malicious request asked for; recv_until would just
    hang until `timeout` on that bug instead of failing fast and reporting
    what type actually came back."""
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def recv_delete_result(ws, timeout=15):
    """Read a delete_session outcome. A SUCCESSFUL delete now emits
    session_deleted FOLLOWED BY a fresh sessions list (ranks are positional and
    renumber on delete), so drain that trailing sessions message to keep the
    stream aligned for the next check. A rejected delete emits only an error,
    with no follow-up."""
    m = await recv_any(ws, timeout)
    if m.get("type") == "session_deleted":
        await recv_until(ws, "sessions", timeout)   # drain the follow-up list
    return m


async def main() -> int:
    checks = {}
    async with websockets.connect(URL, max_size=16 * 1024 * 1024) as ws:
        # 1. connect -> session_started
        first = await recv_until(ws, "session_started")
        first_id, first_number = first.get("id"), first.get("number")
        checks["1_connect_session_started"] = bool(first_id) and isinstance(first_number, int)
        print("1. connect -> session_started:", first_id, first_number,
              "PASS" if checks["1_connect_session_started"] else "FAIL", flush=True)

        # 2. list_sessions includes the live session
        await ws.send(json.dumps({"type": "list_sessions"}))
        sessions = await recv_until(ws, "sessions")
        ids = [s.get("id") for s in sessions.get("sessions", [])]
        checks["2_list_sessions"] = first_id in ids
        print("2. list_sessions includes live id:", "PASS" if checks["2_list_sessions"] else "FAIL", flush=True)

        # 3. rename_session on the live session (in-memory + save_session path)
        await ws.send(json.dumps({"type": "rename_session", "id": first_id, "name": "Renamed Live"}))
        r = await recv_until(ws, "session_renamed")
        checks["3_rename_live"] = r.get("id") == first_id and r.get("name") == "Renamed Live"
        print("3. rename live session:", "PASS" if checks["3_rename_live"] else "FAIL", flush=True)

        # 4. new_session -> a second, different, one-higher-numbered session_started
        await ws.send(json.dumps({"type": "new_session"}))
        second = await recv_until(ws, "session_started")
        second_id, second_number = second.get("id"), second.get("number")
        checks["4_new_session"] = (second_id != first_id and isinstance(second_number, int)
                                    and second_number == first_number + 1)
        print("4. new_session:", second_id, second_number,
              "PASS" if checks["4_new_session"] else "FAIL", flush=True)

        old_id = first_id     # now a past, non-live session from this connection's view
        live_id = second_id   # the connection's current live session going forward

        # 5. get_session on the now-past first session
        await ws.send(json.dumps({"type": "get_session", "id": old_id}))
        g = await recv_until(ws, "session_data")
        checks["5_get_past_session"] = g.get("id") == old_id
        print("5. get_session(old_id):", "PASS" if checks["5_get_past_session"] else "FAIL", flush=True)

        # 6. rename_session on the same past session (the file-patch path)
        await ws.send(json.dumps({"type": "rename_session", "id": old_id, "name": "Renamed Past"}))
        rp = await recv_until(ws, "session_renamed")
        checks["6_rename_past"] = rp.get("id") == old_id and rp.get("name") == "Renamed Past"
        print("6. rename_session(old_id) file-patch path:",
              "PASS" if checks["6_rename_past"] else "FAIL", flush=True)

        # 7. malicious ids: the actual path-traversal regression. None of these
        # must ever produce session_data / session_renamed / session_deleted.
        mal_ok = True
        for mid in MALICIOUS_IDS:
            for mtype, extra in (("get_session", {}), ("rename_session", {"name": "x"}),
                                  ("delete_session", {})):
                await ws.send(json.dumps({"type": mtype, "id": mid, **extra}))
                reply = await recv_any(ws)
                is_error = reply.get("type") == "error"
                mal_ok = mal_ok and is_error
                print(f"7. {mtype}(id={mid!r}) -> {reply.get('type')}:",
                      "PASS" if is_error else "FAIL", flush=True)
            if mid == "../asr_hints":
                if HINTS_FILE.is_file():
                    try:
                        parsed = json.loads(HINTS_FILE.read_text(encoding="utf-8"))
                        intact = isinstance(parsed, dict) and "hotwords" in parsed and "replacements" in parsed
                    except Exception:
                        intact = False
                    checks["7_asr_hints_intact"] = intact
                    print("7. data/asr_hints.json still intact after ../asr_hints delete attempt:",
                          "PASS" if intact else "FAIL", flush=True)
                else:
                    print("7. data/asr_hints.json absent in this environment, skipping intact check", flush=True)
        checks["7_malicious_ids"] = mal_ok

        # 7b. well-formed but (essentially certainly) nonexistent id: get/rename
        # must still error, via the "not found" path rather than "invalid id".
        # delete is idempotent by pre-existing, unrelated-to-this-fix design
        # (unlink's FileNotFoundError is treated as "already gone" success in
        # handle_delete_session), so accept either outcome there as long as
        # the reply is a clean one, not a crash or a hang.
        await ws.send(json.dumps({"type": "get_session", "id": WELLFORMED_MISSING_ID}))
        g2 = await recv_any(ws)
        wf_get_ok = g2.get("type") == "error"
        print("7b. get_session(00000000) ->", g2.get("type"), "PASS" if wf_get_ok else "FAIL", flush=True)

        await ws.send(json.dumps({"type": "rename_session", "id": WELLFORMED_MISSING_ID, "name": "x"}))
        r2 = await recv_any(ws)
        wf_rename_ok = r2.get("type") == "error"
        print("7b. rename_session(00000000) ->", r2.get("type"), "PASS" if wf_rename_ok else "FAIL", flush=True)

        await ws.send(json.dumps({"type": "delete_session", "id": WELLFORMED_MISSING_ID}))
        d2 = await recv_delete_result(ws)
        wf_delete_ok = d2.get("type") in ("error", "session_deleted")
        print("7b. delete_session(00000000) ->", d2.get("type"),
              "(idempotent-delete is pre-existing design, distinct from the invalid-id fix)",
              "PASS" if wf_delete_ok else "FAIL", flush=True)
        checks["7b_wellformed_missing"] = wf_get_ok and wf_rename_ok and wf_delete_ok

        # 8. the live session can never be deleted
        await ws.send(json.dumps({"type": "delete_session", "id": live_id}))
        d = await recv_delete_result(ws)
        checks["8_delete_live_rejected"] = d.get("type") == "error"
        print("8. delete_session(live_id) rejected:", "PASS" if checks["8_delete_live_rejected"] else "FAIL", flush=True)

        # 9. delete the genuinely past session
        await ws.send(json.dumps({"type": "delete_session", "id": old_id}))
        del1 = await recv_delete_result(ws)
        checks["9_delete_past"] = del1.get("id") == old_id
        print("9. delete_session(old_id):", "PASS" if checks["9_delete_past"] else "FAIL", flush=True)

        # 10. deleting it again must not crash or hang
        await ws.send(json.dumps({"type": "delete_session", "id": old_id}))
        del2 = await recv_delete_result(ws)
        checks["10_delete_again_graceful"] = del2.get("type") in ("error", "session_deleted")
        print("10. delete_session(old_id) again (already gone):", del2.get("type"),
              "PASS" if checks["10_delete_again_graceful"] else "FAIL", flush=True)

    ok = all(checks.values())
    print("SESSIONS:", "PASS" if ok else "FAIL", checks, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
