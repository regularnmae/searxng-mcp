"""Tests for the SSRF guard, `server._assert_fetchable`.

The container shares npm-network with bitcoind, monerod, lightningd,
valkey and searxng, and publishes 8000 on the LAN, so this guard is the
only thing standing between a fetched page's contents and a probe of
those services. It is tested directly, with literal IPs, so no test
needs DNS or a network.
"""

from __future__ import annotations

import socket

import pytest

import server

# Reachable-by-design addresses. These are the only class the guard
# should wave through, and they are written as literals so the tests
# never touch a resolver.
PUBLIC = [
    "http://93.184.216.34/",
    "https://1.1.1.1/",
    "https://8.8.8.8/resolve?dns=google",
    "https://[2606:2800:220:1:248:1893:25c8:1946]/ipv6",
]

# Addresses that must never be fetched, each with the reason it is
# dangerous rather than merely unusual.
BLOCKED = [
    # Loopback: the server itself, and anything bound to 127.0.0.0/8.
    "http://127.0.0.1:8000/mcp",
    "http://127.0.0.2/",
    "http://[::1]:8000/",
    # RFC1918 and friends: the rest of the podman networks.
    "http://10.0.0.1/",
    "http://192.168.1.1/admin",
    "http://172.16.0.5/",
    "http://[fc00::1]/",
    # Link-local, including the cloud metadata endpoint.
    "http://169.254.169.254/latest/meta-data/",
    "http://[fe80::1]/",
    # Unspecified, multicast and reserved.
    "http://0.0.0.0/",
    "http://[::]/",
    "http://224.0.0.1/",
    "http://[ff02::1]/",
    "http://240.0.0.1/",
    # Not globally routable, but not in the classic "private" classes
    # either -- these are what a blocklist approach misses.
    "http://100.64.0.1/",  # CGNAT / shared address space, RFC 6598
    "http://[64:ff9b::7f00:1]/",  # NAT64, embeds 127.0.0.1
    "http://198.18.0.1/",  # benchmarking, RFC 2544
    "http://192.0.2.1/",  # documentation, RFC 5737
    "http://203.0.113.5/",  # documentation, RFC 5737
    "http://[2001:db8::1]/",  # documentation, RFC 3849
    # IPv4 tunnelled inside IPv6; is_private/is_loopback cover this in
    # current Pythons, but it is exactly the shape an SSRF filter fumbles.
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:10.0.0.1]/",
]


@pytest.mark.parametrize("url", PUBLIC)
def test_public_addresses_are_allowed(url: str) -> None:
    assert server._assert_fetchable(url) is None


@pytest.mark.parametrize("url", BLOCKED)
def test_non_public_addresses_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="refusing"):
        server._assert_fetchable(url)


def test_refusal_names_the_address() -> None:
    """The message has to tell the caller *which* address, so a 403
    from a proxy is never mistaken for the guard firing."""
    with pytest.raises(ValueError, match="169.254.169.254"):
        server._assert_fetchable("http://169.254.169.254/latest/meta-data/")


@pytest.mark.parametrize(
    "url,reason",
    [
        ("ftp://example.com/file", "unsupported scheme"),
        ("file:///etc/passwd", "unsupported scheme"),
        ("gopher://example.com/", "unsupported scheme"),
        ("data:text/html,hi", "unsupported scheme"),
        ("http://", "could not parse a host"),
        ("https://:8080/path", "could not parse a host"),
    ],
)
def test_malformed_urls_are_refused(url: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        server._assert_fetchable(url)


def test_unresolvable_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(server.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="could not resolve host"):
        server._assert_fetchable("https://no-such-host.invalid/")


def test_partial_resolution_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that resolves to *several* addresses is only accepted if
    every one of them is public: an anycast or round-robin answer that
    hides a private address behind a public one is the classic bypass."""

    def split(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(server.socket, "getaddrinfo", split)
    with pytest.raises(ValueError, match="refusing"):
        server._assert_fetchable("https://mixed.example.com/")


def test_allow_private_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documented escape hatch for intentional internal fetches."""
    monkeypatch.setattr(server, "WEBFETCH_ALLOW_PRIVATE", True)
    # Bypassing the guard also skips resolution, so no DNS is needed.
    assert server._assert_fetchable("http://127.0.0.1:8000/mcp") is None
    assert server._assert_fetchable("http://10.0.0.1/") is None
