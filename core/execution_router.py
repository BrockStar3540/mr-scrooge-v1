# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
# ============================================================
# MR. SCROOGE — OANDA EXECUTION ROUTER
#
# Routes all orders through OANDA.
# Primary: limit orders (no slippage, same spread cost)
# Emergency: market orders (exits only, logs spread impact)
#
# Manages pending limit orders:
# - Polls for fills every cycle
# - Cancels unfilled limits after timeout (price left zone)
# - Never chases price after cancellation
#
# Coordinates with fee_calculator for spread spike detection.
# ============================================================

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass

from core.oanda_client import OANDAClient
from core.fee_calculator import OANDAFeeCalculator

log = logging.getLogger('mr-scrooge.execution_router')


@dataclass
class PendingOrder:
    trade_id: str
    order_id: str
    symbol: str
    side: str
    quantity: float
    limit_price: float
    take_profit: float
    stop_loss: float
    placed_at: datetime
    timeout_minutes: int = 5


class OANDAExecutionRouter:
    """
    Handles all order routing for Mr. Scrooge.

    Workflow:
    1. enter_position() — places limit order at pitchfork trigger
    2. check_and_manage_pending() — polls fills, cancels timeouts
    3. emergency_exit() — market order for box breaks / time exits
    4. manage_stop() — moves stop to break-even or trail level
    5. partial_close() — scales out at TP1 and TP2
    """

    def __init__(self,
                 oanda: OANDAClient,
                 fee_calc: OANDAFeeCalculator,
                 config: dict = None):
        self.oanda = oanda
        self.fee_calc = fee_calc
        cfg = config or {}
        self.limit_timeout_minutes = cfg.get('limit_timeout_minutes', 5)
        self.max_spread_retry = cfg.get('max_spread_retry', 3)

        # Pending limit orders waiting for fills
        self.pending: Dict[str, PendingOrder] = {}

    # ----------------------------------------------------------
    # ENTRY
    # ----------------------------------------------------------

    def enter_position(self,
                        symbol: str,
                        side: str,
                        quantity: float,
                        limit_price: float,
                        take_profit: float,
                        stop_loss: float,
                        trade_id: str,
                        live_spread: float,
                        half_box: float) -> Dict:
        """
        Places a limit order at the pitchfork trigger price.

        Checks spread one final time before placing.
        If spread is spiked, holds off and returns SPREAD_SPIKE.
        """
        # Final spread check before committing
        acceptable, reason = self.fee_calc.is_spread_acceptable(
            symbol, live_spread, half_box
        )
        if not acceptable:
            log.warning(f'Entry blocked — {reason}')
            return {
                'success': False,
                'reason': 'SPREAD_SPIKE',
                'details': reason
            }

        # Use market order for entries — OANDA cancels limits placed at current market price.
        # Spread is the only overhead, already factored into OTR.
        result = self.oanda.place_entry_order(
            instrument=symbol,
            side=side,
            units=quantity,
            take_profit=take_profit,
            stop_loss=stop_loss,
            client_trade_id=trade_id
        )

        if result.get('success'):
            fill_price = result.get('fill_price', limit_price)
            # Market orders fill immediately — no pending tracking needed
            log.info(
                f'Entry order filled: {trade_id} | {symbol} {side} {quantity:.4f} '
                f'@ {fill_price:.5f} | TP: {take_profit:.5f} SL: {stop_loss:.5f}'
            )

        return result

    # ----------------------------------------------------------
    # PENDING ORDER MANAGEMENT
    # ----------------------------------------------------------

    def check_and_manage_pending(self) -> List[Dict]:
        """
        Called every 5-minute candle close.
        Checks all pending limit orders:
        - Filled → notify trade manager to register position
        - Timeout → cancel, log, do not re-enter
        - Still pending → leave it

        Returns list of events for the main loop to process.
        """
        events = []
        to_remove = []

        for trade_id, pending in self.pending.items():
            # Check timeout first
            elapsed = datetime.now() - pending.placed_at
            if elapsed > timedelta(minutes=pending.timeout_minutes):
                cancelled = self.oanda.cancel_order(pending.order_id)
                to_remove.append(trade_id)
                log.info(
                    f'Limit order timed out: {trade_id} | '
                    f'{elapsed.seconds//60}min elapsed | '
                    f'Price left zone — not chasing'
                )
                events.append({
                    'event': 'TIMEOUT',
                    'trade_id': trade_id,
                    'symbol': pending.symbol,
                    'reason': f'Limit unfilled after {self.limit_timeout_minutes} minutes'
                })
                continue

            # Check fill status
            status = self.oanda.check_order_filled(pending.order_id)
            if status.get('filled'):
                to_remove.append(trade_id)
                log.info(f'Limit order filled: {trade_id} | {pending.symbol}')
                events.append({
                    'event': 'FILLED',
                    'trade_id': trade_id,
                    'symbol': pending.symbol,
                    'fill_price': pending.limit_price,
                    'quantity': pending.quantity,
                    'side': pending.side
                })
            elif status.get('cancelled'):
                to_remove.append(trade_id)
                log.warning(f'Order cancelled externally: {trade_id}')
                events.append({
                    'event': 'CANCELLED',
                    'trade_id': trade_id,
                    'symbol': pending.symbol
                })

        for trade_id in to_remove:
            del self.pending[trade_id]

        return events

    # ----------------------------------------------------------
    # TRADE MANAGEMENT
    # ----------------------------------------------------------

    def move_stop(self,
                   trade_id: str,
                   oanda_trade_id: str,
                   new_stop: float,
                   reason: str) -> bool:
        """Moves stop loss — break-even or hourly trail."""
        success = self.oanda.modify_trade_stop(oanda_trade_id, new_stop)
        if success:
            log.info(f'Stop moved: {trade_id} → {new_stop:.5f} | {reason}')
        return success

    def partial_close(self,
                       trade_id: str,
                       oanda_trade_id: str,
                       units: float,
                       tp_level: int) -> Dict:
        """Scales out at TP1 or TP2."""
        result = self.oanda.close_trade_partial(oanda_trade_id, units)
        if result.get('success'):
            log.info(
                f'Partial close TP{tp_level}: {trade_id} | '
                f'{units:.4f} units @ {result.get("fill_price", "N/A")}'
            )
        return result

    # ----------------------------------------------------------
    # EMERGENCY EXIT
    # ----------------------------------------------------------

    def emergency_exit(self,
                        symbol: str,
                        side: str,
                        quantity: float,
                        stop_loss: float,
                        trade_id: str,
                        reason: str) -> Dict:
        """
        Market order exit for:
        - Box break confirmed
        - Time exit (30 min limit)
        - Daily loss limit hit
        - Spread spike mid-trade

        Logs the situation but does NOT calculate spread penalty
        (OANDA spread cost is the same for limit and market —
        the risk here is slippage, not fee differential).
        """
        log.warning(
            f'EMERGENCY EXIT: {trade_id} | {symbol} {side} {quantity:.4f} | '
            f'Reason: {reason}'
        )

        result = self.oanda.place_market_order(
            instrument=symbol,
            side=side,
            units=quantity,
            stop_loss=stop_loss,
            client_trade_id=f'{trade_id}_exit',
            reason=reason
        )

        if result.get('success'):
            fill = result.get('fill_price', 0)
            log.warning(
                f'Emergency exit filled @ {fill:.5f} | '
                f'Note: market orders carry slippage risk on OANDA'
            )

        return result

    # ----------------------------------------------------------
    # STATUS
    # ----------------------------------------------------------

    def get_pending_count(self) -> int:
        return len(self.pending)

    def get_pending_summary(self) -> List[Dict]:
        return [
            {
                'trade_id': p.trade_id,
                'symbol': p.symbol,
                'side': p.side,
                'limit_price': p.limit_price,
                'placed_at': p.placed_at.isoformat(),
                'minutes_pending': int(
                    (datetime.now() - p.placed_at).total_seconds() / 60
                )
            }
            for p in self.pending.values()
        ]
