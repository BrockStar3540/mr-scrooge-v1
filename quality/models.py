# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Trade Quality Feedback Loop — Data Models

MFE  = Maximum Favorable Excursion  (how far it went FOR us)
MAE  = Maximum Adverse Excursion    (how far it went AGAINST us before recovering)
F/T  = Follow-Through Score 0-100   (composite quality rating)

These three numbers tell you whether your entry was early/late/good,
and whether the market respected your direction after you entered.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LiveExcursion:
    """
    Real-time state for one open trade. Updated each 5-min cycle.
    Converted to TradeQualityRecord when the trade closes.
    """
    trade_id:    str
    symbol:      str
    direction:   str    # 'long' or 'short'
    entry_price: float
    entry_time:  str    # ISO
    stop_loss:   float
    tp1_price:   float
    tp2_price:   float
    pip_size:    float  # 0.01 JPY pairs, 0.0001 otherwise
    signal_score: float = 0.0

    # Running excursion (updated each cycle)
    mfe_pips:        float = 0.0   # Maximum pips in our favor so far
    mae_pips:        float = 0.0   # Maximum pips against us (positive = bad)
    current_pips:    float = 0.0   # Current unrealized pips
    cycles_elapsed:  int   = 0     # 5-min candles since entry

    # Milestone flags (set once, never cleared)
    cycles_to_5pips:  Optional[int] = None   # How many cycles to first +5 pips
    cycles_to_10pips: Optional[int] = None
    cycles_to_tp1:    Optional[int] = None
    hit_tp1:          bool = False
    hit_tp2:          bool = False

    def pip_distance(self, price_a: float, price_b: float) -> float:
        """Signed pip distance from a to b (positive = favorable for our direction)."""
        raw = (price_b - price_a) / self.pip_size
        return raw if self.direction == 'long' else -raw


@dataclass
class TradeQualityRecord:
    """
    Finalized quality record written to JSONL when a trade closes.
    This is what you query in 3 months.
    """
    trade_id:      str
    symbol:        str
    direction:     str
    entry_time:    str
    close_time:    str
    entry_price:   float
    close_price:   float
    signal_score:  float

    # Core excursion
    mfe_pips:      float   # Best the trade ever looked
    mae_pips:      float   # Worst the trade ever looked (before recovering or stopping)
    final_pips:    float   # Actual result

    # Time metrics
    cycles_elapsed:    int
    cycles_to_5pips:   Optional[int]   # None = never reached +5
    cycles_to_10pips:  Optional[int]
    cycles_to_tp1:     Optional[int]

    # Derived quality metrics
    mfe_mae_ratio:     float   # MFE/MAE — higher = cleaner, well-timed entry
    follow_through:    float   # 0-100 composite (see tracker._score_follow_through)

    # Outcome
    hit_sl:   bool
    hit_tp1:  bool
    hit_tp2:  bool
    close_reason: str

    def to_dict(self) -> dict:
        return {
            'trade_id':        self.trade_id,
            'symbol':          self.symbol,
            'direction':       self.direction,
            'entry_time':      self.entry_time,
            'close_time':      self.close_time,
            'entry_price':     self.entry_price,
            'close_price':     self.close_price,
            'signal_score':    self.signal_score,
            'mfe_pips':        round(self.mfe_pips, 2),
            'mae_pips':        round(self.mae_pips, 2),
            'final_pips':      round(self.final_pips, 2),
            'cycles_elapsed':  self.cycles_elapsed,
            'cycles_to_5pips':  self.cycles_to_5pips,
            'cycles_to_10pips': self.cycles_to_10pips,
            'cycles_to_tp1':    self.cycles_to_tp1,
            'mfe_mae_ratio':   round(self.mfe_mae_ratio, 2),
            'follow_through':  round(self.follow_through, 1),
            'hit_sl':    self.hit_sl,
            'hit_tp1':   self.hit_tp1,
            'hit_tp2':   self.hit_tp2,
            'close_reason': self.close_reason,
        }


@dataclass
class QualityPressure:
    """
    Summary of recent trade quality for one symbol.
    Returned by get_quality_pressure() — logged but never acted on (yet).
    """
    symbol:        str
    n_trades:      int
    pressure:      str    # 'strong', 'normal', 'weak', 'unknown'
    avg_mfe_pips:  float
    avg_mae_pips:  float
    avg_mfe_mae:   float  # ratio — good trades are > 2.0
    avg_ft_score:  float  # average follow-through 0-100
    avg_cycles_to_5pips: Optional[float]
    detail:        str

    def would_tighten(self) -> bool:
        """Would this pressure have caused the bot to tighten filters?"""
        return self.pressure == 'weak'

    def would_loosen(self) -> bool:
        """Would this pressure have caused the bot to loosen filters?"""
        return self.pressure == 'strong'
