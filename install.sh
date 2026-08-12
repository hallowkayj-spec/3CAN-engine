#!/usr/bin/env bash
# 3CAN v0.1 developer-preview installer.
# Defaults to a minimal local sidecar install. It does not expose the service
# beyond 127.0.0.1 and does not commit runtime graph files.

set -euo pipefail

PY_CMD="${PY_CMD:-python3}"
PORT="${THREECAN_PORT:-9711}"
PROJECT_DIR="${THREECAN_PROJECT_DIR:-$(pwd)}"
PROFILE="${THREECAN_INSTALL_PROFILE:-min}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[3CAN install] checking Python..."
if ! command -v "$PY_CMD" >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python >= 3.11 first." >&2
  exit 1
fi
"$PY_CMD" --version

case "$PROFILE" in
  min|minimal)
    REQ_FILE="requirements-min.txt"
    ;;
  full)
    REQ_FILE="requirements-full.txt"
    ;;
  *)
    echo "ERROR: unknown THREECAN_INSTALL_PROFILE=$PROFILE (use min or full)" >&2
    exit 2
    ;;
esac

echo "[3CAN install] installing $REQ_FILE..."
"$PY_CMD" -m pip install --upgrade pip
"$PY_CMD" -m pip install -r "$REQ_FILE"

echo "[3CAN install] initializing project-local graph..."
if command -v bash >/dev/null 2>&1; then
  bash scripts/init-project.sh --project "$PROJECT_DIR" --port "$PORT"
else
  "$PY_CMD" neural-memory/backend/seed_nodes.py
fi

cat <<EOF

[3CAN install] done.

Start backend:
  $PY_CMD neural-memory/backend/app.py --port $PORT --host 127.0.0.1

Verify after startup:
  $PY_CMD scripts/verify_project.py --base-url http://127.0.0.1:$PORT --min-nodes 10

Token dashboard:
  http://127.0.0.1:$PORT/static/token_usage.html

Security note:
  Keep 3CAN bound to 127.0.0.1 unless you add your own auth/reverse proxy.
EOF
