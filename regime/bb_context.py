# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
BB Context — Dual Bollinger Band state classifier.

Computes two bands from 5m bar data:
  Band A: 20-period SMA ± 2σ on close  (identifies "normal" price range)
  Band B:  4-period SMA ± 4σ on open   (identifies extreme outliers / breakouts)

Output state per cycle:
  NORMAL         — close between Band A lower and upper
  STRETCH_UP     — close above Band A upper (but not breakout/extreme/rejection)
  STRETCH_DOWN   — close below Band A lower
  EXTREME_UP     — close above Band B upper (well beyond any band)
  EXTREME_DOWN   — close below Band B lower
  BREAKOUT_UP    — 2+ consecutive closes above Band A upper, still advancing → trend signal
  BREAKOUT_DOWN  — 2+ consecutive closes below Band A lower, still falling
  REJECTION_SHORT — was above Band A upper in last 3 candles, now closed back inside → snap-back
  REJECTION_LONG  — was below Band A lower in last 3 candles, now closed back inside

Regime signal deltas:
  BREAKOUT_UP / BREAKOUT_DOWN  → -2  (directional momentum, bad for mean-reversion)
  EXTREME_UP / EXTREME_DOWN    →  0  (price at outlier — may reverse, may continue)
  REJECTION_SHORT              → +1  (BB snap-back from top confirms PDH short)
  REJECTION_LONG               → +1  (BB snap-back from bottom confirms PDL long)
  STRETCH / NORMAL             →  0
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd


class BBState(Enum):
    NORMAL          = "NORMAL"
    STRETCH_UP      = "STRETCH_UP"
    STRETCH_DOWN    = "STRETCH_DOWN"
    EXTREME_UP      = "EXTREME_UP"
    EXTREME_DOWN    = "EXTREME_DOWN"
    BREAKOUT_UP     = "BREAKOUT_UP"
    BREAKOUT_DOWN   = "BREAKOUT_DOWN"
    REJECTION_SHORT = "REJECTION_SHORT"
    REJECTION_LONG  = "REJECTION_LONG"


# Score delta each state contributes to regime score when added as a signal
BB_STATE_DELTA = {
    BBState.NORMAL:          0,
    BBState.STRETCH_UP:      0,
    BBState.STRETCH_DOWN:    0,
    BBState.EXTREME_UP:      0,
    BBState.EXTREME_DOWN:    0,
    BBState.BREAKOUT_UP:    -2,
    BBState.BREAKOUT_DOWN:  -2,
    BBState.REJECTION_SHORT: +1,
    BBState.REJECTION_LONG:  +1,
}


@dataclass
class BBContext:
    state:       BBState
    score_delta: int
    detail:      str          # human-readable — what did we observe?
    upper_A:     float        # Band A upper
    lower_A:     float        # Band A lower
    upper_B:     float        # Band B upper (extreme)
    lower_B:     float        # Band B lower (extreme)
    current_close: float


_MIN_BARS_A = 20   # minimum bars to compute Band A
_MIN_BARS_B = 4    # minimum bars to compute Band B


def compute_bb_context(bars_5m: pd.DataFrame) -> Optional[BBContext]:
    """
    Compute BB state from 5m bar data.
    Returns None if insufficient data.
    Bars must have columns: close, open (standard OANDA bar columns).
    """
    if bars_5m is None or len(bars_5m) < _MIN_BARS_A:
        return None

    closes = bars_5m["close"].astype(float)
    opens  = bars_5m["open"].astype(float)

    # Band A — 20-period on close
    sma20   = float(closes.iloc[-20:].mean())
    std20   = float(closes.iloc[-20:].std(ddof=1))
    upper_A = sma20 + 2 * std20
    lower_A = sma20 - 2 * std20

    # Band B — 4-period on open
    n_b = min(_MIN_BARS_B, len(opens))
    sma4   = float(opens.iloc[-n_b:].mean())
    std4   = float(opens.iloc[-n_b:].std(ddof=1)) if n_b >= 2 else 0.0
    upper_B = sma4 + 4 * std4
    lower_B = sma4 - 4 * std4

    c  = float(closes.iloc[-1])    # current close
    c1 = float(closes.iloc[-2]) if len(closes) >= 2 else c
    c2 = float(closes.iloc[-3]) if len(closes) >= 3 else c1

    def _make(state: BBState, detail: str) -> BBContext:
        return BBContext(
            state=state, score_delta=BB_STATE_DELTA[state], detail=detail,
            upper_A=upper_A, lower_A=lower_A,
            upper_B=upper_B, lower_B=lower_B,
            current_close=c,
        )

    # ── Priority 1: Extreme (beyond Band B) ─────────────────
    if c > upper_B and upper_B > upper_A:
        return _make(BBState.EXTREME_UP,
            f"Close {c:.5f} above Band B upper {upper_B:.5f} — extreme extension")
    if c < lower_B and lower_B < lower_A:
        return _make(BBState.EXTREME_DOWN,
            f"Close {c:.5f} below Band B lower {lower_B:.5f} — extreme extension")

    # ── Priority 2: Rejection (was outside Band A, now back inside) ──
    # Look back 3 candles (c2, c1, and now c is inside)
    if c <= upper_A and (c1 > upper_A or c2 > upper_A):
        return _make(BBState.REJECTION_SHORT,
            f"Close {c:.5f} re-entered Band A upper {upper_A:.5f} — snap-back from top")
    if c >= lower_A and (c1 < lower_A or c2 < lower_A):
        return _make(BBState.REJECTION_LONG,
            f"Close {c:.5f} re-entered Band A lower {lower_A:.5f} — snap-back from bottom")

    # ── Priority 3: Breakout (2+ consecutive closes outside Band A, advancing) ──
    if c > upper_A and c1 > upper_A and c >= c1:
        return _make(BBState.BREAKOUT_UP,
            f"2+ consecutive closes above Band A upper {upper_A:.5f} — trending breakout up")
    if c < lower_A and c1 < lower_A and c <= c1:
        return _make(BBState.BREAKOUT_DOWN,
            f"2+ consecutive closes below Band A lower {lower_A:.5f} — trending breakout down")

    # ── Priority 4: Stretch (outside Band A, not yet a breakout/rejection) ──
    if c > upper_A:
        return _make(BBState.STRETCH_UP,
            f"Close {c:.5f} above Band A upper {upper_A:.5f} — stretched, watching")
    if c < lower_A:
        return _make(BBState.STRETCH_DOWN,
            f"Close {c:.5f} below Band A lower {lower_A:.5f} — stretched, watching")

    # ── Normal ───────────────────────────────────────────────
    return _make(BBState.NORMAL,
        f"Close {c:.5f} within Band A [{lower_A:.5f} – {upper_A:.5f}]")
