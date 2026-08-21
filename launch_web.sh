#!/bin/bash
# Start the AlgoPaca trading desk web app and open it in your browser.
set -e
cd "$(dirname "$0")"

PYTHON=".venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "Virtual environment not found. Creating .venv..."
  python3 -m venv .venv
  "$PYTHON" -m pip install --upgrade pip
  "$PYTHON" -m pip install -r requirements.txt
fi

echo "Starting AlgoPaca at http://127.0.0.1:8765"

# Cross-platform browser launcher
open_browser() {
  sleep 1.5
  URL="http://127.0.0.1:8765"
  if command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  elif command -v wslview >/dev/null 2>&1; then
    wslview "$URL" >/dev/null 2>&1 || true
  fi
}

open_browser &
exec "$PYTHON" -m bot.webapp

