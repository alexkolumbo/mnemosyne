#!/bin/bash
set -e
export DOCKER_CONFIG=/tmp/.docker; mkdir -p "$DOCKER_CONFIG"
echo "=== qdrant ==="
mkdir -p /DATA/Mnemosyne/qdrant
docker rm -f mnemosyne-qdrant >/dev/null 2>&1 || true
docker run -d --name mnemosyne-qdrant --network hermes_v3_default --restart unless-stopped \
  -p 6334:6333 -v /DATA/Mnemosyne/qdrant:/qdrant/storage qdrant/qdrant:latest >/dev/null
sleep 4; echo "qdrant: $(docker ps --filter name=mnemosyne-qdrant --format '{{.Status}}')"
echo
echo "=== rebuild store (fastembed — heavy, first run downloads model ~130MB) ==="
cd /DATA/Mnemosyne/store
DOCKER_BUILDKIT=0 docker build -t mnemosyne-store:latest . 2>&1 | tail -4
docker rm -f mnemosyne-store >/dev/null 2>&1 || true
docker run -d --name mnemosyne-store --network hermes_v3_default --restart unless-stopped \
  -p 8782:8782 -e DATADIR=/data -e QDRANT_URL=http://mnemosyne-qdrant:6333 \
  -v /DATA/Mnemosyne/store/data:/data mnemosyne-store:latest >/dev/null
echo "store starting; waiting for vector backend (model download on first boot)..."
for i in $(seq 1 60); do
  H=$(curl -s localhost:8782/healthz 2>/dev/null || true)
  if echo "$H" | grep -q '"memory_backend":"vector"'; then echo "  READY: $H"; break; fi
  [ $((i % 4)) -eq 0 ] && echo "  [$((i*5))s] $H"
  sleep 5
done
echo "--- store logs ---"
docker logs mnemosyne-store --tail 6 2>&1 | grep -iE "store|vector|error|download" | tail -6
