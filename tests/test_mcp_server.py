import json
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from safejsa.mcp_server import (
    MAX_K,
    SNIPPET_CHARS,
    AlertIndex,
    build_server,
    load_taxonomy,
    tokenize,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SEARCH_KEYS = {"alert_number", "title", "date", "categories", "snippet"}
ALERT_KEYS = {"alert_number", "title", "date", "categories", "summary", "recommendations", "url"}


@pytest.fixture(scope="module")
def index():
    return AlertIndex.from_file(FIXTURES / "alerts.jsonl")


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy(ROOT / "data" / "taxonomy.yaml")


# --- Tokens and search -------------------------------------------------------------------


def test_tokenize_folds_plurals_drops_stopwords_and_joins_neighbours():
    assert tokenize("The tag lines") == ["tag", "line", "tagline"]
    assert tokenize("Taglines") == ["tagline"]
    assert tokenize("glass") == ["glass"]


def test_search_finds_lifting_alert_for_tag_line_crane(index):
    hits = index.search("tag line crane")
    assert hits[0]["alert_number"] in {362, 461}
    assert "lifting" in hits[0]["categories"]


def test_search_result_contract(index):
    for hit in index.search("tag line crane", k=10):
        assert set(hit) == SEARCH_KEYS
        assert isinstance(hit["alert_number"], int)
        assert len(hit["snippet"]) <= SNIPPET_CHARS


def test_search_category_filter(index):
    hits = index.search("gas", category="gas_h2s")
    assert hits and all("gas_h2s" in hit["categories"] for hit in hits)
    assert index.search("tagline", category="lifting")
    assert index.search("tagline", category="electrical") == []


def test_search_clamps_k_and_rejects_empty_queries(index):
    assert len(index.search("the alert", k=1)) <= 1
    assert len(index.search("incident", k=0)) == 1
    assert len(index.search("incident", k=50)) <= MAX_K
    with pytest.raises(ValueError):
        index.search("the and of")


def test_get_alert_contract_and_unknown_number(index):
    alert = index.get(464)
    assert set(alert) == ALERT_KEYS
    assert alert["url"].startswith("https://www.bsee.gov/")
    with pytest.raises(ValueError, match="unknown alert number 999"):
        index.get(999)


# --- Over MCP, as a host such as MCP Inspector sees it -----------------------------------


def _with_session(server, body):
    async def run():
        async with create_connected_server_and_client_session(server) as session:
            return await body(session)

    return anyio.run(run)


def test_server_exposes_two_tools_one_resource_one_prompt(index, taxonomy):
    async def body(session):
        tools = (await session.list_tools()).tools
        resources = (await session.list_resources()).resources
        prompts = (await session.list_prompts()).prompts
        return tools, resources, prompts

    tools, resources, prompts = _with_session(build_server(index, taxonomy), body)
    assert sorted(tool.name for tool in tools) == ["get_alert", "search_alerts"]
    search = next(tool for tool in tools if tool.name == "search_alerts")
    assert search.inputSchema["required"] == ["query"]
    assert set(search.inputSchema["properties"]) == {"query", "category", "k"}
    assert [str(resource.uri) for resource in resources] == ["bsee://checklist"]
    assert [prompt.name for prompt in prompts] == ["review_jsa"]
    assert [argument.name for argument in prompts[0].arguments] == ["jsa_markdown"]


def test_tools_over_mcp(index, taxonomy):
    async def body(session):
        search = await session.call_tool("search_alerts", {"query": "tag line crane", "k": 2})
        alert = await session.call_tool("get_alert", {"number": 362})
        missing = await session.call_tool("get_alert", {"number": 999})
        return search, alert, missing

    search, alert, missing = _with_session(build_server(index, taxonomy), body)
    assert not search.isError
    hits = [json.loads(block.text) for block in search.content]
    assert len(hits) == 2 and set(hits[0]) == SEARCH_KEYS
    assert json.loads(alert.content[0].text)["alert_number"] == 362
    assert missing.isError
    assert "unknown alert number 999" in missing.content[0].text


def test_checklist_and_prompt_over_mcp(index, taxonomy):
    jsa = (FIXTURES / "crane_lift.md").read_text(encoding="utf-8")

    async def body(session):
        checklist = await session.read_resource("bsee://checklist")
        prompt = await session.get_prompt("review_jsa", {"jsa_markdown": jsa})
        return checklist, prompt

    checklist, prompt = _with_session(build_server(index, taxonomy), body)
    text = checklist.contents[0].text
    for category_id in taxonomy["categories"]:
        assert f"`{category_id}`" in text
    assert "`lifting`: Crane operations" in text and "(2 alerts)" in text
    brief = prompt.messages[0].content.text
    assert "search_alerts" in brief and "Do not approve the work" in brief
    assert jsa.strip() in brief


# --- The committed knowledge base --------------------------------------------------------


def test_real_alerts_tag_line_crane_returns_lifting_in_top_5():
    hits = AlertIndex.from_file(ROOT / "data" / "alerts.jsonl").search("tag line crane")
    assert any("lifting" in hit["categories"] for hit in hits[:5])
