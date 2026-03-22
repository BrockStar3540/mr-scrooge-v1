# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Signal Engine
Thin orchestrator: instantiates 6 gate objects and runs them in order.
Each gate is a standalone class in gates/<name>_gate/gate.py.
"""
import logging
from datetime import datetime
from typing import Optional, Tuple
import pandas as pd

from config import Config
from health.monitor import HealthMonitor
from zones.models import ZoneMap, DailyBox, MagicLine
from signals.models import SuckerMoveResult, PitchforkResult, SignalResult
from gates.zone_gate import ZoneGate
from gates.trend_gate import TrendGate
from gates.atr_gate import ATRGate
from gates.sucker_move_gate import SuckerMoveGate
from gates.pitchfork_gate import PitchforkGate
from gates.otr_gate import OTRGate

log = logging.getLogger('mr-scrooge-v2.signals')


class SignalEngine:
    """
    Runs all 6 gates in order. First failure stops the pipeline.
    Returns SignalResult with full gate audit trail.
    """

    def __init__(self, config, health_monitor: Optional[HealthMonitor] = None):
        self.config = config
        self._hm = health_monitor or HealthMonitor.get()

        # Instantiate gate objects
        self.zone_gate       = ZoneGate(config, self._hm)
        self.trend_gate      = TrendGate(config, self._hm)
        self.atr_gate        = ATRGate(config, self._hm)
        self.sucker_gate     = SuckerMoveGate(config, self._hm)
        self.pitchfork_gate  = PitchforkGate(config, self._hm)
        self.otr_gate        = OTRGate(config, self._hm)

        # Config values needed for target calculation
        if hasattr(config, 'min_risk_reward'):
            self.min_rr         = config.min_risk_reward
        else:
            self.min_rr         = config.get('min_risk_reward', 1.60)

    def evaluate(self,
                  symbol: str,
                  zone_map: ZoneMap,
                  bars_5m: pd.DataFrame,
                  bars_1h: pd.DataFrame,
                  total_overhead: float,
                  minutes_since_open: int) -> SignalResult:
        """
        Run all 6 gates in sequence. Returns full SignalResult.
        """
        rejection_reasons = []
        daily_box = zone_map.daily_box

        # ── Gate 1: Zone ─────────────────────────────────────
        z = self.zone_gate.safe_evaluate(symbol, zone_map=zone_map)
        if not z.passed:
            rejection_reasons.append(z.reason)
            return self._no_trade(symbol, z_gate=False, z_tier=0, z_details=z.reason,
                                   rejection_reasons=rejection_reasons,
                                   wait_for='Wait for price to reach a magic line zone')

        zone_tier   = z.metadata.get('zone_tier', 3)
        direction   = z.metadata.get('direction', 'long')
        active_zone = z.metadata.get('active_zone')

        # ── Gate 2: Trend ─────────────────────────────────────
        t = self.trend_gate.safe_evaluate(symbol, direction=direction,
                                           bars_1h=bars_1h, zone_tier=zone_tier)
        if not t.passed:
            rejection_reasons.append(t.reason)
            return self._no_trade(symbol, z_gate=True, z_tier=zone_tier, z_details=z.reason,
                                   rejection_reasons=rejection_reasons,
                                   wait_for=t.reason)

        # ── Gate 3: ATR (advisory) ────────────────────────────
        a = self.atr_gate.safe_evaluate(symbol, direction=direction,
                                         bars_5m=bars_5m, daily_box=daily_box)
        atr_pct      = a.metadata.get('atr_pct', 0.0)
        atr_strength = a.metadata.get('strength', 'weak')
        # ATR always passes — advisory only

        # ── Gate 4: Sucker move ───────────────────────────────
        sm = self.sucker_gate.safe_evaluate(symbol, direction=direction,
                                             bars_5m=bars_5m, target_zone=active_zone,
                                             daily_box=daily_box)
        sucker_result = SuckerMoveResult(
            detected=sm.passed,
            direction=direction,
            candle_count=sm.metadata.get('candle_count', 0),
            atr_consumed_pct=sm.metadata.get('atr_consumed', 0.0),
            atr_threshold_met=sm.metadata.get('atr_consumed', 0.0) >= 0.60,
            atr_strength=sm.metadata.get('strength', 'weak'),
            move_size=sm.metadata.get('move_size', 0.0),
            terminating_at_zone=sm.passed,
            zone_hit=active_zone,
            rejection_reason='' if sm.passed else sm.reason,
        )
        if not sm.passed:
            rejection_reasons.append(sm.reason)
            return self._wait_result(
                symbol, True, zone_tier, z.reason, True, atr_pct, atr_strength,
                sucker_result, rejection_reasons,
                'Sucker move not yet formed — watch for explosive directional push into zone',
            )

        # ── Gate 5: Pitchfork ─────────────────────────────────
        box_stop = daily_box.PDH if direction == 'short' else daily_box.PDL
        if active_zone and active_zone.zone_type in ('PHH', 'PHL'):
            box_stop = zone_map.prior_hour_high if direction == 'short' else zone_map.prior_hour_low

        pk = self.pitchfork_gate.safe_evaluate(
            symbol, direction=direction, bars_5m=bars_5m, bars_1h=bars_1h,
            daily_box=daily_box,
            zone_price=active_zone.price if active_zone else None,
            box_stop=box_stop,
        )
        pitchfork_result = None
        if pk.passed:
            pitchfork_result = PitchforkResult(
                detected=True,
                signal_type=pk.metadata.get('signal_type', 'pitchfork'),
                direction=direction,
                directional_candles=pk.metadata.get('directional_candles', 0),
                trigger_price=pk.metadata.get('trigger_price', 0.0),
                stop_price=pk.metadata.get('stop_price', 0.0),
                reversal_candle_idx=pk.metadata.get('reversal_candle_idx', -1),
                confidence=pk.metadata.get('confidence', 'MODERATE'),
                timeframe_used=pk.metadata.get('timeframe_used', '5M'),
                details=pk.metadata.get('details', ''),
            )
        else:
            rejection_reasons.append(pk.reason)
            return self._wait_result(
                symbol, True, zone_tier, z.reason, True, atr_pct, atr_strength,
                sucker_result, rejection_reasons,
                'Sucker move present — waiting for pitchfork reversal candle',
            )

        # ── Gate 6: OTR ───────────────────────────────────────
        o = self.otr_gate.safe_evaluate(
            symbol,
            total_overhead=total_overhead,
            half_box=daily_box.half_box,
            zone_tier=zone_tier,
            sucker_strength=sucker_result.atr_strength,
            pitchfork_type=pitchfork_result.signal_type if pitchfork_result else '',
        )
        otr_ratio    = o.metadata.get('otr_ratio', 0.0)
        otr_category = o.metadata.get('otr_category', 'skip')
        if not o.passed:
            rejection_reasons.append(o.reason)
            return self._no_trade(symbol, True, zone_tier, z.reason,
                                   rejection_reasons, o.reason)

        # ── All gates passed — build entry signal ─────────────
        tp1, tp2 = self._calculate_targets(direction, pitchfork_result.trigger_price, zone_map)

        risk_points   = abs(pitchfork_result.trigger_price - pitchfork_result.stop_price)
        reward_points = abs(tp2 - pitchfork_result.trigger_price)
        rr = reward_points / risk_points if risk_points > 0 else 0.0

        if rr < self.min_rr:
            msg = f'RR {rr:.2f} < min {self.min_rr:.1f}'
            rejection_reasons.append(msg)
            return self._no_trade(symbol, True, zone_tier, z.reason,
                                   rejection_reasons, 'Wait for better reward')

        action = 'ENTER_LONG' if direction == 'long' else 'ENTER_SHORT'
        result = SignalResult(
            symbol=symbol,
            timestamp=datetime.now().isoformat(),
            action=action,
            zone_gate=True, zone_tier=zone_tier, zone_details=z.reason,
            sucker_move_gate=True, sucker_move=sucker_result,
            atr_gate=True, atr_consumed_pct=atr_pct, atr_strength=atr_strength,
            pitchfork_gate=True, pitchfork=pitchfork_result,
            otr_gate=True, otr_ratio=otr_ratio, otr_category=otr_category,
            entry_price=pitchfork_result.trigger_price,
            stop_price=pitchfork_result.stop_price,
            tp1_price=tp1, tp2_price=tp2,
            risk_points=risk_points, reward_points=reward_points, risk_reward_ratio=rr,
            rejection_reasons=[], all_gates_passed=True, wait_for='',
        )
        result.score = self._score(result)
        return result

    # ── Helpers ──────────────────────────────────────────────

    def _calculate_targets(self, direction: str, entry_price: float,
                            zone_map: ZoneMap) -> Tuple[float, float]:
        daily_box = zone_map.daily_box
        if direction == 'long':
            tp1 = daily_box.center
            tp2 = daily_box.PDH
            min_tp1 = entry_price + (tp2 - entry_price) * 0.50
            if tp1 < min_tp1:
                tp1 = min_tp1
        else:
            tp1 = daily_box.center
            tp2 = daily_box.PDL
            max_tp1 = entry_price - (entry_price - tp2) * 0.50
            if tp1 > max_tp1:
                tp1 = max_tp1
        return tp1, tp2

    def _score(self, sig: SignalResult) -> int:
        pts = 0
        pts += {1: 30, 2: 20, 3: 10}.get(sig.zone_tier, 5)
        pts += {'strong': 20, 'valid': 15, 'weak': 5}.get(sig.atr_strength, 5)
        pts += {'excellent': 15, 'acceptable': 10, 'marginal': 5}.get(sig.otr_category, 0)
        if sig.risk_reward_ratio >= 3.0:
            pts += 15
        elif sig.risk_reward_ratio >= 2.0:
            pts += 10
        elif sig.risk_reward_ratio >= 1.5:
            pts += 5
        if sig.pitchfork:
            pts += 10 if sig.pitchfork.signal_type in ('john_wick', 'power_of_towers') else 5
            pts += {'HIGH': 5, 'MODERATE': 3}.get(sig.pitchfork.confidence, 0)
        return pts

    def _no_trade(self, symbol, z_gate, z_tier, z_details, rejection_reasons, wait_for):
        return SignalResult(
            symbol=symbol, timestamp=datetime.now().isoformat(), action='NO_TRADE',
            zone_gate=z_gate, zone_tier=z_tier, zone_details=z_details,
            sucker_move_gate=False, sucker_move=None,
            atr_gate=False, atr_consumed_pct=0, atr_strength='weak',
            pitchfork_gate=False, pitchfork=None,
            otr_gate=False, otr_ratio=0, otr_category='skip',
            entry_price=0, stop_price=0, tp1_price=0, tp2_price=0,
            risk_points=0, reward_points=0, risk_reward_ratio=0,
            rejection_reasons=rejection_reasons, all_gates_passed=False, wait_for=wait_for,
        )

    def _wait_result(self, symbol, z_gate, z_tier, z_details,
                     atr_gate, atr_pct, atr_strength, sucker, rejection_reasons, wait_for):
        return SignalResult(
            symbol=symbol, timestamp=datetime.now().isoformat(), action='WAIT',
            zone_gate=z_gate, zone_tier=z_tier, zone_details=z_details,
            sucker_move_gate=sucker.detected, sucker_move=sucker,
            atr_gate=atr_gate, atr_consumed_pct=atr_pct, atr_strength=atr_strength,
            pitchfork_gate=False, pitchfork=None,
            otr_gate=False, otr_ratio=0, otr_category='unknown',
            entry_price=0, stop_price=0, tp1_price=0, tp2_price=0,
            risk_points=0, reward_points=0, risk_reward_ratio=0,
            rejection_reasons=rejection_reasons, all_gates_passed=False, wait_for=wait_for,
        )
