#!/bin/bash
# Run the trading bot without manually activating the venv.
set -e
cd "$(dirname "$0")"

PYTHON=".venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "Virtualenv missing. Creating it..."
  python3 -m venv .venv
  "$PYTHON" -m pip install -r requirements.txt
fi

exec "$PYTHON" -m bot.main "$@"
