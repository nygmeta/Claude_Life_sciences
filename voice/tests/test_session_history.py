import asyncio
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


# Fake Claude client for autoname tests: no network. `text` is the reply the
# fake returns; `exc` (if set) is raised from create() to exercise the fail-soft
# path. `messages.calls` records every call so a test can assert a guard skipped
# the API entirely.
class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text=None, exc=None):
        self.text = text
        self.exc = exc
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return _FakeResp(self.text)


class FakeLLM:
    def __init__(self, text=None, exc=None):
        self.messages = _FakeMessages(text, exc)


def run(coro):
    return asyncio.run(coro)


def use_temp_sessions(monkeypatch, tmp_path):
    session_dir = tmp_path / "data" / "sessions"
    monkeypatch.setattr(server, "SESSIONS_DIR", session_dir)
    return session_dir


def read_session_file(session_dir, sid):
    return json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))


def _dated(session, iso):
    """Pin a session's started_at BEFORE its first add(), so the file records a
    deterministic timestamp and positional ranks are reproducible."""
    session.started_at = iso
    return session


def test_session_persists_lazily_on_first_message(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    sess = server.Session()
    # a zero-message session never reaches disk (lazy persistence)
    assert not (session_dir / f"{sess.sid}.json").exists()
    assert sess.name is None            # never-named sentinel

    sess.add("user", "first message")   # first message creates the file
    assert (session_dir / f"{sess.sid}.json").exists()

    data = read_session_file(session_dir, sess.sid)
    assert "number" not in data         # rank is derived at read time, not stored
    assert data["name"] is None
    assert data["messages"] == [{"role": "user", "content": "first message"}]


def test_list_includes_live_session_without_file(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    ws = DummyWS()
    live = server.Session()             # no messages -> no file
    assert not (session_dir / f"{live.sid}.json").exists()

    run(server.handle_list_sessions(ws, live))
    listed = ws.messages[-1]
    assert listed["type"] == "sessions"
    row = next(s for s in listed["sessions"] if s["id"] == live.sid)
    assert row["name"] is None
    assert row["number"] == 1           # the only session -> rank 1


def test_ranks_are_positional_and_renumber_on_delete(monkeypatch, tmp_path):
    use_temp_sessions(monkeypatch, tmp_path)
    ws = DummyWS()
    s1 = _dated(server.Session(), "2026-07-10T10:00:00.000001+08:00")
    s1.add("user", "one")
    s2 = _dated(server.Session(), "2026-07-10T10:00:00.000002+08:00")
    s2.add("user", "two")
    s3 = _dated(server.Session(), "2026-07-10T10:00:00.000003+08:00")
    s3.add("user", "three")

    # Every handler now carries the connection's own (scope-bearing) live session;
    # s3 stands in as that caller. It is already persisted, so it is not re-merged
    # and the positional ranks are unperturbed.
    run(server.handle_list_sessions(ws, s3))
    listed = ws.messages[-1]["sessions"]
    # newest-first, ranks 1..N in creation order
    assert [s["id"] for s in listed] == [s3.sid, s2.sid, s1.sid]
    assert [s["number"] for s in listed] == [3, 2, 1]

    # delete rank 1 (oldest, s1): the survivors renumber, old rank 2 -> rank 1.
    # A successful delete emits session_deleted THEN a fresh sessions list.
    run(server.handle_delete_session(ws, s3, {"id": s1.sid}))
    assert ws.messages[-2] == {"type": "session_deleted", "id": s1.sid}
    assert ws.messages[-1]["type"] == "sessions"
    after = ws.messages[-1]["sessions"]
    assert [s["id"] for s in after] == [s3.sid, s2.sid]
    assert [s["number"] for s in after] == [2, 1]


def test_session_data_rank_agrees_with_sessions(monkeypatch, tmp_path):
    use_temp_sessions(monkeypatch, tmp_path)
    ws = DummyWS()
    s1 = _dated(server.Session(), "2026-07-10T11:00:00.000001+08:00")
    s1.add("user", "a")
    s2 = _dated(server.Session(), "2026-07-10T11:00:00.000002+08:00")
    s2.add("user", "b")

    # both the list and each banner derive their rank from the SAME sess view
    # (here s2, this caller's own live session; already persisted, so it is not
    # re-merged and does not perturb the ranks).
    run(server.handle_list_sessions(ws, s2))
    ranks = {s["id"]: s["number"] for s in ws.messages[-1]["sessions"]}

    for sid in (s1.sid, s2.sid):
        run(server.handle_get_session(ws, s2, {"id": sid}))
        data = ws.messages[-1]
        assert data["type"] == "session_data"
        assert data["number"] == ranks[sid]   # session_data rank == picker rank


def test_session_data_rank_agrees_with_sessions_two_clients(monkeypatch, tmp_path):
    """Regression for the two-client rank split: a message-less live session (no
    file) can be OLDER than another client's newer, file-backed session, so it
    sits in the MIDDLE of the ordering, not at the end. Both the picker list and
    the get_session banner must derive rank from the same live-session view, or
    the newer other-client session gets a different '#' in each. Fails if
    handle_get_session ranks without merging this connection's live session."""
    use_temp_sessions(monkeypatch, tmp_path)
    ws = DummyWS()
    # S1: an older, file-backed session (has a message, so it persists).
    s1 = _dated(server.Session(), "2026-07-10T12:00:00.000001+08:00")
    s1.add("user", "oldest")
    # LA: THIS client's live session. No messages -> lazy persistence, no file.
    la = _dated(server.Session(), "2026-07-10T12:00:00.000002+08:00")
    # LB: another client connects later and speaks; its file is NEWER than LA.
    lb = _dated(server.Session(), "2026-07-10T12:00:00.000003+08:00")
    lb.add("user", "from the second client")

    run(server.handle_list_sessions(ws, la))
    ranks = {s["id"]: s["number"] for s in ws.messages[-1]["sessions"]}
    # LA (no file) is merged in and sits BETWEEN s1 and lb, pushing lb to rank 3.
    assert ranks[s1.sid] == 1 and ranks[la.sid] == 2 and ranks[lb.sid] == 3

    # every file-backed session's banner rank must match the picker rank under the
    # same la view. lb is the one that split before the fix (banner 2 vs list 3).
    for sid in (s1.sid, lb.sid):
        run(server.handle_get_session(ws, la, {"id": sid}))
        data = ws.messages[-1]
        assert data["type"] == "session_data"
        assert data["number"] == ranks[sid]


def test_session_handlers_happy_path(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    ws = DummyWS()
    live = _dated(server.Session(), "2026-07-10T09:00:00.000001+08:00")
    live.add("user", "hello")
    past = _dated(server.Session(), "2026-07-10T09:00:00.000002+08:00")
    past.add("assistant", "saved reply")

    run(server.handle_list_sessions(ws, live))
    listed = ws.messages[-1]
    assert listed["type"] == "sessions"
    # newest-first: past (created second, rank 2) then live (rank 1)
    assert [s["id"] for s in listed["sessions"]] == [past.sid, live.sid]
    assert [s["number"] for s in listed["sessions"]] == [2, 1]

    run(server.handle_get_session(ws, live, {"id": past.sid}))
    data = ws.messages[-1]
    assert data["type"] == "session_data"
    assert data["id"] == past.sid
    assert data["number"] == 2                 # derived rank, agrees with the list
    assert data["messages"] == [{"role": "assistant", "content": "saved reply"}]

    run(server.handle_rename_session(ws, live, {"id": past.sid, "name": "Past Demo"}))
    renamed = ws.messages[-1]
    assert renamed == {"type": "session_renamed", "id": past.sid, "name": "Past Demo"}
    assert read_session_file(session_dir, past.sid)["name"] == "Past Demo"

    # delete emits session_deleted THEN a fresh (now live-only) sessions list
    run(server.handle_delete_session(ws, live, {"id": past.sid}))
    assert ws.messages[-2] == {"type": "session_deleted", "id": past.sid}
    assert ws.messages[-1]["type"] == "sessions"
    assert [s["id"] for s in ws.messages[-1]["sessions"]] == [live.sid]
    assert [s["number"] for s in ws.messages[-1]["sessions"]] == [1]
    assert not (session_dir / f"{past.sid}.json").exists()

    # deleting again is idempotent success (file already gone)
    run(server.handle_delete_session(ws, live, {"id": past.sid}))
    assert ws.messages[-2] == {"type": "session_deleted", "id": past.sid}
    assert ws.messages[-1]["type"] == "sessions"


def test_list_and_get_use_filename_id_not_json_body(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    session_dir.mkdir(parents=True)
    path = session_dir / "abcdef12.json"
    path.write_text(json.dumps({
        "id": "../asr_hints",         # a hostile body id that must be ignored
        "number": 7,                  # a legacy stored number that must be ignored
        "name": "Corrupt Body",
        "started_at": "2026-07-09T18:00:00+08:00",
        "messages": [],
    }), encoding="utf-8")

    ws = DummyWS()
    live = server.Session()           # merged into the list; needed for the sess arg
    run(server.handle_list_sessions(ws, live))
    ids = [s["id"] for s in ws.messages[-1]["sessions"]]
    assert "abcdef12" in ids          # the FILENAME stem, not the body's fake id
    assert "../asr_hints" not in ids

    run(server.handle_get_session(ws, live, {"id": "abcdef12"}))
    assert ws.messages[-1]["type"] == "session_data"
    assert ws.messages[-1]["id"] == "abcdef12"


def test_legacy_session_name_treated_as_unnamed(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    session_dir.mkdir(parents=True)
    path = session_dir / "abcdef12.json"
    path.write_text(json.dumps({
        "id": "abcdef12", "number": 7, "name": "Session 7",
        "started_at": "2026-07-09T18:00:00.000000+08:00",
        "messages": [{"role": "user", "content": "hi"}],
    }), encoding="utf-8")

    # direct predicate
    assert server._is_legacy_auto_name("Session 7")
    assert not server._is_legacy_auto_name("Coffee Chat")
    assert not server._is_legacy_auto_name(None)

    ws = DummyWS()
    caller = server.Session()         # the connection's own (public-scope) live session
    run(server.handle_list_sessions(ws, caller))
    row = next(s for s in ws.messages[-1]["sessions"] if s["id"] == "abcdef12")
    assert row["name"] is None        # "Session 7" reads as never-named

    run(server.handle_get_session(ws, caller, {"id": "abcdef12"}))
    assert ws.messages[-1]["name"] is None


def test_nonconforming_ids_do_not_touch_sibling_json(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    data_dir = session_dir.parent
    data_dir.mkdir(parents=True)
    sibling = data_dir / "asr_hints.json"
    original = json.dumps({"hotwords": ["keep"], "replacements": {}})
    sibling.write_text(original, encoding="utf-8")
    live = server.Session()
    ws = DummyWS()

    # a live session is passed (real dispatch always has one), but the traversal
    # id must be rejected by the id-shape guard BEFORE any filesystem access or
    # any _ranked_sessions read, so the sibling stays untouched.
    run(server.handle_get_session(ws, live, {"id": "../asr_hints"}))
    run(server.handle_rename_session(ws, live, {"id": "../asr_hints", "name": "owned"}))
    run(server.handle_delete_session(ws, live, {"id": "../asr_hints"}))

    # every malicious op is rejected with an error, never a success message, so
    # no session_deleted/sessions is emitted for the traversal attempt.
    assert [m["type"] for m in ws.messages[-3:]] == ["error", "error", "error"]
    assert sibling.exists()
    assert sibling.read_text(encoding="utf-8") == original


def test_rejects_empty_rename_live_delete_and_missing_sessions(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    ws = DummyWS()
    live = server.Session()
    old_name = live.name              # None: never named

    run(server.handle_rename_session(ws, live, {"id": live.sid, "name": "  "}))
    assert ws.messages[-1]["type"] == "error"
    assert live.name == old_name

    # deleting the live session is rejected by a server-side invariant. With lazy
    # persistence a message-less live session has NO file, so assert the rejection
    # via the error AND that no file was conjured for it (the previous version
    # asserted the file existed, which no longer holds).
    run(server.handle_delete_session(ws, live, {"id": live.sid}))
    assert ws.messages[-1]["type"] == "error"
    assert not (session_dir / f"{live.sid}.json").exists()

    run(server.handle_get_session(ws, live, {"id": "aaaaaaaa"}))
    assert ws.messages[-1]["type"] == "error"

    run(server.handle_rename_session(ws, live, {"id": "aaaaaaaa", "name": "Missing"}))
    assert ws.messages[-1]["type"] == "error"

    # a well-formed but nonexistent id: delete is idempotent success, so it emits
    # session_deleted then a fresh sessions list.
    run(server.handle_delete_session(ws, live, {"id": "aaaaaaaa"}))
    assert ws.messages[-2] == {"type": "session_deleted", "id": "aaaaaaaa"}
    assert ws.messages[-1]["type"] == "sessions"


def test_live_rename_save_failure_rolls_back_and_reports_error(monkeypatch, tmp_path):
    use_temp_sessions(monkeypatch, tmp_path)
    ws = DummyWS()
    live = server.Session()
    old_name = live.name             # None

    monkeypatch.setattr(server, "save_session", lambda _sess: False)

    run(server.handle_rename_session(ws, live, {"id": live.sid, "name": "New Name"}))

    assert ws.messages[-1]["type"] == "error"
    assert live.name == old_name


def test_new_session_preserving_keeps_tts_config_and_resets_transcript(monkeypatch, tmp_path):
    use_temp_sessions(monkeypatch, tmp_path)
    prev = server.Session()
    prev.add("user", "hello")
    prev.add("assistant", "hi there")
    prev.tts_model = "some-other-model"
    prev.tts_params = {"voice": "narrator", "temperature": 0.7,
                       "cfg_scale": 2.0, "top_k": 30, "max_frames": 1500}

    nxt = server.new_session_preserving(prev)

    # a genuinely new session: distinct id, never-named, empty transcript. (number
    # is no longer a Session field; it is a derived positional rank now.)
    assert nxt.sid != prev.sid
    assert nxt.name is None
    assert nxt.history == []
    assert nxt.messages == []

    # TTS preference carried over by value
    assert nxt.tts_model == "some-other-model"
    assert nxt.tts_params == prev.tts_params

    # ...but it is a copy, not an alias: mutating the new dict must not touch the old
    nxt.tts_params["voice"] = "different"
    assert prev.tts_params["voice"] == "narrator"


def test_past_rename_write_failure_reports_error(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    ws = DummyWS()
    live = server.Session()
    past = server.Session()
    past.add("assistant", "saved reply")   # lazy persistence: a message creates the file
    past_path = session_dir / f"{past.sid}.json"
    path_type = type(past_path)
    original_write_text = path_type.write_text

    def fail_past_write(path, *args, **kwargs):
        if path == past_path:
            raise OSError("write failed")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "write_text", fail_past_write)

    run(server.handle_rename_session(ws, live, {"id": past.sid, "name": "New Past Name"}))

    assert ws.messages[-1]["type"] == "error"
    assert read_session_file(session_dir, past.sid)["name"] == past.name   # unchanged (None)


def test_autoname_titles_unnamed_session(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "llm_client", FakeLLM(text="Coffee Tasting Plans"))
    sess = server.Session()
    sess.add("user", "let's plan a coffee tasting")
    sess.add("assistant", "sure, here are a few ideas")

    run(server.autoname_session(sess))

    assert sess.name == "Coffee Tasting Plans"
    assert read_session_file(session_dir, sess.sid)["name"] == "Coffee Tasting Plans"


def test_autoname_skips_when_already_named(monkeypatch, tmp_path):
    use_temp_sessions(monkeypatch, tmp_path)
    fake = FakeLLM(text="Should Not Be Used")
    monkeypatch.setattr(server, "llm_client", fake)
    sess = server.Session()
    sess.add("user", "hi")
    sess.name = "User Chosen Title"

    run(server.autoname_session(sess))

    assert sess.name == "User Chosen Title"
    assert fake.messages.calls == []        # guard skipped the API entirely


def test_autoname_skips_when_no_messages(monkeypatch, tmp_path):
    use_temp_sessions(monkeypatch, tmp_path)
    fake = FakeLLM(text="Nope")
    monkeypatch.setattr(server, "llm_client", fake)
    sess = server.Session()                 # no messages

    run(server.autoname_session(sess))

    assert sess.name is None
    assert fake.messages.calls == []


def test_autoname_skips_when_no_client(monkeypatch, tmp_path):
    use_temp_sessions(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "llm_client", None)
    sess = server.Session()
    sess.add("user", "hi there")

    run(server.autoname_session(sess))

    assert sess.name is None


def test_autoname_stays_unnamed_when_api_raises(monkeypatch, tmp_path):
    session_dir = use_temp_sessions(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "llm_client", FakeLLM(exc=RuntimeError("boom")))
    sess = server.Session()
    sess.add("user", "hi there")

    run(server.autoname_session(sess))      # fail-soft: must not raise

    assert sess.name is None
    assert read_session_file(session_dir, sess.sid)["name"] is None


def test_autoname_sanitizes_model_output(monkeypatch, tmp_path):
    use_temp_sessions(monkeypatch, tmp_path)
    raw = ('"Weekend Coffee\x00 Roasting\nPlans For The Whole Team '
           'And Extended Family Members Everywhere"')
    monkeypatch.setattr(server, "llm_client", FakeLLM(text=raw))
    sess = server.Session()
    sess.add("user", "let's roast some beans this weekend")

    run(server.autoname_session(sess))

    name = sess.name
    assert name is not None
    assert "\n" not in name                 # newline collapsed
    assert "\x00" not in name               # control char dropped
    assert not name.startswith('"')         # surrounding quotes stripped
    assert not name.endswith('"')
    assert "  " not in name                 # no double spaces left behind
    assert len(name) <= 60                  # capped
    assert name.startswith("Weekend Coffee Roasting Plans")
