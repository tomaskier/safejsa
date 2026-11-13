"""The verification loop: check a review against rubric R1-R6, re-run failing steps, report.

R1, R2, R3 and R6 are checked in code; R4 and R5 by a judge (the same local model with a
separate prompt). A failed review goes back with the failed items as feedback, re-running
only the failing steps, up to 2 retries; then it is marked partial (SPEC.md, loop 2).
"""

from __future__ import annotations

import difflib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from safejsa import agent
from safejsa.config import Config, load_config
from safejsa.models import (
    ADVISORY_NOTICE,
    JSA,
    Alert,
    ReviewRun,
    StepReview,
    parse_jsa,
    report_to_markdown,
)

ALERTS_PATH = Path("data/alerts.jsonl")
RUNS_DIR = Path("runs")
MAX_RETRIES = 2
SIMILARITY = 0.6  # R3: difflib ratio at or above this means "already listed"
ALERT_TEXT_CHARS = 1500  # alert text the judge sees per cited alert

# Settings of `safejsa review --config` (SPEC.md, Harness and ablation configurations).
CONFIGS = {
    "A": {"use_tools": False, "verify": False},
    "B": {"use_tools": True, "verify": False},
    "C": {"use_tools": True, "verify": True},
    "D": {"use_tools": True, "verify": True},
}

_APPROVAL = re.compile(
    r"\b(?:is|are|been|be)\s+approved\b"
    r"|\bI\s+approve\b"
    r"|\bapprove[sd]?\s+(?:the|this)\s+(?:work|job|jsa|task)\b"
    r"|\b(?:safe|ok|okay|cleared)\s+to\s+(?:proceed|start|begin|go)\b"
    r"|\bgood\s+to\s+go\b",
    re.I,
)


@dataclass(frozen=True)
class Issue:
    rule: str
    step: int | None
    message: str

    def __str__(self) -> str:
        where = f" (step {self.step})" if self.step is not None else ""
        return f"{self.rule}{where}: {self.message}"


# --- Code rules --------------------------------------------------------------------------


def load_alerts(path: Path = ALERTS_PATH) -> dict[int, Alert]:
    with path.open(encoding="utf-8") as handle:
        alerts = [Alert.model_validate_json(line) for line in handle if line.strip()]
    return {alert.alert_number: alert for alert in alerts}


def check_r1(run: ReviewRun, known: set[int]) -> list[Issue]:
    """Every cited alert came back from a tool in this run and exists in alerts.jsonl."""
    issues = []
    returned = set(run.tool_alert_numbers)
    for hazard in run.report.missing_hazards:
        for number in hazard.alert_numbers:
            if number not in known:
                issues.append(Issue("R1", hazard.step, f"alert {number} does not exist"))
            elif number not in returned:
                issues.append(
                    Issue("R1", hazard.step, f"alert {number} was not returned by a tool")
                )
    return issues


def check_r2(run: ReviewRun, jsa: JSA) -> list[Issue]:
    """Every JSA step was reviewed."""
    reviewed = set(run.reviewed_steps)
    return [
        Issue("R2", step.number, "step was not reviewed")
        for step in jsa.steps
        if step.number not in reviewed
    ]


def similarity(first: str, second: str) -> float:
    return difflib.SequenceMatcher(None, first.lower().strip(), second.lower().strip()).ratio()


def check_r3(run: ReviewRun, jsa: JSA) -> list[Issue]:
    """No flagged "missing" hazard is already listed in that step (difflib ratio >= 0.6)."""
    listed = {step.number: step.hazards for step in jsa.steps}
    issues = []
    for hazard in run.report.missing_hazards:
        for existing in listed.get(hazard.step, []):
            if similarity(hazard.hazard, existing) >= SIMILARITY:
                issues.append(
                    Issue(
                        "R3",
                        hazard.step,
                        f"'{hazard.hazard}' is already listed as '{existing}'",
                    )
                )
                break
    return issues


def check_r6(run: ReviewRun) -> list[Issue]:
    """The rendered reply has the advisory notice and does not approve the work."""
    markdown = report_to_markdown(run.report)
    issues = []
    if ADVISORY_NOTICE not in markdown:
        issues.append(Issue("R6", None, "the advisory notice is missing"))
    for review in run.step_reviews:
        texts = [h.hazard for h in review.missing_hazards] + [h.why for h in review.missing_hazards]
        texts += review.clarification_questions
        for text in texts:
            if _APPROVAL.search(text):
                issues.append(Issue("R6", review.step, f"text approves the work: '{text}'"))
    return issues


def code_checks(run: ReviewRun, jsa: JSA, known: set[int]) -> list[Issue]:
    return check_r1(run, known) + check_r2(run, jsa) + check_r3(run, jsa) + check_r6(run)


# --- Judge rules -------------------------------------------------------------------------


class Verdict(BaseModel):
    ok: bool
    reason: str


# Asks the judge model one question and returns a Verdict. Tests pass a FakeLLM.
Ask = Callable[[str], Verdict]

R4_PROMPT = """\
You check one finding of an offshore job safety review.
Finding for the step "{step}": missing hazard "{hazard}". Reason given: "{why}".
Cited BSEE Safety Alert text:
{alerts}

Does the cited alert text support this hazard for this kind of work? Answer ok=true if it
does, ok=false if it is about something else. Give a one-sentence reason."""

R5_PROMPT = """\
You check the clarification questions of an offshore job safety review.
JSA step: "{step}". Listed hazards: {hazards}. Listed controls: {controls}.
Questions for the JSA author:
{questions}

Are all questions specific to this step and answerable by the JSA author? Answer ok=false
if any question is vague, generic or not about this step. Give a one-sentence reason."""


def make_ask(llm) -> Ask:
    structured = llm.with_structured_output(Verdict, method="json_schema")

    def ask(prompt: str) -> Verdict:
        return structured.invoke(prompt)

    return ask


def check_r4(run: ReviewRun, jsa: JSA, alerts: dict[int, Alert], ask: Ask) -> list[Issue]:
    """The cited alerts support each missing hazard. Unknown alerts are left to R1."""
    steps = {step.number: step for step in jsa.steps}
    issues = []
    for hazard in run.report.missing_hazards:
        cited = [alerts[number] for number in hazard.alert_numbers if number in alerts]
        if not cited:
            continue
        text = "\n\n".join(
            f"Alert {a.alert_number}: {a.title}\n"
            + f"{a.summary} {a.recommendations}"[:ALERT_TEXT_CHARS]
            for a in cited[:2]
        )
        step = steps[hazard.step].description if hazard.step in steps else ""
        verdict = ask(
            R4_PROMPT.format(step=step, hazard=hazard.hazard, why=hazard.why, alerts=text)
        )
        if not verdict.ok:
            numbers = ", ".join(str(a.alert_number) for a in cited)
            issues.append(
                Issue(
                    "R4",
                    hazard.step,
                    f"alert {numbers} does not support '{hazard.hazard}': {verdict.reason}",
                )
            )
    return issues


def check_r5(run: ReviewRun, jsa: JSA, ask: Ask) -> list[Issue]:
    """Each step's clarification questions are specific and answerable."""
    steps = {step.number: step for step in jsa.steps}
    issues = []
    for review in run.step_reviews:
        if not review.clarification_questions or review.step not in steps:
            continue
        step = steps[review.step]
        verdict = ask(
            R5_PROMPT.format(
                step=step.description,
                hazards="; ".join(step.hazards),
                controls="; ".join(step.controls),
                questions="\n".join(f"- {q}" for q in review.clarification_questions),
            )
        )
        if not verdict.ok:
            issues.append(Issue("R5", review.step, f"questions are not specific: {verdict.reason}"))
    return issues


def all_checks(run: ReviewRun, jsa: JSA, alerts: dict[int, Alert], ask: Ask) -> list[Issue]:
    return (
        code_checks(run, jsa, set(alerts))
        + check_r4(run, jsa, alerts, ask)
        + check_r5(run, jsa, ask)
    )


# --- The review-verify-retry loop --------------------------------------------------------


@dataclass
class VerifiedReview:
    run: ReviewRun
    first_issues: list[Issue] = field(default_factory=list)  # for first-attempt pass rate
    open_issues: list[Issue] = field(default_factory=list)
    attempts: int = 1


# Reviews a JSA, or some of its steps with feedback: (jsa, steps, feedback) -> ReviewRun.
Reviewer = Callable[[JSA, list[int] | None, dict[int, list[str]] | None], ReviewRun]


def merge(jsa: JSA, earlier: ReviewRun, later: ReviewRun) -> ReviewRun:
    """Replace the re-run steps of `earlier` with their results in `later`."""
    redone = set(later.reviewed_steps)
    reviews: list[StepReview] = [r for r in earlier.step_reviews if r.step not in redone]
    reviews += later.step_reviews
    numbers = list(dict.fromkeys(earlier.tool_alert_numbers + later.tool_alert_numbers))
    return ReviewRun(
        report=agent.assemble(jsa, earlier.report.harness_version, reviews),
        step_reviews=sorted(reviews, key=lambda review: review.step),
        reviewed_steps=sorted(review.step for review in reviews),
        tool_alert_numbers=numbers,
        seconds=earlier.seconds + later.seconds,
    )


def verify_loop(
    jsa: JSA,
    reviewer: Reviewer,
    alerts: dict[int, Alert],
    ask: Ask,
    max_retries: int = MAX_RETRIES,
    log: Callable[[str], None] = lambda line: None,
) -> VerifiedReview:
    run = reviewer(jsa, None, None)
    result = VerifiedReview(run)
    for attempt in range(max_retries + 1):
        issues = all_checks(run, jsa, alerts, ask)
        if attempt == 0:
            result.first_issues = issues
        log(f"check {attempt + 1}: {len(issues)} issues" + "".join(f"\n  {i}" for i in issues))
        if not issues or attempt == max_retries:
            break
        failing = sorted({i.step for i in issues if i.step is not None}) or [
            step.number for step in jsa.steps
        ]
        feedback: dict[int, list[str]] = {}
        for issue in issues:
            for step in [issue.step] if issue.step is not None else failing:
                feedback.setdefault(step, []).append(str(issue))
        log(f"retry {attempt + 1}: re-running steps {failing}")
        run = merge(jsa, run, reviewer(jsa, failing, feedback))
        result.attempts += 1

    result.open_issues = issues
    report = run.report.model_copy(
        update={
            "status": "partial" if issues else "complete",
            "open_issues": [str(issue) for issue in issues],
        }
    )
    result.run = run.model_copy(update={"report": report})
    return result


def run_config(
    jsa: JSA,
    config_name: str,
    harness: str,
    config: Config,
    log: Callable[[str], None] = lambda line: None,
) -> VerifiedReview:
    """Review one JSA under configuration A-D, with or without the verification loop."""
    settings = CONFIGS[config_name]

    def reviewer(jsa: JSA, steps, feedback) -> ReviewRun:
        return agent.review(
            jsa,
            harness=harness,
            use_tools=settings["use_tools"],
            steps=steps,
            feedback=feedback,
            config=config,
            log=log,
        )

    if not settings["verify"]:
        return VerifiedReview(reviewer(jsa, None, None))
    ask = make_ask(agent.build_llm(config))
    return verify_loop(jsa, reviewer, load_alerts(), ask, log=log)


def review_file(path: Path, config_name: str = "C") -> Path:
    """`safejsa review`: review one JSA file; write runs/<timestamp>/report.json and .md."""
    config = load_config()
    jsa = parse_jsa(path.read_text(encoding="utf-8"))
    tracing = agent.configure_tracing(config)
    print(
        f"Reviewing {jsa.id} ({len(jsa.steps)} steps), config {config_name}, "
        f"harness {config.harness}, model {config.llm.model}, "
        f"LangSmith {'on' if tracing else 'off'}."
    )
    start = time.monotonic()
    from langsmith import trace

    with trace(
        "safejsa review",
        inputs={"jsa_id": jsa.id, "config": config_name, "harness": config.harness},
        metadata={"model": config.llm.model},
    ):
        result = run_config(jsa, config_name, config.harness, config, log=print)
    elapsed = time.monotonic() - start

    folder = RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(result.run.model_dump_json(indent=2), encoding="utf-8")
    (folder / "report.md").write_text(report_to_markdown(result.run.report), encoding="utf-8")
    print(
        f"\nStatus: {result.run.report.status} after {result.attempts} attempt(s); "
        f"{len(result.run.report.missing_hazards)} missing hazards flagged."
    )
    for issue in result.open_issues:
        print(f"  open: {issue}")
    print(f"Review time: {elapsed / 60:.1f} minutes ({elapsed:.0f} s).")
    print(f"Wrote {folder.as_posix()}/report.json and report.md")
    return folder
