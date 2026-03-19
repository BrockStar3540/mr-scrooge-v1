#!/bin/bash
# ============================================================
# MR. HARVESTER — start.sh
# Profit extraction daemon launcher.
# ============================================================

set -euo pipefail

AGENT_DIR="$HOME/.openclaw/workspace/agents/mr-harvester"
LOG_DIR="$AGENT_DIR/logs"

cd "$AGENT_DIR"
mkdir -p "$LOG_DIR"

if [ -f "DISABLED" ]; then
  echo "Mr. Harvester is DISABLED. Remove DISABLED to re-enable."
  exit 0
fi

source "$HOME/mr_burns_env/bin/activate"

# Load Scrooge credentials (harvester shares the same OANDA account)
SCROOGE_DIR="$HOME/.openclaw/workspace/agents/mr-scrooge"
set -a
source "$SCROOGE_DIR/.env" 2>/dev/null || true
set +a

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) mr-harvester INFO Starting" >> "$LOG_DIR/launchd.log"

exec python3 main.py
