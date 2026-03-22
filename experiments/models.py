# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — Experiment Models
Data structures for per-gate, per-pair A/B testing.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict


@dataclass
class TradeOutcome:
    """A single observed trade result for one variant."""
    variant:    str
    symbol:     str
    gate_name:  str
    experiment: str
    direction:  str               # 'long' | 'short'
    entry_price: float
    exit_price:  float
    pnl:         float
    win:         bool             # True if pnl > 0
    tp1_hit:     bool = False
    tp2_hit:     bool = False
    hold_minutes: int = 0
    timestamp:   datetime = field(default_factory=datetime.utcnow)
    notes:       str = ''


@dataclass
class VariantStats:
    """Running statistics for one variant of one experiment."""
    variant: str
    wins:      int = 0
    losses:    int = 0
    total_pnl: float = 0.0
    tp1_hits:  int = 0
    tp2_hits:  int = 0

    @property
    def n(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n > 0 else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.n if self.n > 0 else 0.0

    def record(self, outcome: TradeOutcome):
        if outcome.win:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl += outcome.pnl
        if outcome.tp1_hit:
            self.tp1_hits += 1
        if outcome.tp2_hit:
            self.tp2_hits += 1

    def to_dict(self) -> dict:
        return {
            'variant': self.variant,
            'n': self.n,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': round(self.win_rate, 4),
            'total_pnl': round(self.total_pnl, 4),
            'avg_pnl': round(self.avg_pnl, 4),
            'tp1_hits': self.tp1_hits,
            'tp2_hits': self.tp2_hits,
        }


@dataclass
class ExperimentResults:
    """Results for a complete experiment (all variants)."""
    experiment: str
    gate_name:  str
    variants:   Dict[str, VariantStats] = field(default_factory=dict)
    p_value:    Optional[float] = None     # Fisher exact test
    significant: bool = False              # p < 0.05
    winner:     Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'experiment': self.experiment,
            'gate_name': self.gate_name,
            'variants': {k: v.to_dict() for k, v in self.variants.items()},
            'p_value': self.p_value,
            'significant': self.significant,
            'winner': self.winner,
        }


@dataclass
class Experiment:
    """
    Definition of one A/B test.
    name:        unique ID, e.g. 'zone_gate_tier_threshold'
    gate_name:   which gate this applies to
    variants:    list of variant names, e.g. ['control', 'treatment']
    description: human-readable purpose
    active:      False = log but do not influence gate behavior
    """
    name:        str
    gate_name:   str
    variants:    List[str]
    description: str = ''
    active:      bool = True
    start_date:  datetime = field(default_factory=datetime.utcnow)
    end_date:    Optional[datetime] = None
    min_samples: int = 30    # minimum n per variant before testing for significance
