"""Test doubles: no model server, network or mailbox (SPEC.md, Rules)."""

from __future__ import annotations

from collections.abc import Iterable

from safejsa.agent import StepResult
from safejsa.models import JSA, JSAStep, MissingHazard, StepReview
from safejsa.verify import Verdict


class FakeLLM:
    """A judge with scripted verdicts, consumed in order; records every prompt it gets.

    When the script runs out it keeps answering `default`.
    """

    def __init__(self, verdicts: Iterable[bool] = (), default: bool = True) -> None:
        self.script = list(verdicts)
        self.default = default
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> Verdict:
        self.prompts.append(prompt)
        ok = self.script.pop(0) if self.script else self.default
        return Verdict(ok=ok, reason="scripted" if ok else "scripted failure")


class FakeStepRunner:
    """A per-step agent with scripted results, keyed by step number.

    Each step's script is a list used in order across calls (first try, then retries); the
    last entry repeats. An entry that is an Exception is raised instead.
    """

    def __init__(self, script: dict[int, list[StepResult | Exception]]) -> None:
        self.script = {step: list(results) for step, results in script.items()}
        self.calls: list[tuple[int, list[str]]] = []

    async def __call__(self, jsa: JSA, step: JSAStep, feedback: list[str]) -> StepResult:
        self.calls.append((step.number, list(feedback)))
        results = self.script.get(step.number) or [result(step.number)]
        entry = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(entry, Exception):
            raise entry
        return entry


def hazard(
    step: int, text: str, alerts=(362,), category="lifting", why="An alert shows it."
) -> MissingHazard:
    return MissingHazard(
        step=step, category=category, hazard=text, why=why, alert_numbers=list(alerts)
    )


def result(step: int, hazards=(), questions=(), tool_numbers=(362,)) -> StepResult:
    review = StepReview(
        step=step, missing_hazards=list(hazards), clarification_questions=list(questions)
    )
    return StepResult(review, list(tool_numbers))
