# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Regime Supervisor — Observer Only

This component watches the market and the bot's own performance.
It does NOT block, modify, or influence any trade decisions.
It logs a structured "protest" whenever it would have intervened,
so we can audit later whether intervention would have helped.

Audit query (3 months from now):
  - Every time regime=RESTRICTED/BLOCKED, what was the trade outcome?
  - If losers heavily outweigh winners in those windows → activate it.
  - If outcomes match baseline → regime detector is noise, discard it.

Signals tracked:
  1. box_acceptance     — closes holding outside PDH/PDL
  2. rejection_decay    — recent wall touches (exponential decay, 8h half-life)
  3. expansion_pressure — directional ATR expansion, session-adjusted thresholds
  4. qualified_failures — our own gated setups hit SL recently
  5. breadth            — correlated pairs showing same regime weakness
  6. quality_pressure   — MFE/MAE follow-through from quality tracker
"""

import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from .models import RegimeAssessment, RegimeSignal, RegimeState, classify_score
from .bb_context import compute_bb_context, BBState

log = logging.getLogger("mr-scrooge.regime")


class RegimeSupervisor:
    """
    Passive observer. Evaluates regime each cycle, logs protests.
    Never touches orders, never modifies coordinator behavior.
    """

    # How many recent candles to examine for each signal
    _ACCEPTANCE_LOOKBACK  = 6    # 30 min of 5m bars — is price holding outside box?
    _DECAY_LOOKBACK       = 24   # 24 hours of 1h bars — how have recent wall touches resolved?
    _EXPANSION_LOOKBACK   = 12   # 1 hour — is ATR expanding directionally?
    _FAILURE_WINDOW_HOURS = 6    # Look back 6 hours for qualified trade failures

    # Exponential decay half-life for rejection signals — a touch 8h ago counts 50% of now
    _DECAY_HALF_LIFE_HOURS = 8.0

    # Full universe — majors + crosses (ordered by correlation strength)
    _CORRELATION_BLOCKS: Dict[str, List[str]] = {
        # EUR/GBP/USD majors
        'EUR_USD': ['GBP_USD', 'USD_CHF', 'EUR_JPY', 'EUR_CAD'],
        'GBP_USD': ['EUR_USD', 'USD_CHF', 'GBP_CAD'],
        'USD_CAD': ['CAD_JPY', 'EUR_CAD', 'GBP_CAD'],
        'USD_CHF': ['EUR_USD', 'GBP_USD'],
        'EUR_CAD': ['EUR_USD', 'USD_CAD', 'GBP_CAD'],
        'GBP_CAD': ['GBP_USD', 'USD_CAD', 'EUR_CAD'],
        # AUD/NZD crosses
        'AUD_USD': ['AUD_NZD', 'AUD_CAD', 'AUD_JPY', 'NZD_USD'],
        'NZD_USD': ['AUD_USD', 'AUD_NZD', 'NZD_JPY'],
        'AUD_NZD': ['AUD_USD', 'NZD_USD', 'AUD_JPY', 'NZD_JPY'],
        'AUD_CAD': ['AUD_USD', 'AUD_JPY'],
        # JPY crosses (merged)
        'USD_JPY': ['EUR_JPY', 'GBP_JPY', 'CAD_JPY', 'CHF_JPY', 'AUD_JPY'],
        'EUR_JPY': ['USD_JPY', 'GBP_JPY', 'CAD_JPY', 'CHF_JPY', 'EUR_USD'],
        'GBP_JPY': ['USD_JPY', 'EUR_JPY', 'CAD_JPY', 'CHF_JPY'],
        'CAD_JPY': ['USD_JPY', 'EUR_JPY', 'GBP_JPY', 'CHF_JPY', 'USD_CAD'],
        'CHF_JPY': ['USD_JPY', 'EUR_JPY', 'GBP_JPY', 'CAD_JPY'],
        'AUD_JPY': ['AUD_USD', 'AUD_NZD', 'USD_JPY', 'EUR_JPY', 'GBP_JPY', 'CAD_JPY', 'CHF_JPY'],
        'NZD_JPY': ['NZD_USD', 'AUD_NZD', 'USD_JPY'],
    }

    _MAX_BREADTH_PEERS = 4  # Cap evaluated peers to control trigger frequency
    def __init__(self, log_path: str):
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        # Per-symbol: deque of recent qualified trade outcomes
        # Each entry: {'trade_id': str, 'symbol': str, 'hit_sl': bool, 'closed_at': str, 'pnl': float}
        self._outcomes: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10))

        # Per-symbol: last assessment (for status display)
        self._last: Dict[str, RegimeAssessment] = {}
        # Per-symbol: last BB context (for dashboard badge)
        self._last_bb = {}

    # ── Public API ────────────────────────────────────────────

    def observe(
        self,
        symbol:      str,
        zone_map,                    # ZoneMap
        bars_5m:     pd.DataFrame,
        bars_1h:     pd.DataFrame,
        bot_action:  str,            # 'ENTERED_LONG', 'ENTERED_SHORT', 'NO_SIGNAL', 'GATE_REJECTED', etc.
        trade_id:    Optional[str] = None,
        signal_score: float = 0.0,
        quality_pressure=None,       # QualityPressure | None — from quality tracker
    ) -> RegimeAssessment:
        """
        Called every cycle for each instrument, after the bot has decided what to do.
        Evaluates regime, logs protest if non-normal AND bot took/qualified an entry.
        Returns the assessment (for status display — never used to gate entries).
        """
        # Infer trade direction from bot_action so box_acceptance scoring can be directional
        action_upper = (bot_action or '').upper()
        if any(x in action_upper for x in ('LONG', 'BUY')):
            direction: Optional[str] = 'long'
        elif any(x in action_upper for x in ('SHORT', 'SELL')):
            direction = 'short'
        else:
            direction = None

        assessment = self._evaluate(symbol, zone_map, bars_5m, bars_1h, direction, quality_pressure)
        self._last[symbol] = assessment

        # Only protest when something interesting happened
        entry_taken = bot_action in ("ENTERED_LONG", "ENTERED_SHORT")
        signal_qualified = bot_action == "SIGNAL_QUALIFIED"  # all gates passed but ranked out

        if assessment.would_have_intervened() and (entry_taken or signal_qualified):
            self._write_protest(assessment, bot_action, trade_id, signal_score)

        if assessment.would_have_intervened():
            log.info(
                f"REGIME {symbol}: {assessment.summary()} "
                f"| bot_action={bot_action}"
                + (f" trade_id={trade_id}" if trade_id else "")
            )

        return assessment

    def record_outcome(
        self,
        trade_id:   str,
        symbol:     str,
        pnl:        float,
        hit_sl:     bool,
        entry_time: str,
    ):
        """
        Called when a trade closes. Two purposes:
        1. Updates the protest log entry so we can tie outcome to regime state.
        2. Feeds the qualified_failures signal for future assessments.
        """
        outcome = {
            "trade_id":  trade_id,
            "symbol":    symbol,
            "hit_sl":    hit_sl,
            "pnl":       pnl,
            "entry_time": entry_time,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._outcomes[symbol].append(outcome)
        self._patch_protest_outcome(trade_id, pnl, hit_sl)

    def get_status(self) -> Dict[str, str]:
        """Returns {symbol: regime_state} for status display."""
        return {s: a.state.value for s, a in self._last.items()}

    def get_bb_states(self) -> Dict[str, str]:
        """Returns {symbol: bb_state_string} for dashboard badges."""
        return {s: ctx.state.value for s, ctx in self._last_bb.items()}

    # ── Evaluation ────────────────────────────────────────────

    def _evaluate(
        self,
        symbol:    str,
        zone_map,
        bars_5m:   pd.DataFrame,
        bars_1h:   pd.DataFrame,
        direction: Optional[str] = None,  # 'long', 'short', or None (unknown)
        quality_pressure=None,
    ) -> RegimeAssessment:
        signals: List[RegimeSignal] = []

        signals.extend(self._check_box_acceptance(symbol, zone_map, bars_5m, direction))
        signals.extend(self._check_rejection_decay(symbol, zone_map, bars_1h))
        signals.extend(self._check_expansion_pressure(symbol, bars_5m, zone_map))
        signals.extend(self._check_qualified_failures(symbol))
        signals.extend(self._check_breadth(symbol))
        signals.extend(self._check_quality_pressure(symbol, quality_pressure))
        signals.extend(self._check_bb_context(symbol, bars_5m))

        score = sum(s.score_delta for s in signals)
        state = classify_score(score)

        return RegimeAssessment(
            symbol=symbol,
            state=state,
            score=score,
            signals=signals,
            assessed_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── Signal 1: Box Acceptance ──────────────────────────────

    def _check_box_acceptance(
        self, symbol: str, zone_map, bars_5m: pd.DataFrame,
        direction: Optional[str] = None,
    ) -> List[RegimeSignal]:
        """
        Is price holding outside PDH/PDL?
        2+ consecutive closes outside the box = market accepting new range = bad for reversal.
        """
        signals = []
        if zone_map is None or bars_5m is None or len(bars_5m) < self._ACCEPTANCE_LOOKBACK:
            return signals

        pdh = zone_map.daily_box.PDH
        pdl = zone_map.daily_box.PDL
        recent = bars_5m.iloc[-self._ACCEPTANCE_LOOKBACK:]
        closes = recent["close"].values

        # Count consecutive closes above PDH
        above_streak = 0
        for c in reversed(closes):
            if c > pdh:
                above_streak += 1
            else:
                break

        # Count consecutive closes below PDL
        below_streak = 0
        for c in reversed(closes):
            if c < pdl:
                below_streak += 1
            else:
                break

        if above_streak >= 2:
            # SHORT into PDH break = mean reversion trade, aligned with strategy → neutral
            # LONG into PDH break = chasing a breakout → penalise
            # Unknown direction → conservative penalty
            if direction == 'short':
                signals.append(RegimeSignal(
                    name="box_acceptance_above",
                    score_delta=0,
                    detail=(
                        f"{above_streak} consecutive closes above PDH {pdh:.5f} — "
                        f"shorting back to box (aligned, no penalty)"
                    ),
                ))
            else:
                signals.append(RegimeSignal(
                    name="box_acceptance_above",
                    score_delta=-2,
                    detail=(
                        f"{above_streak} consecutive closes above PDH {pdh:.5f} — "
                        f"price holding outside box (last close {closes[-1]:.5f})"
                    ),
                ))
        elif above_streak == 1:
            signals.append(RegimeSignal(
                name="box_probe_above",
                score_delta=-1,
                detail=f"1 close above PDH {pdh:.5f} — watching for acceptance",
            ))

        if below_streak >= 2:
            # LONG into PDL break = mean reversion trade, aligned with strategy → neutral
            # SHORT into PDL break = chasing a breakdown → penalise
            # Unknown direction → conservative penalty
            if direction == 'long':
                signals.append(RegimeSignal(
                    name="box_acceptance_below",
                    score_delta=0,
                    detail=(
                        f"{below_streak} consecutive closes below PDL {pdl:.5f} — "
                        f"longing back to box (aligned, no penalty)"
                    ),
                ))
            else:
                signals.append(RegimeSignal(
                    name="box_acceptance_below",
                    score_delta=-2,
                    detail=(
                        f"{below_streak} consecutive closes below PDL {pdl:.5f} — "
                        f"price holding outside box (last close {closes[-1]:.5f})"
                    ),
                ))
        elif below_streak == 1:
            signals.append(RegimeSignal(
                name="box_probe_below",
                score_delta=-1,
                detail=f"1 close below PDL {pdl:.5f} — watching for acceptance",
            ))

        # Bonus: clean rejection behavior (price returned inside box)
        current = float(closes[-1])
        if pdl < current < pdh:
            # Price is back inside — check if it was outside recently and returned
            outside_count = sum(1 for c in closes if c > pdh or c < pdl)
            if outside_count == 0:
                signals.append(RegimeSignal(
                    name="box_contained",
                    score_delta=+1,
                    detail=f"Price fully contained within box for last {self._ACCEPTANCE_LOOKBACK} candles",
                ))

        return signals

    # ── Signal 2: Rejection Quality Decay ────────────────────

    def _check_rejection_decay(
        self, symbol: str, zone_map, bars_1h: pd.DataFrame
    ) -> List[RegimeSignal]:
        """
        Have recent wall touches produced clean rejections or weak ones?
        Looks at last N 1H candles for touches near PDH/PDL.
        A 'weak rejection' = candle touched the wall but close was within 20% of the wall.
        A 'failed rejection' = candle touched wall and continued through it.
        """
        signals = []
        if zone_map is None or bars_1h is None or len(bars_1h) < self._DECAY_LOOKBACK:
            return signals

        pdh = zone_map.daily_box.PDH
        pdl = zone_map.daily_box.PDL
        box_range = zone_map.daily_box.box_range
        if box_range <= 0:
            return signals

        proximity_pct = 0.08  # within 8% of box range = "near the wall"
        proximity = box_range * proximity_pct

        recent = bars_1h.iloc[-self._DECAY_LOOKBACK:]
        clean_rejections  = 0.0
        weak_rejections   = 0.0
        failed_rejections = 0.0

        n_bars = len(recent)
        for i, (_, bar) in enumerate(recent.iterrows()):
            high  = float(bar["high"])
            low   = float(bar["low"])
            close = float(bar["close"])

            # Age: bar 0 is oldest, bar n_bars-1 is most recent
            age_hours = (n_bars - 1 - i)  # hours ago (1h bars)
            weight = 0.5 ** (age_hours / self._DECAY_HALF_LIFE_HOURS)

            # Touch near PDH
            if high >= pdh - proximity:
                if close < pdh - proximity:
                    clean_rejections += weight
                elif close < pdh:
                    weak_rejections += weight
                else:
                    failed_rejections += weight

            # Touch near PDL
            if low <= pdl + proximity:
                if close > pdl + proximity:
                    clean_rejections += weight
                elif close > pdl:
                    weak_rejections += weight
                else:
                    failed_rejections += weight

        total_touches = clean_rejections + weak_rejections + failed_rejections
        if total_touches == 0:
            return signals

        if failed_rejections >= 2:
            signals.append(RegimeSignal(
                name="rejection_failed",
                score_delta=-2,
                detail=(
                    f"{failed_rejections:.1f}w failed rejections at box walls in last {self._DECAY_LOOKBACK}H — "
                    f"wall not holding (clean={clean_rejections:.1f}w weak={weak_rejections:.1f}w)"
                ),
            ))
        elif weak_rejections >= 3 and clean_rejections == 0:
            signals.append(RegimeSignal(
                name="rejection_weak",
                score_delta=-1,
                detail=(
                    f"{weak_rejections:.1f}w weak rejections, 0 clean in last {self._DECAY_LOOKBACK}H — "
                    f"reversal quality degrading"
                ),
            ))
        elif clean_rejections >= 2 and failed_rejections == 0:
            signals.append(RegimeSignal(
                name="rejection_clean",
                score_delta=+2,
                detail=(
                    f"{clean_rejections:.1f}w clean rejections at walls in last {self._DECAY_LOOKBACK}H — "
                    f"reversal framework working well"
                ),
            ))

        return signals

    # ── Signal 3: Expansion Pressure ─────────────────────────

    def _check_expansion_pressure(
        self, symbol: str, bars_5m: pd.DataFrame, zone_map
    ) -> List[RegimeSignal]:
        """
        Is ATR expanding directionally without mean reversion?
        High ATR alone is not bad — it's directional persistence that kills fading.
        """
        signals = []
        if bars_5m is None or len(bars_5m) < self._EXPANSION_LOOKBACK + 20:
            return signals

        recent = bars_5m.iloc[-self._EXPANSION_LOOKBACK:]
        baseline = bars_5m.iloc[-(self._EXPANSION_LOOKBACK + 20):-self._EXPANSION_LOOKBACK]

        # True range per bar
        def true_range(df):
            hl = df["high"] - df["low"]
            return float(hl.mean())

        recent_atr   = true_range(recent)
        baseline_atr = true_range(baseline)

        if baseline_atr <= 0:
            return signals

        expansion_ratio = recent_atr / baseline_atr

        # Directional persistence: what % of recent candles are same direction?
        directions = []
        for _, bar in recent.iterrows():
            directions.append(1 if float(bar["close"]) > float(bar["open"]) else -1)

        same_direction = sum(1 for d in directions if d == directions[-1])
        directionality = same_direction / len(directions)

        # Session-adjusted thresholds — expansion is expected during London/NY open,
        # but the same ratio overnight signals a genuine breakout in thin conditions.
        utc_hour = datetime.now(timezone.utc).hour
        if 12 <= utc_hour <= 16:   # London/NY overlap — high vol expected
            thresh_hard, thresh_mild = 2.2, 1.7
        elif utc_hour in range(7, 13) or utc_hour in range(17, 22):  # London open, NY
            thresh_hard, thresh_mild = 1.8, 1.4
        else:                       # Asian/Sydney — thin, any expansion more alarming
            thresh_hard, thresh_mild = 1.4, 1.2

        if expansion_ratio > thresh_hard and directionality > 0.65:
            signals.append(RegimeSignal(
                name="expansion_pressure",
                score_delta=-2,
                detail=(
                    f"ATR {expansion_ratio:.1f}x baseline + {directionality:.0%} directional candles — "
                    f"persistent expansion, poor fading conditions"
                ),
            ))
        elif expansion_ratio > thresh_mild and directionality > 0.60:
            signals.append(RegimeSignal(
                name="expansion_mild",
                score_delta=-1,
                detail=(
                    f"ATR {expansion_ratio:.1f}x baseline + {directionality:.0%} directional — "
                    f"mild expansion pressure"
                ),
            ))
        elif expansion_ratio < 0.8:
            signals.append(RegimeSignal(
                name="compression",
                score_delta=+1,
                detail=(
                    f"ATR {expansion_ratio:.1f}x baseline — compression, reversal conditions favorable"
                ),
            ))

        return signals

    # ── Signal 4: Qualified Trade Failures ───────────────────

    def _check_qualified_failures(self, symbol: str) -> List[RegimeSignal]:
        """
        Have our own gated setups been failing recently?
        This is the highest signal-to-noise metric — the market is directly
        telling us our framework isn't being honored right now.
        """
        signals = []
        outcomes = list(self._outcomes[symbol])
        if not outcomes:
            return signals

        # Only look at recent window
        now = datetime.now(timezone.utc)
        recent_failures = []
        recent_wins = []

        for o in outcomes:
            try:
                closed_at = datetime.fromisoformat(o["closed_at"])
                if closed_at.tzinfo is None:
                    closed_at = closed_at.replace(tzinfo=timezone.utc)
                hours_ago = (now - closed_at).total_seconds() / 3600
                if hours_ago <= self._FAILURE_WINDOW_HOURS:
                    if o["hit_sl"]:
                        recent_failures.append(o)
                    else:
                        recent_wins.append(o)
            except Exception:
                continue

        n_fail = len(recent_failures)
        n_win  = len(recent_wins)

        if n_fail >= 2 and n_win == 0:
            signals.append(RegimeSignal(
                name="qualified_failures",
                score_delta=-3,
                detail=(
                    f"{n_fail} qualified setups hit SL in last {self._FAILURE_WINDOW_HOURS}h, 0 winners — "
                    f"market not honoring reversal framework"
                ),
            ))
        elif n_fail >= 2:
            signals.append(RegimeSignal(
                name="qualified_failures_mixed",
                score_delta=-2,
                detail=(
                    f"{n_fail} SL hits, {n_win} winners in last {self._FAILURE_WINDOW_HOURS}h — "
                    f"elevated failure rate"
                ),
            ))
        elif n_fail == 1 and n_win == 0:
            signals.append(RegimeSignal(
                name="single_failure",
                score_delta=-1,
                detail=f"1 qualified setup hit SL, no winners in last {self._FAILURE_WINDOW_HOURS}h",
            ))
        elif n_win >= 2 and n_fail == 0:
            signals.append(RegimeSignal(
                name="clean_performance",
                score_delta=+1,
                detail=f"{n_win} winners, 0 SL hits in last {self._FAILURE_WINDOW_HOURS}h",
            ))

        return signals

    # ── Signal 5: Breadth (correlated symbol regime scores) ──

    def _check_breadth(self, symbol: str) -> List[RegimeSignal]:
        """
        Are correlated pairs also in poor regime state?
        Uses last cycle's cached assessments — no circular dependency.
        Weak signal on its own; meant to corroborate other signals.
        """
        signals = []
        peers = self._CORRELATION_BLOCKS.get(symbol)
        if not peers:
            return signals

        peer_scores = [
            self._last[p].score
            for p in peers[:self._MAX_BREADTH_PEERS]
            if p in self._last
        ]
        if len(peer_scores) < 2:
            return signals  # not enough peers assessed yet

        avg = sum(peer_scores) / len(peer_scores)

        if avg <= -2.0:
            signals.append(RegimeSignal(
                name="breadth_negative",
                score_delta=-1,
                detail=(
                    f"{len(peer_scores)} correlated pairs avg regime score {avg:+.1f} — "
                    f"broad weakness, not pair-specific"
                ),
            ))
        elif avg >= 2.0:
            signals.append(RegimeSignal(
                name="breadth_positive",
                score_delta=+1,
                detail=(
                    f"{len(peer_scores)} correlated pairs avg regime score {avg:+.1f} — "
                    f"broad strength confirms reversal conditions"
                ),
            ))

        return signals

    # ── Signal 6: Trade Quality Pressure ─────────────────────

    def _check_quality_pressure(self, symbol: str, quality_pressure) -> List[RegimeSignal]:
        """
        Are recent trades on this symbol showing poor follow-through (low MFE/MAE)?
        Uses QualityPressure from quality tracker — passed in from coordinator.
        Only acts if n_trades >= 3 to avoid noise from single trades.
        """
        signals = []
        if quality_pressure is None:
            return signals
        if quality_pressure.pressure == 'unknown' or quality_pressure.n_trades < 3:
            return signals

        if quality_pressure.pressure == 'weak':
            signals.append(RegimeSignal(
                name="quality_pressure_weak",
                score_delta=-1,
                detail=(
                    f"Last {quality_pressure.n_trades} trades: avg F/T={quality_pressure.avg_ft_score:.0f} "
                    f"MFE/MAE={quality_pressure.avg_mfe_mae:.1f}x — slow follow-through"
                ),
            ))
        elif quality_pressure.pressure == 'strong':
            signals.append(RegimeSignal(
                name="quality_pressure_strong",
                score_delta=+1,
                detail=(
                    f"Last {quality_pressure.n_trades} trades: avg F/T={quality_pressure.avg_ft_score:.0f} "
                    f"MFE/MAE={quality_pressure.avg_mfe_mae:.1f}x — clean follow-through"
                ),
            ))

        return signals


    # ── Signal 7: Bollinger Band Context ─────────────────────

    def _check_bb_context(self, symbol: str, bars_5m) -> list:
        """
        Dual Bollinger Band state classifier.
        Band A: 20-period SMA ± 2σ on close (normal range)
        Band B:  4-period SMA ± 4σ on open  (extreme outlier / breakout)

        Score deltas:
          BREAKOUT_UP / BREAKOUT_DOWN → -2 (directional momentum, bad for mean-reversion)
          REJECTION_SHORT / REJECTION_LONG → +1 (BB snap-back confirms reversal trade)
          All other states → 0 (neutral)
        """
        ctx = compute_bb_context(bars_5m)
        if ctx is not None:
            self._last_bb[symbol] = ctx
        if ctx is None or ctx.score_delta == 0:
            return []
        return [RegimeSignal(
            name=f"bb_{ctx.state.value.lower()}",
            score_delta=ctx.score_delta,
            detail=ctx.detail,
        )]

    # ── Protest log ───────────────────────────────────────────

    def _write_protest(
        self,
        assessment:   RegimeAssessment,
        bot_action:   str,
        trade_id:     Optional[str],
        signal_score: float,
    ):
        entry = {
            "ts":           assessment.assessed_at,
            "symbol":       assessment.symbol,
            "regime":       assessment.state.value,
            "score":        assessment.score,
            "signals":      [
                {"name": s.name, "delta": s.score_delta, "detail": s.detail}
                for s in assessment.signals
            ],
            "bot_action":   bot_action,
            "signal_score": signal_score,
            "trade_id":     trade_id,
            "outcome_pnl":  None,   # filled by record_outcome()
            "outcome_sl":   None,
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _patch_protest_outcome(self, trade_id: str, pnl: float, hit_sl: bool):
        """
        Rewrite the protest log entry for this trade_id with the actual outcome.
        Uses a full file rewrite — protest log is small, this is fine.
        """
        if not os.path.exists(self.log_path):
            return
        try:
            with open(self.log_path, "r") as f:
                lines = f.readlines()
            updated = []
            for line in lines:
                try:
                    entry = json.loads(line)
                    if entry.get("trade_id") == trade_id:
                        entry["outcome_pnl"] = pnl
                        entry["outcome_sl"]  = hit_sl
                        line = json.dumps(entry) + "\n"
                except Exception:
                    pass
                updated.append(line)
            with open(self.log_path, "w") as f:
                f.writelines(updated)
        except Exception as e:
            log.warning(f"Could not patch protest log for {trade_id}: {e}")
