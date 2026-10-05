#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f data/server.pid ]] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then
  kill "$(cat data/server.pid)" && rm -f data/server.pid && echo "stopped"
else
  rm -f data/server.pid; echo "not running"
fi
