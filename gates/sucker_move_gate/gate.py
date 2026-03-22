# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Sucker Move Gate
Gate 4: Detects the explosive directional push into zone that traps retail traders.
"""
from typing import Optional
import pandas as pd
from gates.models import BaseGate, GateResult
from zones.models import MagicLine, DailyBox


class SuckerMoveGate(BaseGate):
    """
    Gate 4 — Sucker move detection.
    Requires fast directional move into zone AND termination at zone.
    """

    name = 'sucker_move'

    def __init__(self, config, health_monitor=None):
        super().__init__(config, health_monitor)
        if hasattr(config, 'zone_proximity_pct'):
            self.min_candles     = 3
            self.max_candles     = 5
            self.zone_tolerance  = 0.005
            self.atr_valid_pct   = config.atr_valid_pct
            self.atr_strong_pct  = config.atr_high_confidence_pct
        else:
            self.min_candles     = config.get('sucker_min_candles', 3)
            self.max_candles     = config.get('sucker_max_candles', 5)
            self.zone_tolerance  = config.get('sucker_zone_tolerance', 0.005)
            self.atr_valid_pct   = config.get('atr_valid_pct', 0.60)
            self.atr_strong_pct  = config.get('atr_high_confidence_pct', 0.80)

    def evaluate(self, symbol: str, **kwargs) -> GateResult:
        """
        kwargs:
            direction:    'long' | 'short'
            bars_5m:      pd.DataFrame
            target_zone:  Optional[MagicLine]
            daily_box:    DailyBox
        """
        direction: str                = kwargs['direction']
        bars: pd.DataFrame            = kwargs['bars_5m']
        target_zone: Optional[MagicLine] = kwargs.get('target_zone')
        daily_box: DailyBox           = kwargs['daily_box']

        if len(bars) < self.max_candles + 2:
            return GateResult(
                gate_name=self.name, passed=False,
                reason='Insufficient bars to evaluate sucker move', score=0.0,
            )

        closes = bars['close'].values
        opens  = bars['open'].values
        best_count, best_start = 0, -1
        lookback     = min(len(bars) - 1, 25)
        search_window = self.max_candles + 2

        for start in range(len(bars) - lookback, len(bars) - self.min_candles):
            end = min(start + search_window, len(bars) - 1)
            directional = sum(
                1 for i in range(start, end)
                if (direction == 'short' and closes[i] > opens[i])
                or (direction == 'long'  and closes[i] < opens[i])
            )
            if direction == 'short':
                net_ok = closes[end - 1] > opens[start]
            else:
                net_ok = closes[end - 1] < opens[start]

            if directional >= self.min_candles and net_ok and directional > best_count:
                best_count, best_start = directional, start

        if best_count < self.min_candles:
            candle_color = 'green' if direction == 'short' else 'red'
            return GateResult(
                gate_name=self.name, passed=False,
                reason=f'Need {self.min_candles}+ {candle_color} candles — found {best_count}',
                score=0.0,
            )

        search_end = min(best_start + search_window, len(bars))
        if direction == 'short':
            move_start = float(bars['low'].iloc[best_start])
            move_end   = float(bars['high'].iloc[best_start:search_end].max())
        else:
            move_start = float(bars['high'].iloc[best_start])
            move_end   = float(bars['low'].iloc[best_start:search_end].min())

        move_size = abs(move_end - move_start)
        atr_consumed = move_size / daily_box.ATR if daily_box.ATR > 0 else 0
        strength = ('strong' if atr_consumed >= self.atr_strong_pct
                    else 'valid' if atr_consumed >= self.atr_valid_pct else 'weak')

        # Did move terminate at zone?
        terminating = False
        if target_zone is not None:
            end_price = move_end
            if abs(end_price - target_zone.price) / max(target_zone.price, 1e-9) < self.zone_tolerance:
                terminating = True

        if not terminating:
            return GateResult(
                gate_name=self.name, passed=False,
                reason='Sucker move not terminating at zone',
                score=0.0,
                metadata={'candle_count': best_count, 'atr_consumed': atr_consumed, 'strength': strength},
            )

        score = 1.0 if strength == 'strong' else 0.7 if strength == 'valid' else 0.4
        return GateResult(
            gate_name=self.name, passed=True,
            reason=f'{best_count} directional candles ({strength}) terminating at zone',
            score=score,
            metadata={
                'candle_count': best_count, 'atr_consumed': atr_consumed,
                'strength': strength, 'move_size': move_size,
            },
        )
