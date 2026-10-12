"""Shared fixtures.

Every test talks to the server through the SDK's in-memory client, so no
test opens a socket of its own. Two exceptions need a real socket and
get one only on loopback: the SearXNG stub, because `search_web` builds
its own client rather than going through `_http_client` (see the note in
server.py), and the guard tests, which are about the guard.

`mock_http` swaps the transport underneath the *production* client
configuration built by `server._http_client`, which keeps the UA, the
redirect policy and every event hook intact and only replaces the wire.
"""

from __future__ import annotations

import copy
import json
import threading
import urllib.parse
from collections.abc import AsyncIterator, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2
import pytest
from mcp import Client

import server
from server import mcp

# A tool handler: given the request, decide what the wire answers.
Handler = Callable[[httpx2.Request], httpx2.Response]

RESULTS = {
    "query": "test query",
    "results": [
        {
            "title": "First hit",
            "url": "https://example.com/1",
            "content": "A snippet about testing.",
            "engine": "duckduckgo",
            "publishedDate": "2026-10-01T12:00:00",
        },
        {
            "title": "Second hit",
            "url": "https://example.com/2",
            "content": "Another snippet.",
            "engine": "stackoverflow",
        },
        {
            "title": "Third hit",
            "url": "https://example.com/3",
            "content": "Never returned.",
            "engine": "wikipedia",
        },
    ],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    """An in-memory client connected to the real server object."""
    async with Client(mcp, raise_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_search_cache() -> Iterator[None]:
    """search_web's cache is process-global; don't let it leak between
    tests, which repoint SEARXNG_URL and mutate payloads."""
    server._search_cache.clear()
    yield
    server._search_cache.clear()


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], None]:
    """Point every fetch tool at a canned in-memory wire.

    Returns an installer: pass a `httpx2.Request -> httpx2.Response`
    handler and the next `_fetch_bytes` call will drive the real client
    (redirects, hooks, UA, all of it) against it. Because redirect
    handling lives in the client and not the transport, a handler that
    answers a 302 exercises the same code path a real redirect does.
    """
    real_client_factory = server._http_client

    def install(handler: Handler) -> None:
        def factory(timeout: float = 15.0, **kwargs: Any) -> httpx2.Client:
            return real_client_factory(
                timeout, transport=httpx2.MockTransport(handler), **kwargs
            )

        monkeypatch.setattr(server, "_http_client", factory)

    return install


@pytest.fixture
def call(client: Client) -> Callable[..., Any]:
    """Call a tool and hand back the text the model would read."""

    async def _call(name: str, **arguments: Any) -> str:
        result = await client.call_tool(name, arguments)
        assert not result.is_error, f"tool {name} failed: {result.content}"
        return result.content[0].text  # type: ignore[union-attr]

    return _call


class _Stub:
    """A loopback SearXNG that records what it was asked and answers
    whatever the test put in `payload`."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list[tuple[str, dict[str, str]]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parsed = urllib.parse.urlparse(self.path)
                stub.requests.append(
                    (parsed.path, dict(urllib.parse.parse_qsl(parsed.query)))
                )
                body = json.dumps(stub.payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # silence the access log
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def search_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/search"

    @property
    def queries(self) -> list[dict[str, str]]:
        return [params for path, params in self.requests if path == "/search"]

    def __enter__(self) -> _Stub:
        threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def searxng(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Stub]:
    """Point the server at a loopback SearXNG stub.

    The payload is a fresh copy of RESULTS per test: tests mutate it, and
    a shared dict would leak one test's edits into the next.
    """
    with _Stub(copy.deepcopy(RESULTS)) as stub:
        monkeypatch.setattr(server, "SEARXNG_URL", stub.search_url)
        yield stub


# fetch_wayback builds its own client, so no transport injection reaches
# it. Any test that calls it replaces the three probes instead, or it
# makes real requests to the Internet Archive.
WAYBACK_PROBES = (
    "_wayback_from_available",
    "_wayback_from_timemap",
    "_wayback_from_archive_ph",
)


@pytest.fixture
def dead_wayback_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every archive source reports 'nothing there'."""
    from tests.fakes import make_probe

    for name in WAYBACK_PROBES:
        monkeypatch.setattr(server, name, make_probe(name, None))
