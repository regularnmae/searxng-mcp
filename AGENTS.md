# searxng-mcp — agent notes

Flat three-module Python MCP server (not a package). `server.py` is the app
— config, tools, auth middleware; `readability.py` is its pure
HTML-to-markdown pass and `wayback.py` its archive-lookup tiers, both
dependency-free; `healthcheck.py` is the container healthcheck probe. Shipped as
a rootless podman/quadlet container; the runtime — its quadlet unit, the
`npm-network` it shares and the env wiring — is owned by the `stack` repo
(https://github.com/regularnmae/stack), not here. Toolchain is uv only — no pip in CI,
in the build, or in the image (CI asserts all three). `mcp` is the 2.x
line: the app object is `MCPServer` (the v2 rename of `FastMCP`), and the
HTTP dependency is `httpx2`, a separate distribution from `httpx` — don't
rewrite an import to plain `httpx`. Env vars, tool list and the security
model are documented in `README.md`; don't re-derive them from the code.

## Commands

```sh
uv run ruff check .            # lint (CI gate). No `ruff format` step; line-length 100, rules E,F,I,UP,B,SIM
uv run pyright                 # typecheck (CI gate); config in [tool.pyright], standard mode, floor Python 3.12
uv run pytest -q              # 186 tests, ~2s, no network
uv run pytest tests/test_guard.py -q          # focused
uv run python scripts/smoke.py               # boots the real app on a free loopback port: /health, 401s, handshake
uv run --python 3.14 pytest -q                # against the image's Python (base is 3.14-alpine; >=3.12, CI matrix 3.12+3.14)
uv add <pkg>                   # edits pyproject.toml AND re-locks — always commit the uv.lock change
uv export --format requirements-txt --no-dev --no-emit-project --locked -o /tmp/r.txt   # what the image installs
uv lock --check                # CI drift gate: fails if pyproject.toml changed without re-locking
uvx pip-audit@2.10.1 --requirement requirements-audit.txt           # audit job (version is pinned deliberately)
podman build -t localhost/searxng-mcp:latest .  # build from repo root; single-stage Containerfile
```

Deployment is owned by the `stack` repo: `cd ~/build/stack && ./deploy.sh --only searxng-mcp`
rebuilds the image, restarts `searxng-mcp.service` and waits for "(healthy)";
`./deploy.sh` deploys the whole stack. The unit and `npm-network` live there,
not here — this repo ships only code, tests, CI and `Containerfile`.

## Build/deploy facts that are easy to get wrong

- Nothing runs the server without `SEARXNG_MCP_TOKEN`: startup raises
  `RuntimeError` unless `MCP_ALLOW_ANONYMOUS=1`. `scripts/smoke.py` sets
  its own token; ad-hoc runs must set one.
- The Containerfile copies the `uv` binary straight out of
  `ghcr.io/astral-sh/uv` pinned **by tag and digest**. In that image uv
  lives at `/usr/local/bin/uv`, not `/uv` (`/uv` is the distroless base
  image; copying it from a derived tag fails the build).
- The install is one `RUN`: `uv pip install --system --no-cache
  --require-hashes` (no venv exists), then delete the uv binary, the
  requirements file and the lockfiles, then `python -m pip uninstall -y pip`.
  All in one layer, because the image must end with neither uv nor pip and
  no build inputs.
- The image itself declares no `HEALTHCHECK`; the deployed unit supplies
  `HealthCmd=python /app/healthcheck.py` plus `SEARXNG_MCP_TOKEN`. Removing
  that unit, or running the image bare, means `stack/deploy.sh` never sees
  "healthy" and times out.
- `.containerignore` excludes `tests/`, `scripts/`, `*.md` and
  `.git`, so only `server.py` + `readability.py` + `wayback.py` +
  `healthcheck.py` land
  in the image — a new top-level module must be added to the `Containerfile`'s
  `COPY` lines and to pyright's `include`, or it won't ship or typecheck.
- The deployed server is wired into `~/.config/opencode/opencode.json` as a
  remote MCP entry (currently `http://192.168.1.112:8000/mcp` with a bearer
  token). `/health` is open and answers exactly `{"status":"ok"}`; any other
  JSON means you reached a different service on port 8000.
- The runtime is owned by the `stack` repo: `stack/deploy/install.sh` renders
  `npm-network.network`, `valkey.container`, `searxng.container` and
  `searxng-mcp.container` into `~/.config/containers/systemd/` (backing up any
  real file to `.bak`), writes `$DATA_DIR/config/searxng-mcp/token.env`, and
  seeds `$DATA_DIR/searxng/` on a fresh host only. The env-file shapes are
  documented in `stack/deploy/examples/searxng-mcp/`.
- The bearer token is never in the repo: the rendered unit reads it via
  `EnvironmentFile=$DATA_DIR/config/searxng-mcp/token.env`, and
  `stack/deploy/install.sh` reuses the token the running container already has
  so existing clients survive the switch; only
  `stack/deploy/examples/searxng-mcp/token.env.example` is committed.
- `stack/deploy/searxng/settings.yml` must keep `search.formats` containing
  `json` (`search_web` gets a 403 otherwise) and carries a blank `secret_key`
  that `stack/deploy/install.sh` fills in when seeding.

## Testing quirks

- Tests talk to the server through the SDK's in-memory `Client(mcp)` — no
  test opens a socket except the loopback SearXNG stub and the guard tests,
  which use literal IPs so no DNS is needed. Keep new tests offline like
  that.
- `mock_http` (in `tests/conftest.py`) monkeypatches `server._http_client`
  and swaps in an `httpx2.MockTransport`, so redirects, hooks and UA are
  still the production ones. Use it for anything that fetches via
  `_fetch_bytes`.
- Two tools bypass `_http_client` and need different treatment:
  `search_web` builds its own client (tests drive it with the `searxng`
  stub fixture, which also records the outgoing query params) and
  `fetch_wayback` builds its own too. Its tier probes live in `wayback.py`,
  so tests monkeypatch `wayback._wayback_from_*` (the `dead_wayback_tiers`
  fixture makes them all report "no snapshot"), not `server`.
- `search_web` results are cached process-globally, so an autouse fixture
  in `conftest.py` clears the cache around every test — tests repoint
  `SEARXNG_URL` and mutate the stub payload, and a stale entry would leak
  between them. `tests/test_search_cache.py` drives `_SearchCache` directly
  with a counting fake fetch and an injected clock.
- Async tests need `@pytest.mark.anyio`; the backend fixture is in
  `conftest.py`. `pytest` runs with `pythonpath = ["."]`, which is why
  `import server` works — `server.py` is a top-level module.

## Conventions the tests/CI enforce

- Tool bodies are synchronous (`def`, not `async def`); anyio runs them on
  worker threads, which is why the search cache is guarded by a
  `threading.Lock`. Keep new tools sync.
- Every text-only `@mcp.tool` must pass `structured_output=False` and
  `annotations=_WEB_TOOLS` / `_LOCAL_TOOLS`; `tests/test_schemas.py`
  asserts this and the exact set of seven tool names — adding a tool means
  updating `TOOLS` there. `search_web` and `webfetch` are the deliberate
  exceptions: they pass `structured_output=True` and annotate the return
  as `Annotated[CallToolResult, <ResponseModel>]`, and are asserted to
  publish an output schema. Returning a `CallToolResult` (rather than a
  bare model) is what keeps the text block as markdown instead of the
  SDK serialising the whole model as pretty-printed JSON into it.
- `webfetch` reduces HTML with a hand-rolled readability pass in
  `readability.py` (`_parse_html`: metadata first, then `_strip_boilerplate`,
  then `_main_node`) rather than a library. Keep it dependency-free; the
  tests pin article-vs-chrome selection and OG/Twitter/JSON-LD metadata.
- Numeric args are bounded with `Annotated[int, Field(ge=…, le=…)]`, not
  validated in the body; closed sets use `Literal`. The docstring's `Args:`
  section is the model-visible contract, so it must describe the bounds.
- Anything that fetches a URL goes through `_fetch_bytes` (guard, UA, byte
  caps, per-hop redirect check). `fetch_wayback` is the deliberate
  exception — it queries archive indexes, not the target — and its bypass
  is commented as such.
