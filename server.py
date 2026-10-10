import ipaddress
import os
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import httpx2
from bs4 import BeautifulSoup
from markdownify import markdownify
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# Initialize MCPServer (v2 API; FastMCP was renamed in mcp 2.x)
mcp = MCPServer("SearXNG Free Search")

# Reached over the podman npm-network (container name resolves
# without any host publishing); SEARXNG_URL env overrides.
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080/search")

# Default categories when the caller doesn't specify any: the
# general web tier plus technical/q&a/science engines
# (stackoverflow, github, arxiv, wikipedia, pypi, ...).
DEFAULT_CATEGORIES = "general,it,q&a,science"

# --- web content tools (webfetch, pdf, rss, wayback) -----------------

# Hard cap on bytes pulled off the wire per fetch, streamed, so a
# multi-hundred-megabyte page can't exhaust the container's memory.
WEBFETCH_MAX_BYTES = 2_000_000

# Papers with embedded figures run a few MB; 10 MB still caps the
# blast radius while covering essentially any text PDF.
PDF_MAX_BYTES = 10_000_000

# Feeds are small by definition; 1 MB is generous headroom.
FEED_MAX_BYTES = 1_000_000

# --- fetch_wayback sources -------------------------------------------
# Internet Archive exposes several backends that flap independently
# (observed 2026-10: replay up, CDX hanging, availability empty then
# recovering, timemap slow). fetch_wayback tries them in order and
# falls back to archive.today, a different org entirely.

# Fastest IA lookup: JSON with one closest snapshot straight to a
# replay URL.
WAYBACK_AVAILABLE_URL = "https://archive.org/wayback/available"

# IA timemap (Memento link format): lists every capture of a URL.
WAYBACK_TIMEMAP_URL = "https://web.archive.org/web/timemap/link/"

# archive.today mirrors, queried through /newest/<url> (302 -> the
# newest snapshot ID). Final fallback, independent of IA. Mirrors
# also answer on .today/.is/.li; any host in the target is accepted.
ARCHIVE_PH_NEWEST = "https://archive.ph/newest/"

# Per-source timeouts, kept short so a dead tier costs little.
WAYBACK_AVAILABLE_TIMEOUT = 8.0
WAYBACK_TIMEMAP_TIMEOUT = 12.0
WAYBACK_PH_TIMEOUT = 8.0

# A source that just failed transport-level is skipped for this long,
# so repeated calls during a sustained outage don't pay each dead
# tier's timeout over and over. Self-healing: probes resume after.
WAYBACK_TIER_COOLDOWN = 300.0
_wayback_tier_cooldowns: dict[str, float] = {}

# Set to "1" to disable the private-address guard (escape hatch for
# intentional internal fetches, e.g. debugging from inside npm-network).
WEBFETCH_ALLOW_PRIVATE = os.environ.get("WEBFETCH_ALLOW_PRIVATE", "") == "1"

# Browsers get blocked by plenty of sites; a plain UA avoids 403s.
WEBFETCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Chrome-window copy of a page; everything below this is markup noise
# that eats tokens without adding content.
_WEBFETCH_DROP_TAGS = [
    "script", "style", "noscript", "nav", "footer", "header",
    "aside", "form", "iframe", "svg", "button", "select",
]


def _assert_fetchable(url: str) -> None:
    """Reject URLs that scheme- or IP-wise cannot be public web pages.

    Guards against SSRF: this container shares npm-network with
    bitcoind/monerod/lightningd/valkey/searxng and publishes 8000 on
    the LAN, so an unrestricted fetcher would turn any prompt
    injection in a fetched page into a probe of those services.
    Raises ValueError with a human-readable reason.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme {parsed.scheme!r} (only http/https)")
    host = parsed.hostname
    if not host:
        raise ValueError("could not parse a host from the URL")

    if WEBFETCH_ALLOW_PRIVATE:
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as err:
        raise ValueError(
            f"could not resolve host {host!r} ({err.strerror})"
        ) from err

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(f"refusing to fetch internal address {ip}")


def _fetch_bytes(
    url: str,
    max_bytes: int = WEBFETCH_MAX_BYTES,
    timeout: float = 15.0,
) -> tuple[bytes, str, str]:
    """Guarded, streamed GET returning (body, content_type, encoding).

    Shared by webfetch/fetch_pdf/fetch_rss so the SSRF guard, UA,
    redirect cap and byte cap are defined exactly once. Raises
    ValueError for blocked URLs and httpx2.HTTPError for HTTP
    failures.
    """
    _assert_fetchable(url)
    with httpx2.Client(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": WEBFETCH_USER_AGENT},
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            encoding = response.charset_encoding or "utf-8"
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) >= max_bytes:
                    body = body[:max_bytes]
                    break
    return body, content_type, encoding


@mcp.tool()
def search_web(
    query: str,
    max_results: int = 5,
    categories: str = "",
    language: str = "",
    time_range: str = "",
    pageno: int = 1,
) -> str:
    """Search the web using self-hosted SearXNG (no API keys required).

    Args:
        query: The search query string.
        max_results: Number of results to return (default: 5).
        categories: Comma-separated SearXNG categories to search.
            Defaults to general,it,q&a,science. Examples: "news",
            "images", "general,news", "it".
        language: Optional language code for results, e.g. "en",
            "de", "fr". Empty means SearXNG's instance default.
        time_range: Optional recency filter: "day", "week",
            "month", or "year". Empty means no filter.
        pageno: Results page number, starting at 1 (default).
    """
    params = {
        "q": query,
        "format": "json",
        "categories": categories or DEFAULT_CATEGORIES,
    }
    if language:
        params["language"] = language
    if time_range:
        params["time_range"] = time_range
    if pageno and int(pageno) > 1:
        params["pageno"] = str(pageno)

    try:
        with httpx2.Client(timeout=10.0) as client:
            response = client.get(SEARXNG_URL, params=params)
            response.raise_for_status()
            data = response.json()

        results = data.get("results", [])[:max_results]

        output = []

        # Query suggestions (spellcheck-style corrections from engines)
        suggestions = data.get("suggestions")
        if suggestions:
            output.append("Did you mean: " + ", ".join(str(s) for s in suggestions[:5]))

        if not results:
            output.append(f"No search results found for '{query}'.")
        else:
            for r in results:
                title = r.get("title", "No Title")
                url = r.get("url", "")
                content = r.get("content", "")
                engine = r.get("engine", "unknown")
                published = r.get("publishedDate") or r.get("pubdate")
                block = (
                    f"### {title}\n"
                    f"**URL:** {url}\n"
                    f"**Source Engine:** {engine}\n"
                )
                if published:
                    block += f"**Published:** {published}\n"
                block += f"**Snippet:** {content}\n"
                output.append(block)

        # Surface engines that dropped out this query (CAPTCHA,
        # suspension, timeout) so the caller knows the result set
        # is thinner than usual and why.
        unresponsive = data.get("unresponsive_engines")
        if unresponsive:
            notes = ", ".join(f"{name} ({reason})" for name, reason in unresponsive[:8])
            output.append(f"Note: engines unavailable this query: {notes}")

        return "\n---\n".join(output)

    except httpx2.HTTPError as err:
        return f"HTTP error connecting to SearXNG: {err}"
    except Exception as e:
        return f"Unexpected error during search: {str(e)}"


@mcp.tool()
def webfetch(url: str, max_chars: int = 20000) -> str:
    """Fetch a web page and return its content as readable text/markdown.

    HTML pages are reduced to markdown; JSON and plain-text responses
    pass through unchanged. Content is truncated to max_chars.

    Args:
        url: The http(s) URL to fetch.
        max_chars: Maximum characters of content to return
            (default: 20000).
    """
    try:
        body, content_type, encoding = _fetch_bytes(url)
    except ValueError as err:
        return f"Blocked: {err}"
    except httpx2.HTTPError as err:
        return f"HTTP error fetching {url}: {err}"
    except Exception as e:
        return f"Unexpected error during fetch: {str(e)}"

    text = body.decode(encoding, errors="replace")

    if "html" in content_type:
        result = _html_to_markdown(text)
    else:
        result = text

    if len(result) > max_chars:
        result = result[:max_chars] + f"\n\n[truncated at {max_chars} chars]"

    return result


def _html_to_markdown(html: str) -> str:
    """Reduce an HTML document to title + markdown body text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_WEBFETCH_DROP_TAGS):
        tag.decompose()

    body = soup.body or soup
    markdown = markdownify(str(body), heading_style="ATX").strip()

    title = soup.title.get_text(strip=True) if soup.title else ""
    if title and title not in markdown[: len(title) + 64]:
        return f"# {title}\n\n{markdown}"
    return markdown or "(no readable content found)"


@mcp.tool()
def fetch_pdf(url: str, max_pages: int = 20, max_chars: int = 20000) -> str:
    """Fetch a PDF and extract its text (e.g. an arxiv paper).

    Args:
        url: The http(s) URL of the PDF.
        max_pages: Maximum number of pages to extract (default: 20).
        max_chars: Maximum characters of extracted text to return
            (default: 20000).
    """
    try:
        body, content_type, _ = _fetch_bytes(url, max_bytes=PDF_MAX_BYTES)
    except ValueError as err:
        return f"Blocked: {err}"
    except httpx2.HTTPError as err:
        return f"HTTP error fetching {url}: {err}"
    except Exception as e:
        return f"Unexpected error during fetch: {str(e)}"

    # Some servers serve PDFs as octet-stream, so the magic bytes are
    # the reliable check.
    if not body.startswith(b"%PDF-") and "pdf" not in content_type:
        return f"Not a PDF: response looks like {content_type!r}, not a PDF document."

    import io

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(body))
    except PdfReadError as err:
        return f"Could not read PDF (encrypted or malformed): {err}"
    except Exception as e:
        return f"Unexpected error reading PDF: {str(e)}"

    parts = []
    for i, page in enumerate(reader.pages[:max_pages]):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        parts.append(f"--- page {i + 1} ---\n{text}")

    extracted = "\n\n".join(parts)
    if not any(ch.isalnum() for ch in extracted):
        return "No extractable text found (the PDF is likely scanned images, not text)."

    if len(extracted) > max_chars:
        extracted = extracted[:max_chars] + f"\n\n[truncated at {max_chars} chars]"

    return extracted


@mcp.tool()
def fetch_rss(url: str, max_items: int = 10) -> str:
    """Fetch an RSS/Atom feed and list recent items.

    Args:
        url: The http(s) URL of the feed (RSS 0.9x/1.0/2.0 or Atom).
        max_items: Maximum number of items to return (default: 10).
    """
    try:
        body, _, _ = _fetch_bytes(url, max_bytes=FEED_MAX_BYTES)
    except ValueError as err:
        return f"Blocked: {err}"
    except httpx2.HTTPError as err:
        return f"HTTP error fetching {url}: {err}"
    except Exception as e:
        return f"Unexpected error during fetch: {str(e)}"

    # Bytes are handed to feedparser directly: feeds lie about their
    # encoding more often than pages do, and feedparser sniffs it.
    feed = feedparser.parse(body)
    if not feed.entries:
        if feed.bozo:
            return f"Could not parse feed: {feed.get('bozo_exception', 'unknown parse error')}"
        return f"Feed parsed but contains no items: {url}"

    output = []
    feed_title = feed.feed.get("title", "")
    if feed_title:
        output.append(f"# {feed_title}")

    for entry in feed.entries[:max_items]:
        title = entry.get("title", "No title")
        link = entry.get("link", "")

        date = ""
        if entry.get("published_parsed"):
            stamp = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            date = stamp.strftime("%Y-%m-%d %H:%M UTC")
        else:
            date = entry.get("published", "") or entry.get("updated", "")

        summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(
            " ", strip=True
        )
        if len(summary) > 200:
            summary = summary[:200] + "…"

        block = f"### {title}"
        if date:
            block += f"\n**Date:** {date}"
        if link:
            block += f"\n**URL:** {link}"
        if summary:
            block += f"\n{summary}"
        output.append(block)

    return "\n---\n".join(output)


def _format_cdx_timestamp(ts: str) -> str:
    """'20020120142510' -> '2002-01-20 14:25:10 UTC' (or ts as-is)."""
    if len(ts) == 14 and ts.isdigit():
        return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]} UTC"
    return ts


def _short_reason(err: Exception) -> str:
    """Human-readable one-liner for an archive-probe failure."""
    name = type(err).__name__
    if "Timeout" in name:
        return "timed out"
    if "ConnectError" in name:
        return "connection failed"
    if name == "RuntimeError":
        return str(err)
    return f"{name}: {str(err)[:60]}"


def _clean_original(url: str) -> str:
    """IA stores captured URLs with default ports (":80/"); clean them."""
    return re.sub(r"^(https?)://([^/?#]+):(?:80|443)(/|$)", r"\1://\2\3", url)


def _wayback_from_available(client, url: str) -> tuple[str, str, str] | None:
    """IA availability API -> (timestamp, original, snapshot_url).

    Returns None when the API answers but has no snapshot (it did
    exactly that during the Oct 2026 window); raises on transport
    trouble so the caller can fall through to the next source.
    """
    response = client.get(
        WAYBACK_AVAILABLE_URL,
        params={"url": url},
        timeout=WAYBACK_AVAILABLE_TIMEOUT,
    )
    response.raise_for_status()
    if response.text.lstrip().startswith("<"):
        return None  # outage HTML page
    closest = (
        (response.json().get("archived_snapshots") or {}).get("closest") or {}
    )
    # The API answers with a full replay URL; split it back apart.
    match = re.match(
        r"^https?://web\.archive\.org/web/(\d{14})/(.*)$",
        closest.get("url", ""),
    )
    if not match:
        return None
    ts, original = match.group(1), match.group(2)
    return ts, original, f"https://web.archive.org/web/{ts}/{original}"


def _wayback_from_timemap(client, url: str) -> tuple[str, str, str] | None:
    """IA timemap (Memento) -> newest capture it lists.

    The timemap is a replay-side backend, independent of the
    (CDX-indexed) availability API; during the Oct 2026 IA incident
    it stayed up while CDX hung.
    """
    response = client.get(
        WAYBACK_TIMEMAP_URL + url, timeout=WAYBACK_TIMEMAP_TIMEOUT
    )
    response.raise_for_status()
    # Link format lines, e.g.:
    # <https://web.archive.org/web/20020120142510/http://x:80/>;
    #   rel="first memento"; datetime="..."
    # Prefer the newest: last memento, else first.
    candidates: dict[str, tuple[str, str]] = {}
    for match in re.finditer(
        r'<(https?://web\.archive\.org/web/(\d{14})/[^>]*)>;'
        r'\s*rel="((?:first|last) memento)"',
        response.text,
    ):
        candidates[match.group(3)] = (match.group(2), match.group(1))
    pick = candidates.get("last memento") or candidates.get("first memento")
    if not pick:
        return None
    ts, snapshot = pick
    original = snapshot.split(f"/web/{ts}/", 1)[1]
    return ts, original, f"https://web.archive.org/web/{ts}/{original}"


def _wayback_from_archive_ph(client, url: str) -> tuple[str, str, str] | None:
    """Newest archive.today snapshot (timestamp, original, snapshot_url).

    Queries the /newest/ redirect, which 302s to the snapshot ID.
    Returns None when there is no snapshot; raises RuntimeError on a
    429 rate-limit and httpx errors on transport trouble.
    """
    response = client.get(
        ARCHIVE_PH_NEWEST + url,
        follow_redirects=False,
        timeout=WAYBACK_PH_TIMEOUT,
    )
    if response.status_code == 429:
        raise RuntimeError("archive.ph rate-limited this host")
    if response.status_code not in (301, 302, 303, 307, 308):
        return None
    location = response.headers.get("location", "")
    # Mirror host varies: archive.ph/.today/.is/.li share the ID scheme.
    match = re.match(r"^https?://archive\.(?:ph|today|is|li)/(\d{14})/(.+)$", location)
    if not match:
        return None
    ts, original = match.group(1), match.group(2)
    return ts, original, f"https://archive.ph/{ts}/{original}"


@mcp.tool()
def fetch_wayback(url: str) -> str:
    """Look up the closest web-archive snapshot of a URL.

    Tries, in order: the Internet Archive availability API, the IA
    timemap (both IA, but separate backends that flap independently),
    then the archive.today mirrors as an IA-independent fallback.
    A source that just failed is skipped for a few minutes so a
    sustained outage does not re-pay each dead tier's timeout.
    Returns the source, snapshot URL, capture timestamp, and original
    URL -- chain with `webfetch` on either URL to read the archived
    or the live page.

    Args:
        url: The http(s) URL to look up in the archive.
    """
    # The lookups go to the archives, not the target, so this is not
    # SSRF -- but guarding anyway keeps internal hosts from becoming
    # an existence oracle through the archive indexes, and keeps this
    # tool's boundary consistent with the others.
    try:
        _assert_fetchable(url)
    except ValueError as err:
        return f"Blocked: {err}"

    tiers = (
        ("Internet Archive", _wayback_from_available),
        ("Internet Archive timemap", _wayback_from_timemap),
        ("archive.today", _wayback_from_archive_ph),
    )

    notes = []
    with httpx2.Client(
        timeout=15.0,
        headers={"User-Agent": WEBFETCH_USER_AGENT},
    ) as client:
        for name, probe in tiers:
            if time.monotonic() < _wayback_tier_cooldowns.get(name, 0.0):
                notes.append(f"{name}: recently unavailable, skipped")
                continue
            try:
                hit = probe(client, url)
            except Exception as err:
                _wayback_tier_cooldowns[name] = (
                    time.monotonic() + WAYBACK_TIER_COOLDOWN
                )
                notes.append(f"{name}: {_short_reason(err)}")
                continue
            if hit:
                ts, original, snapshot = hit
                return _format_wayback_hit(
                    name, snapshot, ts, _clean_original(original)
                )
            notes.append(f"{name}: no snapshot")

    return "No snapshot found. Sources tried: " + "; ".join(notes) + "."


def _format_wayback_hit(
    source: str, snapshot_url: str, timestamp: str, original: str
) -> str:
    """Render one archive hit; live/undated markers explained."""
    # archive.today uses future-dated IDs (2030-/2035-...) to mark a
    # live fetch of the current page rather than a dated capture.
    if timestamp.isdigit() and len(timestamp) == 14 and timestamp >= "20300000000000":
        captured = "live snapshot (undated -- archive.today served the current page)"
    else:
        captured = _format_cdx_timestamp(timestamp)
    return (
        f"**Source:** {source}\n"
        f"**Snapshot:** {snapshot_url}\n"
        f"**Captured:** {captured}\n"
        f"**Original URL:** {original}\n\n"
        f"webfetch the snapshot URL to read the archived page, or the "
        f"original URL to try the live one."
    )


@mcp.tool()
def get_time(tz: str = "UTC") -> str:
    """Return the current time in a timezone.

    Args:
        tz: IANA timezone name, e.g. "UTC", "Europe/Berlin",
            "America/New_York" (default: UTC).
    """
    try:
        zone = ZoneInfo(tz)
    except Exception as e:
        return f"Unknown timezone {tz!r}: {str(e)}"

    now = datetime.now(zone)
    return (
        f"ISO-8601: {now.isoformat()}\n"
        f"Unix timestamp: {int(now.timestamp())}\n"
        f"Timezone: {tz} (UTC{now.strftime('%z')})"
    )


@mcp.tool()
def convert_time(value: str, from_tz: str = "UTC", to_tz: str = "UTC") -> str:
    """Convert a datetime string from one timezone to another.

    Args:
        value: Datetime string, e.g. "2026-10-10 15:30" or
            "2026-10-10T15:30:00".
        from_tz: IANA timezone the value is in (default: UTC).
        to_tz: IANA timezone to convert to (default: UTC).
    """
    try:
        source = ZoneInfo(from_tz)
        target = ZoneInfo(to_tz)
    except Exception as e:
        return f"Unknown timezone: {str(e)}"

    parsed = None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return f"Could not parse {value!r} as a datetime (try ISO-8601, e.g. 2026-10-10T15:30)."

    converted = parsed.replace(tzinfo=source).astimezone(target)
    return (
        f"{converted.isoformat()} ({to_tz})\n"
        f"Unix timestamp: {int(converted.timestamp())}"
    )


class BearerAuthMiddleware:
    """Pure-ASGI bearer auth in front of the streamable-HTTP app.

    If SEARXNG_MCP_TOKEN is unset the middleware is bypassed (with a
    warning) so a missing env var can't brick the server; the quadlet
    always sets it.
    """

    def __init__(self, app, token: str | None):
        self.app = app
        self.token = token or ""
        if not self.token:
            print("WARNING: SEARXNG_MCP_TOKEN not set -- endpoint is UNAUTHENTICATED", flush=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Unauthenticated liveness endpoint for health pollers
            # (OpenCode's background service probes GET /health
            # without the bearer token). Leaks only "up" state.
            if scope.get("path") == "/health":
                from starlette.responses import JSONResponse

                response = JSONResponse({"status": "ok"})
                await response(scope, receive, send)
                return
            if self.token:
                headers = dict(scope.get("headers") or [])
                if headers.get(b"authorization") != f"Bearer {self.token}".encode():
                    from starlette.responses import JSONResponse

                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    # Accept any Host header: DNS-rebinding protection is disabled
    # because the bearer token (SEARXNG_MCP_TOKEN) gates every request.
    app = BearerAuthMiddleware(
        mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        ),
        os.environ.get("SEARXNG_MCP_TOKEN"),
    )
    uvicorn.run(app, host="0.0.0.0", port=8000)
