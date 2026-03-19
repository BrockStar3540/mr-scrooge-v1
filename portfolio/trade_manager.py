# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
# ============================================================
# BOXBOT — TRADE MANAGER
# Handles all post-entry management per Doug & Sylvia's rules:
#
#   BREAK-EVEN STOP:
#     Move stop to break-even after first partial taken
#     or after clear directional move
#
#   PARTIAL PROFITS:
#     Scale out at each magic line encountered
#     Default: 40% at TP1, 40% at TP2, 20% trailing
#
#   HOURLY TRAILING STOP:
#     After break-even: trail stop below each hourly candle LOW (long)
#     or above each hourly candle HIGH (short)
#     Never manually exit — let trailing stop work you out
#
#   RE-ENTRY PROTOCOL:
#     After stopout: check daily chart for liquidity context
#     If still at valid liquidity zone: allow ONE re-entry
#     Re-entry uses same pitchfork confirmation rules
#
#   OVERHEAD AWARENESS:
#     Every partial profit checked against overhead
#     Emergency exits logged with cost penalty
# ============================================================

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone
import logging

from strategy.zone_detector import ZoneMap, MagicLine
from strategy.signal_engine import SignalResult


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class OpenPosition:
    """Represents one active trade."""
    trade_id: str
    symbol: str
    exchange: str
    direction: str              # 'long' or 'short'
    entry_price: float
    original_stop: float
    current_stop: float
    take_profit_1: float
    take_profit_2: float
    original_quantity: float
    remaining_quantity: float
    entry_time: str
    entry_signal_type: str      # 'pitchfork', 'john_wick', 'power_of_towers'
    zone_tier: int
    atr_at_entry: float
    overhead_cost: float        # Total overhead for this position
    break_even_achieved: bool
    partial_1_taken: bool
    partial_2_taken: bool
    last_hourly_close: float    # For hourly trailing stop
    hourly_green_checks_done: int  # Number of hourly green-exit checkpoints already evaluated
    status: str                 # 'open', 'partial', 'closed'
    stop_moved_at: str          # When stop was last adjusted
    oanda_trade_id: str = ''    # Numeric OANDA trade ID (e.g. '727') for stop/close management
    green_exit_tp_set: float = 0.0  # Price of last successfully-set green exit TP (0 = none set)
    highest_price: float = 0.0  # Peak price since entry (long) — for hybrid trailing stop
    lowest_price: float = 0.0   # Trough price since entry (short) — for hybrid trailing stop


@dataclass
class TradeEvent:
    """A discrete event in a trade's lifecycle."""
    trade_id: str
    event_type: str             # 'entry', 'partial_tp', 'stop_move', 'exit', 'reentry'
    timestamp: str
    price: float
    quantity: float
    reason: str
    pnl: float
    overhead_applied: float
    cumulative_pnl: float
    order_type: str             # 'LIMIT' or 'MARKET'


@dataclass
class ReentryAssessment:
    """Evaluates whether a re-entry is valid after a stopout."""
    allowed: bool
    reason: str
    still_at_liquidity_zone: bool
    zone_tested_count: int
    was_liquidity_sweep: bool   # Classic stop-hunt before reversal
    daily_context: str          # Summary of daily chart context
    max_reentries_reached: bool


# ============================================================
# TRADE MANAGER
# ============================================================

class TradeManager:
    """
    Manages all active positions from entry through exit.

    Core philosophy (from Doug & Sylvia):
    - Enter with conviction at zones
    - Take partials along the way to remove pressure
    - Move stop to break-even ASAP
    - Let trailing stop work you out — never manually exit
    - After stopout, zoom out to daily before deciding to re-enter
    """

    def __init__(self, config: dict):
        # Partial profit ratios
        self.partial_1_pct = config.get('partial_1_pct', 0.50)  # 50% at TP1 (was 40%)
        self.partial_2_pct = config.get('partial_2_pct', 0.30)  # 30% at TP2 (20% runs trailing)
        # Remaining 20% runs with trailing stop

        # Break-even trigger
        # Move stop to BE when unrealized profit exceeds this multiple of risk
        self.be_trigger_rr = config.get('be_trigger_rr', 1.0)   # 1.0R profit → move BE (raised from 0.5 — too tight, normal noise triggered it)

        # Re-entry rules
        self.max_reentries_per_setup = config.get('max_reentries_per_setup', 3)  # MASTER_THEORY: re-enter as long as zone holds
        self.reentry_liquidity_tolerance = config.get('reentry_liquidity_tolerance', 0.003)

        # Stop buffer
        self.stop_buffer_pct = config.get('stop_buffer_pct', 0.0005)  # 0.05% beyond candle (raised from 0.02% — too tight for forex trailing)

        # Time exit
        self.max_hold_minutes = config.get('max_hold_minutes', 240)  # MASTER_THEORY: hold while valid; 4h backstop only
        self.hourly_green_exit_enabled = bool(config.get('hourly_green_exit_enabled', False))
        self.hourly_green_exit_minutes = max(1, int(config.get('hourly_green_exit_minutes', 60)))
        self.hourly_green_exit_min_pnl = float(config.get('hourly_green_exit_min_pnl', 0.002))  # legacy
        self.green_exit_min_pct = float(config.get('green_exit_min_pct', 0.00125))  # 0.125% equity — 50% of 0.25% target

        # Track active positions and re-entry counts
        self.open_positions: Dict[str, OpenPosition] = {}
        self.reentry_counts: Dict[str, int] = {}  # symbol: count today
        self.trade_history: List[TradeEvent] = []

    # ----------------------------------------------------------
    # STARTUP RECONCILIATION
    # ----------------------------------------------------------

    def reconcile_from_oanda(self, trades: list) -> int:
        """
        Re-hydrates open_positions from OANDA's open trade list on startup.
        Prevents trade stacking after bot restarts.
        Returns number of positions reconciled.
        """
        count = 0
        for trade in trades:
            client_id = (
                trade.get('clientExtensions', {}).get('id', '')
                or trade.get('tradeClientExtensions', {}).get('id', '')
            )
            if not (client_id.startswith('scrooge-') or client_id.startswith('scrooge_')):
                continue

            if client_id in self.open_positions:
                continue  # Already tracked

            instrument = trade.get('instrument', '')
            initial_units = abs(float(trade.get('initialUnits', 0)))
            current_units = abs(float(trade.get('currentUnits', 0)))
            entry_price   = float(trade.get('price', 0))
            direction     = 'short' if float(trade.get('currentUnits', 0)) < 0 else 'long'
            stop_price    = float(trade.get('stopLossOrder', {}).get('price', 0))
            tp_price      = float(trade.get('takeProfitOrder', {}).get('price', 0))
            entry_time    = trade.get('openTime', datetime.now().isoformat())
            oanda_id      = str(trade.get('id', ''))

            position = OpenPosition(
                trade_id=client_id,
                symbol=instrument,
                exchange='OANDA',
                direction=direction,
                entry_price=entry_price,
                original_stop=stop_price,
                current_stop=stop_price,
                take_profit_1=0.0,       # TP1 partial price not recoverable from OANDA on restart
                take_profit_2=tp_price,  # OANDA native TP = our tp2
                original_quantity=initial_units,
                remaining_quantity=current_units,
                entry_time=entry_time,
                entry_signal_type='reconciled',
                zone_tier=2,
                atr_at_entry=0.0,
                overhead_cost=0.0,
                break_even_achieved=False,
                partial_1_taken=(initial_units != current_units),
                partial_2_taken=False,
                last_hourly_close=entry_price,
                hourly_green_checks_done=0,
                status='open',
                stop_moved_at=entry_time,
                oanda_trade_id=oanda_id,
                highest_price=entry_price,
                lowest_price=entry_price
            )
            self.open_positions[client_id] = position
            count += 1
            logging.info(
                f'Reconciled from OANDA: {client_id} | {instrument} {direction.upper()} '
                f'{current_units:.0f} units @ {entry_price:.5f} | '
                f'SL: {stop_price:.5f} | TP: {tp_price:.5f} | OANDA ID: {oanda_id}'
            )

        return count

    # ----------------------------------------------------------
    # ENTRY
    # ----------------------------------------------------------

    def register_entry(self,
                        trade_id: str,
                        symbol: str,
                        exchange: str,
                        signal: SignalResult,
                        quantity: float,
                        actual_fill_price: float,
                        overhead_cost: float,
                        atr: float,
                        oanda_trade_id: str = '') -> OpenPosition:
        """
        Registers a new position after order fills.
        Sets up all management levels.
        """
        position = OpenPosition(
            trade_id=trade_id,
            symbol=symbol,
            exchange=exchange,
            direction='long' if signal.action == 'ENTER_LONG' else 'short',
            entry_price=actual_fill_price,
            original_stop=signal.stop_price,
            current_stop=signal.stop_price,
            take_profit_1=signal.tp1_price,
            take_profit_2=signal.tp2_price,
            original_quantity=quantity,
            remaining_quantity=quantity,
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_signal_type=signal.pitchfork.signal_type if signal.pitchfork else 'unknown',
            zone_tier=signal.zone_tier,
            atr_at_entry=atr,
            overhead_cost=overhead_cost,
            break_even_achieved=False,
            partial_1_taken=False,
            partial_2_taken=False,
            last_hourly_close=actual_fill_price,
            hourly_green_checks_done=0,
            status='open',
            stop_moved_at=datetime.now().isoformat(),
            oanda_trade_id=oanda_trade_id,
            highest_price=actual_fill_price,
            lowest_price=actual_fill_price
        )

        self.open_positions[trade_id] = position

        self._log_event(TradeEvent(
            trade_id=trade_id,
            event_type='entry',
            timestamp=datetime.now().isoformat(),
            price=actual_fill_price,
            quantity=quantity,
            reason=f'{signal.pitchfork.signal_type if signal.pitchfork else "unknown"} '
                   f'at Tier {signal.zone_tier} zone',
            pnl=0,
            overhead_applied=overhead_cost / 2,  # Entry side of overhead
            cumulative_pnl=-(overhead_cost / 2),
            order_type='LIMIT'
        ))

        logging.info(
            f"Position opened: {trade_id} | {symbol} {position.direction.upper()} "
            f"@ {actual_fill_price:.5f} | Stop: {signal.stop_price:.5f} | "
            f"TP1: {signal.tp1_price:.5f} | TP2: {signal.tp2_price:.5f}"
        )

        return position

    # ----------------------------------------------------------
    # ONGOING MANAGEMENT (called every 5-minute candle close)
    # ----------------------------------------------------------

    def manage_position(self,
                         trade_id: str,
                         current_price: float,
                         bars_5m: pd.DataFrame,
                         bars_1h: pd.DataFrame,
                         minutes_in_trade: int,
                         zone_map=None,
                         account_equity: float = 0.0) -> Dict:
        """
        Called every 5-minute candle close.
        Evaluates and executes management actions.

        Returns dict with actions to execute:
        {
          'action': 'hold' | 'partial_tp' | 'move_stop' | 'close' | 'set_tp',
          'price': float,
          'quantity': float,
          'reason': str,
          'order_type': 'LIMIT' | 'MARKET'
        }
        """
        if trade_id not in self.open_positions:
            return {'action': 'hold', 'reason': 'Position not found'}

        pos = self.open_positions[trade_id]
        actions = []

        # OANDA native TP (TP2) and SL handle all fills — bot never places market orders.
        # Manage position by advancing SL only:
        #   Stage 1: Move SL to break-even when price is 50% of way to TP1
        #   Stage 2: Move SL to TP1 level when price passes TP1 (locks TP1 profit)
        #   Stage 3: Hybrid trailing stop after break-even (hourly candle + ATR + profit lock)
        # OANDA native TP at TP2 closes the full remaining position at limit price.

        # Stage 1: Move SL to break-even when price is 50% of way to TP1.
        # This proactively protects against reversal early in the trade.
        if not pos.break_even_achieved and pos.take_profit_1 > 0:
            if pos.direction == 'long':
                half_to_tp1 = pos.entry_price + 0.5 * (pos.take_profit_1 - pos.entry_price)
                be_cond = current_price >= half_to_tp1
            else:
                half_to_tp1 = pos.entry_price - 0.5 * (pos.entry_price - pos.take_profit_1)
                be_cond = current_price <= half_to_tp1
            if be_cond:
                new_sl = pos.entry_price
                valid_move = (
                    (pos.direction == 'long'  and new_sl > pos.current_stop) or
                    (pos.direction == 'short' and new_sl < pos.current_stop)
                )
                if valid_move:
                    pos.current_stop = new_sl
                    pos.break_even_achieved = True
                    pos.stop_moved_at = datetime.now().isoformat()
                    actions.append({
                        'action': 'move_stop',
                        'price': new_sl,
                        'reason': f'Break-even: price {current_price:.5f} is 50% of way to TP1 {pos.take_profit_1:.5f}'
                    })

        # Stage 2: Move SL to TP1 when price passes TP1 — locks in TP1 profit.
        # Uses partial_1_taken as the 'TP1 passed' sentinel (repurposed from old partial framework).
        # OANDA native TP at TP2 will close the full remaining position at limit price.
        if pos.take_profit_1 > 0 and not pos.partial_1_taken:
            if pos.direction == 'long':
                tp1_cond = current_price >= pos.take_profit_1
            else:
                tp1_cond = current_price <= pos.take_profit_1
            if tp1_cond:
                pos.partial_1_taken = True  # Sentinel: TP1 level passed
                pos.break_even_achieved = True  # Always true after TP1
                new_sl = pos.take_profit_1
                valid_move = (
                    (pos.direction == 'long'  and new_sl > pos.current_stop) or
                    (pos.direction == 'short' and new_sl < pos.current_stop)
                )
                if valid_move:
                    pos.current_stop = new_sl
                    pos.stop_moved_at = datetime.now().isoformat()
                    actions.append({
                        'action': 'move_stop',
                        'price': new_sl,
                        'reason': f'TP1 passed — SL locked to TP1 @ {new_sl:.5f}'
                    })
                    log.info(
                        f'{trade_id}: TP1 {pos.take_profit_1:.5f} passed '
                        f'@ {current_price:.5f} — SL advanced to TP1'
                    )

        # Stage 3: Hybrid trailing stop after break-even.
        # Update peak/trough every call for accurate trail calculations.
        if pos.direction == 'long':
            if pos.highest_price == 0.0 or current_price > pos.highest_price:
                pos.highest_price = current_price
        else:
            if pos.lowest_price == 0.0 or current_price < pos.lowest_price:
                pos.lowest_price = current_price

        if pos.break_even_achieved and len(bars_1h) >= 2:
            new_stop = self._calculate_hybrid_trail(pos, bars_1h, current_price)
            valid_trail = (
                (pos.direction == 'long'  and new_stop > pos.current_stop) or
                (pos.direction == 'short' and new_stop < pos.current_stop)
            )
            if valid_trail:
                pos.current_stop = new_stop
                pos.stop_moved_at = datetime.now().isoformat()
                pos.last_hourly_close = float(bars_1h['close'].iloc[-2])
                actions.append({
                    'action': 'move_stop',
                    'price': new_stop,
                    'reason': f'Hybrid trail: {new_stop:.5f}'
                })

        if not actions:
            return {'action': 'hold', 'reason': 'No management action needed'}

        # Return the best move_stop across all stages (max for long, min for short)
        move_stops = [a for a in actions if a['action'] == 'move_stop']
        if move_stops:
            if pos.direction == 'long':
                return max(move_stops, key=lambda a: a['price'])
            else:
                return min(move_stops, key=lambda a: a['price'])

        return actions[0]

    # ----------------------------------------------------------
    # HOURLY TRAILING STOP
    # ----------------------------------------------------------

    def _calculate_hybrid_trail(self,
                                  pos: 'OpenPosition',
                                  bars_1h,
                                  current_price: float) -> float:
        """
        Hybrid trailing stop: max(hourly_candle_trail, 70%_profit_lock, 1.5xATR_trail)

        Three components, use whichever gives the tightest ratchet floor:
          1. Hourly candle trail: stop behind last completed hourly low/high
          2. Profit lock: entry + 70% of (highest_price - entry)
          3. ATR trail: highest_price - 1.5 * atr_at_entry

        Hybrid components 2+3 only activate after profit > 1 ATR.
        Stop only moves in direction of profit (ratchet - never loosened).
        """
        if len(bars_1h) < 2:
            return pos.current_stop

        atr = pos.atr_at_entry
        profit_lock_pct   = 0.80  # lock 80% of peak unrealized profit
        atr_trail_mult    = 1.5   # trail 1.5x ATR from peak
        activate_atr_mult = 1.0   # engage hybrid only when profit > 1 ATR

        last_1h = bars_1h.iloc[-2]

        if pos.direction == 'long':
            peak   = pos.highest_price if pos.highest_price > 0 else current_price
            profit = peak - pos.entry_price

            candle_trail = float(last_1h['low']) * (1 - self.stop_buffer_pct)

            if atr > 0 and profit >= atr * activate_atr_mult:
                lock_stop = pos.entry_price + profit_lock_pct * profit
                atr_stop  = peak - atr_trail_mult * atr
                new_stop  = max(candle_trail, lock_stop, atr_stop)
            else:
                new_stop = candle_trail

            if new_stop > pos.current_stop:
                return new_stop

        else:
            trough = pos.lowest_price if pos.lowest_price > 0 else current_price
            profit = pos.entry_price - trough

            candle_trail = float(last_1h['high']) * (1 + self.stop_buffer_pct)

            if atr > 0 and profit >= atr * activate_atr_mult:
                lock_stop = pos.entry_price - profit_lock_pct * profit
                atr_stop  = trough + atr_trail_mult * atr
                new_stop  = min(candle_trail, lock_stop, atr_stop)
            else:
                new_stop = candle_trail

            if new_stop < pos.current_stop:
                return new_stop

        return pos.current_stop

    # ----------------------------------------------------------
    # RE-ENTRY PROTOCOL
    # ----------------------------------------------------------

    def assess_reentry(self,
                        symbol: str,
                        stopped_out_position: OpenPosition,
                        zone_map: ZoneMap,
                        bars_5m: pd.DataFrame) -> ReentryAssessment:
        """
        After a stopout, evaluates whether re-entry is valid.

        Doug's rule: Go back to daily chart, check if still at
        important liquidity zone. If yes, look for re-entry.
        Classic liquidity sweep = best re-entry signal.

        Limits to 1 re-entry per setup.
        """
        today_key = f"{symbol}_{datetime.now().date()}"
        current_reentries = self.reentry_counts.get(today_key, 0)

        if current_reentries >= self.max_reentries_per_setup:
            return ReentryAssessment(
                allowed=False,
                reason=f'Max re-entries ({self.max_reentries_per_setup}) reached for this setup today',
                still_at_liquidity_zone=False,
                zone_tested_count=0,
                was_liquidity_sweep=False,
                daily_context='',
                max_reentries_reached=True
            )

        # Check if current price still near original zone
        original_zone_price = stopped_out_position.original_stop  # Zone was near here
        current_price = zone_map.current_price

        distance = abs(current_price - original_zone_price) / original_zone_price
        still_at_zone = distance < self.reentry_liquidity_tolerance

        if not still_at_zone:
            return ReentryAssessment(
                allowed=False,
                reason=f'Price moved away from original zone — '
                       f'{distance:.2%} from original stop level',
                still_at_liquidity_zone=False,
                zone_tested_count=0,
                was_liquidity_sweep=False,
                daily_context='Price no longer at liquidity zone',
                max_reentries_reached=False
            )

        # Find the zone that was being traded
        active_zone = None
        for zone in zone_map.magic_lines:
            if abs(zone.price - original_zone_price) / original_zone_price < 0.005:
                active_zone = zone
                break

        zone_tests = active_zone.test_count if active_zone else 0

        # Liquidity sweep detection:
        # Price broke slightly beyond zone, grabbed stops, now reversing back
        closes = bars_5m['close'].values[-5:] if len(bars_5m) >= 5 else bars_5m['close'].values
        direction = stopped_out_position.direction

        is_sweep = False
        if direction == 'long':
            # Swept below zone then came back above
            went_below = any(c < original_zone_price * (1 - self.reentry_liquidity_tolerance)
                             for c in closes[:-1])
            came_back = closes[-1] > original_zone_price * 0.999
            is_sweep = went_below and came_back
        else:
            # Swept above zone then came back below
            went_above = any(c > original_zone_price * (1 + self.reentry_liquidity_tolerance)
                             for c in closes[:-1])
            came_back = closes[-1] < original_zone_price * 1.001
            is_sweep = went_above and came_back

        # Build daily context summary
        if active_zone:
            context = (f"Zone at {active_zone.price:.5f} | "
                       f"Tier {active_zone.tier} | "
                       f"Tested {zone_tests} times | "
                       f"{'Liquidity sweep detected' if is_sweep else 'Normal test'}")
        else:
            context = "Still near original zone level"

        # Allow re-entry if still at zone AND (sweep detected OR zone is high quality)
        allow = still_at_zone and (
            is_sweep or
            (active_zone and active_zone.tier <= 2) or
            zone_tests >= 3
        )

        reason = ''
        if allow:
            if is_sweep:
                reason = 'Classic liquidity sweep — stopped out traders reversed: HIGH probability re-entry'
            else:
                reason = f'Still at Tier {active_zone.tier if active_zone else "?"} zone ' \
                         f'with {zone_tests} tests — re-entry valid'
        else:
            reason = f'At zone but low quality (Tier {active_zone.tier if active_zone else "?"}, ' \
                     f'{zone_tests} tests, no sweep) — skip re-entry'

        return ReentryAssessment(
            allowed=allow,
            reason=reason,
            still_at_liquidity_zone=still_at_zone,
            zone_tested_count=zone_tests,
            was_liquidity_sweep=is_sweep,
            daily_context=context,
            max_reentries_reached=False
        )

    def record_reentry(self, symbol: str):
        """Increments re-entry counter for today."""
        today_key = f"{symbol}_{datetime.now().date()}"
        self.reentry_counts[today_key] = self.reentry_counts.get(today_key, 0) + 1

    def reset_daily_reentry_counts(self):
        """Call at start of each new session."""
        self.reentry_counts.clear()

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------

    def _is_stop_hit(self, pos: OpenPosition, current_price: float) -> bool:
        if pos.direction == 'long':
            return current_price <= pos.current_stop
        else:
            return current_price >= pos.current_stop

    def _is_tp_reached(self,
                        pos: OpenPosition,
                        current_price: float,
                        tp_level: int) -> bool:
        tp = pos.take_profit_1 if tp_level == 1 else pos.take_profit_2
        if tp == 0.0:
            return False  # Zero = unset (e.g. reconciled positions) — never trigger
        if pos.direction == 'long':
            return current_price >= tp
        else:
            return current_price <= tp

    def _should_move_to_be_early(self,
                                   pos: OpenPosition,
                                   current_price: float) -> bool:
        """
        Move stop to break-even early if profit > 0.5R
        (before TP1 is reached).
        """
        risk = abs(pos.entry_price - pos.original_stop)
        if risk == 0:
            return False

        if pos.direction == 'long':
            profit = current_price - pos.entry_price
        else:
            profit = pos.entry_price - current_price

        return profit >= risk * self.be_trigger_rr

    def _calculate_partial_pnl(self,
                                 pos: OpenPosition,
                                 exit_price: float,
                                 quantity: float) -> float:
        """Calculates PnL for a partial exit, net of proportional overhead."""
        if pos.direction == 'long':
            gross = (exit_price - pos.entry_price) * quantity
        else:
            gross = (pos.entry_price - exit_price) * quantity

        overhead_proportion = pos.overhead_cost * (quantity / pos.original_quantity)
        return gross - overhead_proportion

    def _log_event(self, event: TradeEvent):
        self.trade_history.append(event)
        logging.info(
            f"[{event.event_type.upper()}] {event.trade_id} | "
            f"Price: {event.price:.5f} | Qty: {event.quantity:.6f} | "
            f"PnL: {event.pnl:+.4f} | {event.reason}"
        )

    # ----------------------------------------------------------
    # POSITION STATUS QUERIES
    # ----------------------------------------------------------

    def get_position(self, trade_id: str) -> Optional[OpenPosition]:
        return self.open_positions.get(trade_id)

    def get_all_open(self) -> List[OpenPosition]:
        return list(self.open_positions.values())

    def close_position(self,
                        trade_id: str,
                        exit_price: float,
                        reason: str,
                        order_type: str = 'MARKET',
                        pnl_override: Optional[float] = None):
        """Marks position as closed and logs final event."""
        if trade_id not in self.open_positions:
            return 0.0

        pos = self.open_positions[trade_id]
        pnl = (
            float(pnl_override)
            if pnl_override is not None
            else self._calculate_partial_pnl(pos, exit_price, pos.remaining_quantity)
        )

        if order_type == 'MARKET' and 'emergency' in reason.lower():
            maker_fee = pos.overhead_cost * 0.375  # Approx maker portion
            extra_cost = maker_fee * 0.2            # ~20% more for taker vs maker
            logging.warning(
                f"Emergency market exit on {trade_id}: "
                f"extra cost vs limit: ~{extra_cost:.4f}"
            )

        self._log_event(TradeEvent(
            trade_id=trade_id,
            event_type='exit',
            timestamp=datetime.now().isoformat(),
            price=exit_price,
            quantity=pos.remaining_quantity,
            reason=reason,
            pnl=pnl,
            overhead_applied=pos.overhead_cost * (pos.remaining_quantity / pos.original_quantity) / 2,
            cumulative_pnl=pnl,
            order_type=order_type
        ))

        pos.status = 'closed'
        del self.open_positions[trade_id]

        logging.info(
            f"Position closed: {trade_id} | {pos.symbol} @ {exit_price:.5f} | "
            f"Net PnL: {pnl:+.4f} | Reason: {reason}"
        )
        return pnl
