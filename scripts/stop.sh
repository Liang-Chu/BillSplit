#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f data/server.pid ]] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then
  pid=$(cat data/server.pid)
  kill "$pid"
  for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done  # wait for graceful shutdown
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
  rm -f data/server.pid && echo "stopped"
else
  rm -f data/server.pid; echo "not running"
fi
