# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — Execution Router
Routes all orders through OANDA. Tracks pending limit orders.
Health monitoring: registers as 'execution_router' with HealthMonitor.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass

from .oanda_client import OANDAClient
from .fee_calculator import OANDAFeeCalculator

log = logging.getLogger('mr-scrooge-v2.execution_router')


def _monitor():
    from health.monitor import HealthMonitor
    return HealthMonitor.get()


@dataclass
class PendingOrder:
    trade_id:       str
    order_id:       str
    symbol:         str
    side:           str
    quantity:       float
    limit_price:    float
    take_profit:    float
    stop_loss:      float
    placed_at:      datetime
    timeout_minutes: int = 5


class OANDAExecutionRouter:
    """
    Order routing for mr-scrooge-v2.
    Primary:   market entry orders (fills at bid/ask)
    Exit:      OANDA native close (non-emergency), market (emergency)
    Tracking:  pending limit order dict (legacy — market orders don't need it)
    """

    _COMPONENT = 'execution_router'

    def __init__(self, oanda: OANDAClient, fee_calc: OANDAFeeCalculator, config=None):
        self.oanda = oanda
        self.fee_calc = fee_calc
        # Accept both Config dataclass and legacy dict
        if hasattr(config, 'limit_timeout_minutes'):
            self.limit_timeout_minutes = config.limit_timeout_minutes
            self.max_spread_pct        = config.max_spread_pct_of_half
        else:
            cfg = config or {}
            self.limit_timeout_minutes = cfg.get('limit_timeout_minutes', 5)
            self.max_spread_pct        = cfg.get('max_spread_pct_of_half', 0.30)

        self.pending: Dict[str, PendingOrder] = {}
        _monitor().register(self._COMPONENT, failure_threshold=3, recovery_seconds=60)

    # ── Entry ────────────────────────────────────────────────

    def enter_position(self, symbol: str, side: str, quantity: float,
                        limit_price: float, take_profit: float, stop_loss: float,
                        trade_id: str, live_spread: float, half_box: float) -> Dict:
        """Place a market entry order after spread check."""
        t0 = time.monotonic()
        acceptable, reason = self.fee_calc.is_spread_acceptable(symbol, live_spread, half_box)
        if not acceptable:
            log.warning(f'Entry blocked — {reason}')
            return {'success': False, 'reason': 'SPREAD_SPIKE', 'details': reason}

        result = self.oanda.place_entry_order(
            instrument=symbol,
            side=side,
            units=quantity,
            take_profit=take_profit,
            stop_loss=stop_loss,
            client_trade_id=trade_id,
        )
        ms = (time.monotonic() - t0) * 1000
        if result.get('success'):
            _monitor().record_success(self._COMPONENT, ms)
        else:
            _monitor().record_failure(self._COMPONENT, result.get('error', 'entry failed'), ms)
        return result

    # ── Pending order management ─────────────────────────────

    def check_and_manage_pending(self) -> List[Dict]:
        """Check pending limit orders each cycle. Returns fill/timeout events."""
        events = []
        to_remove = []
        for trade_id, pending in self.pending.items():
            elapsed = datetime.now() - pending.placed_at
            if elapsed > timedelta(minutes=pending.timeout_minutes):
                self.oanda.cancel_order(pending.order_id)
                to_remove.append(trade_id)
                log.info(f'Limit timed out: {trade_id} | {elapsed.seconds//60}min')
                events.append({'event': 'TIMEOUT', 'trade_id': trade_id, 'symbol': pending.symbol})
                continue
            status = self.oanda.check_order_filled(pending.order_id)
            if status.get('filled'):
                to_remove.append(trade_id)
                events.append({'event': 'FILLED', 'trade_id': trade_id,
                                'symbol': pending.symbol, 'fill_price': pending.limit_price,
                                'quantity': pending.quantity, 'side': pending.side})
            elif status.get('cancelled'):
                to_remove.append(trade_id)
                events.append({'event': 'CANCELLED', 'trade_id': trade_id, 'symbol': pending.symbol})
        for tid in to_remove:
            del self.pending[tid]
        return events

    # ── Trade management ─────────────────────────────────────

    def move_stop(self, trade_id: str, oanda_trade_id: str,
                   new_stop: float, reason: str) -> bool:
        success = self.oanda.modify_trade_stop(oanda_trade_id, new_stop)
        if success:
            log.info(f'Stop moved: {trade_id} → {new_stop:.5f} | {reason}')
        return success

    def partial_close(self, trade_id: str, oanda_trade_id: str,
                       units: float, tp_level: int) -> Dict:
        result = self.oanda.close_trade_partial(oanda_trade_id, units)
        if result.get('success'):
            log.info(f'Partial close TP{tp_level}: {trade_id} | {units:.0f} units')
        return result

    # ── Emergency exit ───────────────────────────────────────

    def emergency_exit(self, symbol: str, side: str, quantity: float,
                        stop_loss: float, trade_id: str, reason: str) -> Dict:
        """Market order exit for box breaks, daily loss limit, spread spikes."""
        log.warning(f'EMERGENCY EXIT: {trade_id} | {symbol} {side} {quantity:.0f} | {reason}')
        t0 = time.monotonic()
        result = self.oanda.place_market_order(
            instrument=symbol, side=side, units=quantity,
            stop_loss=stop_loss, client_trade_id=f'{trade_id}_exit', reason=reason,
        )
        ms = (time.monotonic() - t0) * 1000
        if result.get('success'):
            _monitor().record_success(self._COMPONENT, ms)
        else:
            _monitor().record_failure(self._COMPONENT, reason, ms)
        return result

    # ── Status ───────────────────────────────────────────────

    def get_pending_count(self) -> int:
        return len(self.pending)

    def get_pending_summary(self) -> List[Dict]:
        return [
            {'trade_id': p.trade_id, 'symbol': p.symbol, 'side': p.side,
             'limit_price': p.limit_price,
             'minutes_pending': int((datetime.now() - p.placed_at).total_seconds() / 60)}
            for p in self.pending.values()
        ]
