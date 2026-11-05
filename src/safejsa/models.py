"""Pydantic contracts shared by every module, plus JSA parsing and report rendering.

This module imports nothing else from the package (see SPEC.md, Dependency rules).
"""

from __future__ import annotations

import re
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, Field

HazardCategory = Literal[
    "lifting",
    "hot_work_fire",
    "gas_h2s",
    "electrical",
    "pressure_energy",
    "falls_openings",
    "hand_injury",
    "equipment_failure",
    "marine_weather_aviation",
    "well_operations",
    "other",
]
HAZARD_CATEGORIES: tuple[str, ...] = get_args(HazardCategory)

ADVISORY_NOTICE = (
    "Advisory only: SafeJSA is an automated aid. It does not approve this work, and it does "
    "not replace review of the JSA by a competent person before the job starts."
)


class JSAStep(BaseModel):
    number: int
    description: str
    hazards: list[str]
    controls: list[str]


class JSA(BaseModel):
    id: str
    title: str
    steps: list[JSAStep]


class MissingHazard(BaseModel):
    step: int
    category: HazardCategory
    hazard: str
    why: str  # one sentence, in the agent's own words
    alert_numbers: list[int] = Field(min_length=1)


class StepReview(BaseModel):  # returned by one per-step agent call
    step: int
    missing_hazards: list[MissingHazard]
    clarification_questions: list[str]


class ReviewReport(BaseModel):  # assembled in code from the StepReviews
    jsa_id: str
    harness_version: str
    missing_hazards: list[MissingHazard]
    clarification_questions: list[str]
    status: Literal["complete", "partial"]
    open_issues: list[str]  # failed rubric items when status is partial


class ReviewRun(BaseModel):  # returned by agent.review(); used by verify.py and evals.py
    report: ReviewReport
    step_reviews: list[StepReview]  # per-step results, so a failed check can re-run that step
    reviewed_steps: list[int]  # for R2
    tool_alert_numbers: list[int]  # every alert number any tool returned in this run, for R1
    seconds: float  # measured locally


# --- JSA parsing -------------------------------------------------------------------------

JSA_HEADER = ("#", "Step", "Hazards", "Controls")

_FRONT_MATTER = re.compile(r"\A﻿?---[ \t]*\n(.*?)\n---[ \t]*\n(.*)\Z", re.DOTALL)
_SEPARATOR_CELL = re.compile(r":?-+:?")


class JSAParseError(ValueError):
    """The text is not a JSA in the SafeJSA Markdown format."""


def parse_jsa(text: str) -> JSA:
    """Read a Markdown JSA (front matter with id and title, then one table) into a `JSA`.

    Raises `JSAParseError` with a message that says what to fix.
    """
    text = text.replace("\r\n", "\n")
    match = _FRONT_MATTER.match(text)
    if not match:
        raise JSAParseError(
            "missing front matter: the file must start with a '---' line, then 'id:' and "
            "'title:', then another '---' line"
        )
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise JSAParseError(f"front matter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict) or not meta.get("id") or not meta.get("title"):
        raise JSAParseError("front matter must set both 'id' and 'title'")

    lines = [line.strip() for line in match.group(2).split("\n") if line.strip()]
    for line in lines:
        if not line.startswith("|"):
            raise JSAParseError(
                f"only one table may follow the front matter; found other text: {line!r}"
            )
    if len(lines) < 3:
        raise JSAParseError("the table needs a header row, a separator row and at least one step")

    header = _cells(lines[0])
    if [cell.lower() for cell in header] != [cell.lower() for cell in JSA_HEADER]:
        expected = " | ".join(JSA_HEADER)
        raise JSAParseError(f"table header must be '| {expected} |', got {lines[0]!r}")
    separator = _cells(lines[1])
    if len(separator) != len(JSA_HEADER) or not all(
        _SEPARATOR_CELL.fullmatch(cell) for cell in separator
    ):
        raise JSAParseError(
            f"second table row must be a separator like '|---|---|---|---|', got {lines[1]!r}"
        )

    steps: list[JSAStep] = []
    for row in lines[2:]:
        cells = _cells(row)
        if len(cells) != len(JSA_HEADER):
            raise JSAParseError(
                f"each row needs {len(JSA_HEADER)} cells, got {len(cells)}: {row!r}"
            )
        number, description, hazards, controls = cells
        try:
            step_number = int(number)
        except ValueError:
            raise JSAParseError(f"step number must be a whole number, got {number!r}") from None
        if not description:
            raise JSAParseError(f"step {step_number} has no description")
        steps.append(
            JSAStep(
                number=step_number,
                description=description,
                hazards=_split_items(hazards),
                controls=_split_items(controls),
            )
        )

    numbers = [step.number for step in steps]
    if numbers != list(range(1, len(steps) + 1)):
        raise JSAParseError(f"steps must be numbered 1, 2, 3 ... in order, got {numbers}")
    return JSA(id=str(meta["id"]), title=str(meta["title"]), steps=steps)


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _split_items(cell: str) -> list[str]:
    return [item.strip() for item in cell.split(";") if item.strip()]


# --- Report rendering --------------------------------------------------------------------


def report_to_markdown(report: ReviewReport) -> str:
    """Render a review for the email reply. The advisory notice is fixed text, added here."""
    status = "Complete review" if report.status == "complete" else "Partial review"
    lines = [
        f"# SafeJSA review: {report.jsa_id}",
        "",
        f"**{status}** · harness {report.harness_version}",
        "",
    ]
    if report.status == "partial":
        lines += ["Some checks still failed after 2 retries:", ""]
        lines += [f"- {issue}" for issue in report.open_issues] or ["- (no details recorded)"]
        lines.append("")

    lines += ["## Missing hazards", ""]
    if report.missing_hazards:
        for step in sorted({hazard.step for hazard in report.missing_hazards}):
            lines += [f"### Step {step}", ""]
            for hazard in (h for h in report.missing_hazards if h.step == step):
                alerts = ", ".join(str(number) for number in hazard.alert_numbers)
                lines.append(
                    f"- **{hazard.hazard}** (`{hazard.category}`). {hazard.why} "
                    f"BSEE Safety Alert {alerts}."
                )
            lines.append("")
    else:
        lines += ["No missing hazards were flagged.", ""]

    lines += ["## Questions for the JSA author", ""]
    if report.clarification_questions:
        lines += [f"- {question}" for question in report.clarification_questions]
    else:
        lines.append("No questions.")
    lines += ["", "---", "", f"> {ADVISORY_NOTICE}", ""]
    return "\n".join(lines)
