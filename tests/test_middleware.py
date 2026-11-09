"""The bearer-token middleware.

The middleware is plain ASGI -- a scope, a receive, a send -- so it is
driven directly. No socket, no uvicorn, no handshake.
"""

from __future__ import annotations

import anyio
import pytest

import server

TOKEN = "super-secret-token"


class Reached:
    """The app behind the middleware, recording whether it was hit."""

    def __init__(self) -> None:
        self.reached = False

    async def __call__(self, scope, receive, send) -> None:
        self.reached = True


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _drive(app, scope: dict) -> tuple[int, bytes]:
    """Send a request down the app and return (status, body).

    Status is 0 when nothing was sent, which is what a pass-through to
    an inner app that answers nothing looks like.
    """
    status = 0
    body = b""

    async def send(message) -> None:
        nonlocal status, body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, _receive, send)
    return status, body


def _scope(path: str = "/mcp", authorization: bytes | None = None) -> dict:
    headers = [(b"host", b"localhost:8000")]
    if authorization is not None:
        headers.append((b"authorization", authorization))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "headers": headers,
        "query_string": b"",
    }


def _wrapped(inner: Reached, token: str | None = TOKEN) -> server.BearerAuthMiddleware:
    return server.BearerAuthMiddleware(inner, token)


def _call(inner: Reached, scope: dict, token: str | None = TOKEN) -> tuple[int, bytes]:
    return anyio.run(_drive, _wrapped(inner, token), scope)


# --- the happy path --------------------------------------------------


def test_correct_token_passes() -> None:
    inner = Reached()
    _call(inner, _scope(authorization=f"Bearer {TOKEN}".encode()))
    assert inner.reached, "a valid token did not reach the app"


def test_scheme_is_case_insensitive() -> None:
    """RFC 7235 makes the auth scheme case-insensitive, so "bearer" is
    not a client error."""
    for scheme in (b"Bearer", b"bearer", b"BEARER", b"BeArEr"):
        inner = Reached()
        _call(inner, _scope(authorization=scheme + b" " + TOKEN.encode()))
        assert inner.reached, f"{scheme!r} was rejected"


def test_surrounding_whitespace_is_tolerated() -> None:
    inner = Reached()
    _call(inner, _scope(authorization=f"  Bearer   {TOKEN}".encode()))
    assert inner.reached


def test_missing_authorization_is_refused() -> None:
    inner = Reached()
    status, body = _call(inner, _scope())
    assert status == 401
    assert not inner.reached
    assert b"unauthorized" in body


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"Basic " + TOKEN.encode(),
        b"Bearer",
        b"Bearer ",
        b"Bearer wrong-token",
        b"Bearer " + TOKEN.upper().encode(),
        b"token " + TOKEN.encode(),
        TOKEN.encode(),
    ],
)
def test_bad_authorization_is_refused(header: bytes) -> None:
    inner = Reached()
    status, _ = _call(inner, _scope(authorization=header))
    assert status == 401, f"{header!r} was accepted"
    assert not inner.reached


def test_a_prefix_is_not_enough() -> None:
    """A truncated token must not match, whatever the comparison."""
    inner = Reached()
    status, _ = _call(inner, _scope(authorization=b"Bearer " + TOKEN[:-1].encode()))
    assert status == 401


# --- /health ----------------------------------------------------------


def test_health_needs_no_token() -> None:
    inner = Reached()
    status, body = _call(inner, _scope(path=server.HEALTH_PATH))
    assert status == 200
    assert not inner.reached, "/health is answered by the middleware itself"
    assert b"ok" in body


def test_health_answers_even_with_a_bad_token() -> None:
    inner = Reached()
    status, _ = _call(inner, _scope(path=server.HEALTH_PATH, authorization=b"Bearer nope"))
    assert status == 200


def test_health_says_nothing_more() -> None:
    inner = Reached()
    _, body = _call(inner, _scope(path=server.HEALTH_PATH))
    assert body.strip() == b'{"status":"ok"}'


def test_a_similar_path_is_not_health() -> None:
    inner = Reached()
    status, _ = _call(inner, _scope(path="/healthz"))
    assert status == 401


# --- other scope types ------------------------------------------------


def test_lifespan_scope_passes_through() -> None:
    inner = Reached()
    app = _wrapped(inner)

    async def _run() -> None:
        await app({"type": "lifespan"}, _receive, _noop)

    anyio.run(_run)
    assert inner.reached, "the lifespan scope never reached the app"


async def _noop(*_args) -> None:
    return None


# --- no token configured ---------------------------------------------


def test_missing_token_is_a_startup_error(monkeypatch) -> None:
    monkeypatch.setattr(server, "MCP_ALLOW_ANONYMOUS", False)
    with pytest.raises(RuntimeError, match="SEARXNG_MCP_TOKEN"):
        server.BearerAuthMiddleware(Reached(), None)


def test_missing_token_with_the_escape_hatch(monkeypatch, caplog) -> None:
    monkeypatch.setattr(server, "MCP_ALLOW_ANONYMOUS", True)
    with caplog.at_level("WARNING", logger="searxng-mcp"):
        server.BearerAuthMiddleware(Reached(), None)
    assert "UNAUTHENTICATED" in caplog.text


def test_empty_token_counts_as_missing(monkeypatch) -> None:
    monkeypatch.setattr(server, "MCP_ALLOW_ANONYMOUS", False)
    with pytest.raises(RuntimeError):
        server.BearerAuthMiddleware(Reached(), "")


def test_anonymous_serves_everything(monkeypatch) -> None:
    monkeypatch.setattr(server, "MCP_ALLOW_ANONYMOUS", True)
    inner = Reached()
    _call(inner, _scope(), token=None)
    assert inner.reached


# --- /ready -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_ready_cache():
    """The readiness answer is a module global; start every test cold."""
    server._ready_cache = None
    server._ready_lock = None
    yield
    server._ready_cache = None
    server._ready_lock = None


def test_ready_is_open_and_reports_the_engine(monkeypatch) -> None:
    async def _ok() -> tuple[dict, int]:
        return {"status": "ready", "searxng": "ok"}, 200

    monkeypatch.setattr(server, "_probe_readiness", _ok)
    status, body = _call(Reached(), _scope(path="/ready"))
    assert status == 200
    assert b'"ready"' in body


def test_ready_reports_an_unready_engine_without_a_token(monkeypatch) -> None:
    async def _bad() -> tuple[dict, int]:
        return {"status": "unready", "error": "connection refused"}, 503

    monkeypatch.setattr(server, "_probe_readiness", _bad)
    status, body = _call(Reached(), _scope(path="/ready"))
    assert status == 503
    assert b"connection refused" in body


def test_a_similar_path_is_not_ready() -> None:
    assert _call(Reached(), _scope(path="/readyz"))[0] == 401


class _HealthClient:
    """A minimal stand-in for httpx2.Client in the readiness probe."""

    def __init__(self, response) -> None:
        self.response = response
        self.urls: list[str] = []

    def __enter__(self) -> _HealthClient:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get(self, url: str):
        self.urls.append(url)
        return self.response


def _set_health_client(monkeypatch, response) -> _HealthClient:
    fake = _HealthClient(response)
    monkeypatch.setattr(server.httpx2, "Client", lambda **kwargs: fake)
    return fake


def test_the_probe_asks_healthz_at_the_origin(monkeypatch) -> None:
    from tests.fakes import FakeResponse

    fake = _set_health_client(monkeypatch, FakeResponse(status_code=200))
    body, status = anyio.run(server._probe_readiness)
    assert (status, body) == (200, {"status": "ready", "searxng": "ok"})
    assert fake.urls == [server.SEARXNG_HEALTH_URL]
    assert fake.urls[0].endswith("/healthz")


def test_the_probe_turns_a_bad_status_into_unready(monkeypatch) -> None:
    from tests.fakes import FakeResponse

    _set_health_client(monkeypatch, FakeResponse(status_code=503))
    body, status = anyio.run(server._probe_readiness)
    assert status == 503
    assert body["status"] == "unready"


def test_the_probe_turns_a_transport_error_into_unready(monkeypatch) -> None:
    from tests.fakes import FakeResponse

    class _Boom(_HealthClient):
        def get(self, url: str):
            raise OSError("connection refused")

    monkeypatch.setattr(server.httpx2, "Client", lambda **kwargs: _Boom(FakeResponse()))
    body, status = anyio.run(server._probe_readiness)
    assert status == 503
    assert "connection refused" in body["error"]


def test_ready_is_cached_between_requests(monkeypatch) -> None:
    calls = []

    async def _ok() -> tuple[dict, int]:
        calls.append(1)
        return {"status": "ready", "searxng": "ok"}, 200

    monkeypatch.setattr(server, "_probe_readiness", _ok)
    anyio.run(server._readiness)
    anyio.run(server._readiness)
    assert len(calls) == 1
