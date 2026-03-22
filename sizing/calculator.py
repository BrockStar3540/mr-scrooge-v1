# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Sizing Calculator
Margin-utilization based position sizing.
Deploy 80% of equity as margin per trade.
"""
import logging
from typing import Optional
from core.oanda_client import OANDAClient

log = logging.getLogger('mr-scrooge-v2.sizing')


class SizingCalculator:
    """
    Margin-utilization sizing.
    base_units = (equity × margin_utilization) / (base_price_usd × margin_rate)
    Caps at 80% of free margin to avoid INSUFFICIENT_MARGIN rejections.
    """

    def __init__(self, config, oanda: OANDAClient):
        self.oanda = oanda
        if hasattr(config, 'margin_utilization_pct'):
            self.margin_utilization = config.margin_utilization_pct
        else:
            self.margin_utilization = config.get('margin_utilization_pct', 0.80)

    def calculate(self, symbol: str, current_price: float, equity: float,
                   free_margin: Optional[float] = None) -> Optional[int]:
        """
        Returns integer unit count, or None if sizing is impossible.
        """
        inst = self.oanda._instrument_cache.get(symbol, {})
        pip_size    = 10 ** int(inst.get('pipLocation', -4))
        pip_value   = float(self.oanda.get_pip_value_per_unit(symbol, current_price))
        margin_rate = float(inst.get('marginRate', 0.0333))

        base_price_usd = pip_value * current_price / pip_size if pip_size > 0 else current_price
        if base_price_usd <= 0:
            log.warning(f'{symbol}: base_price_usd={base_price_usd} — cannot size')
            return None

        target_margin = equity * self.margin_utilization
        units = target_margin / (base_price_usd * margin_rate)
        units = round(units)

        if units < 1:
            log.warning(f'{symbol}: sized to {units:.0f} units — too small')
            return None

        # Cap to 80% of free margin
        if free_margin is not None:
            usable = free_margin * 0.80
            max_units = round(usable / (base_price_usd * margin_rate))
            if max_units < 1:
                log.warning(f'{symbol}: insufficient free margin (${free_margin:.2f})')
                return None
            if units > max_units:
                if max_units < units * 0.50:
                    log.warning(f'{symbol}: margin cap too severe ({max_units:.0f} < 50% of {units:.0f}) — skip')
                    return None
                log.info(f'{symbol}: units capped by margin: {units:.0f} → {max_units:.0f}')
                units = max_units

        # Sizing log
        pip_dollar = units * pip_value
        log.info(
            f'{symbol}: {units:.0f} units | equity ${equity:.0f} | '
            f'margin target ${target_margin:.0f} ({self.margin_utilization:.0%}) | '
            f'margin_rate {margin_rate:.2%} | ${pip_dollar:.2f}/pip'
        )
        return units
