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
        self.hourly_bars_back  = config.get('hourly_bars_back', 48)   # 2 days of hourly data
        self.box_30m_lookback  = config.get('box_30m_lookback', 24)  # 30m bars in rolling window
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
                       current_price: float,
                       bars_30m: pd.DataFrame = None) -> Optional[ZoneMap]:
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

        # 1. Build box — daily (default) or rolling 30m window (experimental)
        if bars_30m is not None and len(bars_30m) >= 10:
            lookback = getattr(self, 'box_30m_lookback', 8)
            daily_box = self._build_30m_box(symbol, bars_30m, lookback)
        else:
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
    # 30-MINUTE ROLLING BOX (experimental)
    # ----------------------------------------------------------

    def _build_30m_box(self,
                        symbol: str,
                        bars_30m: pd.DataFrame,
                        lookback: int = 8) -> Optional['DailyBox']:
        """
        Builds a box from the last `lookback` completed 30-minute bars.
        PDH = window high, PDL = window low.
        Resets naturally every cycle as bars shift.
        No gap protocol, no percentile filter, no weekend skip.
        """
        try:
            if len(bars_30m) < lookback + 2:
                log.warning(f"{symbol}: insufficient 30m bars ({len(bars_30m)} < {lookback+2})")
                return None

            # Exclude the current (potentially incomplete) bar
            window = bars_30m.iloc[-(lookback + 1):-1]

            PDH = float(window['high'].max())
            PDL = float(window['low'].min())

            if PDH <= PDL:
                log.error(f"{symbol}: 30m box inverted PDH={PDH:.5f} PDL={PDL:.5f}")
                return None

            box_range   = PDH - PDL
            half_box    = box_range / 2
            center      = PDL + half_box
            current_cls = float(bars_30m.iloc[-1]['close'])
            box_range_pct = box_range / current_cls if current_cls > 0 else 0

            # ATR from the window's true ranges
            highs   = window['high'].values
            lows    = window['low'].values
            closes  = window['close'].values
            trs = [max(highs[i] - lows[i],
                       abs(highs[i]  - closes[i - 1]),
                       abs(lows[i]   - closes[i - 1]))
                   for i in range(1, len(highs))]
            atr = float(np.mean(trs)) if trs else box_range

            session_date = str(bars_30m.index[-2])[:16]

            log.info(
                f"{symbol}: 30m box built | lookback={lookback} bars | "
                f"PDH={PDH:.5f} PDL={PDL:.5f} range={box_range_pct*100:.3f}%"
            )

            return DailyBox(
                symbol=symbol,
                PDH=PDH, PDL=PDL,
                center=center,
                box_range=box_range,
                half_box=half_box,
                box_range_pct=box_range_pct,
                ATR=atr,
                ATR_60pct=atr * 0.60,
                ATR_80pct=atr * 0.80,
                ATR_20pct=atr * 0.20,
                session_date=session_date,
                draw_time=bars_30m.index[-1].isoformat() if hasattr(bars_30m.index[-1], 'isoformat') else str(bars_30m.index[-1]),
                pdh_touches=0,
                pdl_touches=0,
                percentile_filtered=False,
                validated=True,
                raw_PDH=PDH,
                raw_PDL=PDL,
            )
        except Exception as e:
            log.error(f"{symbol}: _build_30m_box failed: {e}", exc_info=True)
            return None

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
