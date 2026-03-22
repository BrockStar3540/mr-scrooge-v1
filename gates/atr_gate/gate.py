# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — ATR Gate
Gate 3 (advisory): Measures directional ATR consumption.
Always passes — informs signal quality scoring only.
"""
import pandas as pd
from gates.models import BaseGate, GateResult
from zones.models import DailyBox


class ATRGate(BaseGate):
    """
    Gate 3 — ATR consumption. Advisory only (always passes).
    Measures how much of the daily ATR the directional leg consumed.
    """

    name = 'atr'

    def __init__(self, config, health_monitor=None):
        super().__init__(config, health_monitor)
        if hasattr(config, 'atr_valid_pct'):
            self.atr_min_pct    = config.atr_minimum_pct
            self.atr_valid_pct  = config.atr_valid_pct
            self.atr_strong_pct = config.atr_high_confidence_pct
        else:
            self.atr_min_pct    = config.get('atr_minimum_pct', 0.15)
            self.atr_valid_pct  = config.get('atr_valid_pct', 0.60)
            self.atr_strong_pct = config.get('atr_high_confidence_pct', 0.80)

    def evaluate(self, symbol: str, **kwargs) -> GateResult:
        """
        kwargs:
            direction:  'long' | 'short'
            bars_5m:    pd.DataFrame
            daily_box:  DailyBox
        """
        direction: str      = kwargs['direction']
        bars_5m: pd.DataFrame = kwargs['bars_5m']
        daily_box: DailyBox = kwargs['daily_box']

        if len(bars_5m) < 2 or daily_box.ATR == 0:
            return GateResult(
                gate_name=self.name, passed=True,
                reason='Insufficient data — advisory pass', score=0.3,
                metadata={'atr_pct': 0.0, 'strength': 'weak'},
            )

        current_price = float(bars_5m['close'].iloc[-1])
        if direction == 'short':
            directional_move = float(bars_5m['high'].max()) - current_price
        else:
            directional_move = current_price - float(bars_5m['low'].min())

        consumed_pct = max(directional_move, 0.0) / daily_box.ATR

        if consumed_pct >= self.atr_strong_pct:
            strength = 'strong'
            score = 1.0
        elif consumed_pct >= self.atr_valid_pct:
            strength = 'valid'
            score = 0.7
        else:
            strength = 'weak'
            score = 0.3

        return GateResult(
            gate_name=self.name,
            passed=True,  # Advisory — always passes
            reason=f'ATR {consumed_pct:.1%} consumed ({strength})',
            score=score,
            metadata={'atr_pct': consumed_pct, 'strength': strength},
        )
