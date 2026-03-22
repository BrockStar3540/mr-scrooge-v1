#!/bin/bash
# mr-scrooge-v2 start script
cd "$(dirname "$0")"
source ~/.openclaw/secrets.env 2>/dev/null || true
exec python3 main.py
