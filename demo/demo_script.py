"""
End-to-end demo — drives the Lab Agent through a full spoken conversation with no
server and no API key required (planner falls back to its deterministic mock).

Run:  python -m demo.demo_script

The arc mirrors the live demo:
  1. High-level request           -> agent asks for the missing clinical details
  2. Details incl. a BAD volume   -> validator CATCHES it before anything moves
  3. Correction                   -> passes, agent names the hazard, asks to confirm
  4. Confirmation                 -> Opentrons simulation runs
  5. Bonus: retarget to Echo      -> capability contract rejects the platform mismatch
"""
from __future__ import annotations

from app.main import message, ADAPTERS
from app.models.session import MessageRequest, SessionState

SID = "demo"


def say(turn: str, text: str):
    print(f"\n\033[1m{turn}:\033[0m {text}")


def agent(resp):
    tag = f"[{resp.state.value}]"
    print(f"\033[96mAGENT {tag}:\033[0m {resp.reply}")
    if resp.clarification_questions:
        for q in resp.clarification_questions:
            print(f"    ? {q}")
    if resp.validation and resp.validation.issues:
        for i in resp.validation.issues:
            mark = "\033[91m✗\033[0m" if i.severity == "error" else "\033[93m!\033[0m"
            print(f"    {mark} ({i.rule}) {i.message}")
    if resp.simulation_log:
        print("\033[90m--- simulation log ---")
        print(resp.simulation_log)
        print("----------------------\033[0m")


def send(text: str, adapter: str = "opentrons"):
    resp = message(MessageRequest(transcript=text, session_id=SID), adapter=adapter)
    agent(resp)
    return resp


def main():
    print("=" * 70)
    print("LAB AGENT — spoken intent to validated protocol execution")
    print("=" * 70)

    # 1. High-level request
    say("SCIENTIST", "Run an ELISA on today's plasma samples.")
    send("Run an ELISA on today's plasma samples.")

    # 2. Details — with a deliberately unsafe per-well volume
    say("SCIENTIST", "Let's do IL-6 with 24 samples, 400 microliters per well.")
    r = send("Let's do IL-6 with 24 samples, 400 microliters per well.")
    assert r.state == SessionState.validation_failed, "expected the volume to be caught"

    # 3. Correction
    say("SCIENTIST", "My mistake — make it 100 microliters per well.")
    r = send("My mistake, make it 100 microliters per well.")
    assert r.state == SessionState.awaiting_confirmation

    # 4. Confirmation -> Opentrons simulation
    say("SCIENTIST", "Yes, go ahead.")
    r = send("Yes, go ahead.")
    assert r.state == SessionState.executed

    # Show a slice of the generated Opentrons protocol
    print("\n\033[1mGenerated Opentrons protocol (first 22 lines):\033[0m")
    protocol = ADAPTERS["opentrons"].compile(r.workflow)
    print("\033[90m" + "\n".join(protocol.splitlines()[:22]) + "\033[0m")

    # 5. The ELISA is an Opentrons job. Show Echo correctly refusing it.
    print("\n" + "-" * 70)
    print("Routing check: can this same ELISA run on an acoustic dispenser (Echo)?")
    echo_issues = ADAPTERS["echo"].check_capabilities(r.workflow)
    print("\033[93mNo — the Echo adapter rejects it:\033[0m")
    for i in echo_issues[:1]:
        print(f"    \033[91m✗\033[0m ({i.rule}) {i.message}")
    print("  A microliter ELISA with incubations isn't an acoustic-dispense job.")


def dilution_demo():
    print("\n" + "=" * 70)
    print("PATH 2 — nanoliter serial dilution  ->  Echo picklist")
    print("=" * 70)
    global SID
    SID = "demo2"

    # One-shot request: fully specified, so it goes straight to the confirmation gate.
    say("SCIENTIST", "Set up a 5-point 2-fold serial dilution of compound X.")
    r = send("Set up a 5-point 2-fold serial dilution of compound X.", adapter="echo")
    assert r.state == SessionState.awaiting_confirmation, r.state

    say("SCIENTIST", "Confirmed, run it.")
    r = send("Confirmed, run it.", adapter="echo")
    assert r.state == SessionState.executed

    print("\n\033[1mGenerated Echo picklist (source → dest → nL):\033[0m")
    print("\033[90m" + ADAPTERS["echo"].compile(r.workflow).strip() + "\033[0m")

    # The reverse routing: this nanoliter job is NOT an Opentrons job.
    print("\n" + "-" * 70)
    print("Routing check: can this same dilution run on the tip-based Opentrons?")
    from app.adapters.opentrons_adapter import OpentronsAdapter
    ot_issues = [i for i in OpentronsAdapter().check_capabilities(r.workflow)
                 if i.rule == "volume_under_platform_min"]
    print("\033[93mNo — the Opentrons adapter rejects it:\033[0m")
    for i in ot_issues[:1]:
        print(f"    \033[91m✗\033[0m ({i.rule}) {i.message}")
    print("  Sub-microliter transfers are below a tip-based handler's floor.")

    print("\n\033[92mThe universal claim, demonstrated:\033[0m one IR, one validator.")
    print("The ELISA routes to Opentrons and is refused by Echo; the nanoliter dilution")
    print("routes to Echo and is refused by Opentrons — purely from declared capabilities.\n")


if __name__ == "__main__":
    main()
    dilution_demo()
