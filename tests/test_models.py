from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from safejsa.models import (
    ADVISORY_NOTICE,
    HAZARD_CATEGORIES,
    JSAParseError,
    MissingHazard,
    ReviewReport,
    parse_jsa,
    report_to_markdown,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

VALID = """---
id: dev-01
title: Crane lift of a container to a supply vessel
---
| # | Step | Hazards | Controls |
|---|------|---------|----------|
| 1 | Hold pre-lift meeting | Miscommunication | Lift plan reviewed with crew |
| 2 | Rig the container | Pinch points; dropped load | Certified slings; gloves |
"""


def test_parse_spec_example():
    jsa = parse_jsa(VALID)
    assert jsa.id == "dev-01"
    assert jsa.title == "Crane lift of a container to a supply vessel"
    assert [step.number for step in jsa.steps] == [1, 2]
    assert jsa.steps[1].description == "Rig the container"
    assert jsa.steps[1].hazards == ["Pinch points", "dropped load"]
    assert jsa.steps[1].controls == ["Certified slings", "gloves"]


def test_parse_windows_line_endings_and_bom():
    jsa = parse_jsa("﻿" + VALID.replace("\n", "\r\n"))
    assert len(jsa.steps) == 2


def test_parse_empty_cells_give_empty_lists():
    text = VALID.replace("| Miscommunication | Lift plan reviewed with crew |", "|  |  |")
    step = parse_jsa(text).steps[0]
    assert step.hazards == []
    assert step.controls == []


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.md")), ids=lambda p: p.name)
def test_fixtures_parse(path):
    jsa = parse_jsa(path.read_text(encoding="utf-8"))
    assert jsa.id.startswith("fixture-")
    assert 4 <= len(jsa.steps) <= 8


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (VALID.split("---\n", 2)[2], "front matter"),
        (VALID.replace("id: dev-01\n", ""), "'id' and 'title'"),
        (VALID.replace("title: Crane", "title: [Crane"), "not valid YAML"),
        (VALID.replace("| # | Step |", "| No | Step |"), "table header"),
        (VALID.replace("|---|------|", "|xxx|------|"), "separator"),
        (VALID.replace("| 2 | Rig", "| two | Rig"), "whole number"),
        (VALID.replace("| 2 | Rig", "| 3 | Rig"), "numbered 1, 2, 3"),
        (VALID.replace("| Certified slings; gloves |", "|"), "4 cells"),
        (VALID.replace("| Rig the container |", "|  |"), "no description"),
        (VALID + "\nSome notes after the table.\n", "only one table"),
        (VALID.split("| 1 |")[0], "at least one step"),
    ],
    ids=[
        "no-front-matter",
        "no-id",
        "bad-yaml",
        "wrong-header",
        "bad-separator",
        "non-integer-step",
        "step-gap",
        "missing-cell",
        "empty-description",
        "extra-text",
        "no-steps",
    ],
)
def test_parse_rejects_bad_input(text, message):
    with pytest.raises(JSAParseError, match=message):
        parse_jsa(text)


def _hazard(step: int, category: str = "lifting") -> MissingHazard:
    return MissingHazard(
        step=step,
        category=category,
        hazard="No tag line on the load",
        why="Loads landed by hand have crushed deck crew.",
        alert_numbers=[468],
    )


def _report(**overrides) -> ReviewReport:
    fields = dict(
        jsa_id="dev-01",
        harness_version="v1",
        missing_hazards=[_hazard(2)],
        clarification_questions=["Who acts as signalman during the lift?"],
        status="complete",
        open_issues=[],
    )
    return ReviewReport(**(fields | overrides))


def test_missing_hazard_needs_a_citation():
    with pytest.raises(ValidationError):
        MissingHazard(step=1, category="lifting", hazard="x", why="y", alert_numbers=[])


def test_missing_hazard_rejects_unknown_category():
    with pytest.raises(ValidationError):
        _hazard(1, category="radiation")


def test_markdown_has_hazards_questions_and_notice():
    markdown = report_to_markdown(_report())
    assert "# SafeJSA review: dev-01" in markdown
    assert "**Complete review**" in markdown
    assert "### Step 2" in markdown
    assert "No tag line on the load" in markdown
    assert "BSEE Safety Alert 468" in markdown
    assert "Who acts as signalman during the lift?" in markdown
    assert markdown.rstrip().endswith(ADVISORY_NOTICE)


def test_markdown_groups_hazards_by_step_in_order():
    report = _report(missing_hazards=[_hazard(3), _hazard(1), _hazard(3, "hand_injury")])
    markdown = report_to_markdown(report)
    assert markdown.index("### Step 1") < markdown.index("### Step 3")
    assert markdown.count("### Step 3") == 1


def test_markdown_partial_lists_open_issues():
    report = _report(status="partial", open_issues=["R4: alert 468 does not support step 2"])
    markdown = report_to_markdown(report)
    assert "**Partial review**" in markdown
    assert "- R4: alert 468 does not support step 2" in markdown
    assert ADVISORY_NOTICE in markdown


def test_markdown_empty_report():
    markdown = report_to_markdown(_report(missing_hazards=[], clarification_questions=[]))
    assert "No missing hazards were flagged." in markdown
    assert "No questions." in markdown
    assert ADVISORY_NOTICE in markdown


def test_taxonomy_matches_hazard_categories():
    taxonomy = yaml.safe_load((ROOT / "data" / "taxonomy.yaml").read_text(encoding="utf-8"))
    categories = taxonomy["categories"]
    assert set(categories) == set(HAZARD_CATEGORIES) - {"other"}
    for entry in categories.values():
        assert entry["description"]
        assert entry["bsee_tags"]
