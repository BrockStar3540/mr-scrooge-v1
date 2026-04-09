# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
EXTREME_REVERSAL — Additional entry type for Mr. Scrooge V1.

Pattern:
  1. Price spikes outside Band A (BB 20-period / 2σ on close)
  2. Previous candle shows a qualifying wick rejection
  3. Current candle confirms re-entry direction (close breaks prior candle's high/low)
  4. Price near PDH (short) or PDL (long) within zone_proximity_pct
  5. Price INSIDE the box (close between PDL and PDH)
  6. Regime not BLOCKED (score > -3)

Fires ONLY when main signal engine returned NO_TRADE/WAIT.
Does NOT touch existing gates or exit logic.

SL: rejection candle's extreme wick + 2 pips buffer
TP: same box targets as standard entries (center → opposite wall)
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from regime.bb_context import compute_bb_context, BBState
from gates import SignalResult

log = logging.getLogger('mr-scrooge.extreme_reversal')

WICK_RATIO    = 1.5
ER_FIXED_SCORE = 35


class ExtremeReversalEngine:

    def __init__(self, config: dict):
        self.zone_proximity_pct = config.get('zone_proximity_pct', 0.0025)
        self.min_risk_reward    = config.get('min_risk_reward', 1.60)

    def evaluate(
        self,
        symbol:         str,
        zone_map,
        bars_5m:        pd.DataFrame,
        current_price:  float,
        regime_score:   float,
    ) -> Optional[SignalResult]:
        """
        Returns a SignalResult ready for entry, or None.
        Called after the main signal engine returns NO_TRADE / WAIT.
        """
        if bars_5m is None or len(bars_5m) < 4:
            return None
        if regime_score <= -3:
            return None

        ctx = compute_bb_context(bars_5m)
        if ctx is None:
            return None
        if ctx.state not in (BBState.REJECTION_LONG, BBState.REJECTION_SHORT):
            return None

        direction = 'long' if ctx.state == BBState.REJECTION_LONG else 'short'
        pip = 0.01 if 'JPY' in symbol else 0.0001

        curr = bars_5m.iloc[-1]
        prev = bars_5m.iloc[-2]

        c_open  = float(curr['open']);  c_high  = float(curr['high'])
        c_low   = float(curr['low']);   c_close = float(curr['close'])
        p_open  = float(prev['open']);  p_high  = float(prev['high'])
        p_low   = float(prev['low']);   p_close = float(prev['close'])

        pdh = zone_map.daily_box.PDH
        pdl = zone_map.daily_box.PDL

        if c_close > pdh or c_close < pdl:
            return None

        box_range = max(pdh - pdl, pip)
        proximity = box_range * self.zone_proximity_pct

        body = abs(p_close - p_open)
        body = max(body, pip * 0.5)

        if direction == 'long':
            if c_close > pdl + proximity:
                return None
            lower_wick = min(p_open, p_close) - p_low
            upper_wick = p_high - max(p_open, p_close)
            if lower_wick < body * WICK_RATIO or lower_wick <= upper_wick:
                return None
            if c_close <= p_high:
                return None
            entry = c_close
            sl    = p_low - 2 * pip
            tp1   = zone_map.daily_box.center
            tp2   = pdh
            wick_ratio = lower_wick / body
        else:
            if c_close < pdh - proximity:
                return None
            upper_wick = p_high - max(p_open, p_close)
            lower_wick = min(p_open, p_close) - p_low
            if upper_wick < body * WICK_RATIO or upper_wick <= lower_wick:
                return None
            if c_close >= p_low:
                return None
            entry = c_close
            sl    = p_high + 2 * pip
            tp1   = zone_map.daily_box.center
            tp2   = pdl
            wick_ratio = upper_wick / body

        if direction == 'long'  and (tp2 <= entry or tp1 <= entry):
            return None
        if direction == 'short' and (tp2 >= entry or tp1 >= entry):
            return None

        _buf = pip * 2
        if direction == 'long'  and tp2 <= current_price + _buf:
            return None
        if direction == 'short' and tp2 >= current_price - _buf:
            return None

        risk   = abs(entry - sl)
        reward = abs(tp2 - entry)
        if risk <= 0:
            return None
        rr = reward / risk
        if rr < self.min_risk_reward:
            log.info(
                f'{symbol}: EXTREME_REVERSAL {direction.upper()} — '
                f'RR {rr:.2f} < min {self.min_risk_reward:.1f} — skipped'
            )
            return None

        log.info(
            f'{symbol}: EXTREME_REVERSAL {direction.upper()} | '
            f'bb={ctx.state.value} wick={wick_ratio:.1f}x regime={regime_score:+.0f} | '
            f'entry={entry:.5f} sl={sl:.5f} tp2={tp2:.5f} rr={rr:.2f}'
        )

        action = 'ENTER_LONG' if direction == 'long' else 'ENTER_SHORT'
        sig = SignalResult(
            symbol=symbol,
            timestamp=datetime.now().isoformat(),
            action=action,
            zone_gate=True,
            zone_tier=2,
            zone_details=(
                f'EXTREME_REVERSAL | bb={ctx.state.value} '
                f'wick={wick_ratio:.1f}x regime={regime_score:+.0f}'
            ),
            sucker_move_gate=True,
            sucker_move=None,
            atr_gate=True,
            atr_consumed_pct=0.0,
            atr_strength='valid',
            pitchfork_gate=True,
            pitchfork=None,
            otr_gate=True,
            otr_ratio=0.0,
            otr_category='acceptable',
            entry_price=entry,
            stop_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            risk_points=risk,
            reward_points=reward,
            risk_reward_ratio=rr,
            rejection_reasons=[],
            all_gates_passed=True,
            wait_for='',
        )
        sig.score = ER_FIXED_SCORE
        return sig
