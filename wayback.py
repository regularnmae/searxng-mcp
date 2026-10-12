"""Internet Archive / archive.today lookup for `fetch_wayback`.

Three archive backends that flap independently are tried in order, with a
short per-tier cooldown so a sustained outage does not re-pay each dead
tier's timeout. The tier probes take an `httpx2.Client` from the caller
rather than building one: that keeps the wire configuration in `server.py`,
lets the parsers be tested against canned responses with no network, and
leaves this module free of HTTP construction. The SSRF guard runs in
`server.py` before anything here is reached.
"""

import re
import time

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


def find_snapshot(client, url: str) -> str:
    """Try the archive tiers in order and render the first hit.

    The tiers are tried in order: the IA availability API, the IA timemap
    (a separate IA backend that flaps independently of the API), then the
    archive.today mirrors as an IA-independent fallback. A tier that just
    failed transport-level is skipped for `WAYBACK_TIER_COOLDOWN` so a
    sustained outage does not re-pay each dead tier's timeout.

    `client` is supplied by the caller -- the lookups hit the archives,
    not the target -- and `url` is assumed already guarded. Returns the
    formatted hit (with a note naming any degraded tiers), or a report of
    every source tried when none had a snapshot.
    """
    tiers = (
        ("Internet Archive", _wayback_from_available),
        ("Internet Archive timemap", _wayback_from_timemap),
        ("archive.today", _wayback_from_archive_ph),
    )

    notes = []
    for name, probe in tiers:
        if time.monotonic() < _wayback_tier_cooldowns.get(name, 0.0):
            notes.append(f"{name}: recently unavailable, skipped")
            continue
        try:
            hit = probe(client, url)
        except Exception as err:
            _wayback_tier_cooldowns[name] = time.monotonic() + WAYBACK_TIER_COOLDOWN
            notes.append(f"{name}: {_short_reason(err)}")
            continue
        if hit:
            ts, original, snapshot = hit
            return (
                _format_wayback_hit(name, snapshot, ts, _clean_original(original))
                + _degraded_note(notes)
            )
        notes.append(f"{name}: no snapshot")

    return "No snapshot found. Sources tried: " + "; ".join(notes) + "."
