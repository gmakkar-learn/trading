#!/usr/bin/env bash
# start.sh — Start all auto-trader-01 components in the correct order.
# Usage: bash scripts/start.sh [--no-frontend]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_LOG="$ROOT/logs/backend.log"
FRONTEND_LOG="$ROOT/logs/frontend.log"
DB_CONTAINER="auto-trader-01-db-1"
BACKEND_PORT=8000
FRONTEND_PORT=5173

NO_FRONTEND=0
for arg in "$@"; do [[ "$arg" == "--no-frontend" ]] && NO_FRONTEND=1; done

mkdir -p "$ROOT/logs"

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[0;33m%s\033[0m\n' "$*"; }
info()  { printf '  %-20s' "$1"; }

echo ""
echo "=== auto-trader-01 startup ==="
echo ""

# ── 1. PostgreSQL ──────────────────────────────────────────────────────────────
info "PostgreSQL"
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"; then
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"; then
    docker start "$DB_CONTAINER" > /dev/null
    yellow "started (was stopped)"
  else
    red "container '$DB_CONTAINER' not found — run: docker compose up -d db"
    exit 1
  fi
else
  green "already running"
fi

# Wait for DB to accept connections
info "DB ready check"
for i in $(seq 1 20); do
  if docker exec "$DB_CONTAINER" pg_isready -U autotrader -q 2>/dev/null; then
    green "ready (${i}s)"
    break
  fi
  if [[ $i -eq 20 ]]; then
    red "timed out after 20s"
    exit 1
  fi
  sleep 1
done

# ── 2. Backend ─────────────────────────────────────────────────────────────────
info "Backend (port $BACKEND_PORT)"
if lsof -ti:$BACKEND_PORT > /dev/null 2>&1; then
  green "already running"
else
  cd "$ROOT"
  nohup uv run uvicorn api.main:app \
    --host 0.0.0.0 --port $BACKEND_PORT --log-level warning \
    > "$BACKEND_LOG" 2>&1 &
  # Wait until the port accepts TCP connections (fast check, no external API calls)
  for i in $(seq 1 60); do
    if nc -z localhost $BACKEND_PORT 2>/dev/null; then
      green "started (${i}s)  — logs: logs/backend.log"
      break
    fi
    if [[ $i -eq 60 ]]; then
      red "timed out after 60s — check logs/backend.log"
      tail -20 "$BACKEND_LOG" >&2
      exit 1
    fi
    sleep 1
  done
fi

# ── 3. Frontend ────────────────────────────────────────────────────────────────
if [[ $NO_FRONTEND -eq 0 ]]; then
  info "Frontend (port $FRONTEND_PORT)"
  if lsof -ti:$FRONTEND_PORT > /dev/null 2>&1; then
    green "already running"
  else
    cd "$ROOT/frontend"
    nohup npm run dev -- --port $FRONTEND_PORT \
      > "$FRONTEND_LOG" 2>&1 &
    for i in $(seq 1 20); do
      if lsof -ti:$FRONTEND_PORT > /dev/null 2>&1; then
        green "started (${i}s)  — logs: logs/frontend.log"
        break
      fi
      if [[ $i -eq 20 ]]; then
        red "timed out — check logs/frontend.log"
        tail -20 "$FRONTEND_LOG" >&2
        exit 1
      fi
      sleep 1
    done
  fi
fi

# ── 4. Health summary ──────────────────────────────────────────────────────────
echo ""
echo "=== health check ==="
HEALTH=$(curl -s --max-time 30 "http://localhost:$BACKEND_PORT/health" 2>/dev/null || echo '{"status":"unreachable"}')
STATUS=$(echo "$HEALTH" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")

if [[ "$STATUS" == "ok" ]]; then
  green "All services healthy"
else
  yellow "Status: $STATUS"
  echo "$HEALTH" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for name, svc in d.get('services', {}).items():
    icon = '✓' if svc['status'] == 'ok' else '✗'
    detail = f\" — {svc['detail']}\" if svc.get('detail') else ''
    print(f'  {icon} {name}: {svc[\"status\"]}{detail}')
" 2>/dev/null
fi

echo ""
if [[ $NO_FRONTEND -eq 0 ]]; then
  echo "  Dashboard: http://localhost:$FRONTEND_PORT"
fi
echo "  API:       http://localhost:$BACKEND_PORT"
echo ""
