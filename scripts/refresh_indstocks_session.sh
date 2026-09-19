#!/usr/bin/env bash
# Refresh INDstocks authentication and the broker snapshot. Read/reconcile only:
# this script contains no order command and cannot place a trade.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/trading-agent}"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOCK_FILE="$PROJECT_DIR/memory/.indstocks_refresh.lock"

cd "$PROJECT_DIR"
mkdir -p logs memory

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "INDstocks refresh already running; clean no-op"
  exit 0
fi

"$PYTHON" scripts/indstocks_auth.py --auto --verify
"$PYTHON" -m engine.execute sync
