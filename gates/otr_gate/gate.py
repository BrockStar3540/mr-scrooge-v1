# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — OTR Gate
Gate 6: Overhead-to-range ratio check.
OTR = total_round_trip_spread / half_box
"""
from gates.models import BaseGate, GateResult


class OTRGate(BaseGate):
    """
    Gate 6 — OTR (overhead-to-range ratio).
    Blocks trades where spread costs consume too much of the potential profit.
    """

    name = 'otr'

    def __init__(self, config, health_monitor=None):
        super().__init__(config, health_monitor)
        if hasattr(config, 'otr_excellent'):
            self.otr_excellent  = config.otr_excellent
            self.otr_acceptable = config.otr_acceptable
            self.otr_marginal   = config.otr_marginal
        else:
            self.otr_excellent  = config.get('otr_excellent',  0.05)
            self.otr_acceptable = config.get('otr_acceptable', 0.15)
            self.otr_marginal   = config.get('otr_marginal',   0.25)

    def evaluate(self, symbol: str, **kwargs) -> GateResult:
        """
        kwargs:
            total_overhead: float — round-trip spread cost in price units
            half_box:       float — half the daily range (profit target distance)
            zone_tier:      int   — zone quality (1=best), used for marginal OTR check
            sucker_strength: str  — 'strong'|'valid'|'weak' from sucker_move_gate
            pitchfork_type:  str  — 'john_wick'|'power_of_towers'|'pitchfork'|None
        """
        total_overhead: float = kwargs['total_overhead']
        half_box: float       = kwargs['half_box']
        zone_tier: int        = kwargs.get('zone_tier', 3)
        sucker_strength: str  = kwargs.get('sucker_strength', 'weak')
        pitchfork_type: str   = kwargs.get('pitchfork_type', '')

        if half_box <= 0:
            return GateResult(
                gate_name=self.name, passed=False,
                reason='Half-box is zero — invalid box', score=0.0,
            )

        otr = total_overhead / half_box

        if otr < self.otr_excellent:
            category = 'excellent'
            score = 1.0
            passed = True
        elif otr < self.otr_acceptable:
            category = 'acceptable'
            score = 0.7
            passed = True
        elif otr < self.otr_marginal:
            category = 'marginal'
            # Marginal OTR: only allow if setup quality compensates
            high_quality = (
                zone_tier <= 2 and
                (sucker_strength == 'strong' or pitchfork_type in ('john_wick', 'power_of_towers'))
            )
            passed = high_quality
            score = 0.4 if passed else 0.0
        else:
            category = 'skip'
            score = 0.0
            passed = False

        reason = f'OTR {otr:.1%} ({category})'
        if not passed and category == 'marginal':
            reason += f' — marginal requires Tier<=2 + strong signal (tier={zone_tier} sucker={sucker_strength} fork={pitchfork_type})'

        return GateResult(
            gate_name=self.name, passed=passed,
            reason=reason, score=score,
            metadata={'otr_ratio': otr, 'otr_category': category},
        )
