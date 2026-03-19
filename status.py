# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Scrooge Status — Structured status output for dashboard.

Writes JSON status files that Frank (monitor agent) and the
dashboard can read.

Output files:
  data/status.json  — Current bot state, updated every cycle
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

log = logging.getLogger('scrooge')

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(DATA_DIR, exist_ok=True)


def write_status(cycle: int, equity: float, daily_pnl: float,
                 daily_loss_pct: float, positions: list,
                 signal_results: list, box_states: dict,
                 active_sessions: list, active_instruments: list,
                 extra: dict = None):
    """Write current bot status to data/status.json.

    Args:
        cycle: Current cycle number
        equity: Current account equity in USD
        daily_pnl: P&L since day start in USD
        daily_loss_pct: Accumulated daily loss as fraction (0.05 = 5%)
        positions: List of OpenPosition objects from trade_manager.get_all_open()
        signal_results: List of SignalResult objects from this cycle
        box_states: Dict of {symbol: box_state_dict} pre-serialized
        active_sessions: List of active session names
        active_instruments: List of currently traded instruments
        extra: Optional dict merged into top level
    """
    import time as _time

    # Detect weekend — forex markets: open Sunday 22:00 UTC, close Friday 22:00 UTC
    utc_now = datetime.now(timezone.utc)
    weekday = utc_now.weekday()  # 0=Mon, 6=Sun
    hour = utc_now.hour
    is_weekend = (weekday == 5 or                              # Saturday
                  (weekday == 4 and hour >= 22) or             # Friday after close
                  (weekday == 6 and hour < 22))                # Sunday before open

    status = {
        'timestamp': utc_now.isoformat(),
        'cycle': cycle,
        'equity': round(equity, 2),
        'daily_pnl': round(daily_pnl, 2),
        'daily_loss_pct': round(daily_loss_pct, 4),
        'positions_open': len(positions),
        'active_sessions': active_sessions,
        'active_instruments': active_instruments,
        'weekend': is_weekend,
        'positions': [],
        'signals': [],
        'box_states': box_states or {},
    }

    # ── Position details ──
    for pos in positions:
        try:
            status['positions'].append({
                'trade_id': pos.trade_id,
                'symbol': pos.symbol,
                'direction': pos.direction,
                'entry_price': pos.entry_price,
                'current_stop': pos.current_stop,
                'original_stop': pos.original_stop,
                'tp1': pos.take_profit_1,
                'tp2': pos.take_profit_2,
                'original_quantity': pos.original_quantity,
                'remaining_quantity': pos.remaining_quantity,
                'entry_time': pos.entry_time,
                'signal_type': pos.entry_signal_type,
                'zone_tier': pos.zone_tier,
                'overhead_cost': round(pos.overhead_cost, 4),
                'break_even_achieved': pos.break_even_achieved,
                'partial_1_taken': pos.partial_1_taken,
                'partial_2_taken': pos.partial_2_taken,
                'oanda_trade_id': pos.oanda_trade_id,
                'status': pos.status,
            })
        except Exception as e:
            log.warning(f'[STATUS] Error serializing position: {e}')

    # ── Signal results ──
    for sig in signal_results:
        try:
            sig_dict = {
                'symbol': sig.symbol,
                'action': sig.action,
                'gates': {
                    'zone': {
                        'passed': sig.zone_gate,
                        'tier': sig.zone_tier,
                        'details': sig.zone_details,
                    },
                    'atr': {
                        'passed': sig.atr_gate,
                        'consumed_pct': round(sig.atr_consumed_pct, 3),
                        'strength': sig.atr_strength,
                    },
                    'sucker_move': {
                        'passed': sig.sucker_move_gate,
                    },
                    'pitchfork': {
                        'passed': sig.pitchfork_gate,
                        'type': sig.pitchfork.signal_type if sig.pitchfork else None,
                        'confidence': sig.pitchfork.confidence if sig.pitchfork else None,
                    },
                    'otr': {
                        'passed': sig.otr_gate,
                        'ratio': round(sig.otr_ratio, 4),
                        'category': sig.otr_category,
                    },
                },
                'entry_price': sig.entry_price,
                'stop_price': sig.stop_price,
                'tp1_price': sig.tp1_price,
                'tp2_price': sig.tp2_price,
                'risk_reward': round(sig.risk_reward_ratio, 2),
                'rejection_reasons': sig.rejection_reasons,
                'all_gates_passed': sig.all_gates_passed,
            }
            status['signals'].append(sig_dict)
        except Exception as e:
            log.warning(f'[STATUS] Error serializing signal: {e}')

    if extra:
        status.update(extra)

    path = os.path.join(DATA_DIR, 'status.json')
    try:
        with open(path, 'w') as f:
            json.dump(status, f, indent=2, default=str)
    except Exception as e:
        log.warning(f'[STATUS] Failed to write status: {e}')
