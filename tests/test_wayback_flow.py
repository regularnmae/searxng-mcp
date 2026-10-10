"""`fetch_wayback` orchestration: tier order, cooldown, degraded reports.

fetch_wayback builds its own HTTP client rather than going through
`_http_client`, so there is no transport to swap out -- the three probes
are replaced instead. The autouse fixture below is what keeps this module
off the network: without it these tests would really query the Internet
Archive, which is both slow and flaky.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

import server
from tests.fakes import Hit, make_probe, make_raising_probe

AVAILABLE_HIT: Hit = (
    "20020120142510",
    "http://example.com/",
    "https://web.archive.org/web/20020120142510/http://example.com/",
)
TIMEMAP_HIT: Hit = (
    "20261010120000",
    "http://example.com/",
    "https://web.archive.org/web/20261010120000/http://example.com/",
)
PH_HIT: Hit = (
    "20260101000000",
    "http://example.com/",
    "https://archive.ph/20260101000000/http://example.com/",
)


@pytest.fixture(autouse=True)
def _clear_cooldowns() -> Iterator[None]:
    server._wayback_tier_cooldowns.clear()
    yield
    server._wayback_tier_cooldowns.clear()


@pytest.fixture(autouse=True)
def _dead_tiers(dead_wayback_tiers) -> None:
    """Every tier reports 'nothing' unless a test says otherwise."""
    return None


def _override(monkeypatch: pytest.MonkeyPatch, name: str, probe) -> None:
    monkeypatch.setattr(server, name, probe)


@pytest.mark.anyio
async def test_uses_the_first_tier_that_answers(call, monkeypatch) -> None:
    _override(monkeypatch, "_wayback_from_available", make_probe("available", AVAILABLE_HIT))
    fallen_through: list[str] = []

    def timemap(client, url) -> Hit | None:
        fallen_through.append("timemap")
        return None

    monkeypatch.setattr(server, "_wayback_from_timemap", timemap)

    text = await call("fetch_wayback", url="http://example.com/")
    assert "**Source:** Internet Archive" in text
    assert "web.archive.org/web/20020120142510" in text
    assert fallen_through == [], "a hit on tier 1 must not fall through to tier 2"


@pytest.mark.anyio
async def test_falls_through_when_a_tier_has_no_snapshot(call, monkeypatch) -> None:
    _override(monkeypatch, "_wayback_from_available", make_probe("available", None))
    _override(monkeypatch, "_wayback_from_timemap", make_probe("timemap", TIMEMAP_HIT))

    text = await call("fetch_wayback", url="http://example.com/")
    assert "**Source:** Internet Archive timemap" in text
    assert "20261010120000" in text


@pytest.mark.anyio
async def test_falls_through_when_a_tier_raises(call, monkeypatch) -> None:
    _override(
        monkeypatch,
        "_wayback_from_available",
        make_raising_probe("available", TimeoutError("read timeout")),
    )
    _override(monkeypatch, "_wayback_from_archive_ph", make_probe("ph", PH_HIT))

    text = await call("fetch_wayback", url="http://example.com/")
    assert "**Source:** archive.today" in text
    # A hit from a fallback should still say why the earlier tiers missed.
    assert "timed out" in text


@pytest.mark.anyio
async def test_degraded_tiers_are_named_on_success(call, monkeypatch) -> None:
    _override(
        monkeypatch,
        "_wayback_from_available",
        make_raising_probe("available", ConnectionError("reset by peer")),
    )
    _override(monkeypatch, "_wayback_from_timemap", make_probe("timemap", None))
    _override(monkeypatch, "_wayback_from_archive_ph", make_probe("ph", PH_HIT))

    text = await call("fetch_wayback", url="http://example.com/")
    assert "other archive sources unavailable" in text
    assert "reset by peer" in text


@pytest.mark.anyio
async def test_no_note_when_the_first_tier_answers(call, monkeypatch) -> None:
    _override(monkeypatch, "_wayback_from_available", make_probe("available", AVAILABLE_HIT))
    text = await call("fetch_wayback", url="http://example.com/")
    assert "other archive sources unavailable" not in text


@pytest.mark.anyio
async def test_failing_tier_is_put_in_cooldown(call, monkeypatch) -> None:
    _override(
        monkeypatch,
        "_wayback_from_available",
        make_raising_probe("available", ConnectionError("refused")),
    )
    _override(monkeypatch, "_wayback_from_archive_ph", make_probe("ph", PH_HIT))

    await call("fetch_wayback", url="http://example.com/")
    assert "Internet Archive" in server._wayback_tier_cooldowns
    assert server._wayback_tier_cooldowns["Internet Archive"] > time.monotonic()


@pytest.mark.anyio
async def test_success_does_not_cool_a_tier_down(call, monkeypatch) -> None:
    _override(monkeypatch, "_wayback_from_available", make_probe("available", None))
    await call("fetch_wayback", url="http://example.com/")
    assert "Internet Archive" not in server._wayback_tier_cooldowns


@pytest.mark.anyio
async def test_cooled_down_tier_is_skipped_not_retried(call, monkeypatch) -> None:
    server._wayback_tier_cooldowns["Internet Archive"] = time.monotonic() + 300

    text = await call("fetch_wayback", url="http://example.com/")
    assert "recently unavailable, skipped" in text
    assert "No snapshot found" in text


@pytest.mark.anyio
async def test_cooldown_expires(call, monkeypatch) -> None:
    server._wayback_tier_cooldowns["Internet Archive"] = time.monotonic() - 1

    text = await call("fetch_wayback", url="http://example.com/")
    assert "skipped" not in text


@pytest.mark.anyio
async def test_reports_every_source_it_tried(call, monkeypatch) -> None:
    text = await call("fetch_wayback", url="http://example.com/")
    for tier in ("Internet Archive:", "Internet Archive timemap:", "archive.today:"):
        assert tier in text


@pytest.mark.anyio
async def test_refuses_an_internal_url(call, monkeypatch) -> None:
    """The lookup goes to the archives, not the target, but the boundary
    stays consistent with the other tools."""
    text = await call("fetch_wayback", url="http://169.254.169.254/latest/meta-data/")
    assert "Blocked" in text
