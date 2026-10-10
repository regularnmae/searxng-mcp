"""Redirect handling.

`_assert_fetchable` only ever sees the URL the caller passed. The client
then follows up to five redirects on its own, so without a per-hop check
a page on the public internet that answers 302 with an internal Location
walks straight into npm-network -- which is where bitcoind, monerod,
lightningd, valkey and searxng live.

These tests drive the real client built by `server._http_client` (real
redirect policy, real event hooks) against a mock wire, so they exercise
the path a real redirect takes rather than a stub of it.
"""

from __future__ import annotations

import httpx2
import pytest

# A public address written as a literal, so the initial request passes
# the guard without any DNS.
PUBLIC = "http://93.184.216.34/start"
REDIRECT_TARGET = "http://127.0.0.1:9/bitcoind"
REDIRECT_TO_SERVICE = "http://169.254.169.254/latest/meta-data/"


def _redirect(target: str) -> httpx2.Response:
    return httpx2.Response(302, headers={"Location": target})


@pytest.mark.anyio
async def test_redirect_to_internal_address_is_refused(
    call, mock_http
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return _redirect(REDIRECT_TARGET)
        pytest.fail(f"guard did not block the hop: {request.url}")
        raise AssertionError  # pragma: no cover - unreachable

    mock_http(handler)
    text = await call("webfetch", url=PUBLIC)
    assert "Blocked" in text
    assert "127.0.0.1" in text


@pytest.mark.anyio
async def test_redirect_to_metadata_endpoint_is_refused(call, mock_http) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _redirect(REDIRECT_TO_SERVICE)

    mock_http(handler)
    text = await call("webfetch", url=PUBLIC)
    assert "Blocked" in text
    assert "169.254.169.254" in text


@pytest.mark.anyio
async def test_relative_redirect_is_resolved_against_the_original(
    call, mock_http
) -> None:
    """A Location like /admin is resolved against the requesting URL, not
    the cwd, so the guard sees the real target."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"Location": "/?redirect=1"})
        return httpx2.Response(200, text="# followed once\n")

    mock_http(handler)
    text = await call("webfetch", url=PUBLIC)
    assert "followed once" in text


@pytest.mark.anyio
async def test_redirect_chain_between_public_hosts_is_followed(
    call, mock_http
) -> None:
    """The guard must not break ordinary redirect chains."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return _redirect("http://1.1.1.1/second")
        if request.url.path == "/second":
            return _redirect("http://8.8.8.8/final")
        return httpx2.Response(200, text="# landed\n")

    mock_http(handler)
    text = await call("webfetch", url=PUBLIC)
    assert "landed" in text


@pytest.mark.anyio
async def test_fetch_pdf_and_fetch_rss_are_guarded_too(call, mock_http) -> None:
    """One guard, three tools: the fix belongs in `_http_client`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _redirect(REDIRECT_TARGET)

    mock_http(handler)
    for tool in ("webfetch", "fetch_pdf", "fetch_rss"):
        text = await call(tool, url=PUBLIC)
        assert "Blocked" in text, f"{tool} followed a redirect to {REDIRECT_TARGET}"


@pytest.mark.anyio
async def test_redirect_loop_is_capped(call, mock_http) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _redirect(str(request.url))

    mock_http(handler)
    text = await call("webfetch", url=PUBLIC)
    assert "Blocked" in text or "HTTP error" in text
