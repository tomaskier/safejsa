# Harness changelog

One entry per harness version: the change, the failure it targeted, and its dev (and, for
kept versions, test) scores. Scores come from `evals/results/` only.

## v1

`harness/v1/system.md` only: one step at a time, 1 to 3 `search_alerts` queries, optional
`get_alert`, at most 3 missing hazards and 2 questions, cite only tool-returned alerts,
never approve the work.

Dev scores: not measured yet (build phase 4).

### Agent framework switch (phase 3, Nov 12, 2026)

The per-step call moved from LangChain Deep Agents to a plain LangGraph tool-calling agent
(`langchain.agents.create_agent`) with the same tools, inputs and outputs. The user chose
the switch under the spec's "too heavy for the 4B model" clause; the literal 2-of-3 rule
was not reached, because Deep Agents passed the one fixture it was run on.

Measured on `tests/fixtures/crane_lift.md`, config C, `qwen3.5:4b` on CPU (Intel Core
Ultra 7 258V), first attempt of 4 steps. These are development timings, not eval scores.

| | Deep Agents | Plain LangGraph |
| --- | --- | --- |
| Time for 4 steps | 44 min (725, 67, 1675, 186 s) | 10.6 min (95, 272, 122, 149 s) |
| Prompt per call | 3,500 to 6,000 tokens | about 1,000 to 2,500 tokens |
| Tools called, citations valid | yes | yes |
| Found the red-zone gap (alert 468) | yes | yes |

Earlier single-step probes with Deep Agents: with the native JSON schema on every call the
model never called a tool and cited invented alerts (123, 124, 125). With the output as a
tool, the second call hit Ollama's 4096-token context and returned prose.
