"""The review agent: code loops over the JSA steps and makes one short agent call per step.

Each per-step call gets the harness instructions and, when tools are on, the two BSEE tools
from `safejsa mcp`, started over stdio once per review through langchain-mcp-adapters. This
module never imports mcp_server.py (SPEC.md, Dependency rules). Code then assembles the
step results into a `ReviewReport` and wraps it in a `ReviewRun`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from safejsa.config import Config, load_config
from safejsa.models import JSA, JSAStep, ReviewReport, ReviewRun, StepReview

HARNESS_DIR = Path("harness")
MCP_SERVER = [sys.executable, "-c", "from safejsa.cli import app; app()", "mcp"]
MAX_TOOL_CALLS = 4  # hard cap on tool calls per step
RECURSION_LIMIT = 24  # graph steps per step call; a backstop behind the tool-call cap
TOOL_TEXT_CHARS = 1500  # tool output kept per result when asking for the JSON answer

NO_TOOLS_NOTE = (
    "Tools are switched off for this run. Do not try to call tools. Cite the numbers of "
    "BSEE Safety Alerts you know from memory."
)
ANSWER_NOW = (
    "Give your final answer for this step now, as JSON that matches the schema. Use only "
    "alert numbers from the tool results above."
)


@dataclass
class StepResult:
    review: StepReview
    tool_alert_numbers: list[int] = field(default_factory=list)


# Reviews one step: (jsa, step, checker feedback) -> StepResult. Tests pass a fake one.
StepRunner = Callable[[JSA, JSAStep, list[str]], Awaitable[StepResult]]


# --- Prompts -----------------------------------------------------------------------------


def load_harness(version: str, root: Path = HARNESS_DIR) -> str:
    """The harness instructions: system.md, then any skill files, in name order."""
    folder = root / version
    if not (folder / "system.md").exists():
        raise FileNotFoundError(f"harness {version!r} has no {folder.as_posix()}/system.md")
    parts = [(folder / "system.md").read_text(encoding="utf-8").strip()]
    skills = folder / "skills"
    if skills.is_dir():
        parts += [path.read_text(encoding="utf-8").strip() for path in sorted(skills.glob("*.md"))]
    return "\n\n".join(parts)


def step_message(jsa: JSA, step: JSAStep, feedback: Iterable[str] = ()) -> str:
    """The user message for one step: the job, the step, and any checker feedback."""
    outline = "\n".join(f"  {other.number}. {other.description}" for other in jsa.steps)
    lines = [
        f"Job: {jsa.title}",
        f"All steps:\n{outline}",
        "",
        f"Review step {step.number}: {step.description}",
        f"Listed hazards: {'; '.join(step.hazards) or '(none)'}",
        f"Listed controls: {'; '.join(step.controls) or '(none)'}",
    ]
    feedback = list(feedback)
    if feedback:
        lines += ["", "A checker rejected your previous answer for this step. Fix these issues:"]
        lines += [f"- {item}" for item in feedback]
    return "\n".join(lines)


# --- Model and tools ---------------------------------------------------------------------


def build_llm(config: Config):
    """The one OpenAI-compatible client, pointed at Ollama (SPEC.md, Rules).

    It gets its own async HTTP client: langchain-openai otherwise shares a cached one, which
    breaks the second `asyncio.run` of a retry with "Event loop is closed".
    """
    import httpx
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        http_async_client=httpx.AsyncClient(timeout=1800),
        base_url=config.llm.base_url,
        api_key=config.llm.api_key.get_secret_value(),
        model=config.llm.model,
        temperature=config.llm.temperature,
        reasoning_effort=config.llm.reasoning_effort,
        max_retries=1,
        timeout=1800,
    )


def configure_tracing(config: Config) -> bool:
    """Send traces to LangSmith when a key is configured. Returns whether tracing is on."""
    key = config.langsmith.api_key.get_secret_value()
    enabled = bool(key) and key != "..."
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    if enabled:
        os.environ["LANGSMITH_API_KEY"] = key
        os.environ["LANGSMITH_PROJECT"] = config.langsmith.project
    return enabled


def message_text(content: Any) -> str:
    """Plain text of a LangChain message content (a string or a list of blocks)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            parts.append(str(block.get("text", "")))
        else:
            parts.append(str(block))
    return "\n".join(parts)


_ALERT_NUMBER = re.compile(r'"alert_number"\s*:\s*(\d+)')


def tool_alert_numbers(messages: Iterable[Any]) -> list[int]:
    """Every alert number that appears in the tool results of a conversation."""
    numbers: list[int] = []
    for message in messages:
        if getattr(message, "type", None) == "tool":
            for match in _ALERT_NUMBER.finditer(message_text(message.content)):
                if int(match.group(1)) not in numbers:
                    numbers.append(int(match.group(1)))
    return numbers


def _build_agent(llm, tools: list, system: str):
    """A plain LangGraph tool-calling agent. Deep Agents was tried first and was too heavy
    for the 4B model on CPU (SPEC.md, Rules; HARNESS_CHANGELOG.md)."""
    from langchain.agents import create_agent
    from langchain.agents.middleware import ToolCallLimitMiddleware

    limit = ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS, exit_behavior="continue")
    return create_agent(model=llm, tools=tools, system_prompt=system, middleware=[limit])


def _structured(llm):
    return llm.with_structured_output(StepReview, method="json_schema")


async def _answer_as_json(llm, system: str, user: str, messages: list) -> StepReview:
    """The repair call: turn the tool results and the agent's last words into a StepReview.

    Uses the OpenAI `response_format` JSON schema, so the reply must parse. One retry.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    evidence = []
    for message in messages:
        text = message_text(message.content).strip()
        if message.type == "tool" and text:
            evidence.append(f"Tool result:\n{text[:TOOL_TEXT_CHARS]}")
        elif message.type == "ai" and text:
            evidence.append(f"Your notes:\n{text[:TOOL_TEXT_CHARS]}")
    prompt = [
        SystemMessage(system),
        HumanMessage(user + "\n\n" + "\n\n".join(evidence) + "\n\n" + ANSWER_NOW),
    ]
    error: Exception | None = None
    for _ in range(2):
        try:
            return await _structured(llm).ainvoke(prompt)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            error = exc
    raise ValueError(f"no valid StepReview after one repair retry: {error}")


def _parse_review(text: str) -> StepReview | None:
    """Accept a final message that is already a StepReview in JSON, with or without fences."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return StepReview.model_validate_json(match.group(0))
    except ValidationError:
        return None


def make_runner(llm, tools: list | None, system: str) -> StepRunner:
    """The real per-step call: an agent with tools, or one JSON call when tools are off."""
    if not tools:
        system = f"{system}\n\n{NO_TOOLS_NOTE}"

        async def run_without_tools(jsa: JSA, step: JSAStep, feedback: list[str]) -> StepResult:
            user = step_message(jsa, step, feedback)
            return StepResult(await _answer_as_json(llm, system, user, []))

        return run_without_tools

    agent = _build_agent(llm, tools, system)

    async def run_with_tools(jsa: JSA, step: JSAStep, feedback: list[str]) -> StepResult:
        user = step_message(jsa, step, feedback)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user}]},
            {"recursion_limit": RECURSION_LIMIT, "run_name": f"step {step.number}"},
        )
        messages = result["messages"]
        review = _parse_review(message_text(messages[-1].content))
        if review is None:
            review = await _answer_as_json(llm, system, user, messages[1:])
        return StepResult(review, tool_alert_numbers(messages))

    return run_with_tools


# --- The review loop ---------------------------------------------------------------------


def _normalise(review: StepReview, step: JSAStep) -> StepReview:
    """Pin the step numbers to the step that was asked, whatever the model wrote."""
    hazards = [hazard.model_copy(update={"step": step.number}) for hazard in review.missing_hazards]
    questions = [
        question.strip() for question in review.clarification_questions if question.strip()
    ]
    return StepReview(step=step.number, missing_hazards=hazards, clarification_questions=questions)


def assemble(jsa: JSA, harness: str, step_reviews: list[StepReview]) -> ReviewReport:
    """Merge step results, in step order, into one report."""
    ordered = sorted(step_reviews, key=lambda review: review.step)
    return ReviewReport(
        jsa_id=jsa.id,
        harness_version=harness,
        missing_hazards=[hazard for review in ordered for hazard in review.missing_hazards],
        clarification_questions=[
            f"Step {review.step}: {question}"
            for review in ordered
            for question in review.clarification_questions
        ],
        status="complete",
        open_issues=[],
    )


async def review_steps(
    jsa: JSA,
    harness: str,
    runner: StepRunner,
    steps: list[int] | None = None,
    feedback: dict[int, list[str]] | None = None,
    log: Callable[[str], None] = lambda line: None,
) -> ReviewRun:
    """Run the per-step calls in order. A step whose call fails is left out of reviewed_steps."""
    wanted = set(steps) if steps is not None else {step.number for step in jsa.steps}
    feedback = feedback or {}
    start = time.monotonic()
    reviews: list[StepReview] = []
    numbers: list[int] = []
    for step in jsa.steps:
        if step.number not in wanted:
            continue
        step_start = time.monotonic()
        try:
            result = await runner(jsa, step, feedback.get(step.number, []))
        except Exception as exc:  # a failed step is caught by R2 and re-run
            log(f"  step {step.number}: failed ({type(exc).__name__}: {exc})")
            continue
        reviews.append(_normalise(result.review, step))
        numbers += [number for number in result.tool_alert_numbers if number not in numbers]
        log(
            f"  step {step.number}: {len(result.review.missing_hazards)} missing hazards, "
            f"{time.monotonic() - step_start:.0f} s"
        )
    return ReviewRun(
        report=assemble(jsa, harness, reviews),
        step_reviews=reviews,
        reviewed_steps=[review.step for review in reviews],
        tool_alert_numbers=numbers,
        seconds=time.monotonic() - start,
    )


def review(
    jsa: JSA,
    *,
    harness: str,
    use_tools: bool,
    steps: list[int] | None = None,
    feedback: dict[int, list[str]] | None = None,
    config: Config | None = None,
    log: Callable[[str], None] = lambda line: None,
) -> ReviewRun:
    """Review a JSA (or only `steps` of it) with the local model. Starts the MCP server once."""
    config = config or load_config()
    system = load_harness(harness)
    llm = build_llm(config)

    async def run() -> ReviewRun:
        if not use_tools:
            return await review_steps(
                jsa, harness, make_runner(llm, None, system), steps, feedback, log
            )
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(
            {"bsee": {"transport": "stdio", "command": MCP_SERVER[0], "args": MCP_SERVER[1:]}}
        )
        async with client.session("bsee") as session:
            from langchain_mcp_adapters.tools import load_mcp_tools

            tools = await load_mcp_tools(session)
            runner = make_runner(llm, tools, system)
            return await review_steps(jsa, harness, runner, steps, feedback, log)

    return asyncio.run(run())
