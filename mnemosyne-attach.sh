#!/usr/bin/env bash
#
# Mnemosyne — attach the context layer (store + observer gateway) to ANY docker
# container that makes OpenAI-compatible calls.
#
# Deploy is generic (only needs the target's docker network). Repointing the
# target's OpenAI base_url is auto for Hermes (edits config.yaml model.base_url);
# for other containers the script prints the gateway URL to set manually.
#
# Chain after attach:  <target> -> mnemosyne-gateway -> <previous upstream> -> provider
# (composes cleanly in front of an existing prometheus-proxy.)
#
# Safe to re-run (idempotent). Backs up config. Phase 0 = observe only.
#
# Env vars:
#   TARGET_CONTAINER  container to attach            (default: hermes)
#   GATEWAY_PORT      gateway host/container port     (default: 8781)
#   STORE_PORT        store host/container port       (default: 8782)
#   UPSTREAM          what the gateway forwards to    (default: auto from Hermes base_url)
#   REPOINT           hermes | manual                 (default: auto-detect)
#
set -euo pipefail

TARGET="${TARGET_CONTAINER:-hermes}"
GW="mnemosyne-gateway"; ST="mnemosyne-store"
GPORT="${GATEWAY_PORT:-8781}"; SPORT="${STORE_PORT:-8782}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say(){ printf '\n\033[1;35m== %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found"
docker inspect "$TARGET" >/dev/null 2>&1 || die "target container '$TARGET' not found (set TARGET_CONTAINER)"

say "Inspecting target '$TARGET'"
NET=$(docker inspect "$TARGET" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -1)
[ -n "$NET" ] || die "could not determine target docker network"
echo "network=$NET"

# Locate Hermes config (for auto repoint + auto upstream). Optional for generic targets.
HHOME=$(docker exec "$TARGET" printenv HERMES_HOME 2>/dev/null || true)
CFG=""
if [ -n "$HHOME" ]; then
  CFG_HOST=$(docker inspect "$TARGET" --format "{{range .Mounts}}{{if eq .Destination \"$HHOME\"}}{{.Source}}{{end}}{{end}}" 2>/dev/null || true)
  [ -n "$CFG_HOST" ] && [ -f "$CFG_HOST/config.yaml" ] && CFG="$CFG_HOST/config.yaml"
fi
MODE="${REPOINT:-$([ -n "$CFG" ] && echo hermes || echo manual)}"
GW_URL="http://$GW:$GPORT/v1"

# Determine gateway upstream (what the chain forwards to behind the gateway).
if [ -n "${UPSTREAM:-}" ]; then
  UP="$UPSTREAM"
elif [ -n "$CFG" ]; then
  CUR=$(grep -m1 -E '^[[:space:]]*base_url:[[:space:]]*[^[:space:]]' "$CFG" \
        | sed -E 's/^[[:space:]]*base_url:[[:space:]]*//' | tr -d '"' | tr -d "'" | tr -d '\r\n')
  if [ "$CUR" = "$GW_URL" ]; then
    # already attached — reuse the gateway's existing upstream
    UP=$(docker inspect "$GW" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^UPSTREAM_BASE_URL=//p' | head -1)
    [ -n "$UP" ] || die "already attached but gateway UPSTREAM unknown; remove $GW and re-run"
  else
    UP="${CUR%/v1}"   # gateway appends the /v1 path itself -> upstream must be the ROOT
  fi
else
  die "UPSTREAM not set and target is not Hermes — pass UPSTREAM=http://<provider-or-proxy>[/v1]"
fi
echo "mode=$MODE  gateway_url=$GW_URL  upstream=$UP"

# ── Build + run store and gateway on the target's network ───────────────────
export DOCKER_CONFIG="${DOCKER_CONFIG:-$HOME/.docker}"
mkdir -p "$DOCKER_CONFIG" 2>/dev/null || { export DOCKER_CONFIG=/tmp/.docker; mkdir -p "$DOCKER_CONFIG"; }
BK="${DOCKER_BUILDKIT:-0}"

say "Building + running $ST"
( cd "$HERE/store" && DOCKER_BUILDKIT=$BK docker build -t "$ST:latest" . >/dev/null )
mkdir -p "$HERE/store/data"
docker rm -f "$ST" >/dev/null 2>&1 || true
docker run -d --name "$ST" --network "$NET" --restart unless-stopped \
  -p "$SPORT:8782" -e DATADIR=/data -v "$HERE/store/data:/data" "$ST:latest" >/dev/null

say "Building + running $GW (observer)"
( cd "$HERE/gateway" && DOCKER_BUILDKIT=$BK docker build -t "$GW:latest" . >/dev/null )
mkdir -p "$HERE/gateway/log"
docker rm -f "$GW" >/dev/null 2>&1 || true
docker run -d --name "$GW" --network "$NET" --restart unless-stopped \
  -p "$GPORT:8781" \
  -e UPSTREAM_BASE_URL="$UP" -e STORE_URL="http://$ST:8782" -e LOGDIR=/log \
  -v "$HERE/gateway/log:/log" "$GW:latest" >/dev/null
sleep 3
curl -fsS "http://localhost:$GPORT/healthz" >/dev/null || die "gateway healthcheck failed"
curl -fsS "http://localhost:$SPORT/healthz" >/dev/null || die "store healthcheck failed"
echo "gateway + store healthy"

# ── Repoint target -> gateway ───────────────────────────────────────────────
if [ "$MODE" = "hermes" ]; then
  CUR=$(grep -m1 -E '^[[:space:]]*base_url:[[:space:]]*[^[:space:]]' "$CFG" \
        | sed -E 's/^[[:space:]]*base_url:[[:space:]]*//' | tr -d '"' | tr -d "'" | tr -d '\r\n')
  if [ "$CUR" != "$GW_URL" ]; then
    say "Repointing Hermes base_url -> $GW_URL"
    cp "$CFG" "$CFG.mnemosyne.bak.$(date +%Y%m%d_%H%M%S)"
    python3 - "$CFG" "$CUR" "$GW_URL" <<'PY'
import sys
cfg, old, new = sys.argv[1:4]
s = open(cfg).read().replace(old, new)   # read FIRST, then open-for-write (avoid truncate-before-read)
open(cfg, "w").write(s)
print("base_url replaced")
PY
    say "Restarting $TARGET"
    docker restart "$TARGET" >/dev/null
    for i in $(seq 1 40); do
      H=$(docker inspect "$TARGET" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || echo "?")
      [ "$H" = "healthy" ] || [ "$H" = "running" ] && { echo "target: $H"; break; }
      sleep 3
    done
  else
    echo "Hermes already points at the gateway."
  fi
else
  say "MANUAL repoint required"
  echo "Set your container's OpenAI base_url to:  $GW_URL"
  echo "(the gateway forwards to: $UP)"
fi

say "DONE — Mnemosyne Phase 0 (observe) attached"
echo "gateway:  http://localhost:$GPORT   (log: $HERE/gateway/log/gateway.log)"
echo "store:    http://localhost:$SPORT   (data: $HERE/store/data/)"
echo "watch:    curl -s localhost:$SPORT/contexts | python3 -m json.tool"
echo "rollback: TARGET_CONTAINER=$TARGET ./mnemosyne-detach.sh"
