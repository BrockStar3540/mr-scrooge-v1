# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Pitchfork Gate
Gate 5: Named reversal candle detection.
Priority: John Wick → Power of Towers → Standard Pitchfork.
"""
from typing import Optional
import pandas as pd
from gates.models import BaseGate, GateResult
from zones.models import DailyBox


class PitchforkGate(BaseGate):
    """
    Gate 5 — Reversal candle confirmation.
    Detects John Wick, Power of Towers, or Standard Pitchfork.
    Returns trigger/stop prices in metadata for position sizing.
    """

    name = 'pitchfork'

    def __init__(self, config, health_monitor=None):
        super().__init__(config, health_monitor)
        if hasattr(config, 'sl_floor_pct'):
            self.sl_floor_pct       = config.sl_floor_pct
            self.directional_min    = 3
            self.directional_max    = 5
            self.volatility_mult    = 2.0
        else:
            self.sl_floor_pct       = config.get('sl_floor_pct', 0.0050)
            self.directional_min    = config.get('pitchfork_directional_min', 3)
            self.directional_max    = config.get('pitchfork_directional_max', 5)
            self.volatility_mult    = config.get('high_volatility_atr_mult', 2.0)

    def evaluate(self, symbol: str, **kwargs) -> GateResult:
        """
        kwargs:
            direction:   'long' | 'short'
            bars_5m:     pd.DataFrame — 5M bars
            bars_1h:     pd.DataFrame — 1H bars (for high-vol check)
            daily_box:   DailyBox
            zone_price:  Optional[float] — the magic line price
            box_stop:    Optional[float] — PDH (short) or PDL (long)
        """
        direction: str         = kwargs['direction']
        bars_5m: pd.DataFrame  = kwargs['bars_5m']
        bars_1h: pd.DataFrame  = kwargs['bars_1h']
        daily_box: DailyBox    = kwargs['daily_box']
        zone_price: Optional[float] = kwargs.get('zone_price')
        box_stop: Optional[float]   = kwargs.get('box_stop')

        # Select timeframe
        is_high_vol = self._is_high_volatility(bars_1h, daily_box)
        bars   = bars_1h if is_high_vol else bars_5m
        tf_lbl = '1H' if is_high_vol else '5M'

        if len(bars) < 4:
            return GateResult(
                gate_name=self.name, passed=False,
                reason='Insufficient bars', score=0.0,
            )

        # Priority: John Wick → Power of Towers → Standard Pitchfork
        for detect_fn, signal_type, score in [
            (self._detect_john_wick, 'john_wick', 1.0),
            (self._detect_power_of_towers, 'power_of_towers', 1.0),
            (self._detect_standard_pitchfork, 'pitchfork', 0.7),
        ]:
            result = detect_fn(bars, direction, tf_lbl, zone_price, box_stop)
            if result['detected']:
                confidence_boost = 0.1 if result.get('confidence') == 'HIGH' else 0.0
                return GateResult(
                    gate_name=self.name, passed=True,
                    reason=f'{signal_type} ({tf_lbl}): {result["details"]}',
                    score=min(1.0, score + confidence_boost),
                    metadata=result,
                )

        return GateResult(
            gate_name=self.name, passed=False,
            reason=f'No reversal candle (direction={direction}, tf={tf_lbl})',
            score=0.0,
        )

    # ── Signal detectors ─────────────────────────────────────

    def _detect_john_wick(self, bars, direction, timeframe, zone_price, box_stop):
        for i in range(len(bars) - 4, len(bars) - 1):
            if i < 0:
                continue
            o = float(bars['open'].iloc[i])
            h = float(bars['high'].iloc[i])
            l = float(bars['low'].iloc[i])
            c = float(bars['close'].iloc[i])
            body = abs(c - o)
            total_range = h - l
            if total_range == 0:
                continue
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l

            if direction == 'long':
                wick_ratio = lower_wick / total_range
                if wick_ratio >= 0.60 and (body / total_range) <= 0.30 and lower_wick >= body * 2:
                    confirmed = any(
                        float(bars['close'].iloc[j]) > float(bars['open'].iloc[j])
                        for j in range(i + 1, len(bars))
                    )
                    if confirmed:
                        trigger = h * 1.0001
                        stop = self._apply_sl_floor(trigger, l * (1 - self.sl_floor_pct), direction)
                        return {'detected': True, 'signal_type': 'john_wick',
                                'direction': direction, 'trigger_price': trigger,
                                'stop_price': stop, 'confidence': 'HIGH', 'timeframe_used': timeframe,
                                'details': f'Hammer wick {wick_ratio:.0%} + confirmation'}
            else:
                wick_ratio = upper_wick / total_range
                if wick_ratio >= 0.60 and (body / total_range) <= 0.30 and upper_wick >= body * 2:
                    confirmed = any(
                        float(bars['close'].iloc[j]) < float(bars['open'].iloc[j])
                        for j in range(i + 1, len(bars))
                    )
                    if confirmed:
                        trigger = l * 0.9999
                        stop = self._apply_sl_floor(trigger, h * (1 + self.sl_floor_pct), direction)
                        return {'detected': True, 'signal_type': 'john_wick',
                                'direction': direction, 'trigger_price': trigger,
                                'stop_price': stop, 'confidence': 'HIGH', 'timeframe_used': timeframe,
                                'details': f'Inv hammer wick {wick_ratio:.0%} + confirmation'}
        return {'detected': False}

    def _detect_power_of_towers(self, bars, direction, timeframe, zone_price, box_stop):
        for i in range(1, len(bars) - 1):
            ph = float(bars['high'].iloc[i-1])
            pl = float(bars['low'].iloc[i-1])
            po = float(bars['open'].iloc[i-1])
            pc = float(bars['close'].iloc[i-1])
            ch = float(bars['high'].iloc[i])
            cl = float(bars['low'].iloc[i])
            co = float(bars['open'].iloc[i])
            cc = float(bars['close'].iloc[i])
            if direction == 'long':
                if pc < po and cc > co and cl < pl and ch > ph:
                    trigger = ch * 1.0001
                    stop = self._apply_sl_floor(trigger, cl * (1 - self.sl_floor_pct), direction)
                    return {'detected': True, 'signal_type': 'power_of_towers',
                            'direction': direction, 'trigger_price': trigger,
                            'stop_price': stop, 'confidence': 'HIGH', 'timeframe_used': timeframe,
                            'details': 'Green engulfs full range of prior red'}
            else:
                if pc > po and cc < co and ch > ph and cl < pl:
                    trigger = cl * 0.9999
                    stop = self._apply_sl_floor(trigger, ch * (1 + self.sl_floor_pct), direction)
                    return {'detected': True, 'signal_type': 'power_of_towers',
                            'direction': direction, 'trigger_price': trigger,
                            'stop_price': stop, 'confidence': 'HIGH', 'timeframe_used': timeframe,
                            'details': 'Red engulfs full range of prior green'}
        return {'detected': False}

    def _detect_standard_pitchfork(self, bars, direction, timeframe, zone_price, box_stop):
        closes = bars['close'].values
        opens  = bars['open'].values
        highs  = bars['high'].values
        lows   = bars['low'].values
        for end_idx in range(len(bars) - 2, self.directional_min, -1):
            if direction == 'long':
                is_reversal = closes[end_idx] > opens[end_idx]
            else:
                is_reversal = closes[end_idx] < opens[end_idx]
            if not is_reversal:
                continue
            count = 0
            for k in range(end_idx - 1, max(end_idx - self.directional_max - 1, -1), -1):
                if direction == 'long':
                    if closes[k] < opens[k]:
                        count += 1
                    else:
                        break
                else:
                    if closes[k] > opens[k]:
                        count += 1
                    else:
                        break
            if count >= self.directional_min:
                if direction == 'long':
                    trigger = highs[end_idx] * 1.0001
                    stop = self._apply_sl_floor(trigger, lows[end_idx] * (1 - self.sl_floor_pct), direction)
                else:
                    trigger = lows[end_idx] * 0.9999
                    stop = self._apply_sl_floor(trigger, highs[end_idx] * (1 + self.sl_floor_pct), direction)
                conf = 'HIGH' if count >= 4 else 'MODERATE'
                return {'detected': True, 'signal_type': 'pitchfork',
                        'direction': direction, 'trigger_price': trigger,
                        'stop_price': stop, 'confidence': conf, 'timeframe_used': timeframe,
                        'directional_candles': count,
                        'details': f'{count} directional candles → reversal'}
        return {'detected': False, 'details': f'Need {self.directional_min}-{self.directional_max} directional candles + reversal'}

    def _is_high_volatility(self, bars_1h, daily_box):
        if len(bars_1h) < 2:
            return False
        last_range = float(bars_1h['high'].iloc[-2]) - float(bars_1h['low'].iloc[-2])
        hourly_atr_equiv = daily_box.ATR / 6.5
        return last_range > hourly_atr_equiv * self.volatility_mult

    def _apply_sl_floor(self, trigger, stop, direction):
        if direction == 'short':
            return max(stop, trigger * (1 + self.sl_floor_pct))
        return min(stop, trigger * (1 - self.sl_floor_pct))
