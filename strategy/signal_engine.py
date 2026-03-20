# ============================================================
# BOXBOT — SIGNAL CONFIRMATION ENGINE
# Implements Doug & Sylvia's exact confirmation rules:
#
#   PRIMARY TRIGGER:
#     Pitchfork candlestick (3-5 directional candles
#     → first reversal candle → entry on break)
#
#   NAMED SIGNALS:
#     John Wick (hammer/inverted hammer at zone extreme)
#     Power of Towers (full engulfing candle)
#
#   FILTERS (must pass before pitchfork matters):
#     Sucker Move (fast explosive move into zone)
#     ATR Consumption (60%+ of daily ATR consumed)
#     Zone Quality (tier check)
#     OTR (overhead-to-range ratio)
#
#   TIMEFRAME SELECTION:
#     Normal: 5-minute pitchfork
#     High volatility: 1-hour pitchfork acceptable
# ============================================================

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict
from datetime import datetime
import logging
log = logging.getLogger('mr-scrooge')

from strategy.zone_detector import ZoneMap, MagicLine, DailyBox


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class SuckerMoveResult:
    detected: bool
    direction: str              # 'up' (into sell zone) or 'down' (into buy zone)
    candle_count: int           # How many directional candles
    atr_consumed_pct: float     # % of daily ATR consumed by this move
    atr_threshold_met: bool     # >= 60% ATR
    atr_strength: str           # 'strong' (80%+), 'valid' (60-80%), 'weak' (<60%)
    move_size: float            # Absolute price move
    terminating_at_zone: bool   # Did it end at a known magic line?
    zone_hit: Optional[MagicLine]
    rejection_reason: str


@dataclass
class PitchforkResult:
    detected: bool
    signal_type: str            # 'pitchfork', 'john_wick', 'power_of_towers'
    direction: str              # 'long' or 'short'
    directional_candles: int    # 3-5 candles into zone
    trigger_price: float        # Break of reversal candle high/low = entry
    stop_price: float           # Stop goes just beyond reversal candle
    reversal_candle_idx: int    # Index in bar array
    confidence: str             # 'HIGH', 'MODERATE', 'LOW'
    timeframe_used: str         # '5M' or '1H'
    details: str


@dataclass
class SignalResult:
    """
    Complete signal evaluation for one instrument at one moment.
    All gates must pass for an entry to be triggered.
    """
    symbol: str
    timestamp: str
    action: str                 # 'ENTER_LONG', 'ENTER_SHORT', 'WAIT', 'NO_TRADE'

    # Gate results
    zone_gate: bool             # Is price at a valid zone?
    zone_tier: int              # Tier of the zone (1=best)
    zone_details: str

    sucker_move_gate: bool      # Is there a valid sucker move?
    sucker_move: Optional[SuckerMoveResult]

    atr_gate: bool              # Does ATR consumption qualify?
    atr_consumed_pct: float
    atr_strength: str

    pitchfork_gate: bool        # Is there a valid confirmation candle?
    pitchfork: Optional[PitchforkResult]

    otr_gate: bool              # Is overhead-to-range ratio acceptable?
    otr_ratio: float            # Overhead / half-box
    otr_category: str           # 'excellent', 'acceptable', 'marginal', 'skip'

    # Entry details (populated only when action is ENTER_*)
    entry_price: float
    stop_price: float
    tp1_price: float            # First partial — next magic line
    tp2_price: float            # Second partial — center or further
    risk_points: float
    reward_points: float
    risk_reward_ratio: float

    # Rejection chain
    rejection_reasons: List[str]
    all_gates_passed: bool

    # Open wait instructions
    wait_for: str               # What to watch for next

    # Quality score
    score: int = 0             # Opportunity quality score 0-100 (higher = better setup)


# ============================================================
# SIGNAL ENGINE
# ============================================================

class SignalEngine:
    """
    Evaluates all confirmation gates in sequence.
    All gates must pass for an entry signal to be generated.

    Gate order (fail-fast, cheapest checks first):
    1. Zone gate — is price at a magic line?
    2. Middle filter — reject if price in middle
    3. ATR gate — has 60%+ of daily range been consumed?
    4. Sucker move gate — was the approach fast and explosive?
    5. Pitchfork gate — is there a reversal candle?
    6. OTR gate — is overhead acceptable for this box?
    """

    def __init__(self, config: dict):
        # ATR thresholds
        self.atr_min_pct = config.get('atr_min_pct', 0.20)         # 20% — ignore below this
        self.atr_valid_pct = config.get('atr_valid_pct', 0.60)      # 60% — valid setup
        self.atr_strong_pct = config.get('atr_strong_pct', 0.80)    # 80% — strong setup

        # Sucker move parameters
        self.sucker_min_candles = config.get('sucker_min_candles', 3)
        self.sucker_max_candles = config.get('sucker_max_candles', 5)
        self.sucker_zone_tolerance = config.get('sucker_zone_tolerance', 0.005)  # 0.5%

        # Pitchfork parameters
        self.pitchfork_directional_min = config.get('pitchfork_directional_min', 3)
        self.pitchfork_directional_max = config.get('pitchfork_directional_max', 5)

        # OTR thresholds — widened 2026-03-02 (GBP_JPY at 15% was always blocked)
        self.otr_excellent = config.get('otr_excellent', 0.05)       # <5% — trade freely
        self.otr_acceptable = config.get('otr_acceptable', 0.15)     # 5-15% — normal rules
        self.otr_marginal = config.get('otr_marginal', 0.25)         # 15-25% — all 3 gates required
        # >25% = skip

        # Minimum risk/reward ratio before entering
        self.min_risk_reward = config.get('min_risk_reward', 1.5)

        # Minimum SL distance — prevents fakeout stops on very tight reversal candles
        self.sl_floor_pct = config.get('sl_floor_pct', 0.0030)  # 0.30% default

        # Fixed pip targets
        self.sl_pips   = config.get('sl_pips',   10)   # pips beyond zone for stop
        self.tp1_pips  = config.get('tp1_pips',  5)    # pips from entry for TP1 (ratchet trigger)
        self.tp2_pips  = config.get('tp2_pips',  30)   # pips from entry for TP2 (full exit)

        # Zone proximity — how close to a magic line to be "at zone"
        self.zone_proximity_pct = config.get('zone_proximity_pct', 0.001)  # 0.1% — must be tight to zone

        # Open wait window (minutes)
        self.open_wait_minutes = config.get('open_wait_minutes', 15)

        # Volatility flag threshold (ATR multiplier)
        self.high_volatility_threshold = config.get('high_volatility_atr_mult', 2.0)

    # ----------------------------------------------------------
    # MASTER EVALUATION
    # ----------------------------------------------------------

    def evaluate(self,
                  symbol: str,
                  zone_map: ZoneMap,
                  bars_5m: pd.DataFrame,
                  bars_1h: pd.DataFrame,
                  total_overhead: float,
                  minutes_since_open: int) -> SignalResult:
        """
        Master evaluation function. Runs all gates in sequence.

        Args:
            zone_map: Built by ZoneDetector
            bars_5m: Recent 5-minute bars (at least 20)
            bars_1h: Recent 1-hour bars (at least 10)
            total_overhead: All-in round trip cost in price units
            minutes_since_open: Minutes elapsed since session open
        """
        rejection_reasons = []
        current_price = zone_map.current_price
        daily_box = zone_map.daily_box

        # --- Gate 1: Zone ---
        zone_gate, zone_tier, zone_details, active_zone = self._check_zone_gate(
            current_price, zone_map, symbol=symbol
        )
        if not zone_gate:
            rejection_reasons.append(zone_details)
            return self._no_trade(symbol, zone_gate, zone_tier, zone_details,
                                  rejection_reasons, "Wait for price to reach a magic line zone")

        # Direction from active zone type — computed here (before ATR gate and trend filter)
        # so both gates can use it. Previously sat at Gate 4; moved up.
        direction = 'short' if (active_zone and active_zone.zone_type in ('resistance', 'PDH', 'PHH', 'sell_zone')) \
                    else ('short' if zone_map.current_zone in ('sell_zone', 'above_box') else 'long')

        # --- Gate 2: Trend regime filter (Tier 3/4 zones only) ---
        # MASTER_THEORY: daily bias provides context but box location decides the trade.
        # "Because we're at the lower part of the box I would prefer to trade to the
        # long side trying to catch a bigger bounce" — even in a downtrend.
        # Only block counter-trend entries at low-quality zones (Tier 3/4).
        # Tier 1/2 zones at box extremes are strong enough to trade against trend.
        regime = self._get_trend_regime(bars_1h)
        if (direction == 'short' and regime == 'uptrend') or \
           (direction == 'long' and regime == 'downtrend'):
            if zone_tier >= 3:
                msg = f"Counter-trend: {direction} blocked at Tier {zone_tier} zone — 50H EMA in {regime}"
                rejection_reasons.append(msg)
                return self._no_trade(symbol, zone_gate, zone_tier, zone_details,
                                      rejection_reasons,
                                      f"Trend {regime} — counter-trend only valid at Tier 1/2 zones")
            log.info(f"{symbol}: Counter-trend {direction} allowed — Tier {zone_tier} zone overrides {regime}")

        # --- Gate 3: ATR (advisory only) ---
        # MASTER_THEORY: ATR is used to set expectations, not as a hard gate.
        # "We begin every single day by looking at the daily range... you need to
        # set your expectations." No threshold blocks a trade — ATR informs signal
        # quality only (reflected in atr_strength: weak/valid/strong).
        atr_gate, atr_pct, atr_strength = self._check_atr_gate(
            bars_5m, direction, daily_box
        )
        # ATR gate is now always True — it only affects signal quality metadata.
        atr_gate = True

        # --- Gate 4: Sucker move ---
        sucker = self._check_sucker_move(
            bars_5m, direction, active_zone, daily_box
        )
        if not sucker.detected:
            rejection_reasons.append(f"No sucker move: {sucker.rejection_reason}")
            return self._wait_result(symbol, zone_gate, zone_tier, zone_details,
                                     atr_gate, atr_pct, atr_strength,
                                     sucker, rejection_reasons,
                                     "Sucker move not yet formed — watch for explosive directional push into zone")

        if not sucker.terminating_at_zone:
            rejection_reasons.append("Sucker move not terminating at zone — move didn't end at a pre-identified magic line")
            return self._wait_result(symbol, zone_gate, zone_tier, zone_details,
                                     atr_gate, atr_pct, atr_strength,
                                     sucker, rejection_reasons,
                                     "Sucker move present but not at zone — wait for move to reach a magic line")

        # --- Gate 5: Pitchfork ---
        is_high_volatility = self._is_high_volatility(bars_1h, daily_box)
        timeframe_bars = bars_1h if is_high_volatility else bars_5m
        timeframe_label = '1H' if is_high_volatility else '5M'

        # Box stop: use the zone wall we're trading from
        # If active zone is the prior hour high/low, stop just outside that. Else use daily wall.
        if active_zone and active_zone.zone_type in ('PHH', 'PHL'):
            box_stop_val = zone_map.prior_hour_high if direction == 'short' else zone_map.prior_hour_low
        else:
            box_stop_val = daily_box.PDH if direction == 'short' else daily_box.PDL

        pitchfork = self._check_pitchfork(
            timeframe_bars, direction, timeframe_label,
            zone_price=active_zone.price if active_zone else None,
            box_stop=box_stop_val
        )
        if not pitchfork.detected:
            rejection_reasons.append(f"No pitchfork: {pitchfork.details}")
            return self._wait_result(symbol, zone_gate, zone_tier, zone_details,
                                     atr_gate, atr_pct, atr_strength,
                                     sucker, rejection_reasons,
                                     "Sucker move present — waiting for pitchfork reversal candle")

        # --- Gate 6: OTR — skip if round-trip spread > otr_marginal of half-box ---
        half_box = zone_map.daily_box.half_box
        if half_box <= 0:
            rejection_reasons.append("Invalid box: zero half-box width")
            return self._no_trade(symbol, zone_gate, zone_tier, zone_details,
                                  rejection_reasons, "Box half-width is zero")

        otr_gate, otr_ratio, otr_category = self._check_otr_gate(
            total_overhead, half_box
        )
        if not otr_gate:
            msg = f"OTR {otr_ratio:.1%} > {self.otr_marginal:.1%} — spread too costly for box"
            rejection_reasons.append(msg)
            return self._no_trade(symbol, zone_gate, zone_tier, zone_details,
                                  rejection_reasons, msg)

        # --- All gates passed — build entry signal ---
        # Fixed pip structure: SL/TP1/TP2 measured from trigger price.
        # Actual values get recalculated from fill price in _fire_entry_candidate.
        _center  = zone_map.daily_box.center
        _pip     = 0.01 if _center > 20 else 0.0001
        if direction == 'long':
            fixed_stop = pitchfork.trigger_price - self.sl_pips * _pip
        else:
            fixed_stop = pitchfork.trigger_price + self.sl_pips * _pip

        tp1, tp2 = self._calculate_targets(
            direction, pitchfork.trigger_price, zone_map
        )

        risk_points = abs(pitchfork.trigger_price - fixed_stop)
        reward_points = abs(tp2 - pitchfork.trigger_price)
        rr = reward_points / risk_points if risk_points > 0 else 0

        if rr < self.min_risk_reward:
            msg = f"RR {rr:.2f} < minimum {self.min_risk_reward:.1f} — insufficient reward"
            rejection_reasons.append(msg)
            return self._no_trade(symbol, zone_gate, zone_tier, zone_details,
                                  rejection_reasons, "Wait for better entry with higher reward")

        action = 'ENTER_LONG' if direction == 'long' else 'ENTER_SHORT'

        _result = SignalResult(
            symbol=symbol,
            timestamp=datetime.now().isoformat(),
            action=action,
            zone_gate=True,
            zone_tier=zone_tier,
            zone_details=zone_details,
            sucker_move_gate=True,
            sucker_move=sucker,
            atr_gate=True,
            atr_consumed_pct=atr_pct,
            atr_strength=atr_strength,
            pitchfork_gate=True,
            pitchfork=pitchfork,
            otr_gate=True,
            otr_ratio=otr_ratio,
            otr_category=otr_category,
            entry_price=pitchfork.trigger_price,
            stop_price=fixed_stop,
            tp1_price=tp1,
            tp2_price=tp2,
            risk_points=risk_points,
            reward_points=reward_points,
            risk_reward_ratio=rr,
            rejection_reasons=[],
            all_gates_passed=True,
            wait_for=''
        )
        _result.score = self._calculate_score(_result)
        return _result


    # ----------------------------------------------------------
    # SCORING: Opportunity quality score (0-100)
    # ----------------------------------------------------------

    def _calculate_score(self, sig) -> int:
        """Score a qualifying signal. Higher = better trade setup."""
        pts = 0
        # Zone tier (1=best)
        pts += {1: 30, 2: 20, 3: 10}.get(sig.zone_tier, 5)
        # ATR strength
        pts += {'strong': 20, 'valid': 15, 'weak': 5}.get(sig.atr_strength, 5)
        # OTR category
        pts += {'excellent': 15, 'acceptable': 10, 'marginal': 5}.get(sig.otr_category, 0)
        # Risk/reward ratio
        if sig.risk_reward_ratio >= 3.0:
            pts += 15
        elif sig.risk_reward_ratio >= 2.0:
            pts += 10
        elif sig.risk_reward_ratio >= 1.5:
            pts += 5
        # Pitchfork type: named signals are higher quality
        if sig.pitchfork:
            if sig.pitchfork.signal_type in ('john_wick', 'power_of_towers'):
                pts += 10
            else:
                pts += 5
            conf_pts = {'HIGH': 5, 'MODERATE': 3}
            pts += conf_pts.get(sig.pitchfork.confidence, 0)
        return pts

    # ----------------------------------------------------------
    # GATE 1: ZONE CHECK
    # ----------------------------------------------------------

    def _check_zone_gate(self,
                          current_price: float,
                          zone_map: ZoneMap,
                          symbol: str = '') -> Tuple[bool, int, str, Optional[MagicLine]]:
        """
        Is current price within proximity of a magic line?
        Checks: magic lines, daily PDH/PDL, and prior hour high/low.
        Returns (gate_passed, tier, details, matching_zone)
        Percentage-based proximity already normalises for price magnitude, so
        JPY pairs use the same base pct — just 2× to allow a few extra pips of
        noise (was 5×/0.5% which permitted 79 pips from zone at USD/JPY 158).
        """
        # JPY: 2× zone_proximity_pct (~0.2% ≈ 31 pips at 158) vs non-JPY (~0.1% ≈ 11 pips at 1.16)
        proximity = self.zone_proximity_pct * 2 if 'JPY' in symbol else self.zone_proximity_pct

        best_zone = None
        best_tier = 99

        all_zones = zone_map.sell_zones + zone_map.buy_zones

        for zone in all_zones:
            distance_pct = abs(current_price - zone.price) / current_price
            if distance_pct <= proximity:
                if zone.tier < best_tier:
                    best_tier = zone.tier
                    best_zone = zone

        now_str = datetime.now().isoformat()

        # Also check PDH/PDL directly — always Tier 1
        for level, label in [(zone_map.daily_box.PDH, 'PDH'), (zone_map.daily_box.PDL, 'PDL')]:
            if abs(current_price - level) / current_price <= proximity:
                if best_zone is None or best_tier > 1:
                    best_tier = 1
                    best_zone = MagicLine(price=level, zone_type=label, tier=1,
                                         test_count=0, strength_score=100,
                                         last_tested=now_str, daily_aligned=True,
                                         is_breakout_flip=False,
                                         source_timeframe='D', symbol=zone_map.symbol,
                                         created_at=now_str)

        # Check prior hour high/low — Tier 2 if not already covered
        for level, label in [(zone_map.prior_hour_high, 'PHH'), (zone_map.prior_hour_low, 'PHL')]:
            if abs(current_price - level) / current_price <= proximity:
                if best_zone is None or best_tier > 2:
                    best_tier = 2
                    best_zone = MagicLine(price=level, zone_type=label, tier=2,
                                         test_count=0, strength_score=80,
                                         last_tested=now_str, daily_aligned=False,
                                         is_breakout_flip=False,
                                         source_timeframe='1H', symbol=zone_map.symbol,
                                         created_at=now_str)

        if best_zone is None:
            return False, 0, f"Price {current_price:.5f} not near any magic line zone", None

        details = (
            f"Tier {best_zone.tier} zone at {best_zone.price:.5f} "
            f"({best_zone.zone_type}, strength {best_zone.strength_score:.0f}/100)"
        )
        return True, best_zone.tier, details, best_zone

    # ----------------------------------------------------------
    # TREND REGIME FILTER
    # ----------------------------------------------------------

    def _get_trend_regime(self, bars_1h: pd.DataFrame) -> str:
        """
        Classifies the broader trend using a 50-period EMA slope on 1H closes.
        Returns 'uptrend', 'downtrend', or 'ranging'.

        Gate logic in evaluate():
          - Short blocked in uptrend
          - Long  blocked in downtrend
          - Ranging → both directions allowed

        Slope is computed over the last 10 EMA values (≈10 hours) to catch
        slower-moving trends. Threshold of 0.05% per 10 candles filters genuine
        directional bias while ignoring ranging chop.

        HISTORY: Original threshold was 0.001 over 5 bars. EUR/CAD and EUR/USD
        longs entered during a clear EUR downtrend — the 5-bar slope was -0.0008,
        just below the 0.001 threshold. Switched to 10-bar lookback at 0.0005
        threshold to catch sustained slow trends that 5-bar window misses.
        """
        if len(bars_1h) < 62:   # 50 EMA needs 50+ bars; add buffer for 10-bar slope
            return 'ranging'

        closes = bars_1h['close'].values
        ema50 = pd.Series(closes).ewm(span=50, adjust=False).mean().values

        # Slope as % change over last 10 periods (≈10 hours) — catches slow trends
        base = ema50[-11]
        if base == 0:
            return 'ranging'
        slope = (ema50[-1] - base) / base

        if slope > 0.0005:      # +0.05% rise over 10 hrs → uptrend
            return 'uptrend'
        elif slope < -0.0005:   # −0.05% fall over 10 hrs → downtrend
            return 'downtrend'
        else:
            return 'ranging'

    # ----------------------------------------------------------
    # GATE 3: ATR CONSUMPTION
    # ----------------------------------------------------------

    def _check_atr_gate(self,
                         bars_5m: pd.DataFrame,
                         direction: str,
                         daily_box: DailyBox) -> Tuple[bool, float, str]:
        """
        Has the session consumed enough of the daily ATR?

        Measures the *directional* leg only — not the full session high-low range.
        The old range measure over-counted: if price went up 50 pips then reversed
        50 pips, session_range = 100 even though each leg was only 50. We only care
        about the leg in the direction of the trade (the sucker move leg).

          Short trade: price pushed UP into resistance → measure session_high - close
          Long  trade: price pushed DOWN into support  → measure close - session_low
        """
        if len(bars_5m) < 2:
            return False, 0.0, 'weak'

        if daily_box.ATR == 0:
            return False, 0.0, 'weak'

        current_price = bars_5m['close'].iloc[-1]
        if direction == 'short':
            # How far up price moved to reach the resistance zone
            directional_move = bars_5m['high'].max() - current_price
        else:
            # How far down price moved to reach the support zone
            directional_move = current_price - bars_5m['low'].min()

        consumed_pct = max(directional_move, 0.0) / daily_box.ATR

        if consumed_pct >= self.atr_strong_pct:
            strength = 'strong'
        elif consumed_pct >= self.atr_valid_pct:
            strength = 'valid'
        else:
            strength = 'weak'

        gate_passed = consumed_pct >= self.atr_min_pct

        return gate_passed, consumed_pct, strength

    # ----------------------------------------------------------
    # GATE 4: SUCKER MOVE
    # ----------------------------------------------------------

    def _check_sucker_move(self,
                             bars: pd.DataFrame,
                             direction: str,
                             target_zone: Optional[MagicLine],
                             daily_box: DailyBox) -> SuckerMoveResult:
        """
        Detects the sucker move: fast explosive directional push into zone.

        For shorts: 3-5 consecutive green candles pushing UP into sell zone
        For longs: 3-5 consecutive red candles pushing DOWN into buy zone
        """
        if len(bars) < self.sucker_max_candles + 2:
            return SuckerMoveResult(
                detected=False, direction=direction,
                candle_count=0, atr_consumed_pct=0,
                atr_threshold_met=False, atr_strength='weak',
                move_size=0, terminating_at_zone=False,
                zone_hit=None,
                rejection_reason='Insufficient bars to evaluate sucker move'
            )

        closes = bars['close'].values
        opens = bars['open'].values

        best_count = 0
        best_start_idx = -1

        # MASTER_THEORY: the sucker move does not need to be perfectly clean.
        # "Sometimes it can look a little choppier... red green green red green."
        # Flexible window: count directional candles within a (max_candles+2) bar window,
        # allowing intermixed non-directional candles as long as net price movement is correct.
        lookback = min(len(bars) - 1, 25)
        search_window = self.sucker_max_candles + 2  # 7-bar window allows choppy moves

        for start in range(len(bars) - lookback, len(bars) - self.sucker_min_candles):
            end = min(start + search_window, len(bars) - 1)
            directional = 0
            for i in range(start, end):
                if direction == 'short':
                    if closes[i] > opens[i]:
                        directional += 1
                else:
                    if closes[i] < opens[i]:
                        directional += 1

            # Net movement must confirm the move was into the zone
            if direction == 'short':
                net_move_correct = closes[end - 1] > opens[start]  # Price pushed up into resistance
            else:
                net_move_correct = closes[end - 1] < opens[start]  # Price pushed down into support

            if directional >= self.sucker_min_candles and net_move_correct:
                if directional > best_count:
                    best_count = directional
                    best_start_idx = start

        if best_count < self.sucker_min_candles:
            return SuckerMoveResult(
                detected=False, direction=direction,
                candle_count=0, atr_consumed_pct=0,
                atr_threshold_met=False, atr_strength='weak',
                move_size=0, terminating_at_zone=False,
                zone_hit=None,
                rejection_reason=f'Need {self.sucker_min_candles}-{self.sucker_max_candles} '
                                  f'consecutive {"green" if direction == "short" else "red"} '
                                  f'candles — found {best_count}'
            )

        search_end = min(best_start_idx + (self.sucker_max_candles + 2), len(bars))
        if direction == 'short':
            move_start = float(bars['low'].iloc[best_start_idx])
            # Peak of the sucker move = highest high across the full search window
            move_end = float(bars['high'].iloc[best_start_idx:search_end].max())
        else:
            move_start = float(bars['high'].iloc[best_start_idx])
            # Trough of the sucker move = lowest low across the full search window
            move_end = float(bars['low'].iloc[best_start_idx:search_end].min())

        move_size = abs(move_end - move_start)
        atr_consumed = move_size / daily_box.ATR if daily_box.ATR > 0 else 0

        if atr_consumed >= self.atr_strong_pct:
            strength = 'strong'
        elif atr_consumed >= self.atr_valid_pct:
            strength = 'valid'
        else:
            strength = 'weak'

        terminating = False
        zone_hit = None
        if target_zone is not None:
            end_price = move_end
            if abs(end_price - target_zone.price) / target_zone.price < self.sucker_zone_tolerance:
                terminating = True
                zone_hit = target_zone

        return SuckerMoveResult(
            detected=True,
            direction=direction,
            candle_count=best_count,
            atr_consumed_pct=atr_consumed,
            atr_threshold_met=atr_consumed >= self.atr_valid_pct,
            atr_strength=strength,
            move_size=move_size,
            terminating_at_zone=terminating,
            zone_hit=zone_hit,
            rejection_reason=''
        )

    # ----------------------------------------------------------
    # GATE 5: PITCHFORK / JOHN WICK / POWER OF TOWERS
    # ----------------------------------------------------------

    def _check_pitchfork(self,
                          bars: pd.DataFrame,
                          direction: str,
                          timeframe: str,
                          zone_price: float = None,
                          box_stop: float = None) -> PitchforkResult:
        """
        Checks for Doug & Sylvia's three named confirmation signals.
        Priority: John Wick → Power of Towers → Standard Pitchfork
        zone_price: the magic line price — used as entry trigger
        box_stop: PDH (for shorts) or PDL (for longs) — box wall used as stop reference
        """
        if len(bars) < 4:
            return PitchforkResult(
                detected=False, signal_type='none',
                direction=direction, directional_candles=0,
                trigger_price=0, stop_price=0,
                reversal_candle_idx=-1,
                confidence='LOW', timeframe_used=timeframe,
                details='Insufficient bars'
            )

        wick_result = self._detect_john_wick(bars, direction, timeframe, zone_price, box_stop)
        if wick_result.detected:
            return wick_result

        tower_result = self._detect_power_of_towers(bars, direction, timeframe, zone_price, box_stop)
        if tower_result.detected:
            return tower_result

        return self._detect_standard_pitchfork(bars, direction, timeframe, zone_price, box_stop)

    def _detect_john_wick(self,
                           bars: pd.DataFrame,
                           direction: str,
                           timeframe: str,
                           zone_price: float = None,
                           box_stop: float = None) -> PitchforkResult:
        """John Wick = hammer/inverted hammer with excessive wick at zone extreme."""
        for i in range(len(bars) - 4, len(bars) - 1):
            if i < 0:
                continue

            o = float(bars['open'].iloc[i])
            h = float(bars['high'].iloc[i])
            l = float(bars['low'].iloc[i])
            c = float(bars['close'].iloc[i])

            body = abs(c - o)
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l
            total_range = h - l

            if total_range == 0:
                continue

            if direction == 'long':
                wick_ratio = lower_wick / total_range
                body_pct = body / total_range
                if wick_ratio >= 0.60 and body_pct <= 0.30 and lower_wick >= body * 2:
                    # Require at least 1 confirmation candle closing up after the wick
                    confirmed = any(
                        float(bars['close'].iloc[j]) > float(bars['open'].iloc[j])
                        for j in range(i + 1, len(bars))
                    )
                    if not confirmed:
                        continue
                    # MASTER_THEORY: "entry happens above the high of that candle,
                    # stop-loss placed slightly below." Use candle extreme, not box wall.
                    trigger = h * 1.0001
                    stop = l * (1 - self.sl_floor_pct)
                    stop = self._apply_sl_floor(trigger, stop, direction)
                    return PitchforkResult(
                        detected=True, signal_type='john_wick', direction='long',
                        directional_candles=0, trigger_price=trigger, stop_price=stop,
                        reversal_candle_idx=i, confidence='HIGH', timeframe_used=timeframe,
                        details=f'John Wick hammer: wick {wick_ratio:.0%} of range + confirmation'
                    )
            else:
                wick_ratio = upper_wick / total_range
                body_pct = body / total_range
                if wick_ratio >= 0.60 and body_pct <= 0.30 and upper_wick >= body * 2:
                    # Require at least 1 confirmation candle closing down after the wick
                    confirmed = any(
                        float(bars['close'].iloc[j]) < float(bars['open'].iloc[j])
                        for j in range(i + 1, len(bars))
                    )
                    if not confirmed:
                        continue
                    # MASTER_THEORY: "entry below low of candle, stop slightly above."
                    trigger = l * 0.9999
                    stop = h * (1 + self.sl_floor_pct)
                    stop = self._apply_sl_floor(trigger, stop, direction)
                    return PitchforkResult(
                        detected=True, signal_type='john_wick', direction='short',
                        directional_candles=0, trigger_price=trigger, stop_price=stop,
                        reversal_candle_idx=i, confidence='HIGH', timeframe_used=timeframe,
                        details=f'John Wick inverted hammer: wick {wick_ratio:.0%} of range + confirmation'
                    )

        return PitchforkResult(
            detected=False, signal_type='none', direction=direction,
            directional_candles=0, trigger_price=0, stop_price=0,
            reversal_candle_idx=-1, confidence='LOW',
            timeframe_used=timeframe, details='No John Wick pattern found'
        )

    def _detect_power_of_towers(self,
                                  bars: pd.DataFrame,
                                  direction: str,
                                  timeframe: str,
                                  zone_price: float = None,
                                  box_stop: float = None) -> PitchforkResult:
        """Power of Towers = full engulfing candle (including wicks)."""
        for i in range(1, len(bars) - 1):
            prev_h = float(bars['high'].iloc[i-1])
            prev_l = float(bars['low'].iloc[i-1])
            prev_o = float(bars['open'].iloc[i-1])
            prev_c = float(bars['close'].iloc[i-1])

            curr_h = float(bars['high'].iloc[i])
            curr_l = float(bars['low'].iloc[i])
            curr_o = float(bars['open'].iloc[i])
            curr_c = float(bars['close'].iloc[i])

            if direction == 'long':
                if (prev_c < prev_o and curr_c > curr_o and
                        curr_l < prev_l and curr_h > prev_h):
                    # MASTER_THEORY: enter above the engulfing candle, stop below it.
                    trigger = curr_h * 1.0001
                    stop = curr_l * (1 - self.sl_floor_pct)
                    stop = self._apply_sl_floor(trigger, stop, direction)
                    return PitchforkResult(
                        detected=True, signal_type='power_of_towers', direction='long',
                        directional_candles=0,
                        trigger_price=trigger, stop_price=stop,
                        reversal_candle_idx=i, confidence='HIGH', timeframe_used=timeframe,
                        details='Power of Towers: green engulfs full range of prior red candle'
                    )
            else:
                if (prev_c > prev_o and curr_c < curr_o and
                        curr_h > prev_h and curr_l < prev_l):
                    # MASTER_THEORY: enter below the engulfing candle, stop above it.
                    trigger = curr_l * 0.9999
                    stop = curr_h * (1 + self.sl_floor_pct)
                    stop = self._apply_sl_floor(trigger, stop, direction)
                    return PitchforkResult(
                        detected=True, signal_type='power_of_towers', direction='short',
                        directional_candles=0,
                        trigger_price=trigger, stop_price=stop,
                        reversal_candle_idx=i, confidence='HIGH', timeframe_used=timeframe,
                        details='Power of Towers: red engulfs full range of prior green candle'
                    )

        return PitchforkResult(
            detected=False, signal_type='none', direction=direction,
            directional_candles=0, trigger_price=0, stop_price=0,
            reversal_candle_idx=-1, confidence='LOW',
            timeframe_used=timeframe, details='No Power of Towers pattern found'
        )

    def _detect_standard_pitchfork(self,
                                     bars: pd.DataFrame,
                                     direction: str,
                                     timeframe: str,
                                     zone_price: float = None,
                                     box_stop: float = None) -> PitchforkResult:
        """Standard pitchfork: 3-5 directional candles → first reversal candle."""
        closes = bars['close'].values
        opens = bars['open'].values
        highs = bars['high'].values
        lows = bars['low'].values

        for end_idx in range(len(bars) - 2, self.pitchfork_directional_min, -1):
            reversal_candle_idx = end_idx

            if direction == 'long':
                is_reversal = closes[reversal_candle_idx] > opens[reversal_candle_idx]
            else:
                is_reversal = closes[reversal_candle_idx] < opens[reversal_candle_idx]

            if not is_reversal:
                continue

            directional_count = 0
            for k in range(reversal_candle_idx - 1,
                           max(reversal_candle_idx - self.pitchfork_directional_max - 1, -1),
                           -1):
                if direction == 'long':
                    if closes[k] < opens[k]:
                        directional_count += 1
                    else:
                        break
                else:
                    if closes[k] > opens[k]:
                        directional_count += 1
                    else:
                        break

            if directional_count >= self.pitchfork_directional_min:
                # MASTER_THEORY: "entry above the high of the reversal candle, stop below it."
                if direction == 'long':
                    trigger = highs[reversal_candle_idx] * 1.0001
                    stop = lows[reversal_candle_idx] * (1 - self.sl_floor_pct)
                else:
                    trigger = lows[reversal_candle_idx] * 0.9999
                    stop = highs[reversal_candle_idx] * (1 + self.sl_floor_pct)
                stop = self._apply_sl_floor(trigger, stop, direction)

                confidence = 'HIGH' if directional_count >= 4 else 'MODERATE'

                return PitchforkResult(
                    detected=True, signal_type='pitchfork', direction=direction,
                    directional_candles=directional_count,
                    trigger_price=trigger, stop_price=stop,
                    reversal_candle_idx=reversal_candle_idx,
                    confidence=confidence, timeframe_used=timeframe,
                    details=f'Pitchfork: {directional_count} directional candles → reversal'
                )

        return PitchforkResult(
            detected=False, signal_type='none', direction=direction,
            directional_candles=0, trigger_price=0, stop_price=0,
            reversal_candle_idx=-1, confidence='LOW', timeframe_used=timeframe,
            details=f'No pitchfork: need {self.pitchfork_directional_min}-'
                    f'{self.pitchfork_directional_max} directional candles followed by reversal'
        )

    # ----------------------------------------------------------
    # GATE 6: OTR
    # ----------------------------------------------------------

    def _check_otr_gate(self,
                         total_overhead: float,
                         half_box: float) -> Tuple[bool, float, str]:
        if half_box == 0:
            return False, 1.0, 'skip'

        otr = total_overhead / half_box

        if otr < self.otr_excellent:
            return True, otr, 'excellent'
        elif otr < self.otr_acceptable:
            return True, otr, 'acceptable'
        elif otr < self.otr_marginal:
            return True, otr, 'marginal'
        else:
            return False, otr, 'skip'

    # ----------------------------------------------------------
    # TARGET CALCULATION
    # ----------------------------------------------------------

    def _calculate_targets(self,
                             direction: str,
                             entry_price: float,
                             zone_map: ZoneMap) -> Tuple[float, float]:
        """Fixed pip targets from entry price.
        TP1 = ratchet trigger (SL locks here when hit).
        TP2 = full exit (OANDA native TP closes position).
        Actual values are recalculated from fill price in _fire_entry_candidate.
        """
        _center  = zone_map.daily_box.center
        pip_size = 0.01 if _center > 20 else 0.0001

        if direction == 'long':
            tp1 = entry_price + self.tp1_pips * pip_size
            tp2 = entry_price + self.tp2_pips * pip_size
        else:
            tp1 = entry_price - self.tp1_pips * pip_size
            tp2 = entry_price - self.tp2_pips * pip_size

        return tp1, tp2

    # ----------------------------------------------------------
    # VOLATILITY CHECK
    # ----------------------------------------------------------

    def _is_high_volatility(self,
                              bars_1h: pd.DataFrame,
                              daily_box: DailyBox) -> bool:
        if len(bars_1h) < 2:
            return False
        last_1h_range = float(bars_1h['high'].iloc[-2]) - float(bars_1h['low'].iloc[-2])
        hourly_atr_equiv = daily_box.ATR / 6.5
        return last_1h_range > hourly_atr_equiv * self.high_volatility_threshold

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------

    def _apply_sl_floor(self, trigger: float, stop: float, direction: str) -> float:
        """Enforce minimum SL distance from entry to avoid fakeout stops."""
        if direction == 'short':
            return max(stop, trigger * (1 + self.sl_floor_pct))
        else:
            return min(stop, trigger * (1 - self.sl_floor_pct))

    def _no_trade(self, symbol, zone_gate, zone_tier, zone_details,
                  rejection_reasons, wait_for) -> SignalResult:
        return SignalResult(
            symbol=symbol, timestamp=datetime.now().isoformat(),
            action='NO_TRADE',
            zone_gate=zone_gate, zone_tier=zone_tier, zone_details=zone_details,
            sucker_move_gate=False, sucker_move=None,
            atr_gate=False, atr_consumed_pct=0, atr_strength='weak',
            pitchfork_gate=False, pitchfork=None,
            otr_gate=False, otr_ratio=0, otr_category='skip',
            entry_price=0, stop_price=0, tp1_price=0, tp2_price=0,
            risk_points=0, reward_points=0, risk_reward_ratio=0,
            rejection_reasons=rejection_reasons,
            all_gates_passed=False, wait_for=wait_for
        )

    def _wait_result(self, symbol, zone_gate, zone_tier, zone_details,
                     atr_gate, atr_pct, atr_strength,
                     sucker, rejection_reasons, wait_for) -> SignalResult:
        return SignalResult(
            symbol=symbol, timestamp=datetime.now().isoformat(),
            action='WAIT',
            zone_gate=zone_gate, zone_tier=zone_tier, zone_details=zone_details,
            sucker_move_gate=sucker.detected, sucker_move=sucker,
            atr_gate=atr_gate, atr_consumed_pct=atr_pct, atr_strength=atr_strength,
            pitchfork_gate=False, pitchfork=None,
            otr_gate=False, otr_ratio=0, otr_category='unknown',
            entry_price=0, stop_price=0, tp1_price=0, tp2_price=0,
            risk_points=0, reward_points=0, risk_reward_ratio=0,
            rejection_reasons=rejection_reasons,
            all_gates_passed=False, wait_for=wait_for
        )
