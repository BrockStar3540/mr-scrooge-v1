#!/bin/bash
# ============================================================
# MR. SCROOGE HARVESTER — start.sh
# ============================================================
set -euo pipefail

AGENT_DIR="$HOME/.openclaw/workspace/agents/mr-scrooge/harvester"
LOG_DIR="$AGENT_DIR/logs"

cd "$AGENT_DIR"
mkdir -p "$LOG_DIR"

if [ -f "DISABLED" ]; then
  echo "Harvester is DISABLED. Remove DISABLED to re-enable."
  exit 0
fi

source "$HOME/mr_scrooge_env/bin/activate"

set -a
source "$HOME/.openclaw/workspace/agents/mr-scrooge/.env" 2>/dev/null || true
set +a

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) mr-scrooge.harvester INFO Starting" >> "$LOG_DIR/launchd.log"

exec python3 main.py
