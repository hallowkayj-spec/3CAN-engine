#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
PORT="${PORT:-9711}"
MIN_NODES="${MIN_NODES:-10}"
APPLY_PROJECT_SEEDS=0
START_SERVER=0
NO_SEED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_DIR="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --min-nodes)
      MIN_NODES="$2"
      shift 2
      ;;
    --apply-project-seeds)
      APPLY_PROJECT_SEEDS=1
      shift
      ;;
    --start-server)
      START_SERVER=1
      shift
      ;;
    --no-seed)
      NO_SEED=1
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENGINE_ROOT="$RELEASE_ROOT/neural-memory"
GRAPH_DIR="$ENGINE_ROOT/graph"
BASE_URL="http://127.0.0.1:${PORT}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

mkdir -p "$GRAPH_DIR/nodes"
for name in edges.json agents.json activity_log.json; do
  if [[ ! -f "$GRAPH_DIR/$name" ]]; then
    printf '[]\n' > "$GRAPH_DIR/$name"
  fi
done

export THREECAN_ENGINE_ROOT="$ENGINE_ROOT"
export THREECAN_GRAPH_DIR="$GRAPH_DIR"
export THREECAN_PROJECT_DIR="$PROJECT_DIR"
export THREECAN_BASE_URL="$BASE_URL"
export THREECAN_MIN_NODES="$MIN_NODES"
export THREECAN_READINESS_MODE="development"

pushd "$ENGINE_ROOT" >/dev/null
if [[ "$NO_SEED" != "1" ]]; then
  python3 backend/seed_nodes.py
fi
if [[ "$APPLY_PROJECT_SEEDS" == "1" ]]; then
  python3 tools/project_bootstrapper.py --project "$PROJECT_DIR" --base-url "$BASE_URL" --apply
else
  python3 tools/project_bootstrapper.py --project "$PROJECT_DIR" --base-url "$BASE_URL" --dry-run
fi
popd >/dev/null

if [[ "$START_SERVER" == "1" ]]; then
  mkdir -p "$ENGINE_ROOT/logs"
  (
    cd "$ENGINE_ROOT"
    python3 backend/app.py --port "$PORT"
  ) >"$ENGINE_ROOT/logs/3can_${PORT}.stdout.log" 2>"$ENGINE_ROOT/logs/3can_${PORT}.stderr.log" &
  echo "[3CAN] started $BASE_URL"
fi

cat <<EOF
[3CAN] project initialized
  engine:  $ENGINE_ROOT
  graph:   $GRAPH_DIR
  project: $PROJECT_DIR
  base:    $BASE_URL
  token:   $BASE_URL/static/token_usage.html
EOF
