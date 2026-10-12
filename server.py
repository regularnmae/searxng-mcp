import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import httpx2
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

logger = logging.getLogger("searxng-mcp")

# The SDK types log_level as a closed set, so an unrecognised LOG_LEVEL
# has to be resolved to one of its levels here -- the raw env string is
# not assignable, and an unknown level falling back to INFO beats the
# import-time raise that the SDK would otherwise produce.
_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: tuple[_LogLevel, ...] = get_args(_LogLevel)


def _log_level() -> _LogLevel:
    requested = os.environ.get("LOG_LEVEL", "INFO").upper()
    return requested if requested in _LEVELS else "INFO"


def _env_float(name: str, default: float) -> float:
    """A float env var, or the default when unset or unparseable.

    A typo'd cache setting should not stop the server from starting, so
    a bad value warns and falls back rather than raising at import.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


# Initialize MCPServer (v2 API; FastMCP was renamed in mcp 2.x).
# `instructions` is the one place to tell the model how these tools
# chain together, and every MCP client surfaces it before the first
# call.
mcp = MCPServer(
    "SearXNG Free Search",
    instructions=(
        "Search a self-hosted SearXNG, then read the pages it finds. "
        "search_web takes a query and returns snippets and URLs; "
        "webfetch reads one of those URLs as markdown; fetch_pdf reads "
        "a PDF (an arxiv paper, say); fetch_rss reads a feed; "
        "fetch_wayback finds an archived copy when the live page is "
        "gone. Chain search_web -> webfetch for anything factual, and "
        "prefer fetch_pdf over webfetch for a PDF URL."
    ),
    log_level=_log_level(),
)

# Every tool here only reads. Saying so lets a well-behaved client skip
# a confirmation prompt before running one of them. The web tools are
# open-world by construction; the time tools are not.
_WEB_TOOLS = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_LOCAL_TOOLS = ToolAnnotations(read_only_hint=True, open_world_hint=False)


# Reached over the podman npm-network (container name resolves
# without any host publishing); SEARXNG_URL env overrides.
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080/search")

# Default categories when the caller doesn't specify any: the
# general web tier plus technical/q&a/science engines
# (stackoverflow, github, arxiv, wikipedia, pypi, ...).
DEFAULT_CATEGORIES = "general,it,q&a,science"

# --- search_web caching ----------------------------------------------
# SearXNG answers the same query identically for a while, so search_web
# keeps recent responses in process memory: a repeated query inside the
# TTL window is served without an upstream request, and a second thread
# asking for a query that is already being fetched waits for that fetch
# instead of issuing another. Either knob at 0 (or below) disables it.
SEARCH_TIMEOUT = 10.0
SEARCH_CACHE_TTL = _env_float("SEARCH_CACHE_TTL", 60.0)
SEARCH_CACHE_MAX = int(_env_float("SEARCH_CACHE_MAX", 128.0))

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

# Set to "1" to serve the endpoint with no token at all. Without it a
# missing SEARXNG_MCP_TOKEN is a startup error, not a warning.
MCP_ALLOW_ANONYMOUS = os.environ.get("MCP_ALLOW_ANONYMOUS", "") == "1"

# Unauthenticated liveness path, for pollers that hold no token.
HEALTH_PATH = "/health"

# Browsers get blocked by plenty of sites; a plain UA avoids 403s.
WEBFETCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Chrome inside a page. `header`/`footer` are deliberately absent: an
# article's own title and byline live in its <header>, so those two are
# removed only when they are NOT inside an <article>/<main> (see
# `_strip_boilerplate`).
_WEBFETCH_DROP_TAGS = [
    "script", "style", "noscript", "nav", "aside",
    "form", "iframe", "svg", "button", "select",
]

# Boilerplate that survives the tag strip because it is a <div>. Removed
# by role, by aria-hidden, or by a class/id token; the tokens are matched
# on word boundaries so "header"/"download" don't trip the "ad" rule.
_BOILERPLATE_ROLES = frozenset({
    "navigation", "banner", "complementary", "contentinfo", "search",
    "dialog", "alertdialog", "menu", "menubar",
})
_BOILERPLATE_RE = re.compile(
    r"\b(nav|navbar|subnav|menu|sidebar|cookie|consent|banner|subscribe|"
    r"newsletter|social|share|related|comments?|advert|ads?|promo|paywall|"
    r"modal|popup|breadcrumbs?|masthead|skip-link|toolbar|drawer|overlay|gdpr)\b",
    re.IGNORECASE,
)

# A semantic <article>/<main>/[role=main] is trusted as the content only
# when it holds enough text; the paragraph-scoring fallback below needs a
# paragraph worth counting and a container worth returning.
_MIN_MAIN_CHARS = 200
_MIN_PARA_CHARS = 25

# A JSON-LD block larger than this is ignored rather than parsed.
_JSONLD_MAX_CHARS = 200_000

# Bytes that are never a page. Servers mislabel content often enough
# that the declared type alone cannot be trusted, so the leading magic
# bytes are checked as well -- the same way fetch_pdf decides it has
# been handed a PDF at all. Decoding one of these and returning it as
# text puts a few hundred kilobytes of replacement characters in the
# model's context, so it is better to say what the response is.
_WEBFETCH_BINARY_TYPES = (
    "image/",
    "audio/",
    "video/",
    "font/",
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/x-7z-compressed",
    "application/wasm",
)

_WEBFETCH_BINARY_MAGIC = (
    b"%PDF-",
    b"\x89PNG",
    b"\xff\xd8\xff",  # JPEG
    b"GIF87a",
    b"GIF89a",
    b"PK\x03\x04",  # zip, and thus most OOXML
    b"\x1f\x8b",  # gzip
)


def _assert_fetchable(url: str) -> None:
    """Reject URLs that scheme- or IP-wise cannot be public web pages.

    Guards against SSRF: this container shares npm-network with
    bitcoind/monerod/lightningd/valkey/searxng and publishes 8000 on
    the LAN, so an unrestricted fetcher would turn any prompt
    injection in a fetched page into a probe of those services.

    This is an allowlist, not a blocklist. An address is fetched only
    when every address the host resolves to is globally routable, which
    is what catches the ranges a blocklist tends to miss: CGNAT
    (100.64.0.0/10), NAT64, benchmarking, documentation prefixes, and
    IPv4 tunnelled inside IPv6.

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
        # `is_global` is the allowlist; multicast and reserved are the
        # two classes Python still reports as global, so they are
        # named rather than trusted.
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            # Worth a log line even when the refusal came from a
            # redirect hop: something tried to point this server at an
            # internal address, and that is what an injected prompt
            # looks like.
            logger.warning("refusing fetch of %s: internal address %s", url, ip)
            raise ValueError(f"refusing to fetch internal address {ip}")


def _assert_redirect_target(response) -> None:
    """Reject a redirect hop that points somewhere private.

    `_assert_fetchable` only ever sees the URL the caller passed, and
    the HTTP client follows redirects on its own -- so without this,
    a page on the public internet that answers 302 with a Location
    pointing at bitcoind:8332 or valkey:6379 walks straight into
    npm-network. Registered as a response event hook, this runs once
    per response *before* the next request is built, so a bad hop is
    refused before anything is sent to it.
    """
    if not response.has_redirect_location:
        return
    location = response.headers.get("location", "")
    if not location:
        return
    # Relative Locations mean the request URL, not the original one.
    _assert_fetchable(urljoin(str(response.request.url), location))


def _http_client(timeout: float = 15.0, *, transport=None) -> httpx2.Client:
    """The one client configuration shared by every fetch tool.

    Browser UA, redirect policy and byte caps live here so the tools do
    not each half-remember them. `transport` is an injection point for
    tests: the client it builds is the production client in every other
    respect, same hooks and same redirect policy, pointed at a fake
    wire instead of the network.
    """
    return httpx2.Client(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": WEBFETCH_USER_AGENT},
        transport=transport,
        event_hooks={"response": [_assert_redirect_target]},
    )


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
    with (
        _http_client(timeout=timeout) as client,
        client.stream("GET", url) as response,
    ):
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


# --- structured output for search_web and webfetch -------------------
# These two tools return both a human-readable text block and the same
# data as schema-validated structured content. They are the deliberate
# exception to the "structured_output=False everywhere" rule (see the
# note in tests/test_schemas.py): a caller that wants typed data can
# read `structured_content`, and the text block still carries the
# markdown the model reads. Returning a CallToolResult rather than a
# bare model is what keeps that text block as markdown -- a bare model
# return would make the SDK serialise the whole model, pretty-printed,
# into the text block instead.


class SearchResult(BaseModel):
    """One SearXNG result, flattened for a typed client.

    `engine` is the primary (first) engine; `engines` is every engine that
    found the page after SearXNG's and our own merging. `score` is
    SearXNG's ranking weight (engine-weighted, not comparable across
    engines); `thumbnail` is only set when it is an absolute URL a client
    can actually fetch.
    """

    title: str
    url: str
    snippet: str
    engine: str
    engines: list[str] = Field(default_factory=list)
    published: str | None = None
    score: float | None = None
    thumbnail: str | None = None


class UnresponsiveEngine(BaseModel):
    """An engine that dropped out this query, and the reason it gave."""

    engine: str
    reason: str


class SearchWebResponse(BaseModel):
    """Structured form of a `search_web` result.

    `error` is set (with `results` empty) when the search could not be
    completed; the same message sits in the text block, so a text-only
    reader loses nothing.
    """

    results: list[SearchResult] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    unresponsive_engines: list[UnresponsiveEngine] = Field(default_factory=list)
    error: str | None = None


class PageMetadata(BaseModel):
    """Page metadata from Open Graph / Twitter Card / JSON-LD / <meta>.

    Best-effort: a field the page does not provide (or provides
    malformed) is null rather than absent, so a typed client can rely on
    the shape. Only populated for HTML.
    """

    description: str | None = None
    site_name: str | None = None
    author: str | None = None
    published: str | None = None
    image: str | None = None
    canonical_url: str | None = None
    type: str | None = None


class WebFetchResponse(BaseModel):
    """Structured form of a `webfetch` result.

    `content` is the final text (markdown for HTML, verbatim otherwise),
    already truncated to `max_chars`; `truncated` says whether it was.
    `error` is set (with `content` empty) for blocked, non-readable or
    failed fetches.
    """

    url: str
    content_type: str = ""
    title: str | None = None
    content: str = ""
    truncated: bool = False
    chars: int = 0
    metadata: PageMetadata | None = None
    error: str | None = None


# Query parameters that only track the referrer. Stripped when building
# the de-duplication key so a page reached through a campaign link
# collapses onto its plain URL.
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "igshid"})


def _canonical_url(url: str) -> str:
    """A de-duplication key blind to scheme, www, trailing slash, fragment
    and tracking params -- the near-duplicates SearXNG's own merge key
    leaves behind (it ignores only the scheme)."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(("", netloc, path, urlencode(query), ""))


def _result_from_raw(raw: dict) -> SearchResult:
    """Map one SearXNG JSON result onto the model."""
    listed = raw.get("engines")
    engines = [str(e) for e in listed] if isinstance(listed, list) and listed else []
    primary = str(raw.get("engine") or "")
    if primary and primary not in engines:
        engines.insert(0, primary)
    score = raw.get("score")
    thumbnail = str(raw.get("thumbnail") or "")
    return SearchResult(
        title=str(raw.get("title", "No Title")),
        url=str(raw.get("url", "")),
        snippet=str(raw.get("content", "")),
        engine=primary or "unknown",
        engines=engines,
        published=str(raw.get("publishedDate") or raw.get("pubdate") or "") or None,
        score=float(score) if isinstance(score, int | float) else None,
        thumbnail=thumbnail if thumbnail.startswith(("http://", "https://")) else None,
    )


def _merge_duplicates(results: list[SearchResult]) -> list[SearchResult]:
    """Collapse results that differ only by the `_canonical_url` edits.

    Keeps the first (score-ordered) title and snippet, unions the engine
    list so no source is lost, and takes the best score. Order is first
    appearance, which is SearXNG's ranking.
    """
    merged: dict[str, SearchResult] = {}
    order: list[str] = []
    for result in results:
        key = _canonical_url(result.url)
        existing = merged.get(key)
        if existing is None:
            merged[key] = result
            order.append(key)
            continue
        for engine in result.engines:
            if engine not in existing.engines:
                existing.engines.append(engine)
        if result.score is not None:
            existing.score = max(existing.score or 0.0, result.score)
        existing.published = existing.published or result.published
        existing.thumbnail = existing.thumbnail or result.thumbnail
    return [merged[key] for key in order]


def _search_error(message: str) -> CallToolResult:
    """A `search_web` failure: same text as before, plus a typed error."""
    model = SearchWebResponse(error=message)
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content=model.model_dump(mode="json"),
    )


def _webfetch_error(url: str, message: str, content_type: str = "") -> CallToolResult:
    """A `webfetch` failure: same text as before, plus a typed error."""
    model = WebFetchResponse(url=url, content_type=content_type, error=message)
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content=model.model_dump(mode="json"),
    )


# --- search_web cache ------------------------------------------------
# Keyed on (instance, effective params) so two instances -- or the test
# suite repointing SEARXNG_URL -- never collide. The value is the parsed
# SearXNG JSON, so the text block and the structured content are rendered
# from it identically whether it arrived over the wire or from memory.
_SearchKey = tuple[str, tuple[tuple[str, str], ...]]


class _SearchCache:
    """Bounded TTL cache with in-flight de-duplication.

    The tool is synchronous and runs on anyio's worker threads, so the
    lock is held only for bookkeeping, never across a network call. A
    second thread asking for a key that is already being fetched waits
    for that fetch instead of issuing a duplicate. Nothing is cached on
    failure, so a transient outage is not remembered for the whole TTL.
    """

    def __init__(
        self,
        ttl: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[_SearchKey, tuple[float, dict]] = OrderedDict()
        self._inflight: dict[_SearchKey, threading.Event] = {}

    @property
    def enabled(self) -> bool:
        return self._ttl > 0 and self._max > 0

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._inflight.clear()

    def _live(self, key: _SearchKey) -> dict | None:
        """The value for `key` if present and unexpired; caller holds the lock."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)  # LRU: most recent last
        return data

    def get_or_fetch(self, key: _SearchKey, fetch: Callable[[], dict]) -> dict:
        """Return the response for `key`, fetching it at most once."""
        if not self.enabled:
            return fetch()

        with self._lock:
            cached = self._live(key)
            if cached is not None:
                logger.debug("search_web cache hit: %r", key[1])
                return cached
            event = self._inflight.get(key)
            leader = event is None
            if leader:
                event = threading.Event()
                self._inflight[key] = event

        if not leader:
            # Another thread is already fetching this key: wait for its
            # result instead of issuing a duplicate. If it failed there is
            # nothing cached, so fall through and fetch for ourselves.
            logger.debug("search_web awaiting in-flight query: %r", key[1])
            event.wait(SEARCH_TIMEOUT + 5.0)
            with self._lock:
                cached = self._live(key)
            return cached if cached is not None else fetch()

        try:
            data = fetch()
            # Store before the waiters below are woken, so they find it.
            with self._lock:
                self._entries[key] = (self._clock() + self._ttl, data)
                self._entries.move_to_end(key)
                while len(self._entries) > self._max:
                    self._entries.popitem(last=False)
            return data
        finally:
            # Always release the slot and wake waiters, success or not.
            with self._lock:
                self._inflight.pop(key, None)
            event.set()


_search_cache = _SearchCache(SEARCH_CACHE_TTL, SEARCH_CACHE_MAX)


def _do_search(params: dict[str, str]) -> dict:
    """One SearXNG JSON query; raises on transport or HTTP failure."""
    with httpx2.Client(timeout=SEARCH_TIMEOUT) as client:
        response = client.get(SEARXNG_URL, params=params)
        response.raise_for_status()
        return response.json()


@mcp.tool(structured_output=True, annotations=_WEB_TOOLS)
def search_web(
    query: str,
    max_results: Annotated[int, Field(ge=1, le=20)] = 5,
    categories: str = "",
    engines: str = "",
    language: str = "",
    time_range: Literal["day", "week", "month", "year"] | None = None,
    pageno: Annotated[int, Field(ge=1, le=10)] = 1,
) -> Annotated[CallToolResult, SearchWebResponse]:
    """Search the web using self-hosted SearXNG (no API keys required).

    Returns the results as readable text plus schema-validated
    structured content.

    Args:
        query: The search query string.
        max_results: Number of results to return (default: 5).
        categories: Comma-separated SearXNG categories to search.
            Defaults to general,it,q&a,science. Examples: "news",
            "images", "general,news", "it".
        engines: Optional comma-separated engine names to restrict the
            search to, e.g. "google,duckduckgo". When set and
            `categories` is empty, only these engines run; if
            `categories` is also given, SearXNG runs those engines as
            well (the two lists are unioned, not intersected). Unknown
            names are ignored by SearXNG.
        language: Optional language code for results, e.g. "en",
            "de", "fr". Empty means SearXNG's instance default.
        time_range: Optional recency filter: "day", "week",
            "month", or "year". Empty means no filter.
        pageno: Results page number, starting at 1 (default).
    """
    params = {
        "q": query,
        "format": "json",
    }
    if engines:
        params["engines"] = engines
        # Sending categories too would union the category's engines in
        # (see SearXNG's parse_generic); only forward it when asked.
        if categories:
            params["categories"] = categories
    else:
        params["categories"] = categories or DEFAULT_CATEGORIES
    if language:
        params["language"] = language
    if time_range:
        params["time_range"] = time_range
    if int(pageno) > 1:
        params["pageno"] = str(pageno)

    try:
        key: _SearchKey = (SEARXNG_URL, tuple(sorted(params.items())))
        data = _search_cache.get_or_fetch(key, lambda: _do_search(params))
    except httpx2.HTTPError as err:
        logger.warning("SearXNG request to %s failed: %s", SEARXNG_URL, err)
        return _search_error(f"HTTP error connecting to SearXNG: {err}")
    except Exception as e:
        logger.exception("search_web raised")
        return _search_error(f"Unexpected error during search: {str(e)}")

    # Merge near-duplicates before the cut so merged hits don't shrink
    # the result set below max_results.
    results = _merge_duplicates(
        [_result_from_raw(r) for r in data.get("results", [])]
    )[:max_results]

    # Query suggestions (spellcheck-style corrections from engines).
    suggestions = [str(s) for s in (data.get("suggestions") or [])[:5]]

    # Engines that dropped out this query (CAPTCHA, suspension, timeout)
    # so the caller knows the result set is thinner than usual and why.
    unresponsive = [
        UnresponsiveEngine(engine=str(name), reason=str(reason))
        for name, reason in (data.get("unresponsive_engines") or [])[:8]
    ]

    output = []
    if suggestions:
        output.append("Did you mean: " + ", ".join(suggestions))
    if not results:
        output.append(f"No search results found for '{query}'.")
    else:
        for r in results:
            block = f"### {r.title}\n**URL:** {r.url}\n"
            if len(r.engines) > 1:
                block += f"**Engines:** {', '.join(r.engines)}\n"
            else:
                block += f"**Source Engine:** {r.engine}\n"
            if r.published:
                block += f"**Published:** {r.published}\n"
            block += f"**Snippet:** {r.snippet}\n"
            output.append(block)
    if unresponsive:
        notes = ", ".join(f"{e.engine} ({e.reason})" for e in unresponsive)
        output.append(f"Note: engines unavailable this query: {notes}")

    model = SearchWebResponse(
        results=results,
        suggestions=suggestions,
        unresponsive_engines=unresponsive,
    )
    return CallToolResult(
        content=[TextContent(type="text", text="\n---\n".join(output))],
        structured_content=model.model_dump(mode="json"),
    )


@mcp.tool(structured_output=True, annotations=_WEB_TOOLS)
def webfetch(
    url: str, max_chars: Annotated[int, Field(ge=500, le=50000)] = 20000
) -> Annotated[CallToolResult, WebFetchResponse]:
    """Fetch a web page and return its content as readable text/markdown.

    HTML pages are reduced to markdown; JSON and plain-text responses
    pass through unchanged. Content is truncated to max_chars. Also
    returns the same data as schema-validated structured content.

    Args:
        url: The http(s) URL to fetch.
        max_chars: Maximum characters of content to return
            (default: 20000).
    """
    try:
        body, content_type, encoding = _fetch_bytes(url)
    except ValueError as err:
        return _webfetch_error(url, f"Blocked: {err}")
    except httpx2.HTTPError as err:
        logger.warning("webfetch of %s failed: %s", url, err)
        return _webfetch_error(url, f"HTTP error fetching {url}: {err}")
    except Exception as e:
        logger.exception("webfetch of %s raised", url)
        return _webfetch_error(url, f"Unexpected error during fetch: {str(e)}")

    if _is_binary_content(body, content_type):
        return _webfetch_error(
            url,
            f"Not a readable page: the response is {content_type or 'unlabelled binary data'}.\n"
            "webfetch reads HTML and plain text; use fetch_pdf for PDF documents.",
            content_type=content_type,
        )

    text = body.decode(encoding, errors="replace")

    if "html" in content_type:
        title, result, metadata = _parse_html(text)
    else:
        title, result, metadata = None, text, None

    truncated = len(result) > max_chars
    if truncated:
        result = result[:max_chars] + f"\n\n[truncated at {max_chars} chars]"

    model = WebFetchResponse(
        url=url,
        content_type=content_type,
        title=title,
        content=result,
        truncated=truncated,
        chars=len(result),
        metadata=metadata,
    )
    return CallToolResult(
        content=[TextContent(type="text", text=result)],
        structured_content=model.model_dump(mode="json"),
    )


def _parse_html(html: str) -> tuple[str | None, str, PageMetadata | None]:
    """Reduce an HTML document to (page title, markdown body, metadata).

    Metadata is read from the untouched tree, then boilerplate is dropped
    and the main node is chosen before markdownifying it, so a sidebar or
    cookie banner that shares the page with the article does not leak in.
    """
    soup = BeautifulSoup(html, "html.parser")
    ld = _json_ld(soup)
    metadata = _extract_metadata(soup, ld)
    title = _page_title(soup, ld)

    _strip_boilerplate(soup)
    markdown = markdownify(str(_main_node(soup)), heading_style="ATX").strip()

    if title and title not in markdown[: len(title) + 64]:
        markdown = f"# {title}\n\n{markdown}"
    return title or None, markdown or "(no readable content found)", metadata


def _strip_boilerplate(soup: BeautifulSoup) -> None:
    """Remove chrome so it can't reach the main-content selection."""
    for tag in soup(_WEBFETCH_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(["header", "footer"]):
        # Keep a header/footer that belongs to an article (title, byline).
        if tag.find_parent(["article", "main"]) is None:
            tag.decompose()

    doomed = []
    for tag in soup.find_all(True):
        role = str(tag.get("role") or "").lower()
        marker = " ".join(tag.get("class") or []) + " " + str(tag.get("id") or "")
        if (
            role in _BOILERPLATE_ROLES
            or tag.get("aria-hidden") == "true"
            or (marker.strip() and _BOILERPLATE_RE.search(marker))
        ):
            doomed.append(tag)
    for tag in doomed:
        tag.decompose()


def _main_node(soup: BeautifulSoup) -> Tag:
    """The node most likely to be the page's content rather than its chrome.

    Trusts a semantic <article>/<main>/[role=main] when it holds enough
    text; otherwise scores paragraph containers by their text length less
    their link text (Readability's link-density idea) and returns the
    best, falling back to <body>.
    """
    semantic = soup.find_all(["article", "main"])
    role_main = soup.find(role="main")
    if role_main is not None:
        semantic.append(role_main)
    meaty = [tag for tag in semantic if len(tag.get_text(strip=True)) >= _MIN_MAIN_CHARS]
    if meaty:
        return max(meaty, key=lambda tag: len(tag.get_text(strip=True)))

    # `Tag.parent` is typed as a generic PageElement, so the parent walk
    # is `Any`: only `id()` and `.parent` are used, both of which exist.
    scores: dict[int, tuple[Any, float]] = {}
    for para in soup.find_all(["p", "pre", "blockquote", "td"]):
        text = para.get_text(" ", strip=True)
        if len(text) < _MIN_PARA_CHARS:
            continue
        link_len = sum(len(a.get_text(strip=True)) for a in para.find_all("a"))
        score = len(text) * (1.0 - min(link_len / len(text), 1.0))
        parent: Any = para.parent
        for depth in (0, 1):  # credit the parent, half-credit the grandparent
            if parent is None:
                break
            _, acc = scores.get(id(parent), (parent, 0.0))
            scores[id(parent)] = (parent, acc + score / (depth + 1))
            parent = parent.parent

    if scores:
        node, _ = max(scores.values(), key=lambda item: item[1])
        if node.name in ("p", "pre", "blockquote", "td") and node.parent is not None:
            node = node.parent
        if len(node.get_text(strip=True)) >= _MIN_MAIN_CHARS:
            return node

    return soup.body or soup


def _page_title(soup: BeautifulSoup, ld: dict[str, str]) -> str:
    """og:title beats twitter:title beats <title> beats JSON-LD headline."""
    meta_title = _meta(soup, "og:title") or _meta(soup, "twitter:title")
    if meta_title:
        return meta_title
    if soup.title is not None:
        text = soup.title.get_text(strip=True)
        if text:
            return text
    return ld.get("title") or ""


def _meta(soup: BeautifulSoup, key: str) -> str | None:
    """A meta tag's content, looked up by `property` or `name` (pages mix them)."""
    for attribute in ("property", "name"):
        attrs: dict[str, Any] = {attribute: key}
        tag = soup.find("meta", attrs=attrs)
        if tag is not None:
            content = str(tag.get("content") or "").strip()
            if content:
                return content
    return None


def _extract_metadata(soup: BeautifulSoup, ld: dict[str, str]) -> PageMetadata | None:
    """Open Graph / Twitter / <meta> first, JSON-LD as the fallback."""
    canonical_attrs: dict[str, Any] = {"rel": "canonical"}
    canonical = soup.find("link", attrs=canonical_attrs)
    canonical_url = None
    if canonical is not None:
        canonical_url = str(canonical.get("href") or "").strip() or None

    metadata = PageMetadata(
        description=_meta(soup, "og:description")
        or _meta(soup, "twitter:description")
        or _meta(soup, "description")
        or ld.get("description"),
        site_name=_meta(soup, "og:site_name"),
        author=_meta(soup, "author") or _meta(soup, "article:author") or ld.get("author"),
        published=(
            _meta(soup, "article:published_time")
            or _meta(soup, "date")
            or ld.get("published")
        ),
        image=_meta(soup, "og:image") or _meta(soup, "twitter:image") or ld.get("image"),
        canonical_url=canonical_url,
        type=_meta(soup, "og:type"),
    )
    if not any(metadata.model_dump().values()):
        return None
    return metadata


def _json_ld(soup: BeautifulSoup) -> dict[str, str]:
    """Fields we can use, from every JSON-LD block; first value wins."""
    out: dict[str, str] = {}
    script_attrs: dict[str, Any] = {"type": "application/ld+json"}
    for tag in soup.find_all("script", attrs=script_attrs):
        raw = tag.string or tag.get_text() or ""
        if len(raw) > _JSONLD_MAX_CHARS:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            continue  # malformed JSON-LD is common; ignore it
        for node in _json_ld_nodes(parsed):
            fields = (
                ("title", _first(node.get("headline")) or _first(node.get("name"))),
                ("description", _first(node.get("description"))),
                ("author", _first(node.get("author"))),
                (
                    "published",
                    _first(node.get("datePublished"))
                    or _first(node.get("dateModified")),
                ),
                ("image", _first(node.get("image"))),
            )
            for key, value in fields:
                if value and not out.get(key):
                    out[key] = value
    return out


def _json_ld_nodes(value: object) -> Iterator[dict]:
    """Walk a JSON-LD payload's objects, descending into arrays and @graph."""
    if isinstance(value, list):
        for item in value:
            yield from _json_ld_nodes(item)
    elif isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            yield from _json_ld_nodes(graph)
        else:
            yield value


def _first(value: object) -> str | None:
    """Unwrap a JSON-LD value: first list item, or a dict's name/url."""
    if isinstance(value, list):
        return _first(value[0]) if value else None
    if isinstance(value, dict):
        return _first(value.get("name")) or _first(value.get("url"))
    if isinstance(value, str):
        return value.strip() or None
    return None


def _is_binary_content(body: bytes, content_type: str) -> bool:
    """True when the response is a blob rather than something readable."""
    if any(binary in content_type for binary in _WEBFETCH_BINARY_TYPES):
        return True
    return body.startswith(_WEBFETCH_BINARY_MAGIC)


@mcp.tool(structured_output=False, annotations=_WEB_TOOLS)
def fetch_pdf(
    url: str,
    max_pages: Annotated[int, Field(ge=1, le=100)] = 20,
    max_chars: Annotated[int, Field(ge=500, le=50000)] = 20000,
) -> str:
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
        logger.warning("fetch_pdf of %s failed: %s", url, err)
        return f"HTTP error fetching {url}: {err}"
    except Exception as e:
        logger.exception("fetch_pdf of %s raised", url)
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


@mcp.tool(structured_output=False, annotations=_WEB_TOOLS)
def fetch_rss(url: str, max_items: Annotated[int, Field(ge=1, le=50)] = 10) -> str:
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
        logger.warning("fetch_rss of %s failed: %s", url, err)
        return f"HTTP error fetching {url}: {err}"
    except Exception as e:
        logger.exception("fetch_rss of %s raised", url)
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
            stamp = datetime(*entry.published_parsed[:6], tzinfo=UTC)
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


@mcp.tool(structured_output=False, annotations=_WEB_TOOLS)
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
                return (
                    _format_wayback_hit(
                        name, snapshot, ts, _clean_original(original)
                    )
                    + _degraded_note(notes)
                )
            notes.append(f"{name}: no snapshot")

    return "No snapshot found. Sources tried: " + "; ".join(notes) + "."


def _degraded_note(notes: list[str]) -> str:
    """Explain which tiers were unavailable when a later one answered.

    A hit is a hit, but "this came from archive.today because the
    Internet Archive timed out" is worth knowing before the caller
    trusts the snapshot date.
    """
    if not notes:
        return ""
    return "\n\nNote: other archive sources unavailable: " + "; ".join(notes)


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


@mcp.tool(structured_output=False, annotations=_LOCAL_TOOLS)
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


@mcp.tool(structured_output=False, annotations=_LOCAL_TOOLS)
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

    The endpoint is published on the LAN, so a missing token is a hard
    error rather than a warning: the previous behaviour served every
    tool unauthenticated for as long as the env var was absent. Set
    MCP_ALLOW_ANONYMOUS=1 to run it open on purpose, which is what
    debugging from inside npm-network wants.

    GET /health stays unauthenticated so a poller can say "up"
    without holding the token; it says nothing else.
    """

    def __init__(self, app, token: str | None):
        self.app = app
        self.token = token or ""
        if not self.token:
            if not MCP_ALLOW_ANONYMOUS:
                raise RuntimeError(
                    "SEARXNG_MCP_TOKEN is not set, and this endpoint is "
                    "published on the LAN. Set the token, or set "
                    "MCP_ALLOW_ANONYMOUS=1 to serve without authentication."
                )
            logger.warning(
                "SEARXNG_MCP_TOKEN not set and MCP_ALLOW_ANONYMOUS=1: "
                "endpoint is UNAUTHENTICATED"
            )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == HEALTH_PATH:
            from starlette.responses import JSONResponse

            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
            return

        if self.token and not self._authorised(scope):
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _authorised(self, scope) -> bool:
        """Compare the presented credential in constant time."""
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization", b"").decode("latin-1").strip()
        # RFC 7235 makes the scheme case-insensitive; a client sending
        # "bearer <token>" is not a typo worth a 401, and neither is a
        # stray space inside the header.
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(presented.strip().encode(), self.token.encode())


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

    # 0.0.0.0 is what the container publishes; loopback is what running
    # it for a smoke test wants, hence the env overrides.
    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
    )
