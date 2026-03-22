#!/usr/bin/env python3
# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Entry point.
Boots the Coordinator and runs the 5-minute main loop.
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Separate log files for decisions and trades
decision_handler = logging.FileHandler('logs/decisions.log')
decision_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logging.getLogger('mr-scrooge-v2.decisions').addHandler(decision_handler)

trade_handler = logging.FileHandler('logs/trades.log')
trade_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logging.getLogger('mr-scrooge-v2.trades').addHandler(trade_handler)

from config import CFG
from coordinator import Coordinator

if __name__ == '__main__':
    Coordinator(CFG).run()
