# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Position Manager
Post-entry management: break-even, hybrid trailing stop, re-entry assessment.
Health monitoring: registers as 'position_manager'.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
import pandas as pd

from .models import OpenPosition, TradeEvent, ReentryAssessment
from zones.models import ZoneMap
from signals.models import SignalResult

log = logging.getLogger('mr-scrooge-v2.positions')


def _monitor():
    from health.monitor import HealthMonitor
    return HealthMonitor.get()


class PositionManager:
    """
    Manages all active positions from entry through exit.
    Stages:
      1. Move SL to break-even when price is 50% of way to TP1
      2. Lock SL at TP1 when price passes TP1
      3. Hybrid trailing stop after break-even (hourly candle + ATR + profit lock)
    OANDA native TP at TP2 closes full remaining position.
    """

    _COMPONENT = 'position_manager'

    def __init__(self, config):
        if hasattr(config, 'max_hold_minutes'):
            self.partial_1_pct        = 0.50
            self.partial_2_pct        = 0.30
            self.be_trigger_rr        = 1.0
            self.max_reentries        = 3
            self.reentry_tolerance    = 0.003
            self.stop_buffer_pct      = 0.0005
            self.max_hold_minutes     = config.max_hold_minutes
            self.hourly_green_enabled = config.hourly_green_exit_enabled
            self.hourly_green_minutes = config.hourly_green_exit_minutes
            self.green_exit_min_pct   = config.green_exit_min_pct
        else:
            self.partial_1_pct        = config.get('partial_1_pct', 0.50)
            self.partial_2_pct        = config.get('partial_2_pct', 0.30)
            self.be_trigger_rr        = config.get('be_trigger_rr', 1.0)
            self.max_reentries        = config.get('max_reentries_per_setup', 3)
            self.reentry_tolerance    = config.get('reentry_liquidity_tolerance', 0.003)
            self.stop_buffer_pct      = config.get('stop_buffer_pct', 0.0005)
            self.max_hold_minutes     = config.get('max_hold_minutes', 360)
            self.hourly_green_enabled = config.get('hourly_green_exit_enabled', True)
            self.hourly_green_minutes = config.get('hourly_green_exit_minutes', 60)
            self.green_exit_min_pct   = config.get('green_exit_min_pct', 0.00125)

        self.open_positions: Dict[str, OpenPosition] = {}
        self.reentry_counts: Dict[str, int] = {}
        self.trade_history: List[TradeEvent] = []
        _monitor().register(self._COMPONENT, failure_threshold=5, recovery_seconds=60)

    # ── Startup reconciliation ────────────────────────────────

    def reconcile_from_oanda(self, trades: list) -> int:
        count = 0
        for trade in trades:
            client_id = (
                trade.get('clientExtensions', {}).get('id', '')
                or trade.get('tradeClientExtensions', {}).get('id', '')
            )
            if not (client_id.startswith('scrooge-') or client_id.startswith('scrooge_')):
                continue
            if client_id in self.open_positions:
                continue
            instrument   = trade.get('instrument', '')
            init_units   = abs(float(trade.get('initialUnits', 0)))
            curr_units   = abs(float(trade.get('currentUnits', 0)))
            entry_price  = float(trade.get('price', 0))
            direction    = 'short' if float(trade.get('currentUnits', 0)) < 0 else 'long'
            stop_price   = float(trade.get('stopLossOrder', {}).get('price', 0))
            tp_price     = float(trade.get('takeProfitOrder', {}).get('price', 0))
            entry_time   = trade.get('openTime', datetime.now().isoformat())
            oanda_id     = str(trade.get('id', ''))
            pos = OpenPosition(
                trade_id=client_id, symbol=instrument, exchange='OANDA',
                direction=direction, entry_price=entry_price,
                original_stop=stop_price, current_stop=stop_price,
                take_profit_1=0.0, take_profit_2=tp_price,
                original_quantity=init_units, remaining_quantity=curr_units,
                entry_time=entry_time, entry_signal_type='reconciled',
                zone_tier=2, atr_at_entry=0.0, overhead_cost=0.0,
                break_even_achieved=False, partial_1_taken=(init_units != curr_units),
                partial_2_taken=False, last_hourly_close=entry_price,
                hourly_green_checks_done=0, status='open',
                stop_moved_at=entry_time, oanda_trade_id=oanda_id,
                highest_price=entry_price, lowest_price=entry_price,
            )
            self.open_positions[client_id] = pos
            count += 1
            log.info(f'Reconciled: {client_id} {instrument} {direction} {curr_units:.0f} @ {entry_price:.5f}')
        return count

    # ── Entry ────────────────────────────────────────────────

    def register_entry(self, trade_id: str, symbol: str, exchange: str,
                        signal: SignalResult, quantity: float, actual_fill_price: float,
                        overhead_cost: float, atr: float, oanda_trade_id: str = '') -> OpenPosition:
        pos = OpenPosition(
            trade_id=trade_id, symbol=symbol, exchange=exchange,
            direction='long' if signal.action == 'ENTER_LONG' else 'short',
            entry_price=actual_fill_price,
            original_stop=signal.stop_price, current_stop=signal.stop_price,
            take_profit_1=signal.tp1_price, take_profit_2=signal.tp2_price,
            original_quantity=quantity, remaining_quantity=quantity,
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_signal_type=signal.pitchfork.signal_type if signal.pitchfork else 'unknown',
            zone_tier=signal.zone_tier, atr_at_entry=atr, overhead_cost=overhead_cost,
            break_even_achieved=False, partial_1_taken=False, partial_2_taken=False,
            last_hourly_close=actual_fill_price, hourly_green_checks_done=0,
            status='open', stop_moved_at=datetime.now().isoformat(),
            oanda_trade_id=oanda_trade_id, highest_price=actual_fill_price, lowest_price=actual_fill_price,
        )
        self.open_positions[trade_id] = pos
        self._log_event(TradeEvent(
            trade_id=trade_id, event_type='entry', timestamp=datetime.now().isoformat(),
            price=actual_fill_price, quantity=quantity,
            reason=f'{pos.entry_signal_type} Tier {pos.zone_tier}',
            pnl=0, overhead_applied=overhead_cost / 2,
            cumulative_pnl=-(overhead_cost / 2), order_type='MARKET',
        ))
        log.info(f'Position opened: {trade_id} {symbol} {pos.direction.upper()} @ {actual_fill_price:.5f}')
        return pos

    # ── Manage (called each 5M candle) ───────────────────────

    def manage_position(self, trade_id: str, current_price: float,
                         bars_5m: pd.DataFrame, bars_1h: pd.DataFrame,
                         minutes_in_trade: int, zone_map=None,
                         account_equity: float = 0.0) -> Dict:
        if trade_id not in self.open_positions:
            return {'action': 'hold', 'reason': 'Position not found'}

        pos = self.open_positions[trade_id]
        actions = []

        # Stage 1: Break-even at 50% of way to TP1
        if not pos.break_even_achieved and pos.take_profit_1 > 0:
            if pos.direction == 'long':
                half_tp1 = pos.entry_price + 0.5 * (pos.take_profit_1 - pos.entry_price)
                be_cond = current_price >= half_tp1
            else:
                half_tp1 = pos.entry_price - 0.5 * (pos.entry_price - pos.take_profit_1)
                be_cond = current_price <= half_tp1
            if be_cond:
                new_sl = pos.entry_price
                if self._is_valid_stop_move(pos, new_sl):
                    pos.current_stop = new_sl
                    pos.break_even_achieved = True
                    pos.stop_moved_at = datetime.now().isoformat()
                    actions.append({'action': 'move_stop', 'price': new_sl,
                                    'reason': f'BE: price {current_price:.5f} at 50% of TP1'})

        # Stage 2: Lock SL at TP1 when price passes TP1
        if pos.take_profit_1 > 0 and not pos.partial_1_taken:
            tp1_hit = (current_price >= pos.take_profit_1 if pos.direction == 'long'
                       else current_price <= pos.take_profit_1)
            if tp1_hit:
                pos.partial_1_taken = True
                pos.break_even_achieved = True
                new_sl = pos.take_profit_1
                if self._is_valid_stop_move(pos, new_sl):
                    pos.current_stop = new_sl
                    pos.stop_moved_at = datetime.now().isoformat()
                    actions.append({'action': 'move_stop', 'price': new_sl,
                                    'reason': f'TP1 passed — SL locked @ {new_sl:.5f}'})

        # Stage 3: Hybrid trailing stop
        if pos.direction == 'long':
            pos.highest_price = max(pos.highest_price, current_price)
        else:
            pos.lowest_price = min(pos.lowest_price or current_price, current_price)

        if pos.break_even_achieved and len(bars_1h) >= 2:
            new_stop = self._hybrid_trail(pos, bars_1h, current_price)
            if self._is_valid_stop_move(pos, new_stop):
                pos.current_stop = new_stop
                pos.stop_moved_at = datetime.now().isoformat()
                pos.last_hourly_close = float(bars_1h['close'].iloc[-2])
                actions.append({'action': 'move_stop', 'price': new_stop,
                                'reason': f'Hybrid trail: {new_stop:.5f}'})

        if not actions:
            return {'action': 'hold', 'reason': 'No action needed'}

        move_stops = [a for a in actions if a['action'] == 'move_stop']
        if move_stops:
            return (max(move_stops, key=lambda a: a['price']) if pos.direction == 'long'
                    else min(move_stops, key=lambda a: a['price']))
        return actions[0]

    # ── Hybrid trailing stop ──────────────────────────────────

    def _hybrid_trail(self, pos: OpenPosition, bars_1h: pd.DataFrame,
                       current_price: float) -> float:
        """max(hourly_candle_trail, 80%_profit_lock, 1.5xATR_trail)"""
        last_1h = bars_1h.iloc[-2]
        atr = pos.atr_at_entry
        if pos.direction == 'long':
            peak   = pos.highest_price or current_price
            profit = peak - pos.entry_price
            candle = float(last_1h['low']) * (1 - self.stop_buffer_pct)
            if atr > 0 and profit >= atr:
                lock = pos.entry_price + 0.80 * profit
                atr_s = peak - 1.5 * atr
                return max(candle, lock, atr_s)
            return candle
        else:
            trough = pos.lowest_price or current_price
            profit = pos.entry_price - trough
            candle = float(last_1h['high']) * (1 + self.stop_buffer_pct)
            if atr > 0 and profit >= atr:
                lock = pos.entry_price - 0.80 * profit
                atr_s = trough + 1.5 * atr
                return min(candle, lock, atr_s)
            return candle

    # ── Re-entry ─────────────────────────────────────────────

    def assess_reentry(self, symbol: str, stopped_pos: OpenPosition,
                        zone_map: ZoneMap, bars_5m: pd.DataFrame) -> ReentryAssessment:
        today_key = f'{symbol}_{datetime.now().date()}'
        if self.reentry_counts.get(today_key, 0) >= self.max_reentries:
            return ReentryAssessment(
                allowed=False, reason='Max re-entries reached',
                still_at_liquidity_zone=False, zone_tested_count=0,
                was_liquidity_sweep=False, daily_context='', max_reentries_reached=True,
            )
        original_zone_price = stopped_pos.original_stop
        current_price = zone_map.current_price
        dist = abs(current_price - original_zone_price) / max(original_zone_price, 1e-9)
        still_at_zone = dist < self.reentry_tolerance
        if not still_at_zone:
            return ReentryAssessment(
                allowed=False, reason=f'Price moved {dist:.2%} from zone',
                still_at_liquidity_zone=False, zone_tested_count=0,
                was_liquidity_sweep=False, daily_context='', max_reentries_reached=False,
            )
        active_zone = next(
            (z for z in zone_map.magic_lines
             if abs(z.price - original_zone_price) / max(original_zone_price, 1e-9) < 0.005),
            None,
        )
        zone_tests = active_zone.test_count if active_zone else 0
        closes = bars_5m['close'].values[-5:] if len(bars_5m) >= 5 else bars_5m['close'].values
        tol = self.reentry_tolerance
        if stopped_pos.direction == 'long':
            is_sweep = (any(c < original_zone_price * (1 - tol) for c in closes[:-1])
                        and closes[-1] > original_zone_price * 0.999)
        else:
            is_sweep = (any(c > original_zone_price * (1 + tol) for c in closes[:-1])
                        and closes[-1] < original_zone_price * 1.001)
        allow = still_at_zone and (is_sweep or (active_zone and active_zone.tier <= 2) or zone_tests >= 3)
        reason = ('Classic liquidity sweep — high probability re-entry' if is_sweep
                  else f'Tier {active_zone.tier if active_zone else "?"} zone, {zone_tests} tests')
        return ReentryAssessment(
            allowed=allow, reason=reason, still_at_liquidity_zone=still_at_zone,
            zone_tested_count=zone_tests, was_liquidity_sweep=is_sweep,
            daily_context=f'Zone {original_zone_price:.5f} | {"sweep" if is_sweep else "test"}',
            max_reentries_reached=False,
        )

    def record_reentry(self, symbol: str):
        today_key = f'{symbol}_{datetime.now().date()}'
        self.reentry_counts[today_key] = self.reentry_counts.get(today_key, 0) + 1

    # ── Queries ───────────────────────────────────────────────

    def get_position(self, trade_id: str) -> Optional[OpenPosition]:
        return self.open_positions.get(trade_id)

    def get_all_open(self) -> List[OpenPosition]:
        return list(self.open_positions.values())

    def close_position(self, trade_id: str, exit_price: float, reason: str,
                        order_type: str = 'MARKET', pnl_override: Optional[float] = None) -> float:
        if trade_id not in self.open_positions:
            return 0.0
        pos = self.open_positions[trade_id]
        pnl = float(pnl_override) if pnl_override is not None else self._calc_pnl(pos, exit_price, pos.remaining_quantity)
        self._log_event(TradeEvent(
            trade_id=trade_id, event_type='exit', timestamp=datetime.now().isoformat(),
            price=exit_price, quantity=pos.remaining_quantity, reason=reason,
            pnl=pnl, overhead_applied=0, cumulative_pnl=pnl, order_type=order_type,
        ))
        pos.status = 'closed'
        del self.open_positions[trade_id]
        log.info(f'Position closed: {trade_id} {pos.symbol} @ {exit_price:.5f} | PnL: {pnl:+.4f} | {reason}')
        return pnl

    # ── Private ───────────────────────────────────────────────

    def _is_valid_stop_move(self, pos: OpenPosition, new_stop: float) -> bool:
        if pos.direction == 'long':
            return new_stop > pos.current_stop
        return new_stop < pos.current_stop

    def _calc_pnl(self, pos: OpenPosition, exit_price: float, qty: float) -> float:
        if pos.direction == 'long':
            gross = (exit_price - pos.entry_price) * qty
        else:
            gross = (pos.entry_price - exit_price) * qty
        overhead = pos.overhead_cost * (qty / max(pos.original_quantity, 1))
        return gross - overhead

    def _log_event(self, event: TradeEvent):
        self.trade_history.append(event)
        log.info(f'[{event.event_type.upper()}] {event.trade_id} @ {event.price:.5f} | PnL: {event.pnl:+.4f} | {event.reason}')
