#!/usr/bin/env bash
# Start the API in the background (manual start, for testing).
# Defaults to 127.0.0.1:8000. To listen only on the Tailscale IP: HOST=$(tailscale ip -4) scripts/start.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
if [[ -f data/server.pid ]] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then
  echo "already running (pid $(cat data/server.pid))"; exit 0
fi
nohup .venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --workers 1 >> data/server.log 2>&1 &
echo $! > data/server.pid
for _ in $(seq 1 30); do
  if curl -fsS "http://${HOST:-127.0.0.1}:${PORT:-8000}/api/v1/health" >/dev/null 2>&1; then
    echo "started (pid $(cat data/server.pid)), log: data/server.log"; exit 0
  fi
  sleep 0.5
done
echo "did not become healthy; see data/server.log"; exit 1
