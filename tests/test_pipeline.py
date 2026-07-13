"""
Minimal tests for the core guarantees. Run: python -m tests.test_pipeline
(or `pytest` if installed). No network / API key required.
"""
from app.agent import clarify, planner
from app.compiler.plan_to_ops import compile_plan
from app.adapters.opentrons_adapter import OpentronsAdapter
from app.adapters.echo_adapter import EchoAdapter
from app.models.plan import Intent
from app.validation.validator import validate


def _complete_elisa_plan(sample_vol=100, num_samples=24):
    plan = planner.plan_from_transcript("Run an ELISA on today's plasma samples")
    assert plan.intent == Intent.elisa
    assert "target_analyte" in plan.missing_fields  # never guesses the analyte
    answers = clarify.parse_answers(plan, f"IL-6, {num_samples} samples, {sample_vol} microliters per well")
    plan = planner.merge_clarification(plan, answers)
    assert plan.is_complete
    assert plan.parameters["target_analyte"] == "IL-6"
    assert plan.parameters["num_samples"] == num_samples  # not the '6' in IL-6
    return plan


def test_analyte_not_confused_with_il6_digit():
    plan = _complete_elisa_plan(num_samples=24)
    assert plan.parameters["num_samples"] == 24


def test_bad_volume_is_caught():
    plan = _complete_elisa_plan(sample_vol=400)
    wf = compile_plan(plan)
    report = validate(wf, OpentronsAdapter())
    assert not report.passed
    assert any(i.rule == "well_capacity" for i in report.errors)


def test_good_volume_passes():
    plan = _complete_elisa_plan(sample_vol=100)
    wf = compile_plan(plan)
    report = validate(wf, OpentronsAdapter())
    assert report.passed, [i.message for i in report.errors]


def test_hazard_is_flagged_as_warning():
    plan = _complete_elisa_plan(sample_vol=100)
    wf = compile_plan(plan)
    report = validate(wf, OpentronsAdapter())
    assert any(i.rule == "hazardous_reagent" for i in report.issues)


def test_elisa_routes_to_opentrons_not_echo():
    wf = compile_plan(_complete_elisa_plan(sample_vol=100))
    assert validate(wf, OpentronsAdapter()).passed          # accepted here
    assert not validate(wf, EchoAdapter()).passed            # rejected there


def _dilution_plan(points=5, factor=2):
    plan = planner.plan_from_transcript(
        f"Set up a {points}-point {factor}-fold serial dilution of compound X")
    assert plan.intent == Intent.serial_dilution
    assert plan.is_complete, plan.missing_fields
    return plan


def test_serial_dilution_compiles_and_is_on_droplet_grid():
    wf = compile_plan(_dilution_plan(points=5))
    for op in wf.operations:
        nl = op.volume.to_uL() * 1000
        assert abs(round(nl / 2.5) * 2.5 - nl) < 1e-6, f"{nl} nL off the 2.5 nL grid"


def test_dilution_routes_to_echo_not_opentrons():
    wf = compile_plan(_dilution_plan(points=5))
    assert validate(wf, EchoAdapter()).passed                # accepted here
    report = validate(wf, OpentronsAdapter())
    assert not report.passed                                 # rejected there
    assert any(i.rule == "volume_under_platform_min" for i in report.errors)


def test_dilution_emits_echo_picklist():
    wf = compile_plan(_dilution_plan(points=5))
    csv = EchoAdapter().compile(wf)
    assert "Transfer Volume (nL)" in csv
    assert "62.5" in csv  # exact on-grid volume, not integer-rounded to 62


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
