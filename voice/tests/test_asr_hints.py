"""Per-scope ASR hints (hotwords + replacements).

Hints used to be a single process-global dict backed by one file, so any client
silently rewrote every other client's ASR bias AND every other client's
transcripts. They are now scoped exactly like sessions: one file per scope, an
in-process cache keyed by scope, and no cross-scope addressing at all (not even
for an operator). These tests pin the three things that make that safe: the
defaults are handed out as COPIES, the cache never leaks one scope into another,
and a client-supplied `scope` on get_hints/set_hints is ignored.
"""
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("LA_" + "ANTHROPIC_" + "API_" + "KEY", "placeholder")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import server  # noqa: E402


class DummyWS:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(json.loads(payload))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


SCOPE_A = hashlib.sha256(b"alice@example.com").hexdigest()[:16]
SCOPE_B = hashlib.sha256(b"bob@example.com").hexdigest()[:16]


def use_temp_hints(monkeypatch, tmp_path):
    """Point the hints dir at a tmp dir AND clear the module cache, so no test
    inherits another's scopes (the cache is process-global by design)."""
    hints_dir = tmp_path / "data" / "hints"
    monkeypatch.setattr(server, "HINTS_DIR", hints_dir)
    monkeypatch.setattr(server, "_HINTS_CACHE", {})
    return hints_dir


def session(scope="public", is_operator=False):
    sess = server.Session()
    sess.scope = scope
    sess.is_operator = is_operator
    return sess


# --- defaults + copy semantics ----------------------------------------------

def test_unsaved_scope_gets_the_defaults(monkeypatch, tmp_path):
    hints_dir = use_temp_hints(monkeypatch, tmp_path)
    for scope in ("public", SCOPE_A):
        hints = server.hints_for(scope)
        assert hints == {"hotwords": ["Claude"],
                         "replacements": {"cloud code": "Claude Code"}}
    # a scope that never saved is not written to disk just by being read
    assert not hints_dir.exists()


def test_defaults_are_copied_per_scope(monkeypatch, tmp_path):
    use_temp_hints(monkeypatch, tmp_path)
    a = server.hints_for(SCOPE_A)
    a["hotwords"].append("mutated")
    a["replacements"]["mutated"] = "x"

    # the module defaults are intact...
    assert server.DEFAULT_HINTS == {"hotwords": ["Claude"],
                                    "replacements": {"cloud code": "Claude Code"}}
    # ...and another scope still gets a clean copy, not A's mutated dict
    b = server.hints_for(SCOPE_B)
    assert b == {"hotwords": ["Claude"], "replacements": {"cloud code": "Claude Code"}}
    assert b is not a
    assert b["hotwords"] is not server.DEFAULT_HINTS["hotwords"]
    assert b["replacements"] is not server.DEFAULT_HINTS["replacements"]


def test_saved_empty_hints_are_not_treated_as_unset(monkeypatch, tmp_path):
    hints_dir = use_temp_hints(monkeypatch, tmp_path)
    hints_dir.mkdir(parents=True)
    (hints_dir / f"{SCOPE_A}.json").write_text(
        json.dumps({"hotwords": [], "replacements": {}}), encoding="utf-8")

    # an explicit empty list is a user CHOICE (cleared), never a fallback to defaults
    assert server.hints_for(SCOPE_A) == {"hotwords": [], "replacements": {}}


# --- storage + cache ---------------------------------------------------------

def test_save_writes_one_file_per_scope(monkeypatch, tmp_path):
    hints_dir = use_temp_hints(monkeypatch, tmp_path)
    server.save_hints(SCOPE_A, {"hotwords": ["gepard"], "replacements": {"a": "b"}})
    server.save_hints("public", {"hotwords": ["OmniVAD"], "replacements": {}})

    a = json.loads((hints_dir / f"{SCOPE_A}.json").read_text(encoding="utf-8"))
    pub = json.loads((hints_dir / "public.json").read_text(encoding="utf-8"))
    assert a["hotwords"] == ["gepard"]
    assert pub["hotwords"] == ["OmniVAD"]
    # B never saved: no file, and it still reads the defaults
    assert not (hints_dir / f"{SCOPE_B}.json").exists()
    assert server.hints_for(SCOPE_B)["hotwords"] == ["Claude"]


def test_hot_path_reads_the_cache_not_the_disk(monkeypatch, tmp_path):
    hints_dir = use_temp_hints(monkeypatch, tmp_path)
    server.save_hints(SCOPE_A, {"hotwords": ["gepard"], "replacements": {}})
    # corrupt the file behind the cache's back: transcribe() must not be re-reading
    # it per segment, so the cached value still wins.
    (hints_dir / f"{SCOPE_A}.json").write_text("{ not json", encoding="utf-8")
    assert server.hints_for(SCOPE_A)["hotwords"] == ["gepard"]


def test_set_hints_invalidates_the_cache(monkeypatch, tmp_path):
    use_temp_hints(monkeypatch, tmp_path)
    ws, sess = DummyWS(), session(SCOPE_A)
    assert server.hints_for(SCOPE_A)["hotwords"] == ["Claude"]   # cache now warm

    run(server.handle_set_hints(ws, sess, {"hotwords": ["gepard"],
                                           "replacements": {"a": "b"}}))
    assert server.hints_for(SCOPE_A) == {"hotwords": ["gepard"], "replacements": {"a": "b"}}
    assert ws.messages[-1] == {"type": "hints", "hotwords": ["gepard"],
                               "replacements": {"a": "b"}}


def test_corrupt_file_falls_back_to_defaults(monkeypatch, tmp_path):
    hints_dir = use_temp_hints(monkeypatch, tmp_path)
    hints_dir.mkdir(parents=True)
    (hints_dir / f"{SCOPE_A}.json").write_text("{ not json", encoding="utf-8")
    assert server.hints_for(SCOPE_A)["hotwords"] == ["Claude"]   # fails soft, no crash


def test_bogus_scope_never_becomes_a_path(monkeypatch, tmp_path):
    hints_dir = use_temp_hints(monkeypatch, tmp_path)
    for bogus in ("../../etc/passwd", "not-hex", "", None, "PUBLIC"):
        assert server._hints_path(bogus) is None
        assert server.hints_for(bogus)["hotwords"] == ["Claude"]   # defaults, no read
        server.save_hints(bogus, {"hotwords": ["x"], "replacements": {}})   # no write
    assert not hints_dir.exists()


# --- the WS handlers: own scope only, always ---------------------------------

def test_handlers_resolve_in_the_connections_own_scope(monkeypatch, tmp_path):
    use_temp_hints(monkeypatch, tmp_path)
    ws_a, ws_b = DummyWS(), DummyWS()

    run(server.handle_set_hints(ws_a, session(SCOPE_A),
                                {"hotwords": ["gepard"], "replacements": {"a": "b"}}))
    run(server.handle_get_hints(ws_b, session(SCOPE_B)))

    assert ws_a.messages[-1]["hotwords"] == ["gepard"]
    assert ws_b.messages[-1]["hotwords"] == ["Claude"]            # B's own, untouched
    assert ws_b.messages[-1]["replacements"] == {"cloud code": "Claude Code"}


def test_client_supplied_scope_is_ignored_even_for_an_operator(monkeypatch, tmp_path):
    use_temp_hints(monkeypatch, tmp_path)
    server.save_hints(SCOPE_A, {"hotwords": ["alice-only"], "replacements": {}})
    op = session(SCOPE_B, is_operator=True)
    ws = DummyWS()

    # hints have NO cross-scope addressing: an operator reads its OWN hints...
    run(server.handle_get_hints(ws, op))
    assert ws.messages[-1]["hotwords"] == ["Claude"]

    # ...and a `scope` field on set_hints is ignored, so the write lands in the
    # operator's own scope and A's hints (which rewrite A's transcripts) survive.
    run(server.handle_set_hints(ws, op, {"hotwords": ["operator"], "replacements": {},
                                         "scope": SCOPE_A}))
    assert ws.messages[-1]["hotwords"] == ["operator"]
    assert server.hints_for(SCOPE_B)["hotwords"] == ["operator"]
    assert server.hints_for(SCOPE_A)["hotwords"] == ["alice-only"]


def test_set_hints_keeps_unset_fields_and_honors_an_explicit_clear(monkeypatch, tmp_path):
    use_temp_hints(monkeypatch, tmp_path)
    sess, ws = session(SCOPE_A), DummyWS()
    run(server.handle_set_hints(ws, sess, {"hotwords": ["gepard"],
                                           "replacements": {"a": "b"}}))

    run(server.handle_set_hints(ws, sess, {"hotwords": ["gecko"]}))  # replacements unset
    assert server.hints_for(SCOPE_A) == {"hotwords": ["gecko"], "replacements": {"a": "b"}}

    run(server.handle_set_hints(ws, sess, {"hotwords": []}))         # explicit clear
    assert server.hints_for(SCOPE_A) == {"hotwords": [], "replacements": {"a": "b"}}


# --- the ASR path ------------------------------------------------------------

def test_replacements_come_from_the_passed_hints(monkeypatch, tmp_path):
    use_temp_hints(monkeypatch, tmp_path)
    hints = {"hotwords": [], "replacements": {"cloud code": "Claude Code"}}
    assert server.apply_replacements("  i use cloud code daily ", hints) == "i use Claude Code daily"
    # another scope's map does not apply
    assert server.apply_replacements("i use cloud code daily",
                                     {"hotwords": [], "replacements": {}}) == "i use cloud code daily"


def test_transcribe_sends_the_scopes_hotwords_as_the_prompt(monkeypatch, tmp_path):
    use_temp_hints(monkeypatch, tmp_path)
    calls = []

    class _Resp:
        text = "hello"
        confidence = None

    class _Transcriptions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return _Resp()

    class _FakeASR:
        audio = type("audio", (), {"transcriptions": _Transcriptions()})()

    monkeypatch.setattr(server, "asr_client", _FakeASR())
    server.save_hints(SCOPE_A, {"hotwords": ["gepard", "OmniVAD"], "replacements": {}})

    run(server.transcribe(b"\x00\x00" * 16, "en", server.hints_for(SCOPE_A)))
    assert calls[-1]["prompt"] == "gepard,OmniVAD"          # A's hotwords bias A's audio

    run(server.transcribe(b"\x00\x00" * 16, "en", server.hints_for(SCOPE_B)))
    assert calls[-1]["prompt"] == "Claude"                  # B's own (default) hotwords

    run(server.transcribe(b"\x00\x00" * 16, "en", {"hotwords": [], "replacements": {}}))
    assert calls[-1]["prompt"] is None                      # cleared -> no prompt at all
