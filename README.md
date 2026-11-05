# SafeJSA

A local AI agent that reviews offshore job safety analyses (JSAs) against public
[BSEE Safety Alerts](https://www.bsee.gov/guidance-and-regulations/guidance/safety-alerts-program).
You email it a JSA; it replies with missing hazards and clarification questions, each
backed by a real BSEE alert. It runs on a CPU laptop with a small local Qwen model through
Ollama, reaches the alerts through its own MCP server, checks every review against a
rubric before sending it, and measures how each added loop changes seeded-hazard recall.

> **Status:** under construction. Build phase 0 (scaffold) is done. See [SPEC.md](SPEC.md)
> for the full design and build plan.

## Results

The table below is generated from `evals/results/*.json` by `safejsa eval`. It stays empty
until the evaluation runs.

<!-- results:start -->
_No results yet._
<!-- results:end -->

## Quick start

Requires Windows or Linux, [uv](https://docs.astral.sh/uv/) and, from build phase 3 on,
[Ollama](https://ollama.com).

```
uv sync
copy config.example.yaml config.yaml   # then fill in your values
uv run safejsa --help
uv run pytest
```

## Data and responsible use

SafeJSA uses only public BSEE Safety Alerts (source: US Bureau of Safety and Environmental
Enforcement) and JSAs written for this project. It is advisory: it never approves work and
does not replace review by a competent person.

## License

MIT. See [LICENSE](LICENSE).
