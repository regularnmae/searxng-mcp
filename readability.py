"""Hand-rolled HTML readability pass for `webfetch`.

`_parse_html` reduces an HTML document to a title, a markdown body and
best-effort metadata without pulling in a readability library: metadata is
read from the untouched tree, then chrome is dropped and the main node is
chosen before markdownifying it. It is pure -- no `mcp`, config or network
state -- so it can be reasoned about and tested on its own.

`_is_binary_content` lives here too: deciding whether a fetched response is
a readable page is the same judgement as reducing one.
"""

import json
import re
from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify
from pydantic import BaseModel

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


