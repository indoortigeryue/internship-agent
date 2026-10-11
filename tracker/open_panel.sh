#!/bin/bash
# Open the job dashboard (the version that can add/remove jobs from the page).
# If the panel server is already running, just opens the browser.
#
# Why not double-click dashboard.html: it's a static file and the browser
# can't write to disk, so add/remove has to go through the local panel.py server.

set -u
cd "$(dirname "$0")" || exit 1
PORT="${1:-8765}"

if curl -s --max-time 2 "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1; then
    echo "panel already running → http://127.0.0.1:$PORT"
else
    echo "starting panel…"
    nohup python3 panel.py --no-open --port "$PORT" >/tmp/job-panel.log 2>&1 &
    for _ in $(seq 1 20); do
        curl -s --max-time 1 "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1 && break
        sleep 0.4
    done
    if curl -s --max-time 2 "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1; then
        echo "started → http://127.0.0.1:$PORT"
    else
        echo "failed to start, see /tmp/job-panel.log"
        exit 1
    fi
fi

open "http://127.0.0.1:$PORT"   # IP, not localhost: when panel.py only bound IPv4,
                                # Chrome resolved localhost to ::1 → ERR_EMPTY_RESPONSE.
                                # panel.py now listens on both, but the IP form is safest.
echo "stop the panel: pkill -f 'panel.py'"
