"""
Tests for the confirmation gate in app/main.py::_handle_confirmation.

This is the only gate between "awaiting_confirmation" and actually running a
plan (adapter.simulate), so a substring match here is a safety defect, not a
style nit: "yes" is a substring of "yesterday", "go" is a substring of "going",
and "no" is a substring of neither "now" nor "not" but the words are easy to
mix up by eye. Every case below pins one such phrase to its correct outcome.
"""
from app.main import message
from app.models.session import MessageRequest, SessionState


def _new_awaiting_session() -> str:
    """Drive a fresh session up to awaiting_confirmation and return its id."""
    r1 = message(MessageRequest(transcript="Run an ELISA on today's plasma samples"))
    assert r1.state == SessionState.gathering
    r2 = message(MessageRequest(
        transcript="IL-6, 24 samples, 100 microliters per well",
        session_id=r1.session_id,
    ))
    assert r2.state == SessionState.awaiting_confirmation
    return r1.session_id


def _reply(transcript: str):
    sid = _new_awaiting_session()
    return message(MessageRequest(transcript=transcript, session_id=sid))


# --------------------------------------------------------------- must NOT run
def test_yesterday_does_not_execute():
    r = _reply("yesterday's samples")
    assert r.state != SessionState.executed


def test_yesterday_bare_does_not_execute():
    r = _reply("yesterday")
    assert r.state != SessionState.executed


def test_going_does_not_execute():
    r = _reply("I'm going to check the fridge first")
    assert r.state != SessionState.executed


def test_go_get_the_plate_does_not_execute():
    r = _reply("go get the plate")
    assert r.state != SessionState.executed


def test_thats_not_correct_does_not_execute():
    r = _reply("that's not correct")
    assert r.state != SessionState.executed
    assert r.state != SessionState.idle  # and it must not read as a cancel either


# ------------------------------------------- affirmative phrase + NEW CONTENT: must NOT run
# The dangerous class: the utterance really does contain an affirmative phrase, so word
# boundaries alone do not save us. A confirmation that carries new content is a new
# instruction, not a sign-off.
def test_run_it_on_yesterdays_samples_does_not_execute():
    r = _reply("run it on yesterday's samples")
    assert r.state != SessionState.executed


def test_yes_with_a_correction_does_not_execute():
    r = _reply("yes, use 100 microliters per well")
    assert r.state != SessionState.executed


def test_go_ahead_with_new_content_does_not_execute():
    r = _reply("go ahead and prep the fridge samples")
    assert r.state != SessionState.executed


def test_do_it_with_new_content_does_not_execute():
    r = _reply("do it with the plasma from batch seven")
    assert r.state != SessionState.executed


# --------------------------------------------------------------- must NOT cancel
def test_now_does_not_cancel():
    r = _reply("now")
    assert r.state == SessionState.awaiting_confirmation
    assert "Cancelled" not in r.reply


def test_not_sure_does_not_cancel():
    r = _reply("not sure")
    assert r.state == SessionState.awaiting_confirmation
    assert "Cancelled" not in r.reply


# --------------------------------------------------------------- must execute
def test_plain_yes_executes():
    assert _reply("yes").state == SessionState.executed


def test_confirm_executes():
    assert _reply("confirm").state == SessionState.executed


def test_proceed_executes():
    assert _reply("proceed").state == SessionState.executed


def test_go_ahead_executes():
    assert _reply("go ahead").state == SessionState.executed


def test_do_it_executes():
    assert _reply("do it").state == SessionState.executed


def test_run_it_executes():
    assert _reply("run it").state == SessionState.executed


def test_yes_please_executes():
    assert _reply("yes please").state == SessionState.executed


def test_bare_affirmative_with_fillers_executes():
    assert _reply("ok, just do it now").state == SessionState.executed


# Naming the plan being confirmed is not new content, so these stay sign-offs.
def test_confirm_elisa_executes():
    assert _reply("confirm ELISA").state == SessionState.executed


def test_yes_run_the_elisa_executes():
    assert _reply("yes, run the ELISA").state == SessionState.executed


# --------------------------------------------------------------- must cancel
def test_cancel_cancels():
    r = _reply("cancel")
    assert r.state == SessionState.idle
    assert "Cancelled" in r.reply


def test_no_cancels():
    r = _reply("no")
    assert r.state == SessionState.idle


def test_stop_cancels():
    r = _reply("stop")
    assert r.state == SessionState.idle


def test_abort_cancels():
    r = _reply("abort")
    assert r.state == SessionState.idle


def test_never_mind_cancels():
    r = _reply("never mind")
    assert r.state == SessionState.idle


# --------------------------------------------------------------- ambiguous: cancel wins
def test_both_negative_and_affirmative_cancels():
    # Cancel is the safe direction: an utterance matching both must never execute.
    r = _reply("no, yes")
    assert r.state == SessionState.idle


# --------------------------------------------------------------- late correction
def test_late_correction_routes_to_clarification():
    r = _reply("actually make it 100 microliters per well")
    assert r.state != SessionState.executed
    assert r.state in (SessionState.awaiting_confirmation, SessionState.validation_failed)
