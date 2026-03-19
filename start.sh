#!/bin/bash
# ============================================================
# MR. SCROOGE — start.sh
# OANDA forex bot launcher.
# Activates venv, sets working directory, launches main.py
# Enforces single-instance startup and cleans stale processes.
# ============================================================

set -euo pipefail

AGENT_DIR="$HOME/.openclaw/workspace/agents/mr-scrooge"
LOG_DIR="$AGENT_DIR/logs"
PIDFILE="$LOG_DIR/mr-scrooge.pid"
LOCKFILE="$LOG_DIR/start.lock"

cd "$AGENT_DIR"
mkdir -p "$LOG_DIR"

# Off switch — delete DISABLED file to re-enable
if [ -f "DISABLED" ]; then
  echo "Mr. Scrooge is DISABLED. Remove DISABLED to re-enable."
  exit 0
fi

# Prevent overlapping start.sh invocations
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) mr-scrooge WARNING start.sh already running; aborting duplicate start request" >> "$LOG_DIR/launchd.log"
  exit 0
fi

_list_scrooge_pids() {
  pgrep -f 'mr-scrooge|python3 main.py|mr_burns_env/bin/python3 main.py' | while read -r pid; do
    [ -z "$pid" ] && continue
    [ "$pid" = "$$" ] && continue
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    if [ -r "/proc/$pid/cmdline" ]; then
      cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    else
      cmd=""
    fi
    if [ "$cwd" = "$AGENT_DIR" ] && echo "$cmd" | grep -q "main.py"; then
      echo "$pid"
    fi
  done
}

# Stop all existing mr-scrooge instances (PID file + orphaned/manual launches)
PIDS="$(_list_scrooge_pids | sort -u | tr '\n' ' ')"
if [ -n "${PIDS// }" ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) mr-scrooge INFO Stopping existing process(es): $PIDS" >> "$LOG_DIR/launchd.log"
  kill $PIDS 2>/dev/null || true
  sleep 2
  STILL_RUNNING="$(_list_scrooge_pids | sort -u | tr '\n' ' ')"
  if [ -n "${STILL_RUNNING// }" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) mr-scrooge WARNING Force-killing process(es): $STILL_RUNNING" >> "$LOG_DIR/launchd.log"
    kill -9 $STILL_RUNNING 2>/dev/null || true
  fi
fi
rm -f "$PIDFILE"

# Trading bot virtual environment
source "$HOME/mr_burns_env/bin/activate"

# Load credentials into environment (belt-and-suspenders alongside load_dotenv)
set -a
source .env 2>/dev/null || true
set +a

# Log boot
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) mr-scrooge INFO Mr. Scrooge starting — account: $(grep OANDA_ACCOUNT_ID .env | cut -d= -f2)" >> "$LOG_DIR/launchd.log"

# Release launcher lock before execing Python so future restarts are not blocked.
exec 9>&-

# Launch and save PID
exec python3 main.py
