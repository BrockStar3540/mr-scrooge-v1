# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================

from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum
import logging
log = logging.getLogger('mr-scrooge-v2.box')
from zones.models import DailyBox


class BoxState(Enum):
    IN_BOX_RANGING = "IN_BOX_RANGING"
    BOX_BREACH_PENDING = "BOX_BREACH_PENDING"
    BOX_BROKEN_RECALCULATING = "BOX_BROKEN_RECALCULATING"
    NEW_BOX_STANDBY = "NEW_BOX_STANDBY"
    SHELVED = "SHELVED"


@dataclass
class BoxStateRecord:
    """Full state snapshot for one instrument."""
    symbol: str
    state: BoxState
    active_box: DailyBox
    alternative_box: Optional[DailyBox]
    using_alternative: bool

    # Breach tracking
    breach_started_at: Optional[str]
    breach_price: float
    breach_direction: str           # 'up' or 'down'
    breach_candles_confirmed: int

    # Box age confidence
    session_start_time: str
    box_age_hours: float
    age_confidence_pct: float       # 100% → 50% through session

    # Confirmation requirements (tighten as box ages)
    confirmation_gates_required: int  # 2 or 3 — goes to 3 as box ages

    # New box proven status
    new_box_trades: int             # Successful trades in new box
    new_box_proven: bool            # True after 3 successful trades

    # Return-to-box setup
    broken_wall_price: float        # Price of broken wall for RTB setup
    broken_wall_direction: str      # Which wall was broken

    # Current status
    entry_paused: bool
    pause_reason: str
    last_spread: float
    spread_threshold: float
    last_updated: str


@dataclass
class AlternativeBoxResult:
    """Result of drawing a new alternative box."""
    success: bool
    new_box: Optional[DailyBox]
    method: str                     # 'original_wall_to_new_extreme', 'session_cheat_code'
    viable: bool
    viability_reason: str
    efficiency_ratio: float


# ============================================================
# BOX STATE MACHINE
# ============================================================
