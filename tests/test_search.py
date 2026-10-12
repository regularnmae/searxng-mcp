"""search_web: how the query is built and how the answer is rendered.

SearXNG's JSON format is not enabled by default upstream, so the shape
of the response, and how the tool reports a missing or degraded result
set, is worth pinning down. The stub is a real loopback socket because
`search_web` builds its own client rather than going through
`_http_client` (see the note in server.py), so there is no transport to
swap out.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import server

# --- query construction ---------------------------------------------


@pytest.mark.anyio
async def test_defaults(call, searxng) -> None:
    await call("search_web", query="test query")
    (params,) = searxng.queries
    assert params["q"] == "test query"
    assert params["format"] == "json"
    assert params["categories"] == server.DEFAULT_CATEGORIES
    assert "language" not in params
    assert "time_range" not in params
    assert "pageno" not in params


@pytest.mark.anyio
async def test_optional_filters_are_forwarded(call, searxng) -> None:
    await call(
        "search_web",
        query="mcp",
        categories="news",
        language="de",
        time_range="week",
        pageno=3,
    )
    (params,) = searxng.queries
    assert params["categories"] == "news"
    assert params["language"] == "de"
    assert params["time_range"] == "week"
    assert params["pageno"] == "3"


@pytest.mark.anyio
async def test_explicit_categories_override_the_default(call, searxng) -> None:
    await call("search_web", query="mcp", categories="it")
    (params,) = searxng.queries
    assert params["categories"] == "it"
    assert server.DEFAULT_CATEGORIES not in params["categories"]


@pytest.mark.anyio
async def test_empty_categories_fall_back_to_the_default(call, searxng) -> None:
    await call("search_web", query="mcp", categories="")
    (params,) = searxng.queries
    assert params["categories"] == server.DEFAULT_CATEGORIES


# --- rendering -------------------------------------------------------


@pytest.mark.anyio
async def test_results_are_rendered(call, searxng) -> None:
    text = await call("search_web", query="test query")
    assert "### First hit" in text
    assert "**URL:** https://example.com/1" in text
    assert "**Source Engine:** duckduckgo" in text
    assert "**Published:** 2026-10-01T12:00:00" in text
    assert "**Snippet:** A snippet about testing." in text
    assert "Second hit" in text
    assert "Third hit" in text
    assert "\n---\n" in text


@pytest.mark.anyio
async def test_max_results_is_honoured(call, searxng) -> None:
    text = await call("search_web", query="test query", max_results=1)
    assert "First hit" in text
    assert "Second hit" not in text


@pytest.mark.anyio
async def test_missing_published_date_is_omitted(call, searxng) -> None:
    text = await call("search_web", query="test query")
    block = text.split("\n---\n")[1]
    assert "**Published:**" not in block


@pytest.mark.anyio
async def test_suggestions_are_surfaced(call, searxng) -> None:
    searxng.payload["suggestions"] = ["test query", "testing"]
    text = await call("search_web", query="testqery")
    assert text.startswith("Did you mean: test query, testing")


@pytest.mark.anyio
async def test_no_results(call, searxng) -> None:
    searxng.payload["results"] = []
    text = await call("search_web", query="nothing at all")
    assert "No search results found for 'nothing at all'." in text


@pytest.mark.anyio
async def test_unresponsive_engines_are_reported(call, searxng) -> None:
    searxng.payload["unresponsive_engines"] = [
        ["google", "captcha detected"],
        ["bing", "suspend"],
    ]
    text = await call("search_web", query="test query")
    assert "engines unavailable this query" in text
    assert "google (captcha detected)" in text


@pytest.mark.anyio
async def test_empty_unresponsive_list_is_quiet(call, searxng) -> None:
    searxng.payload["unresponsive_engines"] = []
    text = await call("search_web", query="test query")
    assert "unavailable" not in text


# --- failures --------------------------------------------------------


@pytest.mark.anyio
async def test_reports_http_failure(call, monkeypatch) -> None:
    """A 403 here almost always means the instance has not enabled
    `search.formats: [json]`, and the message should say so."""
    body = b"format not allowed"

    class Denying(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    with ThreadingHTTPServer(("127.0.0.1", 0), Denying) as httpd:
        host, port = httpd.server_address[:2]
        threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()
        monkeypatch.setattr(server, "SEARXNG_URL", f"http://{host}:{port}/search")
        text = await call("search_web", query="test query")
    assert "HTTP error" in text
    assert "403" in text


@pytest.mark.anyio
async def test_reports_connection_failure(call, monkeypatch) -> None:
    """Nothing listening on the port: the tool must say so, not hang."""
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as httpd:
        host, port = httpd.server_address[:2]
    monkeypatch.setattr(server, "SEARXNG_URL", f"http://{host}:{port}/search")
    text = await call("search_web", query="test query")
    assert "HTTP error" in text
