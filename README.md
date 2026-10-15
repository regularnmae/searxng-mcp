# searxng-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives a
client a self-hosted [SearXNG](https://docs.searxng.org/) plus the tools to read
what it finds. No API keys, no third-party search account: everything goes
through your own instance.

Seven tools:

| Tool | What it does |
| --- | --- |
| `search_web` | Search SearXNG; returns titles, URLs, the engines that found each hit, publication date and snippets. Near-duplicate URLs are merged (http/https, `www.`, trailing slash, fragment, tracking params) and every source engine is kept |
| `webfetch` | Read a page as markdown. Boilerplate (nav, sidebars, cookie banners) is stripped and the main content extracted; Open Graph / Twitter / JSON-LD metadata is returned in the structured content |
| `fetch_pdf` | Extract text from a PDF, page by page (an arxiv paper, say) |
| `fetch_rss` | List recent items from an RSS or Atom feed |
| `fetch_wayback` | Find the closest archived copy of a URL across three sources |
| `get_time` | Current time in any IANA zone |
| `convert_time` | Convert a datetime between two zones |

Every tool is read-only. `search_web` and `webfetch` return their data both as
readable text and as schema-validated structured content; the rest return text
only, so nothing is serialised twice.

`search_web` caches recent responses in process for `SEARCH_CACHE_TTL` seconds,
and a query that is already in flight is not issued twice, so repeated and
parallel identical searches share one upstream request. It can also be pointed
at specific engines with `engines="google,duckduckgo"`; when `categories` is
left empty, only those engines run.

## Running it

```sh
uv sync                 # installs from uv.lock
SEARXNG_MCP_TOKEN=<a long random token> uv run python server.py
```

Or, inside the container, with no uv anywhere:

```sh
podman build -t localhost/searxng-mcp:latest .
podman run -e SEARXNG_MCP_TOKEN=<token> -p 8000:8000 localhost/searxng-mcp:latest
```

It listens on `0.0.0.0:8000` and serves streamable HTTP at `/mcp`. `GET /health`
is unauthenticated and answers `{"status":"ok"}` — nothing more, so a poller can
say "up" without holding the token.

The deployed container runs as `nobody` behind a rootless quadlet. The unit
and its env wiring are owned by the separate
[`stack`](https://github.com/regularnmae/stack) repo, not here:
`stack/deploy/install.sh` links the unit into
`~/.config/containers/systemd/`, seeds the token and (on a fresh host) the
SearXNG settings, and reloads systemd; `stack/deploy.sh` runs that, rebuilds,
restarts, and waits for the container's healthcheck to pass.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SEARXNG_URL` | `http://searxng:8080/search` | The SearXNG instance to query |
| `SEARCH_CACHE_TTL` | `60` | Seconds a `search_web` result is reused; `0` disables caching |
| `SEARCH_CACHE_MAX` | `128` | Most `search_web` queries cached (LRU eviction); `0` disables |
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

Dependencies are managed with [uv](https://docs.astral.sh/uv/) from
`pyproject.toml`. `uv.lock` is the exact closure — versions *and* hashes — and it is
the only lock file: uv keeps the transitive pins that a `requirements.txt` never
held.

```sh
uv run pytest -q                       # 163 tests, ~1s, no network
uv run ruff check .                    # lint
uv run python scripts/smoke.py         # boots the real app on loopback
uv run --python 3.14 pytest -q         # against the image's Python
uv add <package>                       # changes pyproject.toml, then re-locks
```

`uv run` creates the virtualenv itself, so there is no `.venv/bin/...` path to
remember and no `pip install -r requirements.txt` step — which is the reason
`requirements.txt`, `requirements-dev.txt`, `requirements.lock` and
`scripts/lock.py` are gone.

Adding a dependency or changing a range means re-locking and committing
`uv.lock`; CI rejects a commit where `uv.lock` no longer matches
`pyproject.toml` (`uv lock --check`).

### The image

`uv` is the only build tool, and it never reaches the running container: a
build stage turns `uv.lock` into a hash-pinned requirements file, the runtime
stage installs it with `uv pip install --system --require-hashes`, and both
build tools — the uv binary and pip itself — are deleted in the same layer. No
uv, no pip in the image, and the hashes are actually enforced rather than
decorative.

```sh
podman build -t localhost/searxng-mcp:latest .
```

### Adding a tool

```python
@mcp.tool(structured_output=False, annotations=_WEB_TOOLS)
def my_tool(url: str) -> str:
    """What the model sees. Args: ..."""
```

Three things to copy: `structured_output=False` so a text result is sent once
rather than twice (only `search_web` and `webfetch` opt into structured output,
returning `Annotated[CallToolResult, <Model>]`); the annotations so well-behaved
clients skip a confirmation prompt; and fetching through `_fetch_bytes` so the
guard, the byte cap and the redirect check come along.

## Deploying

The runtime for this server — its quadlet unit, the podman network, the Valkey
limiter backend and the SearXNG instance — is owned by the separate
[`stack`](https://github.com/regularnmae/stack) repo, which is the single place
that defines how the stack runs:

```sh
cd ~/build/stack
./deploy.sh --only searxng-mcp   # build the image, restart, wait for "(healthy)"
./deploy.sh                      # the whole stack
```

`stack/deploy/install.sh` is idempotent and safe to re-run. It:

- symlinks the unit into `~/.config/containers/systemd/` (quadlet follows
  symlinks), backing up any existing real file to `.bak`;
- writes the bearer token to `~/.config/searxng-mcp/token.env` (mode 600),
  reusing the token the running container already uses so existing clients
  keep working, and only generating one when there is none. No token is ever
  committed — the unit reads it with
  `EnvironmentFile=%h/.config/searxng-mcp/token.env`;
- seeds `~/data/searxng/settings.yml` and `limiter.toml` **only on a fresh
  host** (an existing file is left alone). The settings enable the `json`
  format `search_web` needs and wire the Valkey limiter; a random
  `secret_key` is generated at seed time.

The env-file shapes are documented in `stack/deploy/examples/searxng-mcp/`.
`Containerfile` pins the base image by digest and installs a hash-verified
export of `uv.lock`, so a rebuild is the same image. Roll back the app with
`git checkout <older-sha>` in this repo, then re-run
`stack/deploy.sh --only searxng-mcp`.

CI (`.github/workflows/ci.yml`) runs lint, the suite on 3.12 and 3.14,
`uv lock --check` to catch an un-regenerated lock, `uvx pip-audit` against the
export, builds the image, and asserts that neither uv nor pip leaked into it.

