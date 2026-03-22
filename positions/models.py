# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""mr-scrooge-v2 — Position Models"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OpenPosition:
    """One active trade."""
    trade_id: str
    symbol: str
    exchange: str
    direction: str              # 'long' | 'short'
    entry_price: float
    original_stop: float
    current_stop: float
    take_profit_1: float
    take_profit_2: float
    original_quantity: float
    remaining_quantity: float
    entry_time: str
    entry_signal_type: str
    zone_tier: int
    atr_at_entry: float
    overhead_cost: float
    break_even_achieved: bool
    partial_1_taken: bool       # TP1 level passed sentinel
    partial_2_taken: bool
    last_hourly_close: float
    hourly_green_checks_done: int
    status: str                 # 'open' | 'partial' | 'closed'
    stop_moved_at: str
    oanda_trade_id: str = ''
    green_exit_tp_set: float = 0.0
    highest_price: float = 0.0  # Peak (long) — for hybrid trailing stop
    lowest_price: float = 0.0   # Trough (short) — for hybrid trailing stop


@dataclass
class TradeEvent:
    trade_id: str
    event_type: str
    timestamp: str
    price: float
    quantity: float
    reason: str
    pnl: float
    overhead_applied: float
    cumulative_pnl: float
    order_type: str


@dataclass
class ReentryAssessment:
    allowed: bool
    reason: str
    still_at_liquidity_zone: bool
    zone_tested_count: int
    was_liquidity_sweep: bool
    daily_context: str
    max_reentries_reached: bool
