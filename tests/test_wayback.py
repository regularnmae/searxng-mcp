"""The wayback tool's three parsers: string handling over canned payloads.

The archive sources flap independently, which is why there are three of
them, and the parsing is all regex -- so it can be pinned down without a
network. `fetch_wayback`'s orchestration lives in test_wayback_flow.py.
"""

from __future__ import annotations

import pytest

import server
from tests.fakes import FakeClient, FakeResponse

# The parsers below are pure string handling, so they are tested directly
# with canned payloads. `fetch_wayback` itself is orchestration and lives
# in test_wayback_flow.py.



# --- small pure helpers ---------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("20020120142510", "2002-01-20 14:25:10 UTC"),
        ("20261010120000", "2026-10-10 12:00:00 UTC"),
        ("not-a-timestamp", "not-a-timestamp"),
        ("20020120", "20020120"),
    ],
)
def test_format_cdx_timestamp(raw: str, expected: str) -> None:
    assert server._format_cdx_timestamp(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://example.com:80/path", "http://example.com/path"),
        ("https://example.com:443/path", "https://example.com/path"),
        ("https://example.com:8443/path", "https://example.com:8443/path"),
        ("http://example.com:8080/", "http://example.com:8080/"),
    ],
)
def test_clean_original(raw: str, expected: str) -> None:
    assert server._clean_original(raw) == expected


def test_short_reason_reads_like_a_human_wrote_it() -> None:
    class Timeout(Exception):
        pass

    Timeout.__name__ = "ReadTimeout"
    assert server._short_reason(Timeout("x")) == "timed out"

    class ConnectError(Exception):
        pass

    ConnectError.__name__ = "ConnectError"
    assert server._short_reason(ConnectError("x")) == "connection failed"

    assert server._short_reason(RuntimeError("archive.ph rate-limited")) == (
        "archive.ph rate-limited"
    )


def test_wayback_hit_explains_the_live_marker() -> None:
    text = server._format_wayback_hit(
        "archive.today", "https://archive.ph/20300101000000/https://x/", "20300101000000", "https://x/"
    )
    assert "live snapshot" in text
    assert "webfetch the snapshot URL" in text


def test_wayback_hit_renders_a_dated_snapshot() -> None:
    text = server._format_wayback_hit(
        "Internet Archive",
        "https://web.archive.org/web/20020120142510/http://x/",
        "20020120142510",
        "http://x/",
    )
    assert "2002-01-20 14:25:10 UTC" in text
    assert "**Original URL:** http://x/" in text
    assert "**Source:** Internet Archive" in text


# --- tier 1: the availability API ------------------------------------


def test_wayback_available_hit() -> None:
    client = FakeClient(
        FakeResponse(
            json_data={
                "archived_snapshots": {
                    "closest": {"url": "https://web.archive.org/web/20020120142510/http://example.com/"}
                }
            }
        )
    )
    hit = server._wayback_from_available(client, "http://example.com/")
    assert hit is not None
    ts, original, snapshot = hit
    assert ts == "20020120142510"
    assert original == "http://example.com/"
    assert snapshot == "https://web.archive.org/web/20020120142510/http://example.com/"
    assert client.calls[0]["url"] == server.WAYBACK_AVAILABLE_URL


def test_wayback_available_no_snapshot() -> None:
    client = FakeClient(FakeResponse(json_data={"archived_snapshots": {}}))
    assert server._wayback_from_available(client, "http://example.com/") is None


def test_wayback_available_outage_page_looks_like_no_snapshot() -> None:
    """During the 2026-10 IA incident this API answered with HTML."""
    client = FakeClient(FakeResponse(text="<html>503 Service Unavailable</html>"))
    assert server._wayback_from_available(client, "http://example.com/") is None


def test_wayback_available_garbage_json() -> None:
    client = FakeClient(FakeResponse(json_data={"archived_snapshots": {"closest": {}}}))
    assert server._wayback_from_available(client, "http://example.com/") is None


# --- tier 2: the timemap ---------------------------------------------


TIMEMAP = """\
<https://web.archive.org/web/19980126065830/http://example.com/>; rel="first memento"
<https://web.archive.org/web/20261010000000/http://example.com/>; rel="memento"
<https://web.archive.org/web/20261010010203/http://example.com/>; rel="last memento"
"""


def test_wayback_timemap_prefers_the_newest() -> None:
    client = FakeClient(FakeResponse(text=TIMEMAP))
    hit = server._wayback_from_timemap(client, "http://example.com/")
    assert hit is not None
    ts, original, snapshot = hit
    assert ts == "20261010010203"
    assert original == "http://example.com/"
    assert client.calls[0]["url"] == server.WAYBACK_TIMEMAP_URL + "http://example.com/"


def test_wayback_timemap_first_only() -> None:
    text = (
        '<https://web.archive.org/web/19980126065830/http://example.com/>;'
        ' rel="first memento"; datetime="Mon, 26 Jan 1998 06:58:30 GMT"\n'
    )
    hit = server._wayback_from_timemap(FakeClient(FakeResponse(text=text)), "http://example.com/")
    assert hit is not None
    assert hit[0] == "19980126065830"


def test_wayback_timemap_no_mementos() -> None:
    assert server._wayback_from_timemap(FakeClient(FakeResponse(text="")), "http://x/") is None


# --- tier 3: archive.today -------------------------------------------


def test_wayback_archive_ph_hit() -> None:
    client = FakeClient(
        FakeResponse(status_code=302, headers={"location": "https://archive.ph/20260101000000/http://example.com/"})
    )
    hit = server._wayback_from_archive_ph(client, "http://example.com/")
    assert hit is not None
    ts, original, snapshot = hit
    assert ts == "20260101000000"
    assert snapshot == "https://archive.ph/20260101000000/http://example.com/"


@pytest.mark.parametrize("host", ["archive.today", "archive.is", "archive.li"])
def test_wayback_archive_ph_accepts_every_mirror(host: str) -> None:
    client = FakeClient(
        FakeResponse(status_code=302, headers={"location": f"https://{host}/20260101000000/http://example.com/"})
    )
    hit = server._wayback_from_archive_ph(client, "http://example.com/")
    assert hit is not None and hit[0] == "20260101000000"


def test_wayback_archive_ph_no_snapshot() -> None:
    client = FakeClient(FakeResponse(status_code=404))
    assert server._wayback_from_archive_ph(client, "http://example.com/") is None


def test_wayback_archive_ph_rate_limit_raises() -> None:
    client = FakeClient(FakeResponse(status_code=429))
    with pytest.raises(RuntimeError, match="rate-limited"):
        server._wayback_from_archive_ph(client, "http://example.com/")


