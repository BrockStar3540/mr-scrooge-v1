# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================

import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime
import logging
log = logging.getLogger('mr-scrooge-v2.universe')


BASE_PAIRS = {
    'AUD_CAD',   # +.80
    'AUD_CHF',   # +.17
    'USD_JPY',   # +.87
    'EUR_CAD',   # +.83
    'CHF_JPY',   # +/bin/zsh.18 (small sample, but positive)
}

# Probation pairs are still tradable, but should be handled more conservatively.
# They are excluded from thin-session scanning in "balanced" mode and can run
# with reduced size in main.py.
PROBATION_PAIRS = {
    'AUD_USD', 'EUR_GBP', 'EUR_JPY',
    'GBP_AUD', 'GBP_CAD', 'GBP_JPY', 'NZD_USD',
}


def get_session_pairs(utc_hour: int, mode: str = 'balanced') -> list:
    """
    Session-aware pair list with conservative regime controls.

    Modes:
      - balanced (default): suppress probation pairs when only thin sessions
        (Sydney/Tokyo) are open; include full session list when London/NY open.
      - standard: current behavior (all session pairs).
      - aggressive: include all candidate pairs for any open session.
    """
    active_sessions = get_active_sessions(utc_hour)
    if not active_sessions:
        return CANDIDATE_PAIRS

    mode = (mode or 'balanced').lower()
    active_names: Set[str] = {s['name'] for s in active_sessions}
    liquid_open = bool(active_names & {'London', 'New York'})

    if mode == 'aggressive':
        return CANDIDATE_PAIRS

    seen = set()
    pairs = []
    for session in active_sessions:
        for p in session['pairs']:
            if p in seen or p not in CANDIDATE_PAIRS:
                continue
            if mode == 'balanced' and not liquid_open and p in PROBATION_PAIRS:
                continue
            seen.add(p)
            pairs.append(p)
    return pairs if pairs else CANDIDATE_PAIRS

# Minimum box requirements (pips)
# Updated 2026-03-05: math-justified floors using fee-math formula.
# Formula: min_box = (spread_pips x 2) / otr_marginal (0.25)
# This is the ABSOLUTE FLOOR — spread must fit inside the box at OTR <= 25%.
# Quality buffer added on top for pairs with proven issues (e.g. RR asymmetry).
# Reactive over-restrictions removed for winners and pairs with thin samples.
MIN_BOX_PIPS = {
    # -------------------------------------------------------
    # PHASE 7 — 2026-03-05: Math-corrected floors
    # Previous values were set reactively based on 10-day P&L.
    # This update anchors each pair to its fee-math floor.
    # Winners that were over-restricted are loosened.
    # Thin-sample pairs return to math floor (not punished on <10 trades).
    # -------------------------------------------------------

    # --- PROVEN WINNERS — fee-math floor ---
    'AUD_CAD': 30,   # +$9.80 — fee floor ~15p, kept at 30 (quality buffer)
    'AUD_CHF': 15,   # +$8.17 — fee floor ~12p, was 55 (over-restricted winner)
    'USD_JPY': 30,   # +$2.87 — fee floor ~10p, kept at 30 (quality buffer)
    'EUR_CAD': 30,   # +$1.83 (80% win) — fee floor ~20p, was 75 (over-restricted)
    'CHF_JPY': 60,   # +$0.18 — keep (small sample, fee floor ~15p)

    # --- MARGINAL / SINGLE TRADE — cautious but not choked ---
    'GBP_CHF': 100,  # -$2.60 (1 trade) — raise moderately

    # --- MODERATE LOSERS — math floor + quality buffer ---
    'USD_CHF': 30,   # -$3.33 — fee floor ~15p, was 100 (thin sample, net positive recently)
    'EUR_AUD': 110,  # -$6.74 (50% win, losers outsize winners)
    'EUR_JPY': 90,   # -$11.49 (42% win)
    'NZD_USD': 100,  # -$10.90 (43% win)

    # --- HEAVY LOSERS — math floor + larger quality buffer ---
    'AUD_JPY': 90,   # -$10.69 (27% win)
    'EUR_GBP': 95,   # -$11.72 (38% win)
    'AUD_USD': 100,  # -$9.17 (40 trades at 35% — most active bad pair)
    'GBP_JPY': 130,  # -$10.69 (30 trades at 30% — needs truly wide day)
    'GBP_USD': 30,   # -$25.00 — fee floor ~15p, was 110. Root cause is RR asymmetry not spread.
    'USD_CAD': 40,   # -$8.64 — fee floor ~15p, was 130 (thin 7-trade sample)
    'GBP_AUD': 150,  # -$19.66 (33% win)
    'EUR_USD': 30,   # -$14.97 — fee floor ~13p, was 130 (only 6-trade sample)
    'GBP_CAD': 80,   # -$54.16 — fee floor ~55p, was 200. 80p adds quality buffer above math floor.

    # --- UNTESTED (0 trades in audit period — screener already blocks) ---
    'USD_MXN': 200,
    'CAD_JPY': 55, 'NZD_JPY': 55,
    'EUR_NZD': 85, 'EUR_CHF': 60,
    'GBP_NZD': 110,
    'AUD_NZD': 60,
}
DEFAULT_MIN_BOX_PIPS = 30

# ============================================================
# MINIMUM PRIOR-DAY TICK VOLUME
# OANDA tick volume = number of price updates that session.
# Used as a liquidity proxy — filters bank holidays, dead-market
# days, and thin conditions where fills and spreads degrade OTR.
# Computed from prior_day bar already fetched in screen_one.
# No extra API call needed.
#
# Calibrated 2026-03-04 from live OANDA tick data:
#   High-liquidity pairs (EUR_USD, majors):  200k-400k typical
#   Medium pairs (NZD_USD, EUR_CAD):         100k-165k typical
#   Low/cross pairs (AUD_CAD, CHF_JPY):       60k-100k typical
# Floors set to catch holidays/dead days, not normal sessions.
# ============================================================
MIN_DAILY_TICKS = {
    # High-liquidity majors and crosses — floor 80k
    'EUR_USD': 80_000, 'GBP_USD': 80_000, 'USD_JPY': 80_000,
    'AUD_USD': 80_000, 'USD_CAD': 80_000, 'USD_MXN': 80_000,
    'EUR_JPY': 80_000, 'GBP_JPY': 80_000, 'AUD_JPY': 80_000,
    'EUR_AUD': 80_000, 'GBP_AUD': 80_000, 'AUD_NZD': 80_000,
    # Medium pairs — floor 50k
    'NZD_USD': 50_000, 'EUR_CAD': 50_000, 'USD_CHF': 50_000,
    'GBP_NZD': 50_000, 'EUR_NZD': 50_000, 'GBP_CHF': 50_000,
    # Low-volume crosses — floor 30k
    'EUR_GBP': 30_000, 'GBP_CAD': 30_000, 'CHF_JPY': 30_000,
    'EUR_CHF': 30_000, 'NZD_JPY': 30_000, 'CAD_JPY': 30_000,
    'AUD_CAD': 30_000, 'AUD_CHF': 30_000,
}
DEFAULT_MIN_DAILY_TICKS = 30_000

# ATR minimum — box must be at least this many ATR multiples
MIN_BOX_ATR_MULTIPLE = 0.5   # Box must be at least 50% of daily ATR

# Maximum ATR consumed at screening time
# If too much ATR is already consumed, box may not have room
MAX_ATR_CONSUMED_AT_SCAN = 3.00   # Skip if >300% consumed (very loose — forex is 24h, not 6.5h like stocks.
                                   # Pairs routinely consume 100-200% of ATR across sessions.
                                   # The real filters are OTR, RR, and pitchfork gates in signal_engine.
                                   # Was 0.85 — blocked every pair by NY afternoon.)

# Pip sizes (JPY pairs = 0.01; all others = 0.0001)
PIP_SIZES = {
    'EUR_USD': 0.0001, 'GBP_USD': 0.0001, 'USD_JPY': 0.01,
    'AUD_USD': 0.0001, 'USD_CHF': 0.0001, 'USD_CAD': 0.0001,
    'NZD_USD': 0.0001, 'USD_MXN': 0.0001,
    'EUR_JPY': 0.01,   'GBP_JPY': 0.01,   'AUD_JPY': 0.01,
    'CAD_JPY': 0.01,   'CHF_JPY': 0.01,   'NZD_JPY': 0.01,
    'EUR_GBP': 0.0001, 'EUR_CAD': 0.0001, 'EUR_AUD': 0.0001,
    'EUR_NZD': 0.0001, 'EUR_CHF': 0.0001,
    'GBP_CAD': 0.0001, 'GBP_AUD': 0.0001, 'GBP_NZD': 0.0001,
    'GBP_CHF': 0.0001,
    'AUD_CAD': 0.0001, 'AUD_NZD': 0.0001, 'AUD_CHF': 0.0001,
}


@dataclass
class ScreenResult:
    instrument: str
    viable: bool
    pdh: float
    pdl: float
    box_range_pips: float
    half_box_pips: float
    daily_atr_pips: float
    atr_consumed_pct: float
    current_spread_pips: float
    otr_ratio: float
    prior_day_ticks: int
    rejection_reason: Optional[str]
    last_checked: str


class ForexUniverseScreener:
    """
    Screens OANDA forex pairs for viable box setups.

    Called at:
    - Boot (before main loop starts)
    - Every 30 minutes during session
    - After box breaks (to evaluate alternative box)

    Returns only instruments worth processing each cycle.
    Instruments that fail screening are skipped entirely —
    no zone detection, no signal evaluation, nothing.
    This keeps the main loop fast.
    """

    def __init__(self, oanda_client, fee_calculator):
        self.client = oanda_client
        self.fee_calc = fee_calculator
        self._last_results: List[ScreenResult] = []

    def screen_all(self, pairs: list = None) -> List[ScreenResult]:
        """
        Screens candidate pairs for the current session.
        If pairs is provided, only screens those (session-filtered list).
        Returns full list of ScreenResult objects.
        Main loop should filter to viable=True only.
        """
        to_scan = pairs if pairs else CANDIDATE_PAIRS
        results = []
        for instrument in to_scan:
            try:
                result = self.screen_one(instrument)
                results.append(result)
                status = 'VIABLE' if result.viable else f'SKIP ({result.rejection_reason})'
                log.info(f"Screen {instrument}: {status}")
            except Exception as e:
                log.error(f"Screen failed {instrument}: {e}")
                results.append(ScreenResult(
                    instrument=instrument,
                    viable=False,
                    pdh=0, pdl=0,
                    box_range_pips=0, half_box_pips=0,
                    daily_atr_pips=0, atr_consumed_pct=0,
                    current_spread_pips=0, otr_ratio=1.0,
                    prior_day_ticks=0,
                    rejection_reason=f'Screen error: {e}',
                    last_checked=datetime.now(timezone.utc).isoformat()
                ))

        self._last_results = results
        viable_count = sum(1 for r in results if r.viable)
        log.info(f"Universe scan complete: {viable_count}/{len(results)} viable")
        return results

    def get_viable(self) -> List[str]:
        """Returns just the instrument names that are viable."""
        return [r.instrument for r in self._last_results if r.viable]

    def screen_one(self, instrument: str) -> ScreenResult:
        """
        Full viability check for one instrument.
        """
        pip = PIP_SIZES.get(instrument, 0.0001)
        min_pips = MIN_BOX_PIPS.get(instrument, DEFAULT_MIN_BOX_PIPS)

        # Get daily bars for PDH/PDL and ATR
        daily_bars = self.client.get_daily_bars(instrument, count=20)
        if daily_bars is None or len(daily_bars) < 2:
            return self._reject(instrument, 'No daily bar data', ticks=0)

        # Prior day = second-to-last bar (last bar is today, still forming)
        prior_day = daily_bars.iloc[-2]
        today     = daily_bars.iloc[-1]

        pdh = prior_day['high']
        pdl = prior_day['low']
        box_range = pdh - pdl
        half_box  = box_range / 2
        box_pips  = box_range / pip

        # ATR (14-day simple average of daily ranges)
        ranges = (daily_bars['high'] - daily_bars['low']).tail(14)
        daily_atr = ranges.mean()
        atr_pips  = daily_atr / pip

        # Current price
        current_price = today['close']

        # How much of today's ATR has been consumed so far?
        today_range = today['high'] - today['low']
        atr_consumed = today_range / daily_atr if daily_atr > 0 else 0

        # Prior-day tick volume (liquidity gate)
        prior_ticks = int(prior_day['volume']) if 'volume' in prior_day.index else 0
        min_ticks = MIN_DAILY_TICKS.get(instrument, DEFAULT_MIN_DAILY_TICKS)

        # Live spread
        spread = self.client.get_live_spread(instrument) or (pip * 1.5)
        spread_pips = spread / pip

        # OTR check
        total_overhead = spread * 2  # Entry + exit spread
        otr = total_overhead / half_box if half_box > 0 else 1.0

        # --- VIABILITY GATES ---

        if prior_ticks < min_ticks:
            return self._reject(
                instrument,
                f'Volume too low: {prior_ticks:,} ticks (min {min_ticks:,})',
                pdh, pdl, box_pips, half_box/pip, atr_pips,
                atr_consumed, spread_pips, otr, prior_ticks
            )

        if box_pips < min_pips:
            return self._reject(
                instrument,
                f'Box too narrow: {box_pips:.1f} pips (min {min_pips})',
                pdh, pdl, box_pips, half_box/pip, atr_pips,
                atr_consumed, spread_pips, otr, prior_ticks
            )

        if box_range < daily_atr * MIN_BOX_ATR_MULTIPLE:
            return self._reject(
                instrument,
                f'Box too small vs ATR: {box_range/daily_atr:.1%} of ATR',
                pdh, pdl, box_pips, half_box/pip, atr_pips,
                atr_consumed, spread_pips, otr, prior_ticks
            )

        if atr_consumed > MAX_ATR_CONSUMED_AT_SCAN:
            return self._reject(
                instrument,
                f'Too much ATR consumed: {atr_consumed:.0%} — little room left',
                pdh, pdl, box_pips, half_box/pip, atr_pips,
                atr_consumed, spread_pips, otr, prior_ticks
            )

        if otr > self.fee_calc.otr_marginal:
            return self._reject(
                instrument,
                f'OTR too high: {otr:.1%} — spread eats too much of box',
                pdh, pdl, box_pips, half_box/pip, atr_pips,
                atr_consumed, spread_pips, otr, prior_ticks
            )

        # Passed all gates
        return ScreenResult(
            instrument=instrument,
            viable=True,
            pdh=pdh,
            pdl=pdl,
            box_range_pips=box_pips,
            half_box_pips=half_box / pip,
            daily_atr_pips=atr_pips,
            atr_consumed_pct=atr_consumed,
            current_spread_pips=spread_pips,
            otr_ratio=otr,
            prior_day_ticks=prior_ticks,
            rejection_reason=None,
            last_checked=datetime.now(timezone.utc).isoformat()
        )

    def _reject(self, instrument, reason,
                 pdh=0, pdl=0, box_pips=0, half_pips=0,
                 atr_pips=0, atr_consumed=0, spread_pips=0, otr=1.0, ticks=0):
        return ScreenResult(
            instrument=instrument,
            viable=False,
            pdh=pdh, pdl=pdl,
            box_range_pips=box_pips,
            half_box_pips=half_pips,
            daily_atr_pips=atr_pips,
            atr_consumed_pct=atr_consumed,
            current_spread_pips=spread_pips,
            otr_ratio=otr,
            prior_day_ticks=ticks,
            rejection_reason=reason,
            last_checked=datetime.now(timezone.utc).isoformat()
        )

