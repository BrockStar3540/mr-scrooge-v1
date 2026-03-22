# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Zone Gate
Gate 1: Is current price at a valid magic line zone?
"""
from typing import Optional
from datetime import datetime
import logging

from gates.models import BaseGate, GateResult
from zones.models import ZoneMap, MagicLine


class ZoneGate(BaseGate):
    """
    Gate 1 — Zone proximity check.
    Passes if current price is within zone_proximity_pct of a known magic line.
    """

    name = 'zone'

    def __init__(self, config, health_monitor=None):
        super().__init__(config, health_monitor)
        # Accept both Config dataclass and legacy dict
        if hasattr(config, 'zone_proximity_pct'):
            self.zone_proximity_pct = config.zone_proximity_pct
        else:
            self.zone_proximity_pct = config.get('zone_proximity_pct', 0.0025)

    def evaluate(self, symbol: str, **kwargs) -> GateResult:
        """
        kwargs:
            zone_map: ZoneMap — built by ZoneDetector
        Returns GateResult with metadata:
            zone_tier, zone_price, zone_type, active_zone, direction
        """
        zone_map: ZoneMap = kwargs['zone_map']
        current_price = zone_map.current_price

        # JPY pairs: 2x proximity (was 5x — too wide; 2x ~= 0.2% ≈ 31 pips at USD/JPY 158)
        proximity = self.zone_proximity_pct * 2 if 'JPY' in symbol else self.zone_proximity_pct

        best_zone: Optional[MagicLine] = None
        best_tier = 99
        now_str = datetime.now().isoformat()

        # Check all magic lines
        for zone in zone_map.sell_zones + zone_map.buy_zones:
            dist_pct = abs(current_price - zone.price) / current_price
            if dist_pct <= proximity and zone.tier < best_tier:
                best_tier = zone.tier
                best_zone = zone

        # PDH/PDL — always Tier 1
        for level, label in [(zone_map.daily_box.PDH, 'PDH'), (zone_map.daily_box.PDL, 'PDL')]:
            if abs(current_price - level) / current_price <= proximity:
                if best_zone is None or best_tier > 1:
                    best_tier = 1
                    best_zone = MagicLine(
                        price=level, zone_type=label, tier=1,
                        test_count=0, strength_score=100,
                        last_tested=now_str, daily_aligned=True,
                        is_breakout_flip=False, source_timeframe='D',
                        symbol=zone_map.symbol, created_at=now_str,
                    )

        # Prior hour high/low — Tier 2
        for level, label in [(zone_map.prior_hour_high, 'PHH'), (zone_map.prior_hour_low, 'PHL')]:
            if abs(current_price - level) / current_price <= proximity:
                if best_zone is None or best_tier > 2:
                    best_tier = 2
                    best_zone = MagicLine(
                        price=level, zone_type=label, tier=2,
                        test_count=0, strength_score=80,
                        last_tested=now_str, daily_aligned=False,
                        is_breakout_flip=False, source_timeframe='1H',
                        symbol=zone_map.symbol, created_at=now_str,
                    )

        if best_zone is None:
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=f'Price {current_price:.5f} not near any magic line zone',
                score=0.0,
            )

        # Infer direction from zone type
        direction = 'short' if best_zone.zone_type in ('resistance', 'PDH', 'PHH', 'sell_zone') else 'long'
        if zone_map.current_zone in ('sell_zone', 'above_box'):
            direction = 'short'

        # Score: Tier 1=1.0, Tier 2=0.7, Tier 3=0.4
        score = max(0.0, 1.0 - (best_tier - 1) * 0.3)

        return GateResult(
            gate_name=self.name,
            passed=True,
            reason=f'Tier {best_tier} {best_zone.zone_type} @ {best_zone.price:.5f}',
            score=score,
            metadata={
                'zone_tier': best_tier,
                'zone_price': best_zone.price,
                'zone_type': best_zone.zone_type,
                'zone_strength': best_zone.strength_score,
                'direction': direction,
                'active_zone': best_zone,
            },
        )
