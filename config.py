# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — Configuration
All trading parameters in one typed dataclass.
Loaded once at boot; immutable at runtime.
"""

from dataclasses import dataclass, field
from typing import List
import os
import json


@dataclass(frozen=True)
class Config:
    # ── Risk ─────────────────────────────────────────────────
    max_trade_risk_pct: float = 0.015
    margin_utilization_pct: float = 0.80
    target_profit_pct: float = 0.0025
    tp1_allocation_pct: float = 0.40
    green_exit_min_pct: float = 0.00125
    max_concurrent_positions: int = 1
    max_eur_positions: int = 2
    max_daily_loss_pct: float = 0.075
    min_risk_reward: float = 1.60

    # ── Fixed pip structure ───────────────────────────────────
    sl_pips: int = 10
    tp1_pips: int = 10
    tp2_pips: int = 30

    # ── Timing ───────────────────────────────────────────────
    scan_interval_seconds: int = 300
    rescan_interval_minutes: int = 30
    open_wait_minutes: int = 15
    max_hold_minutes: int = 360
    hourly_green_exit_enabled: bool = True
    hourly_green_exit_minutes: int = 60
    hourly_green_exit_min_pnl: float = 0.002

    # ── Stop loss floor ───────────────────────────────────────
    sl_floor_pct: float = 0.0050

    # ── Execution ────────────────────────────────────────────
    limit_timeout_minutes: int = 5
    time_exit_cooldown_minutes: int = 15

    # ── OTR thresholds ────────────────────────────────────────
    otr_excellent: float = 0.05
    otr_acceptable: float = 0.15
    otr_marginal: float = 0.25
    min_efficiency: float = 2.0
    min_net_profit: float = 0.001
    min_net_profit_pips: int = 7

    # ── Box / zone parameters ─────────────────────────────────
    zone_proximity_pct: float = 0.0025
    atr_period: int = 14
    atr_high_confidence_pct: float = 0.60
    atr_valid_pct: float = 0.60
    atr_minimum_pct: float = 0.15
    max_spread_pct_of_half: float = 0.30
    spread_spike_threshold: float = 4.0

    # ── Universe ─────────────────────────────────────────────
    instrument_tiers: List[str] = field(default_factory=lambda: ['majors'])
    universe_mode: str = 'balanced'
    apply_probation_size: bool = True
    probation_size_multiplier: float = 0.65

    def as_dict(self) -> dict:
        """Return config as plain dict (for legacy code compatibility)."""
        import dataclasses
        return dataclasses.asdict(self)

    @classmethod
    def from_env_overrides(cls) -> 'Config':
        """
        Build Config from defaults, optionally overriding individual
        fields via environment variables (e.g. SCROOGE_SL_PIPS=12).
        All env vars must be prefixed with SCROOGE_ and match field names.
        """
        overrides = {}
        import dataclasses
        for f in dataclasses.fields(cls):
            env_key = f'SCROOGE_{f.name.upper()}'
            val = os.getenv(env_key)
            if val is not None:
                # Cast to field type
                if f.type in (int, 'int'):
                    overrides[f.name] = int(val)
                elif f.type in (float, 'float'):
                    overrides[f.name] = float(val)
                elif f.type in (bool, 'bool'):
                    overrides[f.name] = val.lower() in ('1', 'true', 'yes')
                else:
                    overrides[f.name] = val
        return cls(**overrides)


# Singleton — import this everywhere
CFG = Config.from_env_overrides()
