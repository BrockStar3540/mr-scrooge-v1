# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Regime Supervisor — Data Models

RegimeState: what the supervisor would have said
RegimeSignal: one contributing piece of evidence
RegimeAssessment: the full picture at one point in time
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class RegimeState(Enum):
    """What the supervisor would have done, had it been in control."""
    NORMAL     = "NORMAL"      # Full trading — no concerns
    CAUTION    = "CAUTION"     # Would require top-tier score only
    RESTRICTED = "RESTRICTED"  # Would reduce size, tier-1 zones only
    BLOCKED    = "BLOCKED"     # Would halt new entries entirely


# Thresholds for score → state mapping (rebalanced 2026-04-13)
#   score >=  0  → NORMAL
#   score -1..-2 → CAUTION
#   score -3..-4 → RESTRICTED
#   score <= -5  → BLOCKED
SCORE_THRESHOLDS = {
    RegimeState.NORMAL:     0,
    RegimeState.CAUTION:   -2,
    RegimeState.RESTRICTED: -4,
    RegimeState.BLOCKED:    float('-inf'),
}


def classify_score(score: float) -> RegimeState:
    if score >= 0:
        return RegimeState.NORMAL
    if score >= -2:
        return RegimeState.CAUTION
    if score >= -4:
        return RegimeState.RESTRICTED
    return RegimeState.BLOCKED


@dataclass
class RegimeSignal:
    """One piece of evidence contributing to the regime score."""
    name:        str    # e.g. 'box_acceptance', 'qualified_failures'
    score_delta: float  # positive = bullish for reversal, negative = bad
    detail:      str    # human-readable description of what was observed


@dataclass
class RegimeAssessment:
    """Full regime picture for one symbol at one moment."""
    symbol:   str
    state:    RegimeState
    score:    float
    signals:  List[RegimeSignal]
    assessed_at: str   # ISO timestamp
    direct_score: float = 0.0  # score excluding breadth — used by breadth to avoid feedback loop

    def would_have_intervened(self) -> bool:
        return self.state != RegimeState.NORMAL

    def summary(self) -> str:
        parts = [f"{s.name}({s.score_delta:+.0f}): {s.detail}" for s in self.signals]
        return f"{self.state.value} score={self.score:+.1f} | " + " | ".join(parts) if parts else f"{self.state.value} score={self.score:+.1f} | no signals"
