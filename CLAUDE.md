# SafeJSA

Local-first agent that reviews offshore JSAs against BSEE Safety Alerts, with an evaluation
showing how each loop (retrieval, verification, hill-climbing) changes seeded-hazard
recall. **SPEC.md is the source of truth**; its Interfaces and Repository structure
sections are binding. Change the spec first, then the code. Build in the order of its
Build phases, and finish each phase's checks before starting the next.

## Commands

```
uv sync                      # install
uv run safejsa --help        # CLI: ingest, mcp, review, mail, eval, improve
uv run pytest                # tests (no model server, network or mailbox needed)
uv run ruff check            # lint
```

## Rules

- Nine modules plus `__init__.py` in `src/safejsa/`; no sub-packages, database or Docker.
  No new file or feature unless SPEC.md is updated first.
- Native Windows 10 for development, Linux runner in CI: use `pathlib`, no shell-specific
  commands. Python 3.11, everything through `uv run`.
- All model calls go through one `ChatOpenAI` client pointed at Ollama (`llm.base_url`).
  No paid APIs.
- One short agent call per JSA step; code assembles the `ReviewReport`.
- The per-step call is a plain LangGraph agent (`create_agent`). Deep Agents was tried first
  and was too heavy for the 4B model on CPU (SPEC.md, Rules; HARNESS_CHANGELOG.md).
- Ollama calls need `reasoning_effort: none`; with thinking on, one call takes minutes.
- Secrets only through `config.py`. Never commit `config.yaml`, `data/cache/` or `runs/`.
- Tests use the fakes in `tests/fakes.py`; no model server, network or real mailbox.
- `agent.py` never imports `mcp_server.py`; it reaches BSEE data only through MCP.
- `models.py` and `config.py` import nothing else from the package; `cli.py` holds no logic.
- Never invent evaluation numbers. The README table is generated from `evals/results/`.
- No test-split scoring until the user sets `approved: true` in `answer_key.yaml`.
- Stop and ask the user if BSEE blocks crawling, the model is not served, or email
  credentials are missing.
