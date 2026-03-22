# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Trend Gate
Gate 2: Higher-timeframe trend regime filter.
Blocks counter-trend trades at low-quality zones (Tier 3+).
"""
import pandas as pd
from gates.models import BaseGate, GateResult


class TrendGate(BaseGate):
    """
    Gate 2 — Trend regime filter.
    Uses 50H EMA slope to classify uptrend/downtrend/ranging.
    """

    name = 'trend'

    def evaluate(self, symbol: str, **kwargs) -> GateResult:
        """
        kwargs:
            direction:  'long' | 'short'
            bars_1h:    pd.DataFrame — at least 62 rows
            zone_tier:  int — 1/2/3/4
        """
        direction: str = kwargs['direction']
        bars_1h: pd.DataFrame = kwargs['bars_1h']
        zone_tier: int = kwargs.get('zone_tier', 3)

        regime = self._get_regime(bars_1h)

        counter_trend = (
            (direction == 'short' and regime == 'uptrend') or
            (direction == 'long'  and regime == 'downtrend')
        )

        if counter_trend and zone_tier >= 3:
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=f'Counter-trend: {direction} blocked at Tier {zone_tier} in {regime}',
                score=0.0,
                metadata={'regime': regime, 'direction': direction, 'zone_tier': zone_tier},
            )

        score = 1.0
        note = f'{regime} → {direction} allowed'
        if counter_trend:
            score = 0.6
            note = f'Counter-trend {direction} allowed — Tier {zone_tier} overrides {regime}'

        return GateResult(
            gate_name=self.name,
            passed=True,
            reason=note,
            score=score,
            metadata={'regime': regime, 'direction': direction, 'zone_tier': zone_tier},
        )

    def _get_regime(self, bars_1h: pd.DataFrame) -> str:
        """50H EMA slope over last 10 bars. Returns 'uptrend', 'downtrend', or 'ranging'."""
        if len(bars_1h) < 62:
            return 'ranging'
        closes = bars_1h['close'].values
        ema50 = pd.Series(closes).ewm(span=50, adjust=False).mean().values
        base = ema50[-11]
        if base == 0:
            return 'ranging'
        slope = (ema50[-1] - base) / base
        if slope > 0.0005:
            return 'uptrend'
        elif slope < -0.0005:
            return 'downtrend'
        return 'ranging'
