#!/usr/bin/env bash
#
# Rollback for mnemosyne-attach.sh: repoint the target back to the gateway's
# upstream and remove the gateway + store containers. Stored context data under
# store/data/ is kept (delete it manually if you want it gone).
#
set -euo pipefail
TARGET="${TARGET_CONTAINER:-hermes}"
GW="mnemosyne-gateway"; ST="mnemosyne-store"
GPORT="${GATEWAY_PORT:-8781}"

HHOME=$(docker exec "$TARGET" printenv HERMES_HOME 2>/dev/null || true)
CFG=""
if [ -n "$HHOME" ]; then
  SRC=$(docker inspect "$TARGET" --format "{{range .Mounts}}{{if eq .Destination \"$HHOME\"}}{{.Source}}{{end}}{{end}}" 2>/dev/null || true)
  [ -n "$SRC" ] && [ -f "$SRC/config.yaml" ] && CFG="$SRC/config.yaml"
fi
GW_URL="http://$GW:$GPORT/v1"
UP=$(docker inspect "$GW" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^UPSTREAM_BASE_URL=//p' | head -1)

if [ -n "$CFG" ] && [ -n "$UP" ] && grep -q "$GW_URL" "$CFG" 2>/dev/null; then
  # restore base_url to whatever the gateway pointed at (+ /v1 if it was stripped)
  NEW="$UP"; case "$NEW" in */v1) ;; *) NEW="$UP/v1";; esac
  cp "$CFG" "$CFG.mnemosyne.bak.$(date +%Y%m%d_%H%M%S)"
  python3 - "$CFG" "$GW_URL" "$NEW" <<'PY'
import sys
cfg, old, new = sys.argv[1:4]
s = open(cfg).read().replace(old, new)   # read FIRST, then write
open(cfg, "w").write(s)
print(f"restored base_url -> {new}")
PY
  docker restart "$TARGET" >/dev/null && echo "target restarted"
else
  echo "target config does not point at the gateway (nothing to repoint)."
fi

docker rm -f "$GW" >/dev/null 2>&1 && echo "removed $GW" || true
docker rm -f "$ST" >/dev/null 2>&1 && echo "removed $ST" || true
echo "DONE (store data kept under store/data/)."
