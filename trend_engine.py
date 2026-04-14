# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Trend Participation Engine — BB20 Pullback Entries (V1)

Fires when BB state = TREND or STRETCH. Enters WITH the trend on
pullbacks to the BB20 midline (SMA20). Intentionally simple.

  EXTREME → reversal engine (existing)
  TREND   → this engine (full size)
  STRETCH → this engine (reduced size, 2-candle confirmation)
  RANGE   → idle
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from gates import SignalResult

log = logging.getLogger("mr-scrooge.trend")

_TREND_STATES  = ('TREND_UP', 'TREND_DOWN')
_STRETCH_STATES = ('STRETCH_UP', 'STRETCH_DOWN')
_ALL_TREND = _TREND_STATES + _STRETCH_STATES


class TrendEngine:
    """Enters with-trend on pullback to BB20 midline."""

    def __init__(self, config: dict):
        self.enabled       = config.get('trend_enabled', True)
        self.sma_proximity = config.get('trend_sma_proximity_pct', 0.0015)
        self.atr_stop_mult = config.get('trend_atr_stop_multiple', 1.0)
        self.fixed_score   = config.get('trend_fixed_score', 30)

    def evaluate(
        self,
        symbol:   str,
        bars_5m:  pd.DataFrame,
        zone_map,
        bb_ctx,
        live_spread: float = 0.0,
        half_box: float = 0.0,
    ) -> Optional[SignalResult]:
        if not self.enabled or bb_ctx is None:
            return None

        bb_state = bb_ctx.state.value
        if bb_state not in _ALL_TREND:
            return None

        if bars_5m is None or len(bars_5m) < 22:
            return None

        closes = bars_5m["close"].astype(float)
        sma20 = float(closes.iloc[-20:].mean())
        current_close = float(closes.iloc[-1])
        current_open  = float(bars_5m["open"].astype(float).iloc[-1])

        direction = 'long' if bb_state in ('TREND_UP', 'STRETCH_UP') else 'short'

        # Proximity check
        atr_5m = float((bars_5m["high"] - bars_5m["low"]).iloc[-14:].mean())
        proximity_pct = abs(current_close - sma20) / sma20 if sma20 > 0 else 1.0
        proximity_atr = abs(current_close - sma20) / atr_5m if atr_5m > 0 else 1.0

        max_pct = self.sma_proximity
        max_atr = 0.3

        if proximity_pct > max_pct and proximity_atr > max_atr:
            return None

        # Bounce confirmation
        if direction == 'long':
            if current_close <= current_open:
                return None
            if current_close > sma20 * (1 + max_pct * 2):
                return None
        else:
            if current_close >= current_open:
                return None
            if current_close < sma20 * (1 - max_pct * 2):
                return None

        # STRETCH: 2-candle confirmation
        if bb_state in _STRETCH_STATES:
            prev_close = float(closes.iloc[-2])
            prev_open  = float(bars_5m["open"].astype(float).iloc[-2])
            if direction == 'long':
                if prev_close > sma20 * (1 + max_pct * 3):
                    return None
                if prev_close > prev_open and abs(prev_close - sma20) / sma20 > max_pct * 2:
                    return None
            else:
                if prev_close < sma20 * (1 - max_pct * 3):
                    return None
                if prev_close < prev_open and abs(prev_close - sma20) / sma20 > max_pct * 2:
                    return None

        # Spread check
        if half_box > 0 and live_spread > 0:
            if live_spread / half_box > 0.30:
                return None

        # SL and TP
        sl_distance = atr_5m * self.atr_stop_mult

        if direction == 'long':
            stop_price = current_close - sl_distance
            tp1_price  = bb_ctx.upper_A
            tp2_price  = zone_map.daily_box.PDH if zone_map and zone_map.daily_box else current_close + sl_distance * 2
        else:
            stop_price = current_close + sl_distance
            tp1_price  = bb_ctx.lower_A
            tp2_price  = zone_map.daily_box.PDL if zone_map and zone_map.daily_box else current_close - sl_distance * 2

        risk_points   = abs(current_close - stop_price)
        reward_points = abs(tp2_price - current_close)
        rr = reward_points / risk_points if risk_points > 0 else 0.0

        entry_type = 'TREND_FULL' if bb_state in _TREND_STATES else 'TREND_STRETCH'

        log.info(
            f'{symbol}: {entry_type} {direction.upper()} | '
            f'bb={bb_state} sma20={sma20:.5f} price={current_close:.5f} '
            f'dist={proximity_pct:.4%} | SL={stop_price:.5f} TP1={tp1_price:.5f} '
            f'TP2={tp2_price:.5f} RR={rr:.2f}'
        )

        # Build SignalResult (V1 uses plain class, not dataclass)
        sig = SignalResult.__new__(SignalResult)
        sig.symbol = symbol
        sig.timestamp = datetime.now(timezone.utc).isoformat()
        sig.action = 'ENTER_LONG' if direction == 'long' else 'ENTER_SHORT'
        sig.zone_gate = True
        sig.zone_tier = 0
        sig.zone_details = f'Trend pullback to SMA20 ({entry_type})'
        sig.sucker_move_gate = False
        sig.sucker_move = None
        sig.atr_gate = True
        sig.atr_consumed_pct = 0.0
        sig.atr_strength = 'trend'
        sig.pitchfork_gate = False
        sig.pitchfork = None
        sig.otr_gate = True
        sig.otr_ratio = 0.0
        sig.otr_category = 'trend'
        sig.entry_price = current_close
        sig.stop_price = stop_price
        sig.tp1_price = tp1_price
        sig.tp2_price = tp2_price
        sig.risk_points = risk_points
        sig.reward_points = reward_points
        sig.risk_reward_ratio = rr
        sig.rejection_reasons = []
        sig.all_gates_passed = True
        sig.wait_for = ''
        sig.score = self.fixed_score
        sig.rr_tp1 = abs(tp1_price - current_close) / risk_points if risk_points > 0 else 0.0
        sig.is_counter_trend = False
        return sig
