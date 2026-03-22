# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""mr-scrooge-v2 — Signal Models"""
from dataclasses import dataclass, field
from typing import Optional, List
from zones.models import MagicLine


@dataclass
class SuckerMoveResult:
    detected: bool
    direction: str
    candle_count: int
    atr_consumed_pct: float
    atr_threshold_met: bool
    atr_strength: str           # 'strong', 'valid', 'weak'
    move_size: float
    terminating_at_zone: bool
    zone_hit: Optional[MagicLine]
    rejection_reason: str


@dataclass
class PitchforkResult:
    detected: bool
    signal_type: str            # 'pitchfork', 'john_wick', 'power_of_towers', 'none'
    direction: str
    directional_candles: int
    trigger_price: float
    stop_price: float
    reversal_candle_idx: int
    confidence: str             # 'HIGH', 'MODERATE', 'LOW'
    timeframe_used: str         # '5M', '1H'
    details: str


@dataclass
class SignalResult:
    """Complete evaluation for one instrument at one moment."""
    symbol: str
    timestamp: str
    action: str                 # 'ENTER_LONG', 'ENTER_SHORT', 'WAIT', 'NO_TRADE'

    # Gate results
    zone_gate: bool
    zone_tier: int
    zone_details: str

    sucker_move_gate: bool
    sucker_move: Optional[SuckerMoveResult]

    atr_gate: bool
    atr_consumed_pct: float
    atr_strength: str

    pitchfork_gate: bool
    pitchfork: Optional[PitchforkResult]

    otr_gate: bool
    otr_ratio: float
    otr_category: str

    # Entry details
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    risk_points: float
    reward_points: float
    risk_reward_ratio: float

    rejection_reasons: List[str]
    all_gates_passed: bool
    wait_for: str
    score: int = 0
