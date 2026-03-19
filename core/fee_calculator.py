# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
# ============================================================
# MR. SCROOGE — OANDA FEE CALCULATOR
#
# OANDA cost model: spread only, no commission.
# Spread is paid on entry AND exit.
# Limit orders do NOT reduce cost vs market on OANDA —
# spread is the spread regardless of order type.
#
# Key risks:
#   - Spread spikes during news events (can 5-10x normal)
#   - Spread widens at London close, Asian open
#   - A normally viable box becomes unviable if spread spikes
#
# OTR = (spread_entry + spread_exit) / half_box
# ============================================================

import logging
from dataclasses import dataclass
from typing import Dict, Optional

log = logging.getLogger('mr-scrooge.fee_calculator')


# Typical baseline spreads by pair (in price units, at liquid hours)
# Used for pre-session viability estimates and spike detection.
# Always fetch live spread before actual trade.
# Crosses generally have wider spreads; JPY crosses in pips * 0.01.
BASELINE_SPREADS = {
    # Dollar majors — tightest spreads in forex
    'EUR_USD': 0.00012,
    'GBP_USD': 0.00014,
    'USD_JPY': 0.011,
    'AUD_USD': 0.00014,
    'USD_CAD': 0.00015,
    'USD_CHF': 0.00014,
    'NZD_USD': 0.00016,
    # JPY crosses — pip = 0.01; spreads ~1.3–2.5 pips
    'EUR_JPY': 0.013,
    'GBP_JPY': 0.020,
    'AUD_JPY': 0.014,
    'CAD_JPY': 0.014,
    'CHF_JPY': 0.015,
    'NZD_JPY': 0.016,
    # EUR crosses — typically 1–3 pips
    'EUR_GBP': 0.00013,
    'EUR_CAD': 0.00022,
    'EUR_AUD': 0.00022,
    'EUR_NZD': 0.00030,
    'EUR_CHF': 0.00015,
    # GBP crosses — wider, 2–4 pips
    'GBP_CAD': 0.00030,
    'GBP_AUD': 0.00030,
    'GBP_NZD': 0.00040,
    'GBP_CHF': 0.00022,
    # Commodity crosses
    'AUD_CAD': 0.00018,
    'AUD_NZD': 0.00022,
}

# Spread multipliers by session
# Spread widens significantly at certain times
SESSION_SPREAD_MULTIPLIERS = {
    'london_open':   1.0,    # Best spreads
    'ny_open':       1.1,
    'overlap':       1.0,    # London/NY overlap — tightest
    'london_close':  1.8,    # Spreads widen noticeably
    'asian':         1.5,    # Thinner liquidity
    'weekend':       3.0,    # Do not trade
}


@dataclass
class OANDACostBreakdown:
    instrument: str
    spread: float               # Current spread in price units
    spread_pips: float          # Spread in pips (for reference)
    entry_cost: float           # Cost to enter (1x spread)
    exit_cost: float            # Cost to exit (1x spread)
    total_round_trip: float     # Entry + exit spread cost
    half_box: float             # Half the box range
    net_profit_potential: float # half_box - total_round_trip
    otr_ratio: float            # total_round_trip / half_box
    otr_category: str           # excellent / acceptable / marginal / skip
    efficiency_ratio: float     # net_profit / total_round_trip
    viable: bool
    spread_is_spiked: bool      # True if spread > 2.5x baseline (was 2.0x — Frank fix 2026-02-26)
    notes: str


class OANDAFeeCalculator:
    """
    Calculates all-in trading costs for OANDA forex trades.

    Used by Mr. Scrooge's:
    - Universe screener (pre-session viability)
    - Signal engine (live OTR gate before entry)
    - Trade manager (PnL attribution on partials)
    - Execution router (spread spike detection)
    """

    def __init__(self, config: dict = None):
        cfg = config or {}
        # OTR thresholds
        self.otr_excellent  = cfg.get('otr_excellent',  0.05)
        self.otr_acceptable = cfg.get('otr_acceptable', 0.10)
        self.otr_marginal   = cfg.get('otr_marginal',   0.15)
        # Minimum viable
        self.min_net_profit  = cfg.get('min_net_profit',  5.0)
        self.min_efficiency  = cfg.get('min_efficiency',  2.0)
        # Spread spike threshold (x baseline = spike)
        # Default raised to 2.5x (was 2.0x) to tolerate JPY cross spread spikes during Sydney open (Frank fix 2026-02-26)
        self.spike_threshold = cfg.get('spread_spike_threshold', 2.5)
        # Max spread as % of half-box before pausing entries
        self.max_spread_pct  = cfg.get('max_spread_pct_of_half', 0.15)

    # ----------------------------------------------------------
    # CORE COST CALCULATION
    # ----------------------------------------------------------

    def calculate(self,
                   instrument: str,
                   live_spread: float,
                   half_box: float,
                   units: float = 1.0) -> OANDACostBreakdown:
        """
        Full cost breakdown for one potential trade.

        instrument: e.g. 'EUR_USD'
        live_spread: current bid/ask spread in price units (fetch live)
        half_box: distance from box wall to center (potential profit)
        units: position size multiplier (1.0 = base calculation)
        """
        entry_cost = live_spread * units
        exit_cost  = live_spread * units
        total      = entry_cost + exit_cost

        net_profit = half_box - total
        otr        = total / half_box if half_box > 0 else 1.0
        efficiency = net_profit / total if total > 0 else 0.0

        # OTR category
        if otr < self.otr_excellent:
            category = 'excellent'
        elif otr < self.otr_acceptable:
            category = 'acceptable'
        elif otr < self.otr_marginal:
            category = 'marginal'
        else:
            category = 'skip'

        # Spread spike check
        baseline = BASELINE_SPREADS.get(instrument, live_spread)
        is_spiked = live_spread > baseline * self.spike_threshold

        # Viability
        viable = (
            category != 'skip' and
            net_profit >= self.min_net_profit and
            efficiency >= self.min_efficiency and
            not is_spiked
        )

        # Pip calculation (for logging clarity)
        pip_size = 0.01 if 'JPY' in instrument else 0.0001
        spread_pips = live_spread / pip_size

        notes_parts = [
            f'Spread: {spread_pips:.1f} pips',
            f'OTR: {otr:.1%} ({category})',
            f'Net: {net_profit:.5f}',
            f'Efficiency: {efficiency:.2f}x',
        ]
        if is_spiked:
            notes_parts.append(f'SPREAD SPIKE ({live_spread:.5f} vs baseline {baseline:.5f})')
        notes_parts.append('VIABLE' if viable else 'NOT VIABLE')

        return OANDACostBreakdown(
            instrument=instrument,
            spread=live_spread,
            spread_pips=spread_pips,
            entry_cost=entry_cost,
            exit_cost=exit_cost,
            total_round_trip=total,
            half_box=half_box,
            net_profit_potential=net_profit,
            otr_ratio=otr,
            otr_category=category,
            efficiency_ratio=efficiency,
            viable=viable,
            spread_is_spiked=is_spiked,
            notes=' | '.join(notes_parts)
        )

    # ----------------------------------------------------------
    # SPREAD SPIKE MONITOR
    # ----------------------------------------------------------

    def is_spread_acceptable(self,
                               instrument: str,
                               live_spread: float,
                               half_box: float) -> tuple:
        """
        Quick check: is current spread acceptable for entry?
        Returns (acceptable: bool, reason: str)

        Called every candle during active monitoring.
        If spread spikes mid-session, entries are paused.
        """
        # Absolute check: spread > max_spread_pct of half-box
        if half_box > 0:
            spread_pct = (live_spread * 2) / half_box
            if spread_pct > self.max_spread_pct:
                return False, (
                    f'Spread {live_spread:.5f} = {spread_pct:.1%} of half-box '
                    f'(limit: {self.max_spread_pct:.0%}) — entries paused'
                )

        # Spike check vs baseline
        baseline = BASELINE_SPREADS.get(instrument, 0)
        if baseline > 0 and live_spread > baseline * self.spike_threshold:
            return False, (
                f'Spread spike: {live_spread:.5f} vs baseline {baseline:.5f} '
                f'({live_spread/baseline:.1f}x normal) — entries paused'
            )

        return True, 'Spread normal'

    # ----------------------------------------------------------
    # PRE-SESSION SCREENER ESTIMATE
    # ----------------------------------------------------------

    def estimate_viability(self,
                            instrument: str,
                            half_box: float,
                            session: str = 'london_open') -> OANDACostBreakdown:
        """
        Pre-session viability estimate using baseline spread.
        Used by universe screener before live spread is available.

        session: 'london_open', 'ny_open', 'overlap', 'asian', etc.
        """
        baseline = BASELINE_SPREADS.get(instrument, 0.00015)
        multiplier = SESSION_SPREAD_MULTIPLIERS.get(session, 1.2)
        estimated_spread = baseline * multiplier

        return self.calculate(instrument, estimated_spread, half_box)

    # ----------------------------------------------------------
    # PNL ATTRIBUTION
    # ----------------------------------------------------------

    def calculate_trade_pnl(self,
                              entry_price: float,
                              exit_price: float,
                              units: float,
                              direction: str,
                              live_spread: float) -> Dict:
        """
        Net PnL after spread costs for a completed trade.
        Used by trade manager on each partial and final exit.
        """
        if direction == 'long':
            gross = (exit_price - entry_price) * units
        else:
            gross = (entry_price - exit_price) * units

        # Spread cost: paid on entry and exit
        spread_cost = live_spread * 2 * units
        net = gross - spread_cost

        log.info(
            f'Trade PnL: gross={gross:.5f} spread_cost={spread_cost:.5f} '
            f'net={net:.5f} ({direction} {units:.4f} units)'
        )

        return {
            'gross_pnl': gross,
            'spread_cost': spread_cost,
            'net_pnl': net,
            'units': units,
            'direction': direction
        }

    # ----------------------------------------------------------
    # MINIMUM VIABLE BOX SIZE
    # ----------------------------------------------------------

    def minimum_viable_half_box(self,
                                  instrument: str,
                                  live_spread: float) -> float:
        """
        What is the minimum half-box size for this instrument
        to be worth trading right now?

        Derived from: we need OTR < 10% AND efficiency >= 2x
        So: half_box > total_cost / otr_acceptable
            AND half_box > total_cost * (1 + min_efficiency)
        """
        total_cost = live_spread * 2
        min_from_otr = total_cost / self.otr_acceptable
        min_from_efficiency = total_cost * (1 + self.min_efficiency)
        minimum = max(min_from_otr, min_from_efficiency, self.min_net_profit + total_cost)

        pip_size = 0.01 if 'JPY' in instrument else 0.0001
        minimum_pips = minimum / pip_size

        log.debug(
            f'{instrument} minimum viable half-box: '
            f'{minimum:.5f} ({minimum_pips:.1f} pips) at spread {live_spread:.5f}'
        )
        return minimum
