import asyncio
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeStepRunner, hazard, result
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from safejsa import agent
from safejsa.agent import review_steps, step_message, tool_alert_numbers
from safejsa.models import Alert, parse_jsa
from safejsa.verify import (
    check_r1,
    check_r2,
    check_r3,
    check_r4,
    check_r5,
    check_r6,
    load_alerts,
    verify_loop,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
JSA = parse_jsa((FIXTURES / "crane_lift.md").read_text(encoding="utf-8"))
ALERTS = load_alerts(FIXTURES / "alerts.jsonl")  # 150, 362, 461, 464, 506
KNOWN = set(ALERTS)


def run_of(script, steps=None):
    return asyncio.run(review_steps(JSA, "v1", FakeStepRunner(script), steps))


def all_steps():
    return {n: [result(n, tool_numbers=(362, 461))] for n in range(1, 5)}


# --- Agent loop --------------------------------------------------------------------------


def test_review_steps_assembles_report_and_pins_step_numbers():
    run = run_of(
        all_steps()
        | {
            4: [
                result(
                    4,
                    hazards=[hazard(99, "No tag line on the load", alerts=(362,))],
                    questions=["What tag line length is used?"],
                    tool_numbers=(362, 464),
                )
            ]
        }
    )
    assert run.reviewed_steps == [1, 2, 3, 4]
    assert run.report.missing_hazards[0].step == 4  # the model wrote 99
    assert run.report.clarification_questions == ["Step 4: What tag line length is used?"]
    assert run.tool_alert_numbers == [362, 461, 464]
    assert run.report.status == "complete"


def test_a_failed_step_is_left_out_of_reviewed_steps():
    run = run_of(all_steps() | {2: [ValueError("bad JSON")]})
    assert run.reviewed_steps == [1, 3, 4]


def test_step_message_includes_feedback():
    message = step_message(JSA, JSA.steps[3], ["R1 (step 4): alert 123 does not exist"])
    assert "Review step 4: Land the container on the vessel deck" in message
    assert "Listed hazards: Vessel movement in swell; struck by load" in message
    assert "- R1 (step 4): alert 123 does not exist" in message


def test_tool_alert_numbers_come_only_from_tool_results():
    messages = [
        HumanMessage("Alert 999"),
        AIMessage('{"alert_number": 111}'),
        ToolMessage(
            content=[{"type": "text", "text": '{\n  "alert_number": 362,\n  "title": "x"}'}],
            tool_call_id="1",
        ),
        ToolMessage(content='{"alert_number": 461}', tool_call_id="2"),
    ]
    assert tool_alert_numbers(messages) == [362, 461]


def test_load_harness_appends_skills(tmp_path):
    (tmp_path / "v2" / "skills").mkdir(parents=True)
    (tmp_path / "v2" / "system.md").write_text("System.", encoding="utf-8")
    (tmp_path / "v2" / "skills" / "b.md").write_text("Skill B.", encoding="utf-8")
    (tmp_path / "v2" / "skills" / "a.md").write_text("Skill A.", encoding="utf-8")
    assert agent.load_harness("v2", tmp_path) == "System.\n\nSkill A.\n\nSkill B."
    with pytest.raises(FileNotFoundError):
        agent.load_harness("v9", tmp_path)


def test_harness_v1_exists():
    assert "search_alerts" in agent.load_harness("v1", ROOT / "harness")


# --- Code rules: each passes and fails ---------------------------------------------------


def test_r1_passes_for_tool_returned_known_alerts():
    run = run_of(
        all_steps() | {4: [result(4, [hazard(4, "No tag line", (362,))], tool_numbers=(362,))]}
    )
    assert check_r1(run, KNOWN) == []


def test_r1_fails_for_unknown_or_not_returned_alerts():
    run = run_of(
        all_steps()
        | {
            3: [result(3, [hazard(3, "Load swings", (123,))], tool_numbers=(362,))],
            4: [result(4, [hazard(4, "No tag line", (464,))], tool_numbers=(362,))],
        }
    )
    messages = [str(issue) for issue in check_r1(run, KNOWN)]
    assert messages == [
        "R1 (step 3): alert 123 does not exist",
        "R1 (step 4): alert 464 was not returned by a tool",
    ]


def test_r2_passes_when_every_step_is_reviewed_and_fails_otherwise():
    assert check_r2(run_of(all_steps()), JSA) == []
    issues = check_r2(run_of(all_steps(), steps=[1, 2]), JSA)
    assert [(issue.rule, issue.step) for issue in issues] == [("R2", 3), ("R2", 4)]


def test_r3_flags_hazards_already_listed():
    listed = run_of(all_steps() | {4: [result(4, [hazard(4, "Struck by the load")])]})
    issues = check_r3(listed, JSA)
    assert [(issue.rule, issue.step) for issue in issues] == [("R3", 4)]
    assert "already listed as 'struck by load'" in str(issues[0])


def test_r3_passes_for_new_hazards():
    new = run_of(all_steps() | {4: [result(4, [hazard(4, "No tag line to control the load")])]})
    assert check_r3(new, JSA) == []


def test_r6_passes_for_advisory_text_and_fails_when_work_is_approved():
    assert check_r6(run_of(all_steps())) == []
    approving = run_of(
        all_steps() | {2: [result(2, questions=["The rigging is approved, safe to proceed?"])]}
    )
    issues = check_r6(approving)
    assert [(issue.rule, issue.step) for issue in issues] == [("R6", 2)]


def test_r6_does_not_flag_alert_text_about_crews_being_ready():
    why = "Alert 468 says to communicate clearly so everyone is ready to begin the lift."
    run = run_of(all_steps() | {4: [result(4, [hazard(4, "Personnel in the red zone", why=why)])]})
    assert check_r6(run) == []


def test_r6_does_not_flag_the_word_approval_in_questions():
    run = run_of(
        all_steps() | {1: [result(1, questions=["Who gives approval for the lift plan?"])]}
    )
    assert check_r6(run) == []


# --- Judge rules -------------------------------------------------------------------------


def test_r4_asks_the_judge_with_the_cited_alert_text():
    run = run_of(all_steps() | {4: [result(4, [hazard(4, "No tag line", (362, 999))])]})
    judge = FakeLLM([False])
    issues = check_r4(run, JSA, ALERTS, judge)
    assert len(judge.prompts) == 1
    assert "Alert 362: Safety Alert 362 - Poor Tag Line Awareness" in judge.prompts[0]
    assert [(issue.rule, issue.step) for issue in issues] == [("R4", 4)]
    assert check_r4(run, JSA, ALERTS, FakeLLM([True])) == []


def test_r5_skips_steps_without_questions():
    run = run_of(
        all_steps() | {3: [result(3, questions=["Is the load chart for this boom length?"])]}
    )
    judge = FakeLLM([False])
    issues = check_r5(run, JSA, judge)
    assert len(judge.prompts) == 1 and "Lift and swing the load" in judge.prompts[0]
    assert [(issue.rule, issue.step) for issue in issues] == [("R5", 3)]


# --- The loop ----------------------------------------------------------------------------


def _reviewer(runner):
    def reviewer(jsa, steps, feedback):
        return asyncio.run(review_steps(jsa, "v1", runner, steps, feedback))

    return reviewer


def test_loop_reruns_only_failing_steps_with_feedback():
    bad = result(4, [hazard(4, "No tag line", (123,))], tool_numbers=(362,))
    good = result(4, [hazard(4, "No tag line", (362,))], tool_numbers=(362,))
    runner = FakeStepRunner({4: [bad, good]})
    verified = verify_loop(JSA, _reviewer(runner), ALERTS, FakeLLM())
    assert verified.run.report.status == "complete"
    assert verified.attempts == 2
    assert [str(issue) for issue in verified.first_issues] == [
        "R1 (step 4): alert 123 does not exist"
    ]
    assert [call[0] for call in runner.calls] == [1, 2, 3, 4, 4]
    assert runner.calls[-1][1] == ["R1 (step 4): alert 123 does not exist"]
    assert verified.run.report.missing_hazards[0].alert_numbers == [362]


def test_loop_marks_partial_after_two_retries():
    bad = result(2, [hazard(2, "Dropped load from damaged rigging")])  # already listed: R3
    runner = FakeStepRunner({2: [bad]})
    verified = verify_loop(JSA, _reviewer(runner), ALERTS, FakeLLM())
    assert verified.attempts == 3
    assert verified.run.report.status == "partial"
    assert verified.run.report.open_issues[0].startswith("R3 (step 2)")
    assert [call[0] for call in runner.calls].count(2) == 3


def test_loop_reruns_a_step_that_failed_to_review():
    runner = FakeStepRunner({3: [RuntimeError("model timeout"), result(3)]})
    verified = verify_loop(JSA, _reviewer(runner), ALERTS, FakeLLM())
    assert verified.run.reviewed_steps == [1, 2, 3, 4]
    assert verified.run.report.status == "complete"
    assert [str(issue) for issue in verified.first_issues] == ["R2 (step 3): step was not reviewed"]


def test_fixture_alerts_are_alerts():
    assert all(isinstance(alert, Alert) for alert in ALERTS.values())
