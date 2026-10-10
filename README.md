# searxng-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives a
client a self-hosted [SearXNG](https://docs.searxng.org/) plus the tools to read
what it finds. No API keys, no third-party search account: everything goes
through your own instance.

Seven tools:

| Tool | What it does |
| --- | --- |
| `search_web` | Search SearXNG; returns titles, URLs, source engine, publication date and snippets |
| `webfetch` | Read a page as markdown; HTML is reduced, JSON and text pass through |
| `fetch_pdf` | Extract text from a PDF, page by page (an arxiv paper, say) |
| `fetch_rss` | List recent items from an RSS or Atom feed |
| `fetch_wayback` | Find the closest archived copy of a URL across three sources |
| `get_time` | Current time in any IANA zone |
| `convert_time` | Convert a datetime between two zones |

Every tool is read-only, and the results are text for the model to read — none
of them return structured content, so nothing is serialised twice.

## Running it

```sh
pip install -r requirements.lock
SEARXNG_MCP_TOKEN=<a long random token> python server.py
```

It listens on `0.0.0.0:8000` and serves streamable HTTP at `/mcp`. `GET /health`
is unauthenticated and answers `{"status":"ok"}` — nothing more, so a poller can
say "up" without holding the token.

The deployed container runs as `nobody` behind a rootless quadlet
(`searxng-mcp.service`); `./deploy.sh` rebuilds, restarts, and waits for the
container's healthcheck to pass. Roll back with
`git checkout <older-sha> && ./deploy.sh`.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SEARXNG_URL` | `http://searxng:8080/search` | The SearXNG instance to query |
| `SEARXNG_MCP_TOKEN` | – | Bearer token. **Required** unless `MCP_ALLOW_ANONYMOUS=1` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Listen address |
| `WEBFETCH_ALLOW_PRIVATE` | – | Set to `1` to disable the private-address guard |
| `MCP_ALLOW_ANONYMOUS` | – | Set to `1` to serve with no token (loud warning) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `SEARXNG_MCP_URL` | `http://127.0.0.1:8000/mcp` | Where `healthcheck.py` probes |

Without `SEARXNG_MCP_TOKEN` the server refuses to start rather than serving
unauthenticated — the port is published on the LAN. For debugging from inside
`npm-network`, either set the token or set `MCP_ALLOW_ANONYMOUS=1` and read the
warning in the log.

## Security model

This container shares `npm-network` with bitcoind, monerod, lightningd and
valkey, and publishes 8000 on the LAN. A fetcher that can reach a private
address turns any injected prompt in a fetched page into a probe of those
services, so `webfetch`, `fetch_pdf` and `fetch_rss` all go through one guard:

- only `http` and `https`
- an address is fetched only when **every** address the host resolves to is
  globally routable — an allowlist, not a blocklist, which is what catches
  CGNAT, NAT64 and the documentation prefixes a blocklist misses
- every **redirect hop** is checked too, not just the URL the caller passed.
  Without that, a public page that answers `302` with
  `Location: http://bitcoind:8332/` walks straight through.
- a host resolving to several addresses is rejected if *any* of them is private

What it does not cover: DNS rebinding between the check and the connection, and
the bearer token comparison, which is constant-time but a single secret in a
single header. `fetch_wayback` looks URLs up in public archive indexes and does
not fetch the target, so it is guarded for consistency rather than necessity.

`search_web` is exempt: `SEARXNG_URL` is an internal container hostname, so the
guard would refuse it.

## SearXNG setup

`search_web` asks for JSON output, which SearXNG disables by default. Without
it the instance answers `403` and every search reports an HTTP error. Add `json`
to the enabled formats:

```yaml
search:
  formats:
    - html
    - json
```

Tools report `unresponsive_engines` from SearXNG, so an engine that is
suspended or CAPTCHA'd shows up as a thinner result set with the reason, rather
than as silence.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q          # 163 tests, ~1s, no network
.venv/bin/ruff check .       # lint
.venv/bin/python scripts/smoke.py   # boots the real app on loopback
```

Tests import `server` and drive it through the SDK's in-memory
`Client(mcp)` — no ports, no subprocesses. `scripts/smoke.py` is the one test
that binds a socket, because that is the part the unit tests cannot see.

### Adding a tool

```python
@mcp.tool(structured_output=False, annotations=_WEB_TOOLS)
def my_tool(url: str) -> str:
    """What the model sees. Args: ..."""
```

Three things to copy: `structured_output=False` so the result is sent once
rather than twice; the annotations so well-behaved clients skip a confirmation
prompt; and fetching through `_fetch_bytes` so the guard, the byte cap and the
redirect check come along.

## Deploying

`Containerfile` installs `requirements.lock` and pins the base image by digest,
so a rebuild is the same image. Regenerate the lock after changing
`requirements.txt`:

```sh
podman run --rm -v "$PWD:/app:z" -w /app \
  docker.io/library/python:3.14-alpine python scripts/lock.py
```

CI (`.github/workflows/ci.yml`) runs lint, the suite on 3.12 and 3.14,
`pip-audit` against the lock, builds the image, and refuses a commit that
changes `requirements.txt` without regenerating `requirements.lock`.
