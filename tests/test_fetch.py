"""The fetch tools: webfetch, fetch_pdf, fetch_rss.

All three go through `_fetch_bytes`, so these drive the real client
against a mock wire (see the `mock_http` fixture) and assert on what
the model would actually read.
"""

from __future__ import annotations

import httpx2
import pytest

from tests.minimal_pdf import build_pdf

PDF = build_pdf(text="Hello PDF world", num_pages=3)

PAGE = """<!doctype html>
<html><head><title>Widget Docs</title>
<style>body { color: red; }</style>
<script>alert('should not appear')</script>
</head>
<body>
<nav>Home | Products | About</nav>
<h1>Widgets</h1>
<p>A <strong>widget</strong> is a thing.</p>
<a href="/buy">Buy one</a>
<footer>(c) 2026</footer>
</body></html>"""

RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>Example Feed</title>
<item>
  <title>First post</title>
  <link>https://example.com/1</link>
  <pubDate>Tue, 10 Oct 2026 12:00:00 GMT</pubDate>
  <description>&lt;p&gt;A &lt;b&gt;summary&lt;/b&gt;.&lt;/p&gt;</description>
</item>
<item>
  <title>Second post</title>
  <link>https://example.com/2</link>
  <description>Plain summary.</description>
</item>
</channel></rss>"""


def _html(body: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, text=body, headers={"content-type": "text/html"})


def _bytes(body: bytes, content_type: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, content=body, headers={"content-type": content_type})


# --- webfetch -------------------------------------------------------


@pytest.mark.anyio
async def test_webfetch_reduces_html_to_markdown(call, mock_http) -> None:
    mock_http(lambda request: _html(PAGE))
    text = await call("webfetch", url="http://93.184.216.34/docs")
    assert "# Widget Docs" in text
    assert "# Widgets" in text
    assert "A **widget** is a thing." in text
    assert "[Buy one](/buy)" in text


@pytest.mark.anyio
async def test_webfetch_drops_chrome_not_content(call, mock_http) -> None:
    mock_http(lambda request: _html(PAGE))
    text = await call("webfetch", url="http://93.184.216.34/docs")
    for absent in ("should not appear", "color: red", "Home | Products", "(c) 2026"):
        assert absent not in text, f"dropped tag leaked into output: {absent}"


@pytest.mark.anyio
async def test_webfetch_passes_non_html_through(call, mock_http) -> None:
    mock_http(lambda request: _bytes(b'{"ok": true}', "application/json"))
    text = await call("webfetch", url="http://93.184.216.34/api")
    assert text == '{"ok": true}'


@pytest.mark.anyio
async def test_webfetch_truncates_and_says_so(call, mock_http) -> None:
    mock_http(lambda request: _html("<p>" + "x" * 5000 + "</p>"))
    text = await call("webfetch", url="http://93.184.216.34/long", max_chars=1000)
    assert text.startswith("xxxxx")
    assert "[truncated at 1000 chars]" in text
    assert len(text) < 1100


@pytest.mark.anyio
async def test_webfetch_reports_binary_content(call, mock_http) -> None:
    """A PNG is not a page; handing the model decoded bytes is a context
    bomb, so the tool points at the tool that can read it."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    mock_http(lambda request: _bytes(png, "image/png"))
    text = await call("webfetch", url="http://93.184.216.34/logo.png")
    assert "image/png" in text
    assert "fetch_pdf" in text


@pytest.mark.anyio
async def test_webfetch_reports_http_errors(call, mock_http) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, text="upstream down")

    mock_http(handler)
    text = await call("webfetch", url="http://93.184.216.34/down")
    assert "HTTP error" in text


@pytest.mark.anyio
async def test_webfetch_empty_page(call, mock_http) -> None:
    mock_http(lambda request: _html("<html><body></body></html>"))
    text = await call("webfetch", url="http://93.184.216.34/empty")
    assert "no readable content" in text


# --- fetch_pdf ------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_pdf_extracts_pages(call, mock_http) -> None:
    mock_http(lambda request: _bytes(PDF, "application/pdf"))
    text = await call("fetch_pdf", url="http://93.184.216.34/paper.pdf")
    assert "--- page 1 ---" in text
    assert "Hello PDF world page 1" in text
    assert "--- page 3 ---" in text


@pytest.mark.anyio
async def test_fetch_pdf_honours_max_pages(call, mock_http) -> None:
    mock_http(lambda request: _bytes(PDF, "application/pdf"))
    text = await call("fetch_pdf", url="http://93.184.216.34/paper.pdf", max_pages=2)
    assert "--- page 1 ---" in text
    assert "--- page 2 ---" in text
    assert "page 3" not in text


@pytest.mark.anyio
async def test_fetch_pdf_accepts_mislabelled_pdf(call, mock_http) -> None:
    """Some servers serve PDFs as octet-stream; the magic bytes decide."""
    mock_http(lambda request: _bytes(PDF, "application/octet-stream"))
    text = await call("fetch_pdf", url="http://93.184.216.34/paper.bin")
    assert "Hello PDF world" in text


@pytest.mark.anyio
async def test_fetch_pdf_rejects_non_pdf(call, mock_http) -> None:
    mock_http(lambda request: _html("<h1>Not a PDF</h1>"))
    text = await call("fetch_pdf", url="http://93.184.216.34/trap.pdf")
    assert "Not a PDF" in text


@pytest.mark.anyio
async def test_fetch_pdf_rejects_garbage_with_pdf_magic(call, mock_http) -> None:
    mock_http(lambda request: _bytes(b"%PDF-1.4 not really", "application/pdf"))
    text = await call("fetch_pdf", url="http://93.184.216.34/broken.pdf")
    assert "Could not read PDF" in text


# --- fetch_rss ------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_rss_lists_items(call, mock_http) -> None:
    mock_http(lambda request: _bytes(RSS, "application/rss+xml"))
    text = await call("fetch_rss", url="http://93.184.216.34/feed.xml")
    assert "# Example Feed" in text
    assert "### First post" in text
    assert "https://example.com/1" in text
    assert "2026-10-10 12:00 UTC" in text
    assert "### Second post" in text


@pytest.mark.anyio
async def test_fetch_rss_strips_html_from_summaries(call, mock_http) -> None:
    mock_http(lambda request: _bytes(RSS, "application/rss+xml"))
    text = await call("fetch_rss", url="http://93.184.216.34/feed.xml")
    assert "A summary" in text
    assert "<p>" not in text
    assert "<b>" not in text


@pytest.mark.anyio
async def test_fetch_rss_honours_max_items(call, mock_http) -> None:
    mock_http(lambda request: _bytes(RSS, "application/rss+xml"))
    text = await call("fetch_rss", url="http://93.184.216.34/feed.xml", max_items=1)
    assert "First post" in text
    assert "Second post" not in text


@pytest.mark.anyio
async def test_fetch_rss_reports_unparseable_feed(call, mock_http) -> None:
    mock_http(lambda request: _bytes(b"this is not a feed", "text/plain"))
    text = await call("fetch_rss", url="http://93.184.216.34/notafeed")
    assert "Could not parse feed" in text or "contains no items" in text
