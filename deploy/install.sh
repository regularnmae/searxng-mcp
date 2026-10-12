#!/usr/bin/env bash
# Install the quadlet units for the searxng-mcp stack.
#
# Symlinks this repo's deploy/ units into the user quadlet directory
# (~/.config/containers/systemd) and reloads systemd, so the checkout is
# the single source of truth. Also seeds the bearer-token env file and,
# on a fresh host only, the SearXNG settings under %h/data/searxng.
#
# Idempotent: re-running refreshes the symlinks and leaves existing
# secrets and SearXNG config untouched. No secret is ever committed or
# printed -- the token lives in ~/.config/searxng-mcp/token.env (mode
# 600), the SearXNG secret_key in ~/data/searxng/settings.yml.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_DIR/deploy"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
QUADLET_DIR="$CONFIG_HOME/containers/systemd"
ENV_DIR="$CONFIG_HOME/searxng-mcp"
ENV_FILE="$ENV_DIR/token.env"
DATA_DIR="${SEARXNG_DATA_DIR:-$HOME/data/searxng}"

UNITS=(npm-network.network valkey.container searxng.container searxng-mcp.container)

log() { printf '==> %s\n' "$*"; }

# A hex token, from openssl if present, else /dev/urandom.
rand_hex() {
    openssl rand -hex 32 2>/dev/null && return 0
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# 1. bearer token ---------------------------------------------------------
mkdir -p "$ENV_DIR"
if [ -s "$ENV_FILE" ]; then
    log "token env present: $ENV_FILE"
else
    token=""
    # Reuse the token already in service so existing clients (e.g.
    # opencode.json) keep working across the switch to EnvironmentFile.
    if command -v podman >/dev/null 2>&1; then
        token=$(podman exec searxng-mcp printenv SEARXNG_MCP_TOKEN 2>/dev/null || true)
    fi
    if [ -z "$token" ] && [ -f "$QUADLET_DIR/searxng-mcp.container" ]; then
        token=$(sed -n 's/^Environment=SEARXNG_MCP_TOKEN=//p' \
            "$QUADLET_DIR/searxng-mcp.container" 2>/dev/null | head -n1 || true)
    fi
    if [ -n "$token" ]; then
        log "reused the existing bearer token"
    else
        token=$(rand_hex)
        log "generated a new bearer token -- update clients to match"
    fi
    old_umask=$(umask)
    umask 077
    printf 'SEARXNG_MCP_TOKEN=%s\n' "$token" > "$ENV_FILE"
    umask "$old_umask"
    chmod 600 "$ENV_FILE"
    log "wrote $ENV_FILE (mode 600)"
fi

# 2. SearXNG config -- seed only; never clobber a live file ---------------
mkdir -p "$DATA_DIR"
if [ -f "$DATA_DIR/settings.yml" ]; then
    log "keeping existing $DATA_DIR/settings.yml"
else
    secret=$(rand_hex)
    log "seeding $DATA_DIR/settings.yml with a fresh secret_key"
    sed "s/^  secret_key: .*/  secret_key: \"${secret}\"/" \
        "$DEPLOY_DIR/searxng/settings.yml" > "$DATA_DIR/settings.yml"
fi
if [ -f "$DATA_DIR/limiter.toml" ]; then
    log "keeping existing $DATA_DIR/limiter.toml"
else
    log "seeding $DATA_DIR/limiter.toml"
    cp "$DEPLOY_DIR/searxng/limiter.toml" "$DATA_DIR/limiter.toml"
fi

# 3. symlink units into the quadlet search path ---------------------------
mkdir -p "$QUADLET_DIR"
for unit in "${UNITS[@]}"; do
    target="$QUADLET_DIR/$unit"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        mv -f "$target" "$target.bak"
        log "backed up existing $unit -> $unit.bak"
    fi
    ln -sfn "$DEPLOY_DIR/$unit" "$target"
    log "linked $unit"
done

# 4. reload systemd so the generator re-reads the units -------------------
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    log "systemd --user reloaded"
fi

cat <<EOF

Done. Next:
  ./deploy.sh                         # install, build, restart, wait healthy
  # or, units only:
  systemctl --user start searxng.service searxng-mcp.service

Clients must send the bearer token from
  $ENV_FILE
as \`Authorization: Bearer <token>\` -- see ~/.config/opencode/opencode.json.
EOF
