"""search_web's result cache: TTL, LRU eviction and in-flight de-dup.

The cache is process-global, so `conftest` clears it around every test.
The unit tests drive `_SearchCache` directly with a counting fake fetch
and an injected clock, which is what makes TTL and concurrency behaviour
deterministic; the last few go through the real tool and the loopback
SearXNG stub, counting the requests the stub actually received.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

import server

KEY = ("http://searxng/search", (("format", "json"), ("q", "test query")))


def _counting_fetch(calls: list[int]) -> Callable[[], dict]:
    """A fake fetch that records each call and echoes an incrementing id."""

    def fetch() -> dict:
        calls.append(len(calls))
        return {"n": len(calls)}

    return fetch


# --- unit: TTL and eviction ------------------------------------------


def test_hit_within_ttl_does_not_refetch() -> None:
    now = [1000.0]
    cache = server._SearchCache(ttl=10.0, max_entries=8, clock=lambda: now[0])
    calls: list[int] = []
    fetch = _counting_fetch(calls)

    first = cache.get_or_fetch(KEY, fetch)
    now[0] = 1009.0
    second = cache.get_or_fetch(KEY, fetch)

    assert first is second
    assert calls == [0]


def test_entry_expires_after_ttl() -> None:
    now = [1000.0]
    cache = server._SearchCache(ttl=10.0, max_entries=8, clock=lambda: now[0])
    calls: list[int] = []
    fetch = _counting_fetch(calls)

    cache.get_or_fetch(KEY, fetch)
    now[0] = 1010.1
    cache.get_or_fetch(KEY, fetch)

    assert len(calls) == 2


def test_lru_evicts_the_least_recently_used() -> None:
    now = [0.0]
    cache = server._SearchCache(ttl=100.0, max_entries=2, clock=lambda: now[0])
    fetched: list[str] = []

    def fetch(tag: str):
        def run() -> dict:
            fetched.append(tag)
            return {"tag": tag}

        return run

    for tag in ("a", "b", "c"):
        cache.get_or_fetch(("u", (("q", tag),)), fetch(tag))
    # "a" was evicted by "c"; touching it again must refetch.
    cache.get_or_fetch(("u", (("q", "a"),)), fetch("a"))

    assert fetched == ["a", "b", "c", "a"]


def test_disabled_cache_fetches_every_time() -> None:
    cache = server._SearchCache(ttl=0.0, max_entries=8)
    calls: list[int] = []
    fetch = _counting_fetch(calls)

    cache.get_or_fetch(KEY, fetch)
    cache.get_or_fetch(KEY, fetch)

    assert len(calls) == 2
    assert not cache.enabled


def test_failure_is_not_cached() -> None:
    now = [0.0]
    cache = server._SearchCache(ttl=100.0, max_entries=8, clock=lambda: now[0])
    calls: list[int] = []

    def fetch() -> dict:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    with pytest.raises(RuntimeError):
        cache.get_or_fetch(KEY, fetch)
    # The inflight slot must be released and nothing cached, so a retry
    # actually goes back out.
    assert cache.get_or_fetch(KEY, fetch) == {"ok": True}
    assert len(calls) == 2


# --- unit: in-flight de-duplication ----------------------------------


def test_concurrent_identical_keys_share_one_fetch() -> None:
    now = [0.0]
    cache = server._SearchCache(ttl=100.0, max_entries=8, clock=lambda: now[0])
    calls: list[int] = []
    entered = threading.Event()
    release = threading.Event()

    def fetch() -> dict:
        calls.append(1)
        entered.set()
        release.wait(5.0)
        return {"ok": True}

    results: list[dict] = []

    def worker() -> None:
        results.append(cache.get_or_fetch(KEY, fetch))

    leader = threading.Thread(target=worker)
    leader.start()
    assert entered.wait(5.0)  # leader is inside fetch
    waiter = threading.Thread(target=worker)
    waiter.start()
    time.sleep(0.05)  # let the waiter reach the inflight wait
    release.set()
    leader.join(5.0)
    waiter.join(5.0)

    assert calls == [1]
    assert results == [{"ok": True}, {"ok": True}]


# --- integration: through the tool and the SearXNG stub --------------


@pytest.mark.anyio
async def test_repeated_query_hits_the_cache(client, searxng) -> None:
    await client.call_tool("search_web", {"query": "test query"})
    await client.call_tool("search_web", {"query": "test query"})
    assert len(searxng.queries) == 1


@pytest.mark.anyio
async def test_cache_key_separates_filters(client, searxng) -> None:
    await client.call_tool("search_web", {"query": "mcp"})
    await client.call_tool("search_web", {"query": "mcp", "categories": "news"})
    await client.call_tool("search_web", {"query": "mcp", "pageno": 2})
    assert len(searxng.queries) == 3


@pytest.mark.anyio
async def test_expired_entry_is_refetched(client, searxng, monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(
        server, "_search_cache", server._SearchCache(10.0, 128, clock=lambda: now[0])
    )
    await client.call_tool("search_web", {"query": "test query"})
    now[0] = 5.0
    await client.call_tool("search_web", {"query": "test query"})  # cached
    now[0] = 11.0
    await client.call_tool("search_web", {"query": "test query"})  # expired
    assert len(searxng.queries) == 2


@pytest.mark.anyio
async def test_ttl_zero_disables_caching(client, searxng, monkeypatch) -> None:
    monkeypatch.setattr(server, "_search_cache", server._SearchCache(0.0, 128))
    await client.call_tool("search_web", {"query": "test query"})
    await client.call_tool("search_web", {"query": "test query"})
    assert len(searxng.queries) == 2
