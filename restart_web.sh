#!/usr/bin/env bash
# Restart the AlgoPaca trading desk web app in background and verify health.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${ALGOPACA_PORT:-8765}"
PYTHON=".venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "Virtual environment not found at $PYTHON."
  exit 1
fi

# 1. Gracefully terminate existing web app process on PORT or via pattern
PID=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PID" ]; then
  echo "Stopping server on port $PORT (PID: $PID)..."
  kill "$PID" 2>/dev/null || true
  for _ in {1..10}; do
    if ! kill -0 "$PID" 2>/dev/null; then
      break
    fi
    sleep 0.2
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "Force killing PID $PID..."
    kill -9 "$PID" 2>/dev/null || true
  fi
fi

# Fallback check for any lingering bot.webapp processes
pkill -f "python.*bot\.webapp" 2>/dev/null || true
sleep 0.5

# 2. Start server in background
echo "Starting AlgoPaca web app on port $PORT..."
nohup "$PYTHON" -m bot.webapp > .webapp.log 2>&1 &
NEW_PID=$!

# 3. Health check
echo "Waiting for server to become ready (PID: $NEW_PID)..."
HEALTHY=false
for _ in {1..20}; do
  sleep 0.5
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/login"; then
    HEALTHY=true
    break
  fi
  # If process died early, break out
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    break
  fi
done

if [ "$HEALTHY" = true ]; then
  echo "AlgoPaca web app restarted successfully (PID: $NEW_PID) at http://127.0.0.1:$PORT"
  exit 0
else
  echo "Server restart failed or timed out. Check .webapp.log:"
  tail -n 20 .webapp.log 2>/dev/null || true
  exit 1
fi
