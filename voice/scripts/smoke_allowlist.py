"""Local WS smoke for the server-side email allowlist (security).

Starts its OWN orchestrator with LA_ALLOWLIST set on a free port, then connects via
the `?email=` query param (the frozen client contract) and asserts enforcement:
  a. an allowlisted email          -> session_started with its own scope, is_operator false
  b. a non-allowlisted email       -> auth_error{not_allowlisted} + close, NO session
  c. no email                      -> auth_error{email_required} + close, NO session
  d. an allowlisted OPERATOR email -> session_started, is_operator true
Connect handshake only, so it needs no ASR/TTS.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import websockets

APP = Path(__file__).resolve().parent.parent
PORT = os.environ.get("LA_ALLOWLIST_PORT", "8792")
USER = "alice.allow@example.com"
OP = "op.allow@example.com"
OUTSIDER = "stranger@example.com"


async def _connect_outcome(email, present=True):
    """Connect with (or without) an ?email= and return (session_started, auth_error,
    closed_bool). A rejected connection is closed by the server after auth_error."""
    url = f"ws://localhost:{PORT}/"
    if present:
        url += f"?email={quote(email)}"
    ss = ae = None
    closed = False
    try:
        async with websockets.connect(url, max_size=1 << 20) as ws:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic()))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    break
                if m.get("type") == "session_started":
                    ss = m
                    break
                if m.get("type") == "auth_error":
                    ae = m
    except websockets.ConnectionClosed:
        closed = True
    # a rejected connect may close before or as we read; detect via the auth_error
    return ss, ae, closed


async def scenario():
    results = {}
    ss, ae, _ = await _connect_outcome(USER)
    results["a"] = ss is not None and ss.get("scope") != "public" and ss.get("is_operator") is False
    print(f"a: allowlisted user -> session_started scope={ss and ss.get('scope')!r} "
          f"is_operator={ss and ss.get('is_operator')} -> {'PASS' if results['a'] else 'FAIL'}", flush=True)

    ss, ae, _ = await _connect_outcome(OUTSIDER)
    results["b"] = ss is None and ae is not None and ae.get("reason") == "not_allowlisted"
    print(f"b: non-allowlisted -> auth_error reason={ae and ae.get('reason')!r} no_session={ss is None} "
          f"-> {'PASS' if results['b'] else 'FAIL'}", flush=True)

    ss, ae, _ = await _connect_outcome(None, present=False)
    results["c"] = ss is None and ae is not None and ae.get("reason") == "email_required"
    print(f"c: no email -> auth_error reason={ae and ae.get('reason')!r} no_session={ss is None} "
          f"-> {'PASS' if results['c'] else 'FAIL'}", flush=True)

    ss, ae, _ = await _connect_outcome(OP)
    results["d"] = ss is not None and ss.get("is_operator") is True
    print(f"d: allowlisted operator -> is_operator={ss and ss.get('is_operator')} "
          f"-> {'PASS' if results['d'] else 'FAIL'}", flush=True)
    return all(results.values())


def _wait_port(port, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", int(port))) == 0:
                return True
        time.sleep(0.3)
    return False


async def main():
    env = dict(os.environ)
    env["LA_WS_PORT"] = PORT
    env["LA_ALLOWLIST"] = f"{USER},{OP}"
    env["LA_OPERATOR_EMAILS"] = OP
    env.setdefault("LA_FUNASR_URL", "http://localhost:9001/v1")
    env.setdefault("LA_TTS_MODELS",
                   "gepard-1.0=http://localhost:9002,gepard-1.0-alt=http://localhost:9003")
    env.setdefault("LA_TTS_URL", "http://localhost:9002")
    proc = subprocess.Popen([sys.executable, str(APP / "web" / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_port(PORT):
            print("ALLOWLIST-SMOKE: FAIL: orchestrator never bound the port", flush=True)
            return 1
        ok = await scenario()
        print("ALLOWLIST-SMOKE: PASS" if ok else "ALLOWLIST-SMOKE: FAIL", flush=True)
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
