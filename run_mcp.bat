@echo off
rem Launch AlgoPaca's MCP server (stdio transport) for Claude Desktop, Cursor, etc.
setlocal enabledelayedexpansion

cd /d "%~dp0"

set PYTHON=.venv\Scripts\python.exe

if not exist "%PYTHON%" (
    echo Virtual environment not found. Creating .venv...
    python -m venv .venv
    "%PYTHON%" -m pip install --upgrade pip
    "%PYTHON%" -m pip install -r requirements.txt
)

"%PYTHON%" -m bot.mcp_server %*
