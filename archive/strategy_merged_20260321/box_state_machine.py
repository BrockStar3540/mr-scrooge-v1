# ============================================================
# BOXBOT — BOX STATE MACHINE
# Runtime box state management per the protocol defined:
#
#   States: IN_BOX_RANGING → BOX_BREACH_PENDING →
#           BOX_BROKEN_RECALCULATING → NEW_BOX_STANDBY
#
#   Box Age Decay: Confidence degrades through session
#   Spread Monitor: Pauses entries if spread spikes
#   Alternative Box: Cheat code when original box broken
#   Return-to-Box: Tradeable setup when price revisits broken wall
# ============================================================

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timedelta
from enum import Enum
import logging
log = logging.getLogger('mr-scrooge')

from strategy.zone_detector import DailyBox, ZoneMap


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

class BoxStateMachine:
    """
    Manages runtime box states for all active instruments.

    Transitions:
    IN_BOX → BREACH_PENDING: Price closes 1 candle beyond box + buffer
    BREACH_PENDING → IN_BOX: Price returns inside within 2-candle window
    BREACH_PENDING → BROKEN: 2 consecutive candles outside, or >1% extension
    BROKEN → NEW_BOX_STANDBY: Alternative box drawn and viability checked
    NEW_BOX_STANDBY → SHELVED: If new box not viable
    """

    def __init__(self, config: dict):
        self.breach_buffer_pct = config.get('breach_buffer_pct', 0.002)    # 0.2%
        self.breach_confirm_candles = config.get('breach_confirm_candles', 2)
        self.breach_extension_pct = config.get('breach_extension_pct', 0.01)  # 1% = instant confirm
        self.max_spread_pct_of_half = config.get('max_spread_pct_of_range', 0.15)
        self.new_box_proven_trades = config.get('new_box_proven_trades', 3)
        self.min_efficiency_ratio = config.get('min_efficiency', 2.0)
        self.min_net_profit = config.get('min_net_profit', 0.0)
        # Pair-consistent floor: use pip-based net threshold if configured.
        self.min_net_profit_pips = config.get('min_net_profit_pips', None)

        # Box age confidence schedule (hours: confidence %)
        self.age_confidence = {
            0: 1.00,
            2: 0.85,
            4: 0.70,
            6: 0.50
        }

        self.states: Dict[str, BoxStateRecord] = {}

    def _pip_size(self, symbol: str) -> float:
        return 0.01 if 'JPY' in symbol else 0.0001

    def _min_net_profit_for_symbol(self, symbol: str) -> float:
        if self.min_net_profit_pips is None:
            return float(self.min_net_profit)
        return float(self.min_net_profit_pips) * self._pip_size(symbol)

    # ----------------------------------------------------------
    # INITIALIZE
    # ----------------------------------------------------------

    def initialize_instrument(self,
                                symbol: str,
                                daily_box: DailyBox,
                                current_spread: float) -> BoxStateRecord:
        """Called at boot or when instrument passes screening."""
        spread_threshold = daily_box.half_box * self.max_spread_pct_of_half

        record = BoxStateRecord(
            symbol=symbol,
            state=BoxState.IN_BOX_RANGING,
            active_box=daily_box,
            alternative_box=None,
            using_alternative=False,
            breach_started_at=None,
            breach_price=0.0,
            breach_direction='',
            breach_candles_confirmed=0,
            session_start_time=datetime.now().isoformat(),
            box_age_hours=0.0,
            age_confidence_pct=100.0,
            confirmation_gates_required=2,
            new_box_trades=0,
            new_box_proven=False,
            broken_wall_price=0.0,
            broken_wall_direction='',
            entry_paused=False,
            pause_reason='',
            last_spread=current_spread,
            spread_threshold=spread_threshold,
            last_updated=datetime.now().isoformat()
        )

        self.states[symbol] = record
        log.info(f"BoxStateMachine initialized: {symbol} | State: IN_BOX_RANGING")
        return record

    # ----------------------------------------------------------
    # MAIN UPDATE (called every 5-minute candle close)
    # ----------------------------------------------------------

    def update(self,
                symbol: str,
                current_price: float,
                current_bar: pd.Series,
                current_spread: float,
                total_overhead: float,
                minutes_since_session_start: int) -> BoxStateRecord:
        """
        Master update function. Evaluates all state transitions
        and updates management parameters.
        """
        if symbol not in self.states:
            log.warning(f"Symbol {symbol} not initialized in state machine")
            return None

        record = self.states[symbol]
        record.last_updated = datetime.now().isoformat()
        record.last_spread = current_spread

        # 1. Update box age and confidence
        record.box_age_hours = minutes_since_session_start / 60
        record.age_confidence_pct = self._get_age_confidence(record.box_age_hours) * 100
        record.confirmation_gates_required = 3 if record.box_age_hours >= 4 else 2

        # 2. Check spread
        if current_spread > record.spread_threshold:
            record.entry_paused = True
            record.pause_reason = (
                f"Spread {current_spread:.5f} exceeds threshold "
                f"{record.spread_threshold:.5f} — entries paused"
            )
            log.warning(f"{symbol}: {record.pause_reason}")
        else:
            if record.entry_paused and 'Spread' in record.pause_reason:
                record.entry_paused = False
                record.pause_reason = ''
                log.info(f"{symbol}: Spread normalized — entries resumed")

        # 3. State machine transitions
        active_box = record.alternative_box if record.using_alternative else record.active_box

        if record.state == BoxState.IN_BOX_RANGING:
            self._handle_ranging(record, current_price, current_bar, active_box)

        elif record.state == BoxState.BOX_BREACH_PENDING:
            self._handle_breach_pending(record, current_price, current_bar, active_box)

        elif record.state == BoxState.BOX_BROKEN_RECALCULATING:
            # This state is resolved by calling draw_alternative_box externally
            pass

        return record

    # ----------------------------------------------------------
    # STATE HANDLERS
    # ----------------------------------------------------------

    def _handle_ranging(self,
                          record: BoxStateRecord,
                          current_price: float,
                          current_bar: pd.Series,
                          box: DailyBox):
        """Checks for breach conditions from IN_BOX_RANGING state."""
        candle_close = float(current_bar['close'])
        breach_up = box.PDH * (1 + self.breach_buffer_pct)
        breach_dn = box.PDL * (1 - self.breach_buffer_pct)

        if candle_close > breach_up:
            # Transition to breach pending — upside
            record.state = BoxState.BOX_BREACH_PENDING
            record.breach_started_at = datetime.now().isoformat()
            record.breach_price = candle_close
            record.breach_direction = 'up'
            record.breach_candles_confirmed = 1
            record.entry_paused = True
            record.pause_reason = (
                f"Possible upside breach at {candle_close:.5f} "
                f"(PDH {box.PDH:.5f}) — monitoring"
            )
            log.info(f"{record.symbol}: BREACH PENDING (up) at {candle_close:.5f}")

        elif candle_close < breach_dn:
            # Transition to breach pending — downside
            record.state = BoxState.BOX_BREACH_PENDING
            record.breach_started_at = datetime.now().isoformat()
            record.breach_price = candle_close
            record.breach_direction = 'down'
            record.breach_candles_confirmed = 1
            record.entry_paused = True
            record.pause_reason = (
                f"Possible downside breach at {candle_close:.5f} "
                f"(PDL {box.PDL:.5f}) — monitoring"
            )
            log.info(f"{record.symbol}: BREACH PENDING (down) at {candle_close:.5f}")

    def _handle_breach_pending(self,
                                  record: BoxStateRecord,
                                  current_price: float,
                                  current_bar: pd.Series,
                                  box: DailyBox):
        """
        Determines if breach is false (return to box) or confirmed.
        """
        candle_close = float(current_bar['close'])

        # False break — price returned inside box
        if record.breach_direction == 'up' and candle_close <= box.PDH:
            record.state = BoxState.IN_BOX_RANGING
            record.breach_candles_confirmed = 0
            record.breach_started_at = None
            record.entry_paused = False
            record.pause_reason = ''
            log.info(
                f"{record.symbol}: False break confirmed — returned to box. "
                f"Note: repeated false breaks reinforce zone strength."
            )
            return

        if record.breach_direction == 'down' and candle_close >= box.PDL:
            record.state = BoxState.IN_BOX_RANGING
            record.breach_candles_confirmed = 0
            record.breach_started_at = None
            record.entry_paused = False
            record.pause_reason = ''
            log.info(f"{record.symbol}: False break confirmed — returned to box.")
            return

        # Still outside — increment confirmation count
        record.breach_candles_confirmed += 1

        # Check extension confirmation (big move = instant confirm)
        if record.breach_direction == 'up':
            extension = (candle_close - box.PDH) / box.PDH
        else:
            extension = (box.PDL - candle_close) / box.PDL

        instant_confirm = extension >= self.breach_extension_pct

        # Confirmed breach
        if record.breach_candles_confirmed >= self.breach_confirm_candles or instant_confirm:
            record.state = BoxState.BOX_BROKEN_RECALCULATING
            record.broken_wall_price = box.PDH if record.breach_direction == 'up' else box.PDL
            record.broken_wall_direction = record.breach_direction
            record.pause_reason = (
                f"Box CONFIRMED broken — {record.breach_direction} "
                f"({record.breach_candles_confirmed} candles, "
                f"extension {extension:.2%}). Recalculating..."
            )
            log.info(
                f"{record.symbol}: BOX BROKEN ({record.breach_direction}) "
                f"— drawing alternative box"
            )

    # ----------------------------------------------------------
    # ALTERNATIVE BOX DRAWING
    # ----------------------------------------------------------

    def draw_alternative_box(self,
                               symbol: str,
                               bars_5m: pd.DataFrame,
                               session_high: float,
                               session_low: float,
                               original_box: DailyBox,
                               total_overhead: float) -> AlternativeBoxResult:
        """
        Doug's alternative box protocol:

        Scenario A (broke up through PDH):
          Option 1: New PDH (session high) → Original PDH (becomes new PDL)
          Option 2: Session high → Session low (cheat code)
          Pick whichever gives better efficiency

        Scenario B (broke down through PDL):
          Option 1: Original PDL (becomes new PDH) → Session low
          Option 2: Session high → Session low (cheat code)

        Scenario C (both walls broken):
          Cheat code only: Session high → Session low
        """
        if symbol not in self.states:
            return AlternativeBoxResult(
                success=False, new_box=None,
                method='', viable=False,
                viability_reason='Symbol not found',
                efficiency_ratio=0
            )

        record = self.states[symbol]
        direction = record.breach_direction

        candidates = []

        if direction == 'up':
            # Option 1: Original PDH becomes new PDL
            new_pdh_a = session_high
            new_pdl_a = original_box.PDH
            if new_pdh_a > new_pdl_a:
                candidates.append(('original_wall_to_new_extreme', new_pdh_a, new_pdl_a))

        elif direction == 'down':
            # Option 1: Original PDL becomes new PDH
            new_pdh_b = original_box.PDL
            new_pdl_b = session_low
            if new_pdh_b > new_pdl_b:
                candidates.append(('original_wall_to_new_extreme', new_pdh_b, new_pdl_b))

        # Always add cheat code as option
        if session_high > session_low:
            candidates.append(('session_cheat_code', session_high, session_low))

        # Evaluate each candidate for viability
        best_result = None
        best_efficiency = 0

        current_price = float(bars_5m['close'].iloc[-1])

        for method, pdh, pdl in candidates:
            box_range = pdh - pdl
            half_box = box_range / 2
            net = half_box - total_overhead

            if half_box > 0:
                efficiency = net / total_overhead
            else:
                efficiency = 0

            viable = (
                net >= self._min_net_profit_for_symbol(symbol) and
                efficiency >= self.min_efficiency_ratio
            )

            if efficiency > best_efficiency:
                best_efficiency = efficiency
                # Build new DailyBox
                new_box = DailyBox(
                    symbol=symbol,
                    PDH=pdh,
                    PDL=pdl,
                    center=pdl + (pdh - pdl) / 2,
                    box_range=box_range,
                    half_box=half_box,
                    box_range_pct=box_range / current_price if current_price > 0 else 0,
                    ATR=original_box.ATR,
                    ATR_60pct=original_box.ATR * 0.60,
                    ATR_80pct=original_box.ATR * 0.80,
                    ATR_20pct=original_box.ATR * 0.20,
                    session_date=datetime.now().date().isoformat(),
                    draw_time=datetime.now().isoformat()
                )
                viability_reason = (
                    f"Net half-box: {net:.4f} | "
                    f"Efficiency: {efficiency:.2f}x | "
                    f"{'VIABLE' if viable else 'NOT VIABLE'}"
                )
                best_result = AlternativeBoxResult(
                    success=True,
                    new_box=new_box,
                    method=method,
                    viable=viable,
                    viability_reason=viability_reason,
                    efficiency_ratio=efficiency
                )

        if best_result is None:
            self._shelve_instrument(symbol, "Could not construct valid alternative box")
            return AlternativeBoxResult(
                success=False, new_box=None,
                method='', viable=False,
                viability_reason='No valid alternative box geometry',
                efficiency_ratio=0
            )

        if best_result.viable:
            # Activate new box
            record.alternative_box = best_result.new_box
            record.using_alternative = True
            record.state = BoxState.NEW_BOX_STANDBY
            record.new_box_trades = 0
            record.new_box_proven = False
            record.entry_paused = False
            record.pause_reason = ''
            log.info(
                f"{symbol}: New box active via {best_result.method} | "
                f"PDH: {best_result.new_box.PDH:.5f} | "
                f"PDL: {best_result.new_box.PDL:.5f} | "
                f"Efficiency: {best_result.efficiency_ratio:.2f}x"
            )
        else:
            self._shelve_instrument(symbol, best_result.viability_reason)

        return best_result

    # ----------------------------------------------------------
    # RETURN-TO-BOX SETUP
    # ----------------------------------------------------------

    def check_return_to_box(self,
                              symbol: str,
                              current_price: float) -> Dict:
        """
        After box is broken, price often returns to test the broken wall.
        Old resistance becomes new support (and vice versa).

        This is itself a tradeable setup per Doug's protocol.
        Returns setup info if applicable.
        """
        if symbol not in self.states:
            return {'setup_available': False}

        record = self.states[symbol]

        if record.broken_wall_price == 0:
            return {'setup_available': False}

        wall = record.broken_wall_price
        distance = abs(current_price - wall) / wall

        # Price returning to test the broken wall
        if distance < 0.002:  # Within 0.2% of broken wall
            if record.broken_wall_direction == 'up':
                # Broke up, returning to test PDH as support
                return {
                    'setup_available': True,
                    'direction': 'long',
                    'zone_price': wall,
                    'zone_type': 'broken_resistance_now_support',
                    'description': (
                        f"Price returning to test broken PDH {wall:.5f} as new support. "
                        f"Classic return-to-box long setup."
                    )
                }
            else:
                # Broke down, returning to test PDL as resistance
                return {
                    'setup_available': True,
                    'direction': 'short',
                    'zone_price': wall,
                    'zone_type': 'broken_support_now_resistance',
                    'description': (
                        f"Price returning to test broken PDL {wall:.5f} as new resistance. "
                        f"Classic return-to-box short setup."
                    )
                }

        return {'setup_available': False}

    # ----------------------------------------------------------
    # NEW BOX PROVEN TRACKING
    # ----------------------------------------------------------

    def record_new_box_trade_result(self, symbol: str, was_winner: bool):
        """Track wins in new box to determine when it's proven."""
        if symbol not in self.states:
            return
        record = self.states[symbol]
        if was_winner:
            record.new_box_trades += 1
            if record.new_box_trades >= self.new_box_proven_trades:
                record.new_box_proven = True
                log.info(
                    f"{symbol}: New box PROVEN after "
                    f"{record.new_box_trades} successful trades — "
                    f"full position sizing restored"
                )

    def get_position_size_multiplier(self, symbol: str) -> float:
        """
        Returns position size multiplier based on box state.
        Reduces size in new/unproven box.
        """
        if symbol not in self.states:
            return 1.0
        record = self.states[symbol]

        if record.state == BoxState.NEW_BOX_STANDBY and not record.new_box_proven:
            # Scale up as box proves itself
            return 0.5 + (record.new_box_trades / self.new_box_proven_trades) * 0.5
        elif record.age_confidence_pct < 70:
            # Reduce size as box ages
            return record.age_confidence_pct / 100
        return 1.0

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------

    def _get_age_confidence(self, hours: float) -> float:
        """Interpolates confidence from age schedule."""
        hours_sorted = sorted(self.age_confidence.keys())
        for i in range(len(hours_sorted) - 1):
            h1, h2 = hours_sorted[i], hours_sorted[i+1]
            if h1 <= hours <= h2:
                c1, c2 = self.age_confidence[h1], self.age_confidence[h2]
                t = (hours - h1) / (h2 - h1)
                return c1 + t * (c2 - c1)
        if hours < hours_sorted[0]:
            return self.age_confidence[hours_sorted[0]]
        return self.age_confidence[hours_sorted[-1]]

    def _shelve_instrument(self, symbol: str, reason: str):
        if symbol not in self.states:
            return
        record = self.states[symbol]
        record.state = BoxState.SHELVED
        record.entry_paused = True
        record.pause_reason = f"Shelved: {reason}"
        log.info(f"{symbol}: SHELVED — {reason}")

    def get_state(self, symbol: str) -> Optional[BoxStateRecord]:
        return self.states.get(symbol)

    def is_tradeable(self, symbol: str) -> Tuple[bool, str]:
        """Quick check: can we trade this instrument right now?"""
        if symbol not in self.states:
            return False, "Not initialized"
        record = self.states[symbol]
        if record.state == BoxState.SHELVED:
            return False, record.pause_reason
        if record.entry_paused:
            return False, record.pause_reason
        if record.state == BoxState.BOX_BREACH_PENDING:
            return False, "Box breach pending — monitoring"
        if record.state == BoxState.BOX_BROKEN_RECALCULATING:
            return False, "Box broken — recalculating"
        return True, "Tradeable"
