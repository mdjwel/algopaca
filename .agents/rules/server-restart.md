---
trigger: always_on
---

# Automated Web Server Restart Policy

Whenever working on this project, the local web application server (`bot.webapp` running on port 8765) MUST be restarted automatically whenever relevant code or asset changes are made.

## 1. When to Automatically Restart the Server

The server must be automatically restarted whenever changes are made to:
- **Frontend Assets**:
  - JavaScript files (`web/static/js/*.js`, `frontend/static/js/*.js`)
  - CSS stylesheets (`web/static/css/*.css`, `frontend/static/css/*.css`)
  - HTML templates or static documents (`web/*.html`, `frontend/*.html`)
- **Backend Code & API**:
  - Core web server routes, dependencies, or handlers (`bot/webapp.py`, `bot/web_state.py`)
  - Strategy, execution, presets, models, or data stores in `bot/*.py`
  - Environment variables, dependencies, or configuration affecting runtime behavior
- **Feature Completion & Bug Fixes**:
  - After implementing new features or fixing bugs in the web app or API before concluding your task or presenting results to the user.

## 2. Restart Execution

Always use the project's restart script or standard sequence to cleanly restart the server:

### Primary Method (Recommended):
Run the provided script from the project root:
```bash
./restart_web.sh
```

### Manual Fallback Sequence:
If executing manually, use the following bash sequence:
```bash
PORT="${ALGOPACA_PORT:-8765}"
PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [ -n "$PID" ]; then
  kill "$PID" 2>/dev/null || true
  sleep 1
fi
pkill -f "python.*bot\.webapp" 2>/dev/null || true
sleep 0.5
nohup ./.venv/bin/python -m bot.webapp > .webapp.log 2>&1 &
```

## 3. Mandatory Post-Restart Verification

After triggering a restart, always verify:
1. **HTTP Status**: Verify the web service is responding:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/login
   ```
   (Should return HTTP `200`).
2. **Log Inspection**: Inspect the last 15–20 lines of `.webapp.log` to ensure no startup exceptions, import errors, or traceback crashes occurred.
3. If the server fails to start or encounters an error, immediately investigate the traceback, resolve the issue, and restart again.
