# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Mr. Scrooge Gates — All trading logic in one place.

Contains: ZoneDetector, BoxStateMachine, SignalEngine, ForexUniverseScreener.
Previously split across strategy/zone_detector.py, strategy/box_state_machine.py,
strategy/signal_engine.py, strategy/universe_screener.py.
"""



# ==============================================================
# ZONE DETECTOR
# ==============================================================

# ============================================================
# BOXBOT — ZONE DETECTOR
# Identifies all liquidity zones across timeframes:
#   - Daily PDH/PDL box
#   - Hourly magic lines (highs, lows, breakout flips)
#   - Zone tier ranking (Tier 1 = hourly + daily alignment)
#   - Breakout flip detection (old resistance → new support)
# ============================================================

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from datetime import datetime
import logging
log = logging.getLogger('mr-scrooge')


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
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

class ZoneDetector:
    """
    Builds the complete zone map for an instrument using:
    - Daily chart for PDH/PDL and ATR
    - Hourly chart for magic lines
    - Cross-timeframe validation for tier assignment

    Workflow:
    1. Fetch daily OHLC → draw box, calculate ATR
    2. Fetch hourly OHLC → detect pivot highs/lows
    3. Identify breakout flip levels
    4. Cross-validate: hourly level near daily level → Tier 1
    5. Rank all zones by tier and test count
    6. Determine current price location
    """

    def __init__(self, config: dict):
        self.pivot_lookback = config.get('pivot_lookback_bars', 5)
        self.zone_merge_pct = config.get('zone_merge_pct', 0.0015)  # 0.15% — merge nearby levels
        self.daily_align_pct = config.get('daily_align_pct', 0.002)  # 0.2% — hourly/daily match threshold
        self.buy_zone_pct = config.get('buy_zone_pct', 0.30)         # Bottom 30% of box = buy zone (MASTER_THEORY: buy near the lower line)
        self.sell_zone_pct = config.get('sell_zone_pct', 0.30)       # Top 30% of box = sell zone (MASTER_THEORY: sell near the upper line)
        self.atr_period = config.get('atr_period', 14)
        self.hourly_bars_back = config.get('hourly_bars_back', 48)   # 2 days of hourly data
        # Percentile filtering — removes spike wicks from PDH/PDL
        self.use_percentile_filter = config.get('use_percentile_filter', True)
        self.pdh_percentile = config.get('pdh_percentile', 98)  # MASTER_THEORY: use true prior high/low; only trim extreme single-bar spikes
        self.pdl_percentile = config.get('pdl_percentile', 2)  # MASTER_THEORY: only trim extreme single-bar spike lows
        # Touch validation — require level to be tested N times
        self.min_level_touches = config.get('min_level_touches', 1)  # MASTER_THEORY: one prior high/low is a valid magic line
        self.touch_buffer_pct = config.get('touch_buffer_pct', 0.001)  # 0.1%
        self.reject_unvalidated = config.get('reject_unvalidated_boxes', False)  # Keep False — MASTER_THEORY does not require touch validation

    # ----------------------------------------------------------
    # MASTER BUILD FUNCTION
    # ----------------------------------------------------------

    def build_zone_map(self,
                       symbol: str,
                       exchange: str,
                       daily_bars: pd.DataFrame,
                       hourly_bars: pd.DataFrame,
                       current_price: float) -> Optional[ZoneMap]:
        """
        Builds complete ZoneMap for one instrument.

        Args:
            daily_bars: DataFrame with OHLCV, at least 15 rows
            hourly_bars: DataFrame with OHLCV, at least 48 rows
            current_price: Latest price tick

        Returns:
            ZoneMap or None if insufficient data
        """
        if len(daily_bars) < 3 or len(hourly_bars) < 10:
            log.warning(f"Insufficient bar data for {symbol}")
            return None

        # 1. Build daily box
        daily_box = self._build_daily_box(symbol, daily_bars, hourly_bars)
        if daily_box is None:
            return None

        # 1b. Prior completed hour high/low — additional magic line
        prior_hour_high = float(hourly_bars['high'].iloc[-2]) if len(hourly_bars) >= 2 else daily_box.PDH
        prior_hour_low  = float(hourly_bars['low'].iloc[-2])  if len(hourly_bars) >= 2 else daily_box.PDL
        prior_hour_mid  = (prior_hour_high + prior_hour_low) / 2

        # 2. Detect hourly magic lines
        magic_lines = self._detect_magic_lines(
            symbol, hourly_bars, daily_bars, daily_box
        )

        # 3. Tier all zones
        self._assign_tiers(magic_lines, daily_box)

        # 4. Sort into buy/sell zones
        sell_zones, buy_zones = self._classify_directional_zones(
            magic_lines, daily_box
        )

        # 5. Determine current price location
        current_zone = self._identify_current_zone(
            current_price, daily_box, magic_lines
        )

        # 6. Find nearest actionable zones
        nearest_buy = self._nearest_zone(current_price, buy_zones, direction='above')
        nearest_sell = self._nearest_zone(current_price, sell_zones, direction='below')

        return ZoneMap(
            symbol=symbol,
            exchange=exchange,
            daily_box=daily_box,
            prior_hour_high=prior_hour_high,
            prior_hour_low=prior_hour_low,
            prior_hour_mid=prior_hour_mid,
            magic_lines=magic_lines,
            tier1_zones=[z for z in magic_lines if z.tier == 1],
            tier2_zones=[z for z in magic_lines if z.tier == 2],
            tier3_zones=[z for z in magic_lines if z.tier == 3],
            sell_zones=sell_zones,
            buy_zones=buy_zones,
            current_price=current_price,
            current_zone=current_zone,
            nearest_buy_zone=nearest_buy,
            nearest_sell_zone=nearest_sell,
            built_at=datetime.now().isoformat()
        )

    # ----------------------------------------------------------
    # DAILY BOX
    # ----------------------------------------------------------

    def _build_daily_box(self,
                          symbol: str,
                          daily_bars: pd.DataFrame,
                          hourly_bars: pd.DataFrame = None) -> Optional[DailyBox]:
        """
        Builds the PDH/PDL box from prior completed day.
        Also calculates ATR for sucker move thresholds.
        """
        try:
            # Find the most recent completed bar with a meaningful range.
            # Weekend bars (Sat/Sun) have near-zero movement and produce invalid tiny boxes.
            # Skip any bar whose range is less than 0.05% of its close price.
            prior = None
            for i in range(2, min(len(daily_bars) + 1, 10)):
                candidate = daily_bars.iloc[-i]
                cand_high  = float(candidate['high'])
                cand_low   = float(candidate['low'])
                cand_close = float(candidate['close']) if candidate['close'] else cand_high
                cand_range_pct = (cand_high - cand_low) / cand_close if cand_close > 0 else 0
                if cand_range_pct >= 0.0005:  # At least 0.05% range = real trading session
                    prior = candidate
                    break
            if prior is None:
                prior = daily_bars.iloc[-2]  # fallback

            raw_PDH = float(prior['high'])
            raw_PDL = float(prior['low'])
            PDH = raw_PDH
            PDL = raw_PDL
            percentile_filtered = False

            # ── Percentile filtering: use hourly bar distribution to remove spike wicks ──
            # Instead of raw daily high/low (which includes news spikes), use the
            # 95th/5th percentile of hourly bars from the prior day.  This drops the
            # single most extreme spike bar and sets PDH/PDL at a realistic level.
            prior_hourly = None
            if self.use_percentile_filter and hourly_bars is not None and len(hourly_bars) >= 24:
                prior_hourly = self._get_prior_day_hourly(hourly_bars)
                if len(prior_hourly) >= 6:
                    clean_high = float(np.percentile(prior_hourly['high'].values, self.pdh_percentile))
                    clean_low = float(np.percentile(prior_hourly['low'].values, self.pdl_percentile))
                    if clean_high < raw_PDH:
                        PDH = clean_high
                        percentile_filtered = True
                        log.info(
                            f"{symbol}: Percentile filter — PDH {raw_PDH:.5f} → {PDH:.5f} "
                            f"(spike of {((raw_PDH - PDH) / PDH) * 100:.2f}% removed)"
                        )
                    if clean_low > raw_PDL:
                        PDL = clean_low
                        percentile_filtered = True
                        log.info(
                            f"{symbol}: Percentile filter — PDL {raw_PDL:.5f} → {PDL:.5f} "
                            f"(spike of {((PDL - raw_PDL) / raw_PDL) * 100:.2f}% removed)"
                        )

            today         = daily_bars.iloc[-1]
            today_open    = float(today['open'])
            today_high    = float(today['high'])
            today_low     = float(today['low'])
            current_close = float(today['close'])

            # Gap detection — if today's open is >0.5% outside the prior day box,
            # redraw using the gap protocol from the spec:
            #   Gap up: new ceiling = session high, new floor = prior PDH (flipped)
            #   Gap down: new ceiling = prior PDL (flipped), new floor = session low
            is_gap = False
            GAP_THRESHOLD = 0.005  # 0.5%
            if today_open > PDH * (1 + GAP_THRESHOLD):
                is_gap = True
                old_PDH = PDH
                PDH = today_high        # session high = new ceiling
                PDL = old_PDH           # prior resistance = new support
                log.info(
                    f"{symbol}: Gap UP detected — open {today_open:.5f} > PDH {old_PDH:.5f} "
                    f"| New box: {PDL:.5f} → {PDH:.5f}"
                )
            elif today_open < PDL * (1 - GAP_THRESHOLD):
                is_gap = True
                old_PDL = PDL
                PDH = old_PDL           # prior support = new resistance
                PDL = today_low         # session low = new floor
                log.info(
                    f"{symbol}: Gap DOWN detected — open {today_open:.5f} < PDL {old_PDL:.5f} "
                    f"| New box: {PDL:.5f} → {PDH:.5f}"
                )

            box_range = PDH - PDL

            # ── Sanity check: PDH must be above PDL ──
            # The percentile filter can produce PDL > PDH if it uses wrong hourly bars
            # (stale data from the wrong day). Detect and recover by falling back to raw values.
            if box_range <= 0:
                log.error(
                    f"{symbol}: Inverted box after filtering — "
                    f"PDL {PDL:.5f} >= PDH {PDH:.5f} | "
                    f"raw PDH={raw_PDH:.5f} PDL={raw_PDL:.5f} — falling back to raw values"
                )
                PDH = raw_PDH
                PDL = raw_PDL
                percentile_filtered = False
                box_range = PDH - PDL
                if box_range <= 0:
                    log.error(f"{symbol}: Raw box also inverted — discarding instrument")
                    return None

            half_box = box_range / 2
            center = PDL + half_box
            box_range_pct = box_range / current_close if current_close > 0 else 0

            # ── Touch validation: count hourly candles that tested each level ──
            # A level with more touches is stronger — price has proven it matters.
            # Levels with zero or one touch may be phantom levels from a single spike.
            pdh_touches = 0
            pdl_touches = 0
            validated = True

            if not is_gap:
                if prior_hourly is None and hourly_bars is not None and len(hourly_bars) >= 24:
                    prior_hourly = self._get_prior_day_hourly(hourly_bars)
                if prior_hourly is not None and len(prior_hourly) >= 6:
                    pdh_touches = self._count_level_touches(PDH, prior_hourly, 'high')
                    pdl_touches = self._count_level_touches(PDL, prior_hourly, 'low')

                validated = (pdh_touches >= self.min_level_touches and
                             pdl_touches >= self.min_level_touches)

                if not validated:
                    log.info(
                        f"{symbol}: Box touch validation "
                        f"{'FAILED' if self.reject_unvalidated else 'WARNING'} "
                        f"— PDH touches: {pdh_touches}/{self.min_level_touches}, "
                        f"PDL touches: {pdl_touches}/{self.min_level_touches}"
                    )
                    if self.reject_unvalidated:
                        return None

            # ATR calculation
            atr = self._calculate_atr(daily_bars, self.atr_period)

            return DailyBox(
                symbol=symbol,
                PDH=PDH,
                PDL=PDL,
                center=center,
                box_range=box_range,
                half_box=half_box,
                box_range_pct=box_range_pct,
                ATR=atr,
                ATR_60pct=atr * 0.60,
                ATR_80pct=atr * 0.80,
                ATR_20pct=atr * 0.20,
                session_date=datetime.now().date().isoformat(),
                draw_time=datetime.now().isoformat(),
                pdh_touches=pdh_touches,
                pdl_touches=pdl_touches,
                percentile_filtered=percentile_filtered,
                validated=validated,
                raw_PDH=raw_PDH,
                raw_PDL=raw_PDL,
            )
        except Exception as e:
            log.error(f"Daily box build failed {symbol}: {e}")
            return None

    def _calculate_atr(self, bars: pd.DataFrame, period: int) -> float:
        """Standard ATR calculation."""
        highs = bars['high'].values
        lows = bars['low'].values
        closes = bars['close'].values

        tr_list = []
        for i in range(1, len(bars)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            tr_list.append(tr)

        tr_series = pd.Series(tr_list)
        atr = tr_series.ewm(span=period, adjust=False).mean().iloc[-1]
        return float(atr)

    # ----------------------------------------------------------
    # PERCENTILE FILTERING & TOUCH VALIDATION
    # ----------------------------------------------------------

    def _get_prior_day_hourly(self, hourly_bars: pd.DataFrame) -> pd.DataFrame:
        """
        Extract approximately the prior day's hourly bars from the lookback window.
        Bars are chronologically ordered (oldest first, newest last).

        With n bars: the last 24 bars = today's session, so iloc[-48:-24] = yesterday.
        This works regardless of how many total bars are passed in (48, 200, etc.)
        because we always anchor to the END of the array, not the start.

        BUG HISTORY: original code used iloc[:24] which was correct for count=48
        but returned 8-day-old bars when count=200 was passed in from main.py.
        That caused the percentile filter to use stale levels, corrupting PDH/PDL.
        """
        n = len(hourly_bars)
        if n >= 48:
            return hourly_bars.iloc[-48:-24]   # Yesterday: 24 bars ending 24 bars before now
        elif n >= 24:
            return hourly_bars.iloc[:n - 12]   # Best approximation with partial data
        return hourly_bars

    def _count_level_touches(self, level: float, bars: pd.DataFrame, col: str) -> int:
        """
        Count candles whose extreme (high for resistance, low for support)
        came within touch_buffer_pct of a price level.

        A 'touch' means the level was tested — price approached it and reversed.
        More touches = stronger, more validated level = higher quality box wall.

        Args:
            level: The price level to check touches against
            bars: DataFrame of hourly bars
            col: 'high' for resistance levels, 'low' for support levels
        """
        count = 0
        buffer = level * self.touch_buffer_pct
        values = bars[col].astype(float).values
        for val in values:
            if abs(val - level) <= buffer:
                count += 1
        return count

    # ----------------------------------------------------------
    # HOURLY MAGIC LINES
    # ----------------------------------------------------------

    def _detect_magic_lines(self,
                              symbol: str,
                              hourly_bars: pd.DataFrame,
                              daily_bars: pd.DataFrame,
                              daily_box: DailyBox) -> List[MagicLine]:
        """
        Detects all significant horizontal levels on the hourly chart:
        - Pivot highs and lows
        - Breakout areas that have flipped polarity
        """
        lines = []

        # 1. Pivot highs → resistance / sell zones
        pivot_highs = self._find_pivots(hourly_bars, 'high')
        for price, test_count, last_tested in pivot_highs:
            flip = self._is_breakout_flip(price, hourly_bars, 'resistance')
            lines.append(MagicLine(
                price=price,
                zone_type='resistance' if not flip else 'breakout_flip',
                tier=4,  # Will be upgraded in _assign_tiers
                test_count=test_count,
                last_tested=last_tested,
                daily_aligned=False,  # Set in _assign_tiers
                is_breakout_flip=flip,
                strength_score=0,     # Set in _assign_tiers
                source_timeframe='1H',
                symbol=symbol,
                created_at=datetime.now().isoformat()
            ))

        # 2. Pivot lows → support / buy zones
        pivot_lows = self._find_pivots(hourly_bars, 'low')
        for price, test_count, last_tested in pivot_lows:
            flip = self._is_breakout_flip(price, hourly_bars, 'support')
            lines.append(MagicLine(
                price=price,
                zone_type='support' if not flip else 'breakout_flip',
                tier=4,
                test_count=test_count,
                last_tested=last_tested,
                daily_aligned=False,
                is_breakout_flip=flip,
                strength_score=0,
                source_timeframe='1H',
                symbol=symbol,
                created_at=datetime.now().isoformat()
            ))

        # 3. Add PDH and PDL explicitly as magic lines
        lines.append(MagicLine(
            price=daily_box.PDH,
            zone_type='resistance',
            tier=2,
            test_count=1,
            last_tested=daily_box.draw_time,
            daily_aligned=True,
            is_breakout_flip=False,
            strength_score=0,
            source_timeframe='D',
            symbol=symbol,
            created_at=datetime.now().isoformat()
        ))
        lines.append(MagicLine(
            price=daily_box.PDL,
            zone_type='support',
            tier=2,
            test_count=1,
            last_tested=daily_box.draw_time,
            daily_aligned=True,
            is_breakout_flip=False,
            strength_score=0,
            source_timeframe='D',
            symbol=symbol,
            created_at=datetime.now().isoformat()
        ))

        # 4. Merge nearby levels (within zone_merge_pct)
        lines = self._merge_nearby_zones(lines)

        return lines

    def _find_pivots(self,
                      bars: pd.DataFrame,
                      price_col: str,
                      min_tests: int = 1) -> List[Tuple[float, int, str]]:
        """
        Finds significant pivot points on the hourly chart.
        A pivot high = local maximum with n bars lower on each side.
        Returns list of (price, test_count, last_tested_iso).
        """
        n = self.pivot_lookback
        prices = bars[price_col].values
        times = bars.index if hasattr(bars.index, '__iter__') else range(len(bars))
        results = []
        found_levels = []

        for i in range(n, len(bars) - n):
            val = prices[i]

            if price_col == 'high':
                is_pivot = all(val >= prices[i-j] for j in range(1, n+1)) and \
                           all(val >= prices[i+j] for j in range(1, n+1))
            else:
                is_pivot = all(val <= prices[i-j] for j in range(1, n+1)) and \
                           all(val <= prices[i+j] for j in range(1, n+1))

            if is_pivot:
                # Check if near existing found level
                merged = False
                for k, (existing_price, count, _) in enumerate(found_levels):
                    if abs(val - existing_price) / existing_price < self.zone_merge_pct:
                        # Update test count and keep most recent
                        found_levels[k] = (
                            (existing_price * count + val) / (count + 1),
                            count + 1,
                            str(times[i]) if hasattr(times[i], '__str__') else datetime.now().isoformat()
                        )
                        merged = True
                        break

                if not merged:
                    found_levels.append((
                        val,
                        1,
                        str(times[i]) if hasattr(times[i], '__str__') else datetime.now().isoformat()
                    ))

        return found_levels

    def _is_breakout_flip(self,
                           price: float,
                           bars: pd.DataFrame,
                           original_type: str) -> bool:
        """
        Detects if a level has flipped polarity:
        - Was resistance, got broken, now acting as support
        - Was support, got broken, now acting as resistance

        This creates the highest-quality zones.
        """
        closes = bars['close'].values
        tolerance = price * self.zone_merge_pct * 2

        broke_through = False
        returned_and_held = False

        for i in range(1, len(closes)):
            if original_type == 'resistance':
                # Did price break above this level?
                if closes[i] > price + tolerance and not broke_through:
                    broke_through = True
                # After breaking, did it return and hold above?
                if broke_through and abs(closes[i] - price) < tolerance:
                    returned_and_held = True
            else:
                # Did price break below?
                if closes[i] < price - tolerance and not broke_through:
                    broke_through = True
                # After breaking, did it return and hold below?
                if broke_through and abs(closes[i] - price) < tolerance:
                    returned_and_held = True

        return broke_through and returned_and_held

    def _merge_nearby_zones(self,
                              lines: List[MagicLine]) -> List[MagicLine]:
        """
        Merges magic lines that are within zone_merge_pct of each other.
        Keeps the one with the highest test count and best attributes.
        """
        if not lines:
            return lines

        lines_sorted = sorted(lines, key=lambda x: x.price)
        merged = []

        i = 0
        while i < len(lines_sorted):
            current = lines_sorted[i]
            group = [current]

            j = i + 1
            while j < len(lines_sorted):
                if abs(lines_sorted[j].price - current.price) / current.price < self.zone_merge_pct:
                    group.append(lines_sorted[j])
                    j += 1
                else:
                    break

            if len(group) == 1:
                merged.append(current)
            else:
                # Merge: take average price, sum test counts, keep best attributes
                avg_price = sum(z.price for z in group) / len(group)
                total_tests = sum(z.test_count for z in group)
                any_daily = any(z.daily_aligned for z in group)
                any_flip = any(z.is_breakout_flip for z in group)
                best_source = 'D' if any_daily else '1H'

                merged.append(MagicLine(
                    price=avg_price,
                    zone_type='breakout_flip' if any_flip else group[0].zone_type,
                    tier=4,
                    test_count=total_tests,
                    last_tested=group[-1].last_tested,
                    daily_aligned=any_daily,
                    is_breakout_flip=any_flip,
                    strength_score=0,
                    source_timeframe=best_source,
                    symbol=current.symbol,
                    created_at=datetime.now().isoformat()
                ))

            i = j

        return merged

    # ----------------------------------------------------------
    # TIER ASSIGNMENT
    # ----------------------------------------------------------

    def _assign_tiers(self,
                       magic_lines: List[MagicLine],
                       daily_box: DailyBox):
        """
        Assigns tier and strength score to each magic line.

        Tier 1: Hourly level aligns with daily level
                → Strongest, highest probability
        Tier 2: Breakout flip OR multiple tests (3+)
        Tier 3: 2 tests
        Tier 4: Single test, no special properties
        """
        daily_levels = [daily_box.PDH, daily_box.PDL]

        for line in magic_lines:
            # Check daily alignment
            for dl in daily_levels:
                if abs(line.price - dl) / dl < self.daily_align_pct:
                    line.daily_aligned = True
                    break

            # Assign tier
            if line.daily_aligned:
                line.tier = 1
            elif line.is_breakout_flip or line.test_count >= 3:
                line.tier = 2
            elif line.test_count >= 2:
                line.tier = 3
            else:
                line.tier = 4

            # Strength score (0-100)
            score = 0
            score += min(line.test_count * 15, 45)    # Up to 45 for test count
            score += 25 if line.daily_aligned else 0   # +25 for daily alignment
            score += 20 if line.is_breakout_flip else 0  # +20 for flip
            score += 10 if line.source_timeframe == 'D' else 0
            line.strength_score = min(score, 100)

    # ----------------------------------------------------------
    # ZONE CLASSIFICATION
    # ----------------------------------------------------------

    def _classify_directional_zones(self,
                                      magic_lines: List[MagicLine],
                                      daily_box: DailyBox) -> Tuple[List[MagicLine], List[MagicLine]]:
        """
        Separates magic lines into sell zones (upper) and buy zones (lower).
        Sell zones: at or above center line
        Buy zones: at or below center line
        """
        sell_zones = [z for z in magic_lines if z.price >= daily_box.center]
        buy_zones = [z for z in magic_lines if z.price < daily_box.center]

        # Sort sell zones descending (highest first)
        sell_zones.sort(key=lambda x: x.price, reverse=True)
        # Sort buy zones ascending (lowest first)
        buy_zones.sort(key=lambda x: x.price)

        return sell_zones, buy_zones

    def _identify_current_zone(self,
                                 current_price: float,
                                 daily_box: DailyBox,
                                 magic_lines: List[MagicLine]) -> str:
        """
        Determines where current price sits relative to the box.
        """
        sell_threshold = daily_box.PDL + (daily_box.box_range * (1 - self.sell_zone_pct))
        buy_threshold = daily_box.PDL + (daily_box.box_range * self.buy_zone_pct)

        if current_price > daily_box.PDH * 1.002:
            return 'above_box'
        elif current_price < daily_box.PDL * 0.998:
            return 'below_box'
        elif current_price >= sell_threshold:
            return 'sell_zone'
        elif current_price <= buy_threshold:
            return 'buy_zone'
        else:
            return 'middle'

    def _nearest_zone(self,
                       current_price: float,
                       zones: List[MagicLine],
                       direction: str) -> Optional[MagicLine]:
        """
        Finds the nearest zone in a given direction.
        direction='above' → find nearest zone above current price
        direction='below' → find nearest zone below current price
        """
        if not zones:
            return None

        if direction == 'above':
            candidates = [z for z in zones if z.price >= current_price]
            if not candidates:
                return None
            return min(candidates, key=lambda z: z.price - current_price)
        else:
            candidates = [z for z in zones if z.price <= current_price]
            if not candidates:
                return None
            return min(candidates, key=lambda z: current_price - z.price)

# ==============================================================
# BOX STATE MACHINE
# ==============================================================

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

# ==============================================================
# SIGNAL ENGINE
# ==============================================================

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
        tp1, tp2 = self._calculate_targets(
            direction, pitchfork.trigger_price, zone_map
        )

        risk_points = abs(pitchfork.trigger_price - pitchfork.stop_price)
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
            stop_price=pitchfork.stop_price,
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
        daily_box = zone_map.daily_box

        # TP1 = daily box midline, TP2 = opposite daily box wall
        # Guard: if entry is near center, TP1 ≈ entry → DOA trade.
        # In that case, push TP1 to midpoint between entry and opposite wall.
        if direction == 'long':
            tp1 = daily_box.center
            tp2 = daily_box.PDH
            # Ensure TP1 is at least 50% of the way from entry to TP2
            min_tp1 = entry_price + (tp2 - entry_price) * 0.50
            if tp1 < min_tp1:
                tp1 = min_tp1
        else:
            tp1 = daily_box.center
            tp2 = daily_box.PDL
            # Ensure TP1 is at least 50% of the way from entry to TP2
            max_tp1 = entry_price - (entry_price - tp2) * 0.50
            if tp1 > max_tp1:
                tp1 = max_tp1

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

# ==============================================================
# UNIVERSE SCREENER
# ==============================================================

# ============================================================
# MR. SCROOGE — UNIVERSE SCREENER (FOREX)
# Scans OANDA forex pairs every 30 minutes and returns
# the list of instruments that have viable boxes today.
#
# Viability = box exists + box is wide enough to cover spread
#             + session hasn't consumed too much ATR yet
#             + we're not in a weekend/holiday gap situation
#
# Screener runs at boot and every 30 min.
# Main loop only processes instruments that pass screening.
# ============================================================

import logging
import pandas as pd
from dataclasses import dataclass
from typing import List, Optional, Set
from datetime import datetime, timezone

log = logging.getLogger('mr-scrooge.universe_screener')


# ============================================================
# SESSION WINDOWS
# Major forex sessions with their UTC open/close hours
# and the pairs that are active / liquid in that session.
# Scrooge only scans pairs whose session is currently open.
# ============================================================

SESSION_WINDOWS = [
    {
        'name':      'Sydney',
        'open_utc':  21,    # 9PM UTC
        'close_utc': 6,     # 6AM UTC (crosses midnight)
        'pairs': [
            'AUD_USD', 'NZD_USD', 'AUD_NZD', 'AUD_CAD',
            'AUD_JPY', 'NZD_JPY',
        ]
    },
    {
        'name':      'Tokyo',
        'open_utc':  0,     # Midnight UTC
        'close_utc': 9,     # 9AM UTC
        'pairs': [
            'USD_JPY', 'EUR_JPY', 'GBP_JPY', 'AUD_JPY',
            'CAD_JPY', 'CHF_JPY', 'NZD_JPY',
            'AUD_USD', 'NZD_USD', 'AUD_NZD',
        ]
    },
    {
        'name':      'London',
        'open_utc':  7,     # 7AM UTC
        'close_utc': 16,    # 4PM UTC
        'pairs': [
            'EUR_USD', 'GBP_USD', 'EUR_GBP',
            'EUR_JPY', 'GBP_JPY', 'EUR_CHF', 'GBP_CHF',
            'EUR_CAD', 'EUR_AUD', 'EUR_NZD',
            'GBP_CAD', 'GBP_AUD', 'GBP_NZD',
            'USD_CHF', 'USD_JPY', 'AUD_CHF',
        ]
    },
    {
        'name':      'New York',
        'open_utc':  13,    # 1PM UTC
        'close_utc': 22,    # 10PM UTC
        'pairs': [
            'EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CAD',
            'USD_CHF', 'AUD_USD', 'NZD_USD',
            'EUR_JPY', 'GBP_JPY', 'EUR_GBP',
            'CAD_JPY', 'EUR_CAD', 'GBP_CAD',
        ]
    },
]


def get_active_sessions(utc_hour: int) -> list:
    """Returns list of session dicts that are currently open."""
    active = []
    for session in SESSION_WINDOWS:
        o = session['open_utc']
        c = session['close_utc']
        if o > c:  # crosses midnight (e.g. Sydney 21-6)
            if utc_hour >= o or utc_hour < c:
                active.append(session)
        else:
            if o <= utc_hour < c:
                active.append(session)
    return active


# Instruments to consider — 24 pairs across majors, minors, and crosses
CANDIDATE_PAIRS = [
    # Dollar majors
    'EUR_USD', 'GBP_USD', 'USD_JPY', 'AUD_USD',
    'USD_CHF', 'USD_CAD', 'NZD_USD', 'USD_MXN',
    # JPY crosses — active Asian and London overlap
    'EUR_JPY', 'GBP_JPY', 'AUD_JPY', 'CAD_JPY', 'CHF_JPY', 'NZD_JPY',
    # EUR crosses
    'EUR_GBP', 'EUR_CAD', 'EUR_AUD', 'EUR_NZD', 'EUR_CHF',
    # GBP crosses
    'GBP_CAD', 'GBP_AUD', 'GBP_NZD', 'GBP_CHF',
    # Commodity crosses
    'AUD_CAD', 'AUD_NZD', 'AUD_CHF',
]

# Proven net-positive pairs (audit Feb 25 - Mar 4 2026)
# These get capital priority over extended pairs.
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

