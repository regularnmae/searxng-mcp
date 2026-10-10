"""Test doubles shared across the suite."""

from __future__ import annotations

from collections.abc import Callable

# A probe: (client, url) -> hit tuple or None.
Probe = Callable[[object, str], object]
Hit = tuple[str, str, str]


class FakeResponse:
    """Just enough of an httpx response for the wayback probes."""

    def __init__(
        self,
        text: str = "",
        json_data: dict | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict:
        assert self._json is not None, "no JSON payload on this fake response"
        return self._json

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    """Stands in for the httpx client the probes take as an argument."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


def make_probe(name: str, value: Hit | None) -> Probe:
    def _probe(client, url) -> Hit | None:
        return value

    _probe.__name__ = f"probe_{name}"
    return _probe


def make_raising_probe(name: str, error: Exception) -> Probe:
    def _probe(client, url) -> Hit | None:
        raise error

    _probe.__name__ = f"probe_{name}"
    return _probe
