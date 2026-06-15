#!/usr/bin/env bash
#
# run.sh — process supervisor for unattended (VPS) paper recording.
#
# The bot's feeds and main loop already self-heal at the thread level (each WS feed has
# its own reconnect loop; the main loop wraps every tick in try/except). This wrapper adds
# PROCESS-level resilience: if python exits for any reason (OOM, unhandled fatal, manual
# kill), restart it with capped backoff so a 7-day session survives. Clean Ctrl-C (exit 0)
# stops the supervisor too.
#
# Usage:
#   ./run.sh                      # paper, all configured assets
#   ./run.sh --assets BTC,ETH     # forwards extra args to main.py
#   tmux new -s polybot './run.sh'   (or use polybot.service — see below)
#
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
LOG="bot.log"
DELAY=2
MAX_DELAY=60

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | SUPERVISOR | $*" | tee -a "$LOG"; }

trap 'log "supervisor received INT/TERM — stopping"; exit 0' INT TERM

log "supervisor starting: $PY main.py --mode paper $*"
while true; do
  "$PY" main.py --mode paper "$@"
  code=$?
  if [ "$code" -eq 0 ]; then
    log "main.py exited cleanly (0) — stopping supervisor"
    break
  fi
  log "main.py exited with code $code — restarting in ${DELAY}s"
  sleep "$DELAY"
  DELAY=$(( DELAY * 2 ))
  [ "$DELAY" -gt "$MAX_DELAY" ] && DELAY="$MAX_DELAY"
done
