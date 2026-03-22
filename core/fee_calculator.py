# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — OANDA Fee Calculator
OANDA cost model: spread only, no commission.
OTR = (spread_entry + spread_exit) / half_box
Health monitoring: registers as 'fee_calculator' with HealthMonitor.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

log = logging.getLogger('mr-scrooge-v2.fee_calculator')


def _monitor():
    from health.monitor import HealthMonitor
    return HealthMonitor.get()


# Typical baseline spreads by pair (price units, liquid London hours)
BASELINE_SPREADS = {
    # Dollar majors
    'EUR_USD': 0.00012, 'GBP_USD': 0.00014, 'USD_JPY': 0.011,
    'AUD_USD': 0.00014, 'USD_CAD': 0.00015, 'USD_CHF': 0.00014,
    'NZD_USD': 0.00016,
    # JPY crosses
    'EUR_JPY': 0.013, 'GBP_JPY': 0.020, 'AUD_JPY': 0.014,
    'CAD_JPY': 0.014, 'CHF_JPY': 0.015, 'NZD_JPY': 0.016,
    # EUR crosses
    'EUR_GBP': 0.00013, 'EUR_CAD': 0.00022, 'EUR_AUD': 0.00022,
    'EUR_NZD': 0.00030, 'EUR_CHF': 0.00015,
    # GBP crosses
    'GBP_CAD': 0.00030, 'GBP_AUD': 0.00030,
    'GBP_NZD': 0.00040, 'GBP_CHF': 0.00022,
    # Commodity crosses
    'AUD_CAD': 0.00018, 'AUD_NZD': 0.00022,
}


@dataclass
class OANDACostBreakdown:
    instrument:         str
    spread:             float
    spread_pips:        float
    entry_cost:         float
    exit_cost:          float
    total_round_trip:   float
    half_box:           float
    net_profit_potential: float
    otr_ratio:          float
    otr_category:       str     # excellent / acceptable / marginal / skip
    efficiency_ratio:   float
    viable:             bool
    spread_is_spiked:   bool
    notes:              str


class OANDAFeeCalculator:
    """
    All-in cost calculations for OANDA forex trades.
    Used by: universe screener, signal engine, execution router, trade manager.
    """

    _COMPONENT = 'fee_calculator'

    def __init__(self, config=None):
        # Accept both Config dataclass and legacy dict
        if hasattr(config, 'otr_excellent'):
            self.otr_excellent    = config.otr_excellent
            self.otr_acceptable   = config.otr_acceptable
            self.otr_marginal     = config.otr_marginal
            self.min_net_profit   = config.min_net_profit
            self.min_efficiency   = config.min_efficiency
            self.spike_threshold  = config.spread_spike_threshold
            self.max_spread_pct   = config.max_spread_pct_of_half
        else:
            cfg = config or {}
            self.otr_excellent   = cfg.get('otr_excellent',   0.05)
            self.otr_acceptable  = cfg.get('otr_acceptable',  0.15)
            self.otr_marginal    = cfg.get('otr_marginal',    0.25)
            self.min_net_profit  = cfg.get('min_net_profit',  0.001)
            self.min_efficiency  = cfg.get('min_efficiency',  2.0)
            self.spike_threshold = cfg.get('spread_spike_threshold', 4.0)
            self.max_spread_pct  = cfg.get('max_spread_pct_of_half', 0.30)

        _monitor().register(self._COMPONENT, failure_threshold=10, recovery_seconds=60)

    def calculate(self, instrument: str, live_spread: float,
                   half_box: float, units: float = 1.0) -> OANDACostBreakdown:
        try:
            entry_cost = live_spread * units
            exit_cost  = live_spread * units
            total      = entry_cost + exit_cost
            net_profit = half_box - total
            otr        = total / half_box if half_box > 0 else 1.0
            efficiency = net_profit / total if total > 0 else 0.0

            if otr < self.otr_excellent:
                category = 'excellent'
            elif otr < self.otr_acceptable:
                category = 'acceptable'
            elif otr < self.otr_marginal:
                category = 'marginal'
            else:
                category = 'skip'

            baseline  = BASELINE_SPREADS.get(instrument, live_spread)
            is_spiked = live_spread > baseline * self.spike_threshold

            viable = (
                category != 'skip' and
                net_profit >= self.min_net_profit and
                efficiency >= self.min_efficiency and
                not is_spiked
            )

            pip_size    = 0.01 if 'JPY' in instrument else 0.0001
            spread_pips = live_spread / pip_size

            notes = (
                f'Spread: {spread_pips:.1f}p | OTR: {otr:.1%} ({category}) | '
                f'Net: {net_profit:.5f} | Eff: {efficiency:.2f}x'
                + (f' | SPIKE({live_spread/baseline:.1f}x)' if is_spiked else '')
                + (' | VIABLE' if viable else ' | NOT VIABLE')
            )

            _monitor().record_success(self._COMPONENT)
            return OANDACostBreakdown(
                instrument=instrument, spread=live_spread, spread_pips=spread_pips,
                entry_cost=entry_cost, exit_cost=exit_cost, total_round_trip=total,
                half_box=half_box, net_profit_potential=net_profit,
                otr_ratio=otr, otr_category=category, efficiency_ratio=efficiency,
                viable=viable, spread_is_spiked=is_spiked, notes=notes,
            )
        except Exception as exc:
            _monitor().record_failure(self._COMPONENT, str(exc))
            log.error(f'calculate() failed for {instrument}: {exc}')
            # Return a safe non-viable breakdown
            return OANDACostBreakdown(
                instrument=instrument, spread=live_spread, spread_pips=0,
                entry_cost=0, exit_cost=0, total_round_trip=0,
                half_box=half_box, net_profit_potential=0,
                otr_ratio=1.0, otr_category='skip', efficiency_ratio=0,
                viable=False, spread_is_spiked=False,
                notes=f'ERROR: {exc}',
            )

    def is_spread_acceptable(self, instrument: str, live_spread: float,
                               half_box: float) -> Tuple[bool, str]:
        if half_box > 0:
            spread_pct = (live_spread * 2) / half_box
            if spread_pct > self.max_spread_pct:
                return False, (f'Spread {live_spread:.5f} = {spread_pct:.1%} of half-box '
                                f'(limit {self.max_spread_pct:.0%})')
        baseline = BASELINE_SPREADS.get(instrument, 0)
        if baseline > 0 and live_spread > baseline * self.spike_threshold:
            return False, (f'Spike: {live_spread:.5f} vs baseline {baseline:.5f} '
                            f'({live_spread/baseline:.1f}x)')
        return True, 'Spread normal'

    def estimate_viability(self, instrument: str, half_box: float,
                            session: str = 'london_open') -> OANDACostBreakdown:
        multipliers = {'london_open': 1.0, 'ny_open': 1.1, 'overlap': 1.0,
                       'london_close': 1.8, 'asian': 1.5, 'weekend': 3.0}
        baseline = BASELINE_SPREADS.get(instrument, 0.00015)
        spread   = baseline * multipliers.get(session, 1.2)
        return self.calculate(instrument, spread, half_box)

    def calculate_trade_pnl(self, entry_price: float, exit_price: float,
                              units: float, direction: str,
                              live_spread: float) -> Dict:
        if direction == 'long':
            gross = (exit_price - entry_price) * units
        else:
            gross = (entry_price - exit_price) * units
        spread_cost = live_spread * 2 * units
        net = gross - spread_cost
        return {'gross_pnl': gross, 'spread_cost': spread_cost,
                'net_pnl': net, 'units': units, 'direction': direction}

    def minimum_viable_half_box(self, instrument: str, live_spread: float) -> float:
        total_cost = live_spread * 2
        minimum    = max(
            total_cost / self.otr_acceptable,
            total_cost * (1 + self.min_efficiency),
            self.min_net_profit + total_cost,
        )
        return minimum
