#!/usr/bin/env bash
# HECinBOX container entrypoint.
#
# Starts two processes:
#   1. The auto-scheduler daemon (background) — keeps the real-time
#      forecasting loop alive for months without any browser session.
#   2. The Streamlit web UI (foreground) — PID 1, so Docker signals
#      reach it cleanly when the container stops.

set -e

LOG=/app/.autoschedule.log
# Cap the daemon log at the last ~1000 lines so it never grows forever.
if [[ -f "$LOG" ]]; then
    tail -n 1000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" || true
else
    touch "$LOG"
fi

# Start the daemon in the background.  -u keeps stdout unbuffered so
# `docker logs` shows it live.
python -u -m auto_scheduler &
DAEMON_PID=$!
echo "[entrypoint] auto_scheduler daemon started (pid=$DAEMON_PID)"

# If the daemon dies, log it but keep the container alive — Streamlit
# is the foreground process that defines container life-cycle.
(
    wait "$DAEMON_PID"
    echo "[entrypoint] WARNING: auto_scheduler daemon exited (code $?)"
) &

# Streamlit in the foreground.
exec streamlit run /app/src/app.py \
    --server.port=8501 \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
