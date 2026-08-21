@echo off
rem Start AlgoPaca Web Trading Desk on Windows
setlocal enabledelayedexpansion

cd /d "%~dp0"

set PYTHON=.venv\Scripts\python.exe

if not exist "%PYTHON%" (
    echo Virtual environment not found. Creating .venv...
    python -m venv .venv
    "%PYTHON%" -m pip install --upgrade pip
    "%PYTHON%" -m pip install -r requirements.txt
)

echo Starting AlgoPaca at http://127.0.0.1:8765
start http://127.0.0.1:8765
"%PYTHON%" -m bot.webapp

pause
