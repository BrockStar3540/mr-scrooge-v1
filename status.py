# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""mr-scrooge-v2 — Status writer. Writes JSON to data/status.json each cycle."""
import os
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger('mr-scrooge-v2.status')
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(DATA_DIR, exist_ok=True)


def write_status(cycle: int, equity: float, daily_pnl: float,
                 daily_loss_pct: float, positions: list,
                 signal_results: list, box_states: dict,
                 active_sessions: list, active_instruments: list,
                 health_report: dict = None, extra: dict = None):
    utc_now = datetime.now(timezone.utc)
    wd, hr = utc_now.weekday(), utc_now.hour
    is_weekend = wd == 5 or (wd == 4 and hr >= 22) or (wd == 6 and hr < 22)
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
        'health': health_report or {},
        'positions': [],
        'signals': [],
        'box_states': box_states or {},
    }
    for pos in positions:
        try:
            status['positions'].append({
                'trade_id': pos.trade_id, 'symbol': pos.symbol, 'direction': pos.direction,
                'entry_price': pos.entry_price, 'current_stop': pos.current_stop,
                'original_stop': pos.original_stop, 'tp1': pos.take_profit_1, 'tp2': pos.take_profit_2,
                'original_quantity': pos.original_quantity, 'remaining_quantity': pos.remaining_quantity,
                'entry_time': pos.entry_time, 'signal_type': pos.entry_signal_type,
                'zone_tier': pos.zone_tier, 'break_even_achieved': pos.break_even_achieved,
                'partial_1_taken': pos.partial_1_taken, 'oanda_trade_id': pos.oanda_trade_id,
            })
        except Exception as exc:
            log.warning(f'Error serializing position: {exc}')
    for sig in signal_results:
        try:
            status['signals'].append({
                'symbol': sig.symbol, 'action': sig.action,
                'zone_gate': sig.zone_gate, 'zone_tier': sig.zone_tier,
                'sucker_move_gate': sig.sucker_move_gate,
                'atr_gate': sig.atr_gate, 'atr_strength': sig.atr_strength,
                'pitchfork_gate': sig.pitchfork_gate,
                'pitchfork_type': sig.pitchfork.signal_type if sig.pitchfork else None,
                'otr_gate': sig.otr_gate, 'otr_category': sig.otr_category,
                'all_gates_passed': sig.all_gates_passed, 'score': sig.score,
                'rejection': ' | '.join(sig.rejection_reasons) if sig.rejection_reasons else '',
            })
        except Exception as exc:
            log.warning(f'Error serializing signal: {exc}')
    if extra:
        status.update(extra)
    path = os.path.join(DATA_DIR, 'status.json')
    tmp  = path + '.tmp'
    try:
        with open(tmp, 'w') as fh:
            json.dump(status, fh, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as exc:
        log.error(f'write_status failed: {exc}')
