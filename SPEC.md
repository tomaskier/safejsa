# SafeJSA: Local AI Reviewer for Offshore Job Safety Analyses — Spec

Nov 6, 2026 · @T

## Instructions for Claude Code

**Goal.** Build SafeJSA, a local-first agent that reviews offshore job safety analyses (JSAs) against BSEE Safety Alerts, and prove with an evaluation that each added loop (retrieval, verification, hill-climbing) changes seeded-hazard recall.

**How to use this spec**

- This spec lives at the repo root as `SPEC.md` (exported from this doc). `CLAUDE.md` holds a short summary of the goal, the commands and the rules below, and points here.
- The sections **Interfaces** and **Repository structure** are binding contracts. If a change is needed, update the spec first, then the code.
- Diagrams are illustrations. Where a diagram and the text differ, the text wins.
- Build in the order of **Build phases**. Finish each phase's acceptance checks before starting the next.

**Rules**

- Keep it simple: nine modules plus `__init__.py` in `src/safejsa/`, no sub-packages, no database, no Docker. Build only what the goals need; add a file or a feature only if this spec is updated first.
- Repo: `oil_gas/safejsa/`, a new git repository with this spec saved as `SPEC.md`, pushed to a public GitHub repository so CI runs on every push.
- Platform: native Windows 10 for development and runs. Use `pathlib` for all paths and avoid shell-specific commands, so the same code also passes CI on GitHub's Linux runner. Python 3.11 with uv; run everything through `uv run`; the console entry point is `safejsa`.
- All model calls go through one OpenAI-compatible client (`ChatOpenAI`) pointed at Ollama's endpoint in `llm.base_url`. No paid APIs.
- Agent design: code loops over the JSA steps and makes one short agent call per step; code then assembles the `ReviewReport`. Do not run the whole JSA as one long agent conversation.
- Agent framework: use LangChain Deep Agents for each per-step call first. If, in phase 3, the 4B model fails to call tools or to return a valid `StepReview` on 2 of the 3 fixture JSAs, switch the per-step call to a plain LangGraph tool-calling agent with the same tools, inputs and outputs, and record the switch in `HARNESS_CHANGELOG.md`.
- **Decided in phase 3 (Nov 12, 2026): the per-step call uses the plain LangGraph agent** (`langchain.agents.create_agent`). Deep Agents passed fixture 1 but was too heavy for the 4B model on CPU: its built-in instructions and tools filled most of Ollama's 4096-token context, and 4 steps took 44 minutes against 10.6 minutes for the plain agent, with equal quality. That put the overnight evaluation budget out of reach. The user chose the switch under the "too heavy" clause of **The four loops**, loop 1. See `HARNESS_CHANGELOG.md`.
- Read secrets only through `config.py`. Never commit `config.yaml`, `data/cache/` or `runs/`.
- Tests must not need a model server, the network or a real mailbox. Use the fakes in `tests/fakes.py`.
- The agent reaches BSEE data only through MCP: `agent.py` never imports `mcp_server.py`.
- Never invent evaluation numbers. The README results table is generated from `evals/results/*.json`.
- Claude Code may draft JSAs and their seeded gaps, but the user must review `answer_key.yaml` and set `approved: true` before any test-split scoring.
- Stop and ask the user if the BSEE site blocks crawling, the configured model is not served, or email credentials are absent.

**Definition of done**

- [ ] `safejsa ingest` writes `data/alerts.jsonl` from every alert reachable on the BSEE Safety Alerts pages, and prints any it could not parse.
- [ ] The MCP server exposes 2 tools, 1 resource and 1 prompt, and works in MCP Inspector.
- [ ] `safejsa review <file>` writes a `ReviewReport` that passes the code-checked rubric items R1, R2, R3 and R6.
- [ ] The mail worker completes a real round trip from an allow-listed sender: receipt, then reply.
- [ ] Configurations A to D are scored on the approved test split, and the README table is generated from the results.
- [ ] `HARNESS_CHANGELOG.md` records at least 2 harness versions after v1, each with dev scores.
- [ ] CI runs ruff and pytest, and both pass.

## Overview

SafeJSA is an AI agent that reviews offshore job safety analyses (JSAs). You email it a JSA; it replies with missing hazards, weak controls and clarification questions, each backed by a real BSEE Safety Alert. It runs entirely on a laptop with a local Qwen model, and it applies the four loops from Loop Engineering: agent, verification, event-driven (email) and hill-climbing.

**Problem.** A JSA breaks a job into steps and lists each step's hazards and controls. They are often written quickly from templates, and the hazard that causes an incident is frequently the one nobody wrote down. Regulators have published hundreds of offshore incident lessons, but nobody cross-checks every JSA against them.

**Goals**

- Review a JSA step by step and flag missing hazards, each citing at least one BSEE Safety Alert. This is the scored output.
- Add clarification questions for the JSA author. These are rubric-checked, not scored.
- Verify every review against an explicit rubric (R1 to R6) before it is sent.
- Trigger reviews by email and reply automatically.
- Measure how each loop changes seeded-hazard recall, with every run traced in LangSmith.

**Non-goals**

- No approval of work. SafeJSA is advisory and never replaces review by a competent person.
- No model training or fine-tuning.
- No real company JSAs. The test set is written for this project.

**Constraints**

- Native Windows 10 laptop with no dedicated GPU (designed for 16 GB RAM, CPU only): the agent runs a small local model through Ollama for Windows, so tool design stays simple and runs stay few. The development laptop (Intel Core Ultra 7 258V, 32 GB) runs the model on its integrated Intel Arc 140V GPU through Ollama's Vulkan backend (`OLLAMA_IGPU_ENABLE=1`), about 3 times faster per call than its CPU; everything also works on CPU only, just slower.
- Shortest path to a finished portfolio piece: about 3 weeks part-time.
- Target audience: AI engineering roles, so the README leads with MCP, the loops and the eval results.
- Released under the MIT license.

## Architecture

Five parts in a line: an emailed JSA reaches a mail worker, a local AI agent reviews it using BSEE alerts, a checker verifies the review, and the engineer gets the reply.

&#91;embedded content: SafeJSA at a glance · 5 parts, 2 feedback loops\]

The agent only reaches the alerts through the MCP tools. A failed check sends the review back for another attempt, and LangSmith traces from evaluation runs drive improvements to the agent. Everything runs on one laptop.

**The pipeline in text** (authoritative over the diagram)

1. The mail worker finds an unread email from an allow-listed sender, parses the attached Markdown JSA into the `JSA` model, emails a receipt and starts the review.
2. `agent.review()` loops over the JSA steps in code. For each step it makes one short agent call, which uses `search_alerts` and `get_alert` through the MCP server and returns a `StepReview`.
3. Code assembles the step results into a `ReviewReport` and wraps it in a `ReviewRun` with the reviewed steps, the alert numbers the tools returned and the elapsed time.
4. The verifier runs R1, R2, R3 and R6 in code, and R4 and R5 with the judge model. On failure it re-runs only the failing steps, with the failed items as feedback, up to 2 retries.
5. The report is rendered to Markdown and emailed back. After 2 failed retries it is sent with `status: partial` and the open issues listed.
6. Every run is traced in LangSmith.
7. Offline, `safejsa eval` and `safejsa improve` drive the hill-climbing loop.

## User flow

The engineer only ever touches email: send a JSA, then read the checked review.

&#91;embedded content: User flow · 5 lanes, 1 decision\]

If the review still fails after 2 retries, it is sent anyway, marked "partial review" with the open issues listed. Improvement happens offline from evaluation runs, not from email replies.

## Data

Two datasets: a public knowledge base of offshore incident lessons, and a small test set of JSAs you write with known gaps.

### Knowledge base: BSEE Safety Alerts

[BSEE Safety Alerts](https://www.bsee.gov/guidance-and-regulations/guidance/safety-alerts-program) are short notices from the US Bureau of Safety and Environmental Enforcement. Each describes an offshore incident or near miss and gives recommendations to prevent it. The numbered series runs past 520 alerts, and each is tagged with categories such as Cranes/Lifting, H2S, Fire, Electrical, Personnel Safety and Well Operations.

Ingestion steps:

1. Crawl the alert listing pages politely (rate-limited, cached in `data/cache/`) and download each alert PDF.
2. Keep only numbered Safety Alerts, so the alert number stays a unique integer key (see **Alert record**). The listing also holds Safety Bulletins, Joint Safety Alerts and non-safety notices such as scam alerts, and older regional series reuse numbers. Ingest therefore skips bulletins, joint alerts and scam notices, merges entries that repeat the same number and title, and skips every number that names more than one different alert. Skipped entries are printed with the reason.
3. Extract text with pypdf and keep metadata: number, title, date, BSEE category tags, URL.
4. Split each alert's text into summary and recommendations, and map its tags to taxonomy IDs. Alerts with no tags, which covers most alerts numbered below 300, get `other`.
5. Write one JSON line per alert to `data/alerts.jsonl`. The MCP server builds a BM25 index from this file at start-up.
6. Vector search is left out of version 1. It is a candidate change for the hill-climbing loop if keyword search misses too much.

### Test set: seeded JSAs

You write 24 JSAs for common offshore jobs, then remove known hazards from each to create ground truth. Writing them yourself (an LLM may help draft, but you edit and own the answer key) keeps the test honest.

| Job type | Example seeded gap |
| --- | --- |
| Crane lift to supply vessel | No tag line or red-zone control |
| Hot work near process piping | No gas test before and during work |
| Work in an H2S-classified area | No personal gas monitor or escape route |
| Electrical maintenance | No voltage verification before work |
| Pressure test of piping | No exclusion zone or bleed-down step |
| Working at height on grating | Missing check for open holes and grating |

- Each JSA follows the Markdown template in **Interfaces**, with 4 to 8 steps.
- 20 JSAs carry 1 to 3 seeded gaps each; 4 are "clean" with no gaps, to measure false alarms.
- Gaps are recorded in `data/jsas/answer_key.yaml` (schema in **Interfaces**): step, taxonomy category, hazard text and a reference BSEE alert.
- Split: 12 dev (10 seeded + 2 clean) for tuning and hill-climbing; 12 test (10 seeded + 2 clean), scored once per harness version.

## MCP server: `bsee-alerts-mcp`

One FastMCP server makes the BSEE knowledge base usable by any MCP host: the SafeJSA agent, MCP Inspector, or Claude Desktop for a quick demo. It has few tools with flat, simple arguments, because small local models call tools more reliably that way.

**Tools**

| Tool | Arguments | Returns |
| --- | --- | --- |
| `search_alerts` | `query` (text), `category` (optional), `k` (default 5) | Alert number, title, date, categories and the best-matching snippet, from BM25 search |
| `get_alert` | `number` | Full alert text, recommendations and source URL |

**Resources**

- `bsee://checklist`: the 10 hazard categories from the taxonomy, each with a one-line description and its alert count.

**Prompts**

- `review_jsa`: the standard review brief (what to check for each step and the output format). Lets any MCP host, such as Claude Desktop, run the same review.

**Running it**

- stdio only: the agent starts `safejsa mcp` as a subprocess, so there is nothing else to run. MCP Inspector connects the same way.

## Models and configuration

Everything runs locally through Ollama for Windows, on CPU or an integrated GPU (see **Constraints**). The code uses one OpenAI-compatible chat client (`langchain-openai`'s `ChatOpenAI` with `base_url` set to Ollama's `/v1` endpoint). With 16 GB of RAM the default model is Qwen 3.5 4B.

| Job | Default model | Notes |
| --- | --- | --- |
| Review agent (loop 1) | `qwen3.5:4b` | Fits comfortably in 16 GB at 4-bit; try the 9B once and keep it if speed is acceptable |
| Verifier judge (loop 2) | Same model, separate prompt | Most rubric items are checked in code, so the judge only handles R4 and R5 |

vLLM is not used. Its CPU backend runs only on Linux, and this project runs on native Windows. Because the client is OpenAI-compatible, a different server could be swapped in later by changing `llm.base_url` alone.

**Making a small model reliable**

- Two tools with flat arguments and short descriptions.
- Structured output through the OpenAI `response_format` JSON schema on Ollama's endpoint, plus one repair-and-retry if parsing fails.
- Low temperature (0.2) and a hard cap on tool calls per step.
- Thinking mode off (`llm.reasoning_effort: none`, sent as the OpenAI `reasoning_effort` parameter). With it on, Qwen 3.5 4B took 28 minutes for one 31-token answer on the development laptop; with it off, 5 seconds.
- One short agent call per JSA step, so each prompt stays small and fits a modest context window.

**`config.yaml`**

```yaml
# config.yaml: local only, listed in .gitignore. Copy from config.example.yaml.
llm:
  base_url: http://localhost:11434/v1   # Ollama for Windows
  api_key: local                        # any string; Ollama ignores it
  model: qwen3.5:4b                     # confirm the exact tag with `ollama list`
  temperature: 0.2
  reasoning_effort: none                # turns off Qwen's thinking mode; it makes CPU calls take minutes
harness: v1                             # used by safejsa review and the mail worker
best_harness: v1                        # used by eval config D; update after hill-climbing
email:
  imap_host: imap.gmail.com
  smtp_host: smtp.gmail.com
  address: "your-demo-account@gmail.com"
  app_password: "..."                   # Gmail app password, never your main password
  allowed_senders: ["you@..."]
  poll_seconds: 120
langsmith:
  api_key: "..."
  project: safejsa
```

- `config.yaml` is git-ignored; the repo ships `config.example.yaml` with placeholders.
- Environment variables override file values. Secrets are never logged or written to traces.

## Interfaces

These contracts are binding. Code, tests and evaluation all depend on them; change this section first if one must change.

### JSA input format

The only input format is Markdown with front matter and one table. `models.parse_jsa()` reads it into the `JSA` model and rejects anything else with a clear error.

```markdown
---
id: dev-01
title: Crane lift of a container to a supply vessel
---
| # | Step | Hazards | Controls |
|---|------|---------|----------|
| 1 | Hold pre-lift meeting | Miscommunication | Lift plan reviewed with crew |
| 2 | Rig the container | Pinch points; dropped load | Certified slings; gloves |
```

Multiple hazards or controls in one cell are separated by semicolons.

### Hazard taxonomy (`data/taxonomy.yaml`)

Ten category IDs are used everywhere: in the answer key, in `ReviewReport` and in scoring. Ingest maps each BSEE category tag to one or more IDs. Unmapped tags go to `other` and are listed in an ingest report for the user to map.

| ID | Covers (BSEE category tags) |
| --- | --- |
| `lifting` | Cranes/Lifting, Critical Lifts, Tag Lines |
| `hot_work_fire` | Fire, Explosion, welding and burning |
| `gas_h2s` | H2S, Gas Release, Chemical Exposure |
| `electrical` | Electrical, Arc Flash, Power Outage |
| `pressure_energy` | Process Safety, pressure release, stored energy |
| `falls_openings` | Grating/Open Holes, falls, fall protection |
| `hand_injury` | Hand Injuries, PPE, hand tools |
| `equipment_failure` | Component Failures, Equipment, Manufacturer Deficiency |
| `marine_weather_aviation` | Weather, Aviation, vessel and helideck operations |
| `well_operations` | Well Operations, Well Control, BOP/LMRP, Pipe Handling |

### Data models (`src/safejsa/models.py`, Pydantic v2)

```python
HazardCategory = Literal["lifting", "hot_work_fire", "gas_h2s", "electrical",
    "pressure_energy", "falls_openings", "hand_injury", "equipment_failure",
    "marine_weather_aviation", "well_operations", "other"]

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
    why: str                     # one sentence, in the agent's own words
    alert_numbers: list[int] = Field(min_length=1)

class StepReview(BaseModel):     # returned by one per-step agent call
    step: int
    missing_hazards: list[MissingHazard]
    clarification_questions: list[str]

class ReviewReport(BaseModel):   # assembled in code from the StepReviews
    jsa_id: str
    harness_version: str
    missing_hazards: list[MissingHazard]
    clarification_questions: list[str]
    status: Literal["complete", "partial"]
    open_issues: list[str]       # failed rubric items when status is partial

class ReviewRun(BaseModel):      # returned by agent.review(); used by verify.py and evals.py
    report: ReviewReport
    step_reviews: list[StepReview] # per-step results, so a failed check can re-run just that step
    reviewed_steps: list[int]    # for R2
    tool_alert_numbers: list[int]  # every alert number any tool returned in this run, for R1
    seconds: float               # measured locally
```

Config A uses the same schema. With tools off, its cited alert numbers come from the model's memory; the citation-validity metric (see **Evaluation**) checks whether they are real and relevant.

The advisory notice is fixed text added by `models.report_to_markdown()`, not generated by the model.

### Alert record (`data/alerts.jsonl`, `Alert` in `models.py`)

One JSON object per line, written by `ingest.py` and read by `mcp_server.py` and `verify.py`.

```python
class Alert(BaseModel):
    alert_number: int            # unique in the file
    title: str
    date: str | None             # ISO date from the listing; None when BSEE gives none
    bsee_tags: list[str]         # category tags exactly as BSEE lists them
    categories: list[HazardCategory] = Field(min_length=1)  # ["other"] when nothing maps
    summary: str                 # alert text before the recommendations
    recommendations: str         # empty when the alert has no recommendations section
    url: str                     # the alert's page on bsee.gov
    pdf_url: str
```

### Answer key (`data/jsas/answer_key.yaml`)

```yaml
approved: false                  # the user sets true after review
jsas:
  dev-01:
    clean: false
    seeded_gaps:
      - step: 2
        category: lifting
        hazard: No tag line or red-zone control while the load is landed
        reference_alert: 468
  dev-11:
    clean: true
    seeded_gaps: []
```

### MCP tool contracts

| Tool | Input | Output (JSON) |
| --- | --- | --- |
| `search_alerts` | `query: str`, `category: HazardCategory \| None = None`, `k: int = 5` (max 10) | list of `{alert_number, title, date, categories, snippet}` |
| `get_alert` | `number: int` | `{alert_number, title, date, categories, summary, recommendations, url}`, or an error if unknown |

Resource: `bsee://checklist`. Prompt: `review_jsa(jsa_markdown)`.

### Harness and ablation configurations

Each harness version is a folder `harness/vN/` holding `system.md` and an optional `skills/` folder. Version 1 has `system.md` only; hill-climbing adds a skill file when one hazard category keeps being missed. A new version copies the previous folder and changes one thing; `config.yaml` selects the version.

| Config | MCP tools | Verification loop | Harness |
| --- | --- | --- | --- |
| A | off | off | v1 |
| B | on | off | v1 |
| C | on | on | v1 |
| D | on | on | `best_harness` from `config.yaml` |

This table applies to `safejsa eval` only. `safejsa review` and `safejsa mail` use the `harness` key from `config.yaml`; for `review`, `--config` sets only the tools and the verification loop (default C), and `mail` always runs as C.

### Command-line interface

| Command | Does |
| --- | --- |
| `safejsa ingest` | Crawl BSEE alerts and write `data/alerts.jsonl` |
| `safejsa mcp` | Run the MCP server over stdio (the agent starts it automatically) |
| `safejsa review PATH [--config A\|B\|C\|D]` | Review one JSA (default C, harness from `harness`); write `runs/<timestamp>/report.json` and `report.md` |
| `safejsa mail` | Run the email worker (behaves as C) |
| `safejsa eval --split dev\|test --config A\|B\|C\|D [--repeats N]` | Score a split N times (default 1); write `evals/results/<split>-<config>-<harness>-r<N>.json` per run; regenerate the README table with the mean and spread |
| `safejsa improve` | Count dev failures by rubric item and category; write `evals/reports/<date>.md` |

## The four loops

This is the course's requirements-review agent applied to safety: a JSA replaces the software spec, and each loop can be switched off for the ablation study.

### 1. Agent loop: review the JSA

- Code loops over the JSA steps. Each step is one short call to a plain LangGraph tool-calling agent (`create_agent`; Deep Agents was tried first, see **Rules**) on `ChatOpenAI` (pointed at Ollama), with MCP tools loaded through `langchain-mcp-adapters`, which starts `safejsa mcp` over stdio once per review.
- Each call returns a `StepReview`; code merges them into the `ReviewReport`. From v2 on, a call may also load skill files added by hill-climbing.
- If Deep Agents proves too heavy for the 4B model, the per-step call switches to a plain LangGraph agent (see **Rules**). The loop and the outputs do not change. This switch was made in phase 3.
- If the agent's last message is not a valid `StepReview`, one repair call with the `response_format` JSON schema turns its tool results and notes into one, with one retry. Config A (tools off) makes that JSON call directly.

### 2. Verification loop: check before replying

| ID | Criterion | Checked by |
| --- | --- | --- |
| R1 | Every cited alert number is in `ReviewRun.tool_alert_numbers` and exists in `alerts.jsonl` | Code |
| R2 | `ReviewRun.reviewed_steps` covers every JSA step | Code |
| R3 | No flagged "missing" hazard is already listed in the JSA (`difflib` ratio of 0.6 or more against that step's hazards) | Code |
| R4 | The cited alert actually supports the hazard | Judge |
| R5 | Clarification questions are specific and answerable | Judge |
| R6 | The rendered reply includes the advisory notice and does not approve the work | Code |

A failed review goes back with the failed items. After 2 retries (kept low because CPU runs are slow) the reply is sent marked "partial review" with the open issues listed.

### 3. Event-driven loop: email in, email out

1. `safejsa mail` checks the mailbox over IMAP every `email.poll_seconds` (default 120) for unread messages.
2. It accepts only messages from `email.allowed_senders` with one `.md` attachment. Anything else is marked read and logged.
3. It replies at once with "received, review in progress", then reviews the JSAs one at a time. The inbox's unread state is the queue.
4. The agent and verifier run; the reply goes out by SMTP with the report in the body and attached as Markdown.

Email suits slow CPU inference: nobody waits on a screen for a reply that takes a few minutes.

### 4. Hill-climbing loop: improve across runs

1. Run the dev set with `safejsa eval --split dev`; each run is a LangSmith experiment with the answer key and rubric checks as evaluators.
2. `safejsa improve` counts failures by rubric item and hazard category, and lists the seeded hazards most often missed.
3. You read the report and pick one change: edit `system.md`, add a skill file for the most-missed category, or change a tool description. Copy `harness/vN/` to `harness/vN+1/` and make that one change.
4. Keep the version only if dev recall rises and no rubric pass rate drops. Score the test set once per kept version.
5. Log each version in `HARNESS_CHANGELOG.md` with the change, the failure it targeted and its scores.

## Evaluation

The headline result is how many seeded hazards the agent catches, and how that changes as each loop is added.

**Metrics**

- **Seeded-hazard recall** (headline): a seeded gap counts as caught when a `missing_hazards` item has the same step number and the same taxonomy category.
- **False alarms:** missing hazards flagged on the 4 clean JSAs.
- **Flags per JSA:** the average number of missing hazards flagged, so recall cannot rise just by flagging everything.
- **Citation validity:** share of citations where the alert number exists in `alerts.jsonl` and the R4 judge agrees it supports the hazard. The same test applies to every configuration, so A (memory) and B (retrieval) compare fairly. `evals.py` runs it once after each review, with no retries. R1 (tool-returned numbers) stays a rule inside the verification loop only.
- **First-attempt rubric pass rate.**
- **Minutes per review:** `ReviewRun.seconds`, timed locally and saved in the results JSON, so scoring works offline.

**Ablation table (filled in as results come in)**

| Configuration | Recall | False alarms (clean) | Flags per JSA | Citation validity | Minutes per review |
| --- | --- | --- | --- | --- | --- |
| A. Agent, no tools (model knowledge only) | TBD | TBD | TBD | TBD | TBD |
| B. Agent + MCP retrieval | TBD | TBD | TBD | TBD | TBD |
| C. B + verification loop | TBD | TBD | TBD | TBD | TBD |
| D. C + hill-climbing (`best_harness`) | TBD | TBD | TBD | TBD | TBD |

**Run budget on CPU**

- In week 1, time one review end to end. Size the eval so a full test pass (4 configurations × 12 JSAs) fits in one overnight run.
- Repeat each configuration 2 to 3 times if time allows, and report the spread; otherwise state that results are single runs.
- Use a 3-JSA smoke set for quick checks while developing.

## Repository structure

Nine Python modules in one package, one file per job. No sub-packages, no Docker, no database: Ollama runs natively, the agent starts the MCP server itself over stdio, and state lives in plain files.

```
safejsa/
├── SPEC.md                  # this spec, exported as Markdown
├── CLAUDE.md                # goal, commands and rules in brief; points to SPEC.md
├── README.md                # pitch, diagram, results table (generated)
├── HARNESS_CHANGELOG.md     # one entry per harness version
├── LICENSE                  # MIT
├── pyproject.toml           # uv project; console script `safejsa`
├── uv.lock                  # committed; CI installs with `uv sync --locked`
├── .python-version          # 3.11, read by uv locally and in CI
├── config.example.yaml      # copy to config.yaml (git-ignored)
├── .gitignore               # config.yaml, data/cache/, runs/
├── .github/workflows/ci.yml # ruff + pytest
├── harness/
│   └── v1/
│       └── system.md        # agent instructions; skills/ is added by hill-climbing
├── data/
│   ├── taxonomy.yaml        # 10 hazard IDs + BSEE tag mapping
│   ├── alerts.jsonl         # committed: one BSEE alert per line
│   ├── cache/               # git-ignored: downloaded pages and PDFs
│   └── jsas/
│       ├── dev/             # dev-01.md … dev-12.md
│       ├── test/            # test-01.md … test-12.md
│       └── answer_key.yaml
├── src/safejsa/
│   ├── cli.py               # Typer commands; thin wrappers over the modules below
│   ├── config.py            # load config.yaml, env vars override
│   ├── models.py            # Pydantic contracts, parse_jsa(), report_to_markdown()
│   ├── ingest.py            # crawl BSEE pages, extract PDF text, write alerts.jsonl
│   ├── mcp_server.py        # BM25 search over alerts.jsonl + FastMCP tools, resource, prompt
│   ├── agent.py             # build the per-step agent (ChatOpenAI + MCP over stdio), run a review
│   ├── verify.py            # rubric rules, judge, review-verify-retry loop
│   ├── mail.py              # IMAP poll, allowlist, receipt, reply
│   └── evals.py             # scoring, eval runs A–D, failure counts, README table
├── evals/
│   ├── results/             # committed results JSON
│   └── reports/             # failure counts from safejsa improve
├── runs/                    # git-ignored: report.json + report.md per review
└── tests/
    ├── fakes.py             # FakeLLM with scripted outputs, in-memory mailbox
    ├── fixtures/            # 3 small JSAs, 5 sample alerts, a sample answer key
    ├── test_config.py       # file loading, defaults and env overrides
    ├── test_models.py       # parsing and Markdown rendering
    ├── test_ingest.py       # listing parsing, alert selection, text split, tag mapping
    ├── test_mcp_server.py   # search and tool contracts
    ├── test_verify.py       # each code rule passes and fails
    └── test_evals.py        # scores match hand-computed values
```

**Dependency rules**

- `models.py` and `config.py` import nothing else from the package.
- `agent.py` reaches alerts only through MCP. It never imports `mcp_server.py`.
- `verify.py` may read `data/alerts.jsonl` to check that a cited alert exists (R1) and to give the judge the cited alert's text (R4).
- `cli.py` holds no logic; each command calls one function in another module.

**Stack**

| Area | Choice |
| --- | --- |
| Platform | Native Windows 10 (development); GitHub Actions Linux runner (CI) |
| Language and tooling | Python 3.11+, uv, ruff, pytest, Typer |
| Model server | Ollama for Windows, through its OpenAI-compatible API |
| Search | rank-bm25 over `alerts.jsonl`; pypdf for extraction |
| MCP | FastMCP over stdio (from the `mcp` SDK, pinned below 2.0: version 2 renamed FastMCP, and langchain-mcp-adapters requires `mcp<2`), MCP Inspector |
| Agent | LangGraph agent via `langchain.agents.create_agent` (Deep Agents tried first, too heavy on CPU), langchain-openai, langchain-mcp-adapters |
| Tracing and evals | LangSmith |
| Email | imap-tools, smtplib |

## Build phases

Each phase ends with checks Claude Code can run. Do not start a phase until the previous one passes.

| Phase | Build | Done when |
| --- | --- | --- |
| 0. Scaffold | Repo `oil_gas/safejsa/` with `SPEC.md`, `pyproject.toml`, `config.py`, `models.py`, `taxonomy.yaml`, `cli.py`, CI | Repo pushed to GitHub; `uv run safejsa --help` lists all commands; `uv run pytest` and `uv run ruff check` pass locally and in CI |
| 1. Knowledge base | `ingest.py` | `safejsa ingest` writes `data/alerts.jsonl` and prints alerts it could not parse and unmapped tags |
| 2. MCP server | `mcp_server.py` | `test_mcp_server.py` passes, including: "tag line crane" returns a `lifting` alert in the top 5; MCP Inspector shows 2 tools, 1 resource, 1 prompt |
| 3. Agent and verification | `agent.py`, `verify.py`, `harness/v1/system.md` | `safejsa review tests/fixtures/<jsa>.md --config C` writes a report passing R1, R2, R3, R6; the run appears in LangSmith; review time is printed; the Deep Agents fallback rule is applied if needed |
| 4. Test set and scoring | 24 JSAs + `answer_key.yaml` (user approves), `evals.py` | `test_evals.py` passes; `safejsa eval --split dev --config B` writes a results file |
| 5. Email loop | `mail.py` | One real round trip with the user's Gmail: receipt, then reply |
| 6. Improve and publish | harness v2+, README | At least 2 harness versions in `HARNESS_CHANGELOG.md`; `best_harness` set; configs A–D scored on test; README table generated |

Schedule: week 1 covers phases 0 to 2, week 2 phases 3 and 4, week 3 phases 5 and 6.

## Risks and responsible use

| Risk | Mitigation |
| --- | --- |
| A 4B model misses tool calls or breaks the output format | Two flat tools, schema-constrained output, one repair retry, one step per prompt; try the 9B model once |
| Slow CPU runs limit evaluation | Small test set, overnight eval runs, email as the interface |
| Prompt injection through emailed documents | Sender allowlist; attachment text treated as data only; the only side effect is a reply to the sender |
| The judge is the same model as the agent | Four of six rubric items are checked in code; the judge handles only R4 and R5 |
| Answer key bias (you wrote both JSAs and keys) | Seed gaps from real BSEE incident types; publish the answer key so others can audit it |
| BSEE site layout changes | Cache downloads; commit `alerts.jsonl` so the project still runs without re-crawling |

**Responsible use**

- Every reply states that SafeJSA is advisory and does not replace review by a competent person.
- The README says the project uses only public BSEE material and synthetic JSAs, and cites BSEE as the source.

All earlier open questions are now decided: native Windows 10, Ollama only, repo at `oil_gas/safejsa/` on GitHub, Gmail, Deep Agents with a LangGraph fallback, MIT license.

**Set-up you do before phase 0**

- [ ] Install Git, and the GitHub CLI with the Windows `.msi` installer from https://cli.github.com (this machine has no `winget`).
- [ ] Log in with `gh auth login` (browser), then run `gh auth setup-git`. Claude Code then creates the public `safejsa` repo with `gh repo create`.

**Set-up you do before phase 3**

- [ ] Install Ollama for Windows, pull the Qwen 3.5 4B model, and confirm its exact tag with `ollama list`.
- [ ] Create a LangSmith API key.
- [ ] Optional, for an Intel integrated GPU: set the user environment variable `OLLAMA_IGPU_ENABLE=1` (Vulkan is on by default in recent Ollama) and restart Ollama; `ollama ps` then shows `100% GPU`.

**Set-up you do before phases 4 and 5**

- [ ] Set aside about 2 hours in week 2 to review the 24 JSAs and approve `answer_key.yaml`.
- [ ] Create a new Gmail account for the demo, turn on 2-Step Verification, create an app password and put it in `config.yaml`.

## Sources

- [BSEE Safety Alerts Program](https://www.bsee.gov/guidance-and-regulations/guidance/safety-alerts-program)
- [Loop Engineering with LangChain Deep Agents (DeepLearning.AI)](https://www.deeplearning.ai/courses/loop-engineering-with-langchain-deep-agents)
- [MCP: Build Rich-Context AI Apps with Anthropic (DeepLearning.AI)](https://www.deeplearning.ai/courses/mcp-build-rich-context-ai-apps-with-anthropic)
