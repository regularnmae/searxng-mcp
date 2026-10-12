#!/usr/bin/env bash
# Rebuild and redeploy the SearXNG MCP server.
#
# Run from anywhere. This repo IS the podman build context, so the
# script builds the image from the checked-out tree, restarts the
# rootless quadlet service, and waits for the container healthcheck
# (an authenticated MCP initialize handshake) to pass.
#
# Rollback:  git checkout <older-sha> && ./deploy.sh
set -euo pipefail

IMAGE="localhost/searxng-mcp:latest"
CONTAINER="searxng-mcp"
SERVICE="${MCP_SERVICE:-searxng-mcp.service}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

cd "$(dirname "$0")"

# Fail fast on syntax errors instead of deploying a broken image.
python3 -m py_compile server.py readability.py wayback.py

# Show what's being deployed; warn if the tree has uncommitted work.
if git rev-parse --git-dir >/dev/null 2>&1; then
    echo "==> deploying $(git rev-parse --short HEAD 2>/dev/null || echo 'no commits') -- $(git log -1 --format='%s' 2>/dev/null || echo 'uncommitted')"
    [ -z "$(git status --porcelain)" ] || \
        echo "WARNING: uncommitted or staged changes present" >&2
fi

echo "==> building $IMAGE"
podman build -t "$IMAGE" .

echo "==> restarting $SERVICE"
systemctl --user restart "$SERVICE"

echo "==> waiting up to ${HEALTH_TIMEOUT}s for healthy status"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
status=""
while :; do
    status=$(podman ps --filter "name=$CONTAINER" --format '{{.Status}}')
    case "$status" in
        *"(healthy)"*)
            echo "==> live: $status"
            exit 0
            ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "ERROR: not healthy after ${HEALTH_TIMEOUT}s (status: ${status:-container missing})" >&2
        echo "check with: podman logs $CONTAINER" >&2
        exit 1
    fi
    sleep 5
done
