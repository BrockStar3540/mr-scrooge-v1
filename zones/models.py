# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from datetime import datetime
import logging
log = logging.getLogger('mr-scrooge-v2.zones')


class MagicLine:
    """
    A single horizontal liquidity zone drawn on the hourly chart.
    These are the primary reference levels for all trade decisions.
    """
    price: float
    zone_type: str          # 'resistance', 'support', 'breakout_flip'
    tier: int               # 1=highest (hourly+daily align), 2, 3, 4
    test_count: int         # How many times price has tested this level
    last_tested: str        # ISO datetime of most recent test
    daily_aligned: bool     # Does this level match a daily high/low?
    is_breakout_flip: bool  # Was this a breakout level that flipped polarity?
    strength_score: float   # Composite score 0-100
    source_timeframe: str   # '1H', 'D', 'pre_market'
    symbol: str
    created_at: str


@dataclass
class DailyBox:
    """
    The primary PDH/PDL box drawn from prior day's range.
    Foundation of the entire system.
    """
    symbol: str
    PDH: float              # Prior day high
    PDL: float              # Prior day low
    center: float           # Midpoint — do not trade here
    box_range: float        # PDH - PDL
    half_box: float         # Half the range
    box_range_pct: float    # Range as % of price
    ATR: float              # Daily ATR(14)
    ATR_60pct: float        # 60% of ATR — sucker move threshold
    ATR_80pct: float        # 80% of ATR — strong reversal threshold
    ATR_20pct: float        # 20% of ATR — minimum threshold
    session_date: str
    draw_time: str
    # Validation fields (percentile filtering + touch validation)
    pdh_touches: int = 0
    pdl_touches: int = 0
    percentile_filtered: bool = False
    validated: bool = True
    raw_PDH: float = 0.0
    raw_PDL: float = 0.0


@dataclass
class ZoneMap:
    """
    Complete zone picture for one instrument at one point in time.
    This is what the signal engine queries.
    """
    symbol: str
    exchange: str
    daily_box: DailyBox
    prior_hour_high: float          # Prior completed hour high — secondary magic line
    prior_hour_low: float           # Prior completed hour low — secondary magic line
    prior_hour_mid: float           # Prior hour midpoint
    magic_lines: List[MagicLine]
    tier1_zones: List[MagicLine]    # Hourly + daily aligned
    tier2_zones: List[MagicLine]    # Multiple hourly tests
    tier3_zones: List[MagicLine]    # Single hourly pivot
    sell_zones: List[MagicLine]     # Near PDH and upper magic lines
    buy_zones: List[MagicLine]      # Near PDL and lower magic lines
    current_price: float
    current_zone: str               # 'buy_zone', 'sell_zone', 'middle', 'above_box', 'below_box'
    nearest_buy_zone: Optional[MagicLine]
    nearest_sell_zone: Optional[MagicLine]
    built_at: str


# ============================================================
# ZONE DETECTOR
# ============================================================
