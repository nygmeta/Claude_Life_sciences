"""Pure-logic tests for the Phase 3 announcement arbitration queue
(web/server.AnnounceManager) and the event-text helpers. No network, no event
loop: they exercise the synchronous _pick() priority logic directly."""
import os
import sys
from pathlib import Path

os.environ.setdefault("LA_ANTHROPIC_API_KEY", "placeholder")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import server  # noqa: E402


class FakeTask:
    """Stands in for an asyncio reply task: _pick only calls .done()."""
    def __init__(self, done):
        self._done = done

    def done(self):
        return self._done


def _mgr():
    """An AnnounceManager wired to a fresh (real) Session via a Conn."""
    sess = server.Session()
    conn = server.Conn(None, sess)
    return conn.announce, sess


def _ann(severity, eid):
    return {"event_id": eid, "severity": severity, "text": eid,
            "source": "test", "enqueue_perf": 0.0}


def test_alerts_jump_infos_and_fifo_within_severity():
    m, _sess = _mgr()
    m._infos.append(_ann("info", "i1"))
    m._infos.append(_ann("info", "i2"))
    m._alerts.append(_ann("alert", "a1"))
    m._alerts.append(_ann("alert", "a2"))
    order = [m._pick()["event_id"] for _ in range(4)]
    assert order == ["a1", "a2", "i1", "i2"]   # alerts first (FIFO), then infos (FIFO)
    assert m._pick() is None


def test_info_defers_under_committed_reply_but_alert_still_jumps():
    m, sess = _mgr()
    sess.reply_task = FakeTask(done=False)
    sess.reply_ctx = {"commit": {"committed": True}}
    m._infos.append(_ann("info", "i1"))
    assert m._pick() is None                    # info deferred while a committed reply runs

    m._alerts.append(_ann("alert", "a1"))
    assert m._pick()["event_id"] == "a1"        # an alert preempts and is delivered

    sess.reply_task = FakeTask(done=True)        # reply finished
    assert m._pick()["event_id"] == "i1"        # now the info is deliverable


def test_info_deliverable_during_uncommitted_speculation():
    m, sess = _mgr()
    sess.reply_task = FakeTask(done=False)
    sess.reply_ctx = {"commit": {"committed": False, "spec": True}}
    m._infos.append(_ann("info", "i1"))
    # an uncommitted speculation is invisible to the client, so it does not defer an info
    assert m._pick()["event_id"] == "i1"


def test_committed_reply_in_flight_predicate():
    m, sess = _mgr()
    assert m._committed_reply_in_flight() is False       # idle
    sess.reply_task = FakeTask(done=False)
    sess.reply_ctx = {"commit": {"committed": True}}
    assert m._committed_reply_in_flight() is True
    sess.reply_ctx = {"commit": {"committed": False, "spec": True}}
    assert m._committed_reply_in_flight() is False       # speculation does not count
    sess.reply_task = FakeTask(done=True)
    sess.reply_ctx = {"commit": {"committed": True}}
    assert m._committed_reply_in_flight() is False       # a finished task is not in flight


def test_centrifuge_announce_minutes_formatting():
    # the minutes formatting moved into lab_gate.completion_announce (via _fmt_num):
    # an integral value drops its trailing zero, a fractional one is kept verbatim.
    from web import lab_gate
    assert "for 5 minutes" in lab_gate.completion_announce(
        "start_centrifuge", {"rpm": 3000, "minutes": 5.0})
    assert "for 1 minutes" in lab_gate.completion_announce(
        "start_centrifuge", {"rpm": 3000, "minutes": 1})
    assert "for 0.02 minutes" in lab_gate.completion_announce(
        "start_centrifuge", {"rpm": 3000, "minutes": 0.02})
