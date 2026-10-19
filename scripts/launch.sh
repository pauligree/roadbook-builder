#!/bin/bash
# Start the Roadbook Builder web app (if not already running) and open it in
# the default browser. Used by the desktop launcher app built by
# scripts/make_desktop_app.sh; also fine to run by hand.
#
#   scripts/launch.sh          start (if needed) + open browser
#   scripts/launch.sh --stop   stop the background server

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=8000
URL="http://127.0.0.1:$PORT"
LOG="$PROJECT_DIR/work/server.log"

running() { curl -s -o /dev/null --max-time 1 "$URL/api/providers"; }

if [ "$1" = "--stop" ]; then
    pids=$(lsof -ti tcp:$PORT -sTCP:LISTEN)
    [ -n "$pids" ] && kill $pids && echo "Stopped server ($pids)" || echo "Server not running"
    exit 0
fi

if ! running; then
    mkdir -p "$PROJECT_DIR/work"
    cd "$PROJECT_DIR" || exit 1
    nohup "$PROJECT_DIR/.venv/bin/python" -m webapp.server >"$LOG" 2>&1 &
    for _ in $(seq 1 40); do
        running && break
        sleep 0.25
    done
    if ! running; then
        osascript -e "display alert \"Roadbook Builder failed to start\" message \"See $LOG\"" >/dev/null 2>&1
        exit 1
    fi
fi

open "$URL"
