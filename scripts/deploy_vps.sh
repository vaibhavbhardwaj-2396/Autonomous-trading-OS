#!/usr/bin/env bash
# Deploy one already-merged main commit to the VPS, with safety checks.
#
# This script never places orders, changes broker credentials, resumes trading,
# or edits live capital state. It only updates tracked source code and, if
# explicitly requested, restarts the read-only dashboard API service.

set -Eeuo pipefail

HOST="trading-os-vps"
REPO_DIR="/root/trading-agent"
REF="origin/main"
RESTART_API=false
ALLOW_MARKET_WINDOW=false
INSTALL_DEPS=false

usage() {
  cat <<'EOF'
Usage: scripts/deploy_vps.sh [options]

Options:
  --host HOST             SSH host alias (default: trading-os-vps)
  --repo-dir PATH         Repository path on the VPS (default: /root/trading-agent)
  --ref REF               Merged commit/ref to deploy (default: origin/main)
  --restart-api           Restart only trading-api.service after successful tests
  --install-deps          Run venv/bin/pip install -r requirements.txt before tests
  --allow-market-window   Override the weekday 09:15–15:45 IST deployment block
  -h, --help              Show this help

The script refuses a dirty VPS worktree, requires the target to be reachable
from origin/main, runs safety/API tests before activation, and restores the
previous checkout if those tests fail.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --repo-dir) REPO_DIR="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --restart-api) RESTART_API=true; shift ;;
    --install-deps) INSTALL_DEPS=true; shift ;;
    --allow-market-window) ALLOW_MARKET_WINDOW=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$ALLOW_MARKET_WINDOW" != true ]]; then
  ist_day="$(TZ=Asia/Kolkata date +%u)"
  ist_time="$(TZ=Asia/Kolkata date +%H%M)"
  # Force decimal interpretation: Bash otherwise treats a leading-zero time
  # such as 0915 as invalid octal in an arithmetic comparison.
  if (( 10#$ist_day <= 5 && 10#$ist_time >= 915 && 10#$ist_time < 1545 )); then
    echo "Refusing deployment during NSE market hours (09:15–15:45 IST)." >&2
    echo "Use --allow-market-window only for an urgent, assessed operational fix." >&2
    exit 3
  fi
fi

# Resolve locally first so the remote receives an immutable SHA, never an
# ambiguous branch name which could move during the deployment.
TARGET_SHA="$(git rev-parse "${REF}^{commit}")"

ssh "$HOST" bash -s -- "$REPO_DIR" "$TARGET_SHA" "$RESTART_API" "$INSTALL_DEPS" <<'REMOTE'
set -Eeuo pipefail

repo_dir="$1"
target_sha="$2"
restart_api="$3"
install_deps="$4"

cd "$repo_dir"

# Never overwrite a manual server-side code change. Ignored operational files
# (.env, sessions, state, logs) are intentionally not part of this check.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Refusing deployment: VPS worktree has tracked changes." >&2
  git status --short >&2
  exit 10
fi

previous_sha="$(git rev-parse HEAD)"
git fetch --prune origin main
git cat-file -e "${target_sha}^{commit}"
if ! git merge-base --is-ancestor "$target_sha" origin/main; then
  echo "Refusing deployment: $target_sha is not reachable from origin/main." >&2
  exit 11
fi

rollback() {
  echo "Restoring previous checkout: $previous_sha" >&2
  git reset --hard "$previous_sha" >&2
}

git switch main
git reset --hard "$target_sha"

if [[ "$install_deps" == true ]]; then
  venv/bin/python -m pip install -r requirements.txt || { rollback; exit 20; }
fi

# This is a deterministic production-safe gate. The full suite belongs in
# GitHub CI. In particular, tests/test_api.py deliberately asserts an empty
# local development history and is not valid against a VPS with real regime
# history. Broker sync/order commands are deliberately absent here.
venv/bin/python tests/test_guardrails.py || { rollback; exit 21; }

if [[ "$restart_api" == true ]]; then
  systemctl restart trading-api.service
  systemctl is-active --quiet trading-api.service
  api_port="$(sed -n 's/^DASHBOARD_API_PORT=//p' deploy/api.env | tail -n 1)"
  api_port="${api_port:-8787}"
  curl --fail --silent --show-error --max-time 10 "http://127.0.0.1:${api_port}/health" >/dev/null
fi

echo "DEPLOYED_SHA=$target_sha"
REMOTE
