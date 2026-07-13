"""Small blocking HTTP client for the Opentrons OT-2 robot server (port 31950).

Scope-limited on purpose. This is the shim that turns "initialize the robot" from
voice into a real move; it is NOT the `execute()` promised in HARDWARE_EXECUTION.md.
No workflow-to-protocol upload path here, no run-state tracking, no stop-in-flight.
When that broader work lands, this file goes away.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_HOST = os.environ.get("OT2_HOST", "192.168.108.210")
DEFAULT_PORT = int(os.environ.get("OT2_PORT", "31950"))
DEFAULT_TIMEOUT = float(os.environ.get("OT2_TIMEOUT", "10"))

_HEADERS = {"Opentrons-Version": "2"}


class OT2Error(RuntimeError):
    pass


def _url(path: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}{path}"


def _request(method: str, path: str, body: dict | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(_HEADERS)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_url(path), data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw)
    except TimeoutError as e:
        raise OT2Error(f"OT-2 did not respond within {timeout:.0f}s on {path}") from e
    except urllib.error.URLError as e:
        raise OT2Error(f"could not reach OT-2 at {DEFAULT_HOST}:{DEFAULT_PORT}: {e}") from e
    except json.JSONDecodeError as e:
        raise OT2Error(f"OT-2 returned non-JSON: {e}") from e


def home_robot() -> dict:
    """Home all axes. Physical move: gantry returns to home position.
    The OT-2 blocks the HTTP response until homing finishes (~30-60s on a cold gantry)."""
    return _request("POST", "/robot/home", {"target": "robot"}, timeout=120)


def set_lights(on: bool) -> dict:
    return _request("POST", "/robot/lights", {"on": bool(on)})


def get_lights() -> dict:
    return _request("GET", "/robot/lights")


def get_pipettes() -> dict:
    return _request("GET", "/pipettes")


def get_networking_status() -> dict:
    """Non-broken alternative to /health (which returns an ExceptionGroup on lab1)."""
    return _request("GET", "/networking/status")


def get_server_health() -> dict:
    return _request("GET", "/server/update/health")


def list_protocols() -> list[dict]:
    return _request("GET", "/protocols").get("data", [])


def latest_protocol() -> dict | None:
    """Most recently uploaded standard protocol on the robot, or None if there are none."""
    ps = [p for p in list_protocols() if p.get("protocolKind", "standard") == "standard"]
    if not ps:
        return None
    return max(ps, key=lambda p: p.get("createdAt") or "")


def create_run(protocol_id: str) -> dict:
    return _request("POST", "/runs", {"data": {"protocolId": protocol_id}})


def play_run(run_id: str) -> dict:
    return _request(
        "POST",
        f"/runs/{run_id}/actions",
        {"data": {"actionType": "play"}},
    )


def get_run(run_id: str) -> dict:
    return _request("GET", f"/runs/{run_id}").get("data", {})


def run_and_wait(protocol_id: str, poll_s: float = 2.0, max_s: float = 300.0) -> dict:
    """Create a run, play it, and block until it reaches a terminal state.
    Returns the final run record."""
    import time
    rid = create_run(protocol_id)["data"]["id"]
    play_run(rid)
    deadline = time.time() + max_s
    terminal = {"succeeded", "failed", "stopped", "stop-requested"}
    while time.time() < deadline:
        r = get_run(rid)
        if r.get("status") in terminal:
            return r
        time.sleep(poll_s)
    raise OT2Error(f"run {rid} did not finish within {max_s:.0f}s")


def health_summary() -> dict:
    """Composite status suitable for a spoken read-back."""
    server = get_server_health()
    pips = get_pipettes()
    net = get_networking_status()
    wlan = net.get("interfaces", {}).get("wlan0", {})
    return {
        "api_version": server.get("apiServerVersion"),
        "system_version": server.get("systemVersion"),
        "left_pipette": (pips.get("left") or {}).get("name"),
        "right_pipette": (pips.get("right") or {}).get("name"),
        "wifi_ip": wlan.get("ipAddress"),
        "wifi_state": wlan.get("state"),
    }
