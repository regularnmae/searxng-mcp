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

# An article surrounded by chrome a <div>-only page would otherwise leak.
ARTICLE_PAGE = """<!doctype html>
<html><head><title>Widget Docs</title></head>
<body>
  <header><a href="/">Site home</a></header>
  <div class="sidebar"><p>Sidebar promo: buy widgets now, limited offer!</p></div>
  <div id="cookie-banner"><p>We use cookies to improve your experience.</p></div>
  <article>
    <header><h1>Widgets</h1><p class="byline">By Ada</p></header>
    <p>A widget is a thing that does widget things, described here at a
       length long enough to clear the minimum content size the extractor
       needs before it trusts a semantic article element over the body.</p>
    <p>A second paragraph with more detail about widgets, how they are
       assembled and where they are used, so the article plainly has more
       text than the sidebar and the cookie banner put together.</p>
  </article>
  <footer>(c) 2026</footer>
</body></html>"""

META_PAGE = """<!doctype html>
<html><head>
<title>Fallback Title</title>
<meta property="og:title" content="OG Title">
<meta property="og:description" content="An OG description.">
<meta property="og:site_name" content="Example">
<meta property="og:type" content="article">
<meta property="og:image" content="https://example.com/og.png">
<meta name="author" content="Ada Lovelace">
<meta property="article:published_time" content="2026-10-01T12:00:00Z">
<link rel="canonical" href="https://example.com/canonical">
</head>
<body><article><p>Body text long enough to be selected as the main content
of this page, comfortably over the threshold.</p></article></body></html>"""

JSONLD_PAGE = """<!doctype html>
<html><head><title>Doc</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"WebSite","name":"Site"},
 {"@type":"Article","headline":"Graph headline",
  "author":[{"@type":"Person","name":"Grace Hopper"}],
  "datePublished":"2026-09-01",
  "image":{"url":"https://example.com/a.png"},
  "description":"From JSON-LD."}]}
</script>
<script type="application/ld+json">{ this is not valid json</script>
</head>
<body><article><p>Some article body that is comfortably longer than the
minimum size threshold for selection as the main content.</p></article></body></html>"""


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


@pytest.mark.anyio
async def test_webfetch_structured_content(client, mock_http) -> None:
    mock_http(lambda request: _html(PAGE))
    result = await client.call_tool("webfetch", {"url": "http://93.184.216.34/docs"})
    structured = result.structured_content
    assert structured["url"] == "http://93.184.216.34/docs"
    assert structured["content_type"] == "text/html"
    assert structured["title"] == "Widget Docs"
    assert structured["truncated"] is False
    assert "# Widgets" in structured["content"]
    assert structured["chars"] == len(structured["content"])
    assert structured["error"] is None


@pytest.mark.anyio
async def test_webfetch_structured_truncation(client, mock_http) -> None:
    mock_http(lambda request: _html("<p>" + "x" * 5000 + "</p>"))
    result = await client.call_tool(
        "webfetch", {"url": "http://93.184.216.34/long", "max_chars": 1000}
    )
    structured = result.structured_content
    assert structured["truncated"] is True
    assert "[truncated at 1000 chars]" in structured["content"]


@pytest.mark.anyio
async def test_webfetch_structured_error_for_binary(client, mock_http) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    mock_http(lambda request: _bytes(png, "image/png"))
    result = await client.call_tool("webfetch", {"url": "http://93.184.216.34/logo.png"})
    structured = result.structured_content
    assert structured["error"] is not None
    assert structured["content"] == ""
    assert structured["content_type"] == "image/png"


@pytest.mark.anyio
async def test_webfetch_prefers_the_article_over_chrome(call, mock_http) -> None:
    mock_http(lambda request: _html(ARTICLE_PAGE))
    text = await call("webfetch", url="http://93.184.216.34/docs")
    assert "A widget is a thing" in text
    assert "By Ada" in text  # the article's own <header> is kept
    for absent in ("Sidebar promo", "We use cookies", "Site home", "(c) 2026"):
        assert absent not in text, f"chrome leaked into the extraction: {absent}"


@pytest.mark.anyio
async def test_webfetch_extracts_open_graph_metadata(client, mock_http) -> None:
    mock_http(lambda request: _html(META_PAGE))
    result = await client.call_tool("webfetch", {"url": "http://93.184.216.34/post"})
    assert result.structured_content["metadata"] == {
        "description": "An OG description.",
        "site_name": "Example",
        "author": "Ada Lovelace",
        "published": "2026-10-01T12:00:00Z",
        "image": "https://example.com/og.png",
        "canonical_url": "https://example.com/canonical",
        "type": "article",
    }
    # og:title wins over the <title> tag.
    assert result.structured_content["title"] == "OG Title"


@pytest.mark.anyio
async def test_webfetch_falls_back_to_json_ld(client, mock_http) -> None:
    """No OG tags: metadata comes from the @graph block, and the malformed
    JSON-LD block beside it is ignored rather than fatal."""
    mock_http(lambda request: _html(JSONLD_PAGE))
    result = await client.call_tool("webfetch", {"url": "http://93.184.216.34/g"})
    metadata = result.structured_content["metadata"]
    assert metadata["description"] == "From JSON-LD."
    assert metadata["author"] == "Grace Hopper"
    assert metadata["published"] == "2026-09-01"
    assert metadata["image"] == "https://example.com/a.png"
    assert metadata["site_name"] is None


@pytest.mark.anyio
async def test_webfetch_non_html_has_no_metadata(client, mock_http) -> None:
    mock_http(lambda request: _bytes(b'{"ok": true}', "application/json"))
    result = await client.call_tool("webfetch", {"url": "http://93.184.216.34/api"})
    assert result.structured_content["metadata"] is None


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
