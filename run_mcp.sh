#!/bin/bash
# Launch AlgoPaca's MCP server (stdio transport) for Claude Desktop, Cursor, etc.
set -e
cd "$(dirname "$0")"

PYTHON=".venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "Virtualenv missing. Creating it..." >&2
  python3 -m venv .venv
  "$PYTHON" -m pip install -r requirements.txt
fi

exec "$PYTHON" -m bot.mcp_server "$@"
