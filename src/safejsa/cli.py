"""Command-line entry point. Each command calls one function in another module.

Modules are imported when a command runs, so `safejsa --help` works before every build
phase is done, and a command whose module is not built yet says which phase builds it.
"""

from __future__ import annotations

from enum import StrEnum
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Annotated

import typer

app = typer.Typer(
    help="SafeJSA: local AI reviewer for offshore job safety analyses.",
    no_args_is_help=True,
    add_completion=False,
)


class RunConfig(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class Split(StrEnum):
    dev = "dev"
    test = "test"


def _module(name: str, phase: int) -> ModuleType:
    try:
        return import_module(f"safejsa.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"safejsa.{name}":
            raise
        typer.echo(f"safejsa.{name} is not built yet (build phase {phase}).", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def ingest() -> None:
    """Crawl BSEE Safety Alerts and write data/alerts.jsonl."""
    _module("ingest", 1).run_ingest()


@app.command()
def mcp() -> None:
    """Run the BSEE alerts MCP server over stdio (the agent starts it automatically)."""
    _module("mcp_server", 2).main()


@app.command()
def review(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="JSA Markdown file")],
    config: Annotated[RunConfig, typer.Option(help="Tools and verification setting")] = (
        RunConfig.C
    ),
) -> None:
    """Review one JSA; write runs/<timestamp>/report.json and report.md."""
    _module("verify", 3).review_file(path, config.value)


@app.command()
def mail() -> None:
    """Run the email worker (behaves as config C)."""
    _module("mail", 5).run_worker()


@app.command("eval")
def eval_(
    split: Annotated[Split, typer.Option(help="Which JSA split to score")],
    config: Annotated[RunConfig, typer.Option(help="Ablation configuration")],
    repeats: Annotated[int, typer.Option(min=1, help="Number of runs")] = 1,
) -> None:
    """Score a split; write evals/results/*.json and regenerate the README table."""
    _module("evals", 4).run_eval(split.value, config.value, repeats)


@app.command()
def improve() -> None:
    """Count dev failures by rubric item and category; write evals/reports/<date>.md."""
    _module("evals", 4).improve()
