"""What the server advertises in `tools/list`.

The schema is the contract a client validates against, so the parts that
protect the caller -- bounds on every numeric argument, an enum where
the set of valid values is closed -- are asserted here rather than left
to the model's good judgement.

`structured_output=False` on every tool is asserted too: a tool that
returns `str` publishes an output schema and sends its payload twice,
once as text and once as structured content, which doubles the bytes on
the wire for a 20k-character page fetch.
"""

from __future__ import annotations

import anyio
import httpx2
import pytest
from mcp import Client

import server

TOOLS = {
    "search_web",
    "webfetch",
    "fetch_pdf",
    "fetch_rss",
    "fetch_wayback",
    "get_time",
    "convert_time",
}


@pytest.fixture
def advertised() -> dict:
    """The tools/list result, fetched once per test through a real client."""

    async def _list():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    listing = anyio.run(_list)
    return {tool.name: tool for tool in listing.tools}


def test_every_tool_is_advertised(advertised: dict) -> None:
    assert set(advertised) == TOOLS


# --- output ----------------------------------------------------------


@pytest.mark.anyio
async def test_tool_results_carry_no_structured_content(
    client, mock_http, searxng, dead_wayback_tiers
) -> None:
    """A tool returning a plain `str` gets an output schema and the SDK
    puts the same text in `structured_content` as well. Every tool here
    is for the model to read, so the second copy is wasted bytes."""

    async def _run(name: str, arguments: dict) -> None:
        result = await client.call_tool(name, arguments)
        assert result.structured_content is None, (
            f"{name} sends its result twice: as content and as "
            f"structured_content ({len(str(result.structured_content))} chars)"
        )

    mock_http(
        lambda request: httpx2.Response(
            200, text="<p>ok</p>", headers={"content-type": "text/html"}
        )
    )
    await _run("webfetch", {"url": "https://93.184.216.34/"})
    mock_http(lambda request: httpx2.Response(200, text="unparseable feed"))
    await _run("fetch_rss", {"url": "https://93.184.216.34/"})
    await _run("fetch_pdf", {"url": "https://93.184.216.34/"})
    await _run("search_web", {"query": "schema check", "max_results": 1})
    await _run("fetch_wayback", {"url": "https://93.184.216.34/"})
    await _run("get_time", {})
    await _run("convert_time", {"value": "2026-10-10"})


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tools_publish_no_output_schema(advertised: dict, name: str) -> None:
    assert advertised[name].output_schema is None, (
        f"{name} publishes an output schema, which means its str return "
        "is serialised twice; pass structured_output=False to @mcp.tool()"
    )


# --- input ------------------------------------------------------------


@pytest.mark.parametrize(
    "name,field,minimum,maximum",
    [
        ("search_web", "max_results", 1, 20),
        ("search_web", "pageno", 1, 10),
        ("webfetch", "max_chars", 500, 50000),
        ("fetch_pdf", "max_pages", 1, 100),
        ("fetch_pdf", "max_chars", 500, 50000),
        ("fetch_rss", "max_items", 1, 50),
    ],
)
def test_numeric_arguments_are_bounded(
    advertised: dict, name: str, field: str, minimum: int, maximum: int
) -> None:
    schema = advertised[name].input_schema["properties"][field]
    assert schema.get("minimum") == minimum, f"{name}.{field} has no lower bound"
    assert schema.get("maximum") == maximum, f"{name}.{field} has no upper bound"


def test_time_range_is_an_enum(advertised: dict) -> None:
    schema = advertised["search_web"].input_schema["properties"]["time_range"]
    # `Literal[...] | None` renders as an anyOf, enum first.
    (string_arm,) = (arm for arm in schema["anyOf"] if "enum" in arm)
    assert set(string_arm["enum"]) == {"day", "week", "month", "year"}


def test_every_tool_declares_itself_read_only(advertised: dict) -> None:
    for name, tool in advertised.items():
        assert tool.annotations is not None, f"{name} publishes no annotations"
        assert tool.annotations.read_only_hint is True, f"{name} is not marked read-only"
