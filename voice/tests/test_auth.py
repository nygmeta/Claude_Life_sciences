"""Unit tests for the server-side email allowlist + per-user scoping (replaces
Cloudflare Access). Identity comes from the `?email=` connect query param.

Covered: the query parse; that a non-empty LA_ALLOWLIST rejects a missing or
non-listed email at the WS handshake (one auth_error + close 4001, no session) and
admits an allowlisted email with its own scope; that an empty allowlist enforces
nothing (an email scopes, no email is public); and that an allowlisted operator
still resolves is_operator. No network.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("LA_" + "ANTHROPIC_" + "API_" + "KEY", "placeholder")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import server  # noqa: E402

OP = "boss@example.com"
USER = "someone@example.com"
OUTSIDER = "stranger@example.com"


class _Req:
    def __init__(self, path):
        self.path = path


class _WS:
    """A WS stub: carries the connect path (with query), records sent messages and
    the close code, and is an empty async iterator so handler()'s receive loop exits
    immediately on the accepted path."""
    def __init__(self, path):
        self.request = _Req(path)
        self.remote_address = ("127.0.0.1", 0)
        self.sent = []
        self.closed = None

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _path(email=None):
    if email is None:
        return "/"
    from urllib.parse import quote
    return f"/?email={quote(email)}"


def _sent_types(ws):
    return [m.get("type") for m in ws.sent]


def _first(ws, t):
    return next((m for m in ws.sent if m.get("type") == t), None)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "lat.jsonl")
    monkeypatch.setattr(server, "OPERATOR_EMAILS", frozenset({OP}))


def _run_handler(ws):
    asyncio.run(server.handler(ws))


# ------------------------------------------------------------------ the query parse
def test_read_client_email_parses_and_normalizes():
    assert server._read_client_email(_WS("/?email=Someone%40Example.com")) == USER
    assert server._read_client_email(_WS("/?email=%20Boss%40Example.com%20")) == OP
    assert server._read_client_email(_WS("/")) is None
    assert server._read_client_email(_WS("/?email=")) is None
    assert server._read_client_email(_WS("/?other=x")) is None


# ---------------------------------------------------------- allowlist enforcement on
def test_allowlist_rejects_missing_email(monkeypatch):
    monkeypatch.setattr(server, "ALLOWLIST", frozenset({USER, OP}))
    ws = _WS(_path(None))
    _run_handler(ws)
    ae = _first(ws, "auth_error")
    assert ae is not None and ae["reason"] == "email_required"
    assert ws.closed is not None and ws.closed[0] == 4001
    assert "session_started" not in _sent_types(ws)   # no session created


def test_allowlist_rejects_non_listed_email(monkeypatch):
    monkeypatch.setattr(server, "ALLOWLIST", frozenset({USER, OP}))
    ws = _WS(_path(OUTSIDER))
    _run_handler(ws)
    ae = _first(ws, "auth_error")
    assert ae is not None and ae["reason"] == "not_allowlisted"
    assert ws.closed is not None and ws.closed[0] == 4001
    assert "session_started" not in _sent_types(ws)


def test_allowlist_admits_listed_email_with_its_own_scope(monkeypatch):
    monkeypatch.setattr(server, "ALLOWLIST", frozenset({USER, OP}))
    ws = _WS(_path(USER))
    _run_handler(ws)
    assert ws.closed is None                          # not rejected
    ss = _first(ws, "session_started")
    assert ss is not None
    assert ss["scope"] != "public" and ss["is_operator"] is False


def test_allowlisted_operator_still_gets_operator(monkeypatch):
    monkeypatch.setattr(server, "ALLOWLIST", frozenset({USER, OP}))
    ws = _WS(_path(OP))
    _run_handler(ws)
    ss = _first(ws, "session_started")
    assert ss is not None and ss["is_operator"] is True and ss["scope"] != "public"


# --------------------------------------------------------- allowlist empty (default)
def test_empty_allowlist_no_email_is_public(monkeypatch):
    monkeypatch.setattr(server, "ALLOWLIST", frozenset())
    ws = _WS(_path(None))
    _run_handler(ws)
    assert ws.closed is None
    ss = _first(ws, "session_started")
    assert ss is not None and ss["scope"] == "public" and ss["is_operator"] is False


def test_empty_allowlist_email_still_scopes(monkeypatch):
    monkeypatch.setattr(server, "ALLOWLIST", frozenset())
    ws = _WS(_path(USER))
    _run_handler(ws)
    ss = _first(ws, "session_started")
    assert ss is not None and ss["scope"] != "public"


def test_empty_allowlist_operator_email_resolves_operator(monkeypatch):
    monkeypatch.setattr(server, "ALLOWLIST", frozenset())
    ws = _WS(_path(OP))
    _run_handler(ws)
    ss = _first(ws, "session_started")
    assert ss is not None and ss["is_operator"] is True
