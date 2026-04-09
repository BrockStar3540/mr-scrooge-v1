# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Trade Quality Tracker — Passive Observer

Tracks post-entry behavior for every trade:
  MFE  — how far the trade went in our favor
  MAE  — how far it went against us before recovering (or stopping)
  F/T  — follow-through score (composite 0-100)

Data is logged to quality-trades.jsonl. It does not influence entries.

After 3 months, query the log to answer:
  - Do trades with MAE > 5 pips at entry have worse outcomes? → tighten zone proximity
  - Does follow-through correlate with signal score? → find the real score threshold
  - Are slow trades (>12 cycles to +5 pips) worse? → add time filter
  - Does quality degrade at certain times of day / sessions?

Only activate adaptive filtering once you have answers with N >= 50 trades.
"""

import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .models import LiveExcursion, QualityPressure, TradeQualityRecord

log = logging.getLogger('mr-scrooge-v2.quality')

# Thresholds for QualityPressure classification
_STRONG_FT  = 65.0   # avg follow-through above this → 'strong'
_WEAK_FT    = 40.0   # below this → 'weak'
_STRONG_MMR = 2.5    # MFE/MAE ratio above this → strong
_WEAK_MMR   = 1.2    # below this → weak
_RECENT_N   = 5      # how many recent trades to evaluate pressure on


class TradeQualityTracker:
    """
    Passive observer. Never modifies entries, never blocks anything.
    Logs every trade's MFE/MAE/follow-through to JSONL.
    """

    def __init__(self, log_path: str):
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        # Active trades being tracked
        self._live: Dict[str, LiveExcursion] = {}

        # Per-symbol history of finalized records (for quality pressure calc)
        self._history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=20))

    # ── Lifecycle ─────────────────────────────────────────────

    def open_trade(
        self,
        trade_id:     str,
        symbol:       str,
        direction:    str,
        entry_price:  float,
        entry_time:   str,
        stop_loss:    float,
        tp1_price:    float,
        tp2_price:    float,
        signal_score: float = 0.0,
    ):
        """Called immediately after fire_entry succeeds."""
        pip_size = 0.01 if 'JPY' in symbol else 0.0001
        self._live[trade_id] = LiveExcursion(
            trade_id=trade_id, symbol=symbol, direction=direction,
            entry_price=entry_price, entry_time=entry_time,
            stop_loss=stop_loss, tp1_price=tp1_price, tp2_price=tp2_price,
            pip_size=pip_size, signal_score=signal_score,
        )
        log.debug(f'Quality tracking opened: {trade_id} {symbol} {direction} @ {entry_price}')

    def update(self, trade_id: str, current_price: float):
        """
        Called each 5-min cycle for every open position.
        Updates MFE, MAE, milestones.
        """
        exc = self._live.get(trade_id)
        if exc is None:
            return

        exc.cycles_elapsed += 1
        pips = exc.pip_distance(exc.entry_price, current_price)
        exc.current_pips = pips

        # Update MFE / MAE
        if pips > exc.mfe_pips:
            exc.mfe_pips = pips
        if -pips > exc.mae_pips:   # adverse = negative pips
            exc.mae_pips = -pips

        # Milestones (set once)
        if exc.cycles_to_5pips is None and pips >= 5.0:
            exc.cycles_to_5pips = exc.cycles_elapsed
            log.debug(f'{trade_id}: +5 pips in {exc.cycles_elapsed} cycles')

        if exc.cycles_to_10pips is None and pips >= 10.0:
            exc.cycles_to_10pips = exc.cycles_elapsed

        tp1_pips = exc.pip_distance(exc.entry_price, exc.tp1_price)
        if exc.cycles_to_tp1 is None and pips >= tp1_pips > 0:
            exc.cycles_to_tp1 = exc.cycles_elapsed
            exc.hit_tp1 = True

        tp2_pips = exc.pip_distance(exc.entry_price, exc.tp2_price)
        if not exc.hit_tp2 and pips >= tp2_pips > 0:
            exc.hit_tp2 = True

    def close_trade(
        self,
        trade_id:     str,
        close_price:  float,
        close_reason: str,
        hit_sl:       bool,
    ) -> Optional[TradeQualityRecord]:
        """
        Called when a position closes. Finalizes the record and writes to log.
        Returns the record (for coordinator to log alongside trade close).
        """
        exc = self._live.pop(trade_id, None)
        if exc is None:
            return None

        final_pips = exc.pip_distance(exc.entry_price, close_price)
        mfe_mae    = exc.mfe_pips / max(exc.mae_pips, 0.1)  # avoid divide-by-zero
        ft_score   = self._score_follow_through(exc, final_pips)

        record = TradeQualityRecord(
            trade_id=trade_id, symbol=exc.symbol, direction=exc.direction,
            entry_time=exc.entry_time, close_time=datetime.now(timezone.utc).isoformat(),
            entry_price=exc.entry_price, close_price=close_price,
            signal_score=exc.signal_score,
            mfe_pips=exc.mfe_pips, mae_pips=exc.mae_pips, final_pips=final_pips,
            cycles_elapsed=exc.cycles_elapsed,
            cycles_to_5pips=exc.cycles_to_5pips,
            cycles_to_10pips=exc.cycles_to_10pips,
            cycles_to_tp1=exc.cycles_to_tp1,
            mfe_mae_ratio=round(mfe_mae, 2),
            follow_through=ft_score,
            hit_sl=hit_sl, hit_tp1=exc.hit_tp1, hit_tp2=exc.hit_tp2,
            close_reason=close_reason,
        )

        self._history[exc.symbol].append(record)
        self._write(record)

        log.info(
            f'Quality finalized: {trade_id} | {exc.symbol} {exc.direction} | '
            f'MFE={exc.mfe_pips:.1f}p MAE={exc.mae_pips:.1f}p final={final_pips:.1f}p '
            f'F/T={ft_score:.0f} MFE/MAE={mfe_mae:.1f}x | {close_reason}'
        )
        return record

    # ── Quality pressure ─────────────────────────────────────

    def get_quality_pressure(self, symbol: str) -> QualityPressure:
        """
        Summarizes recent trade quality for one symbol.
        Logged by coordinator each cycle — never used to gate entries (yet).
        """
        history = list(self._history[symbol])
        recent  = history[-_RECENT_N:] if len(history) >= 2 else history

        if not recent:
            return QualityPressure(
                symbol=symbol, n_trades=0, pressure='unknown',
                avg_mfe_pips=0.0, avg_mae_pips=0.0, avg_mfe_mae=0.0,
                avg_ft_score=0.0, avg_cycles_to_5pips=None,
                detail='insufficient data',
            )

        avg_mfe     = sum(r.mfe_pips for r in recent) / len(recent)
        avg_mae     = sum(r.mae_pips for r in recent) / len(recent)
        avg_mmr     = sum(r.mfe_mae_ratio for r in recent) / len(recent)
        avg_ft      = sum(r.follow_through for r in recent) / len(recent)
        to5         = [r.cycles_to_5pips for r in recent if r.cycles_to_5pips is not None]
        avg_to5     = sum(to5) / len(to5) if to5 else None

        # Classify pressure
        if avg_ft >= _STRONG_FT and avg_mmr >= _STRONG_MMR:
            pressure = 'strong'
            detail   = f'avg F/T={avg_ft:.0f} MFE/MAE={avg_mmr:.1f}x — clean follow-through, entries well-timed'
        elif avg_ft <= _WEAK_FT or avg_mmr <= _WEAK_MMR:
            pressure = 'weak'
            detail   = f'avg F/T={avg_ft:.0f} MFE/MAE={avg_mmr:.1f}x — slow/choppy, entries fighting market'
        else:
            pressure = 'normal'
            detail   = f'avg F/T={avg_ft:.0f} MFE/MAE={avg_mmr:.1f}x'

        return QualityPressure(
            symbol=symbol, n_trades=len(recent), pressure=pressure,
            avg_mfe_pips=round(avg_mfe, 1), avg_mae_pips=round(avg_mae, 1),
            avg_mfe_mae=round(avg_mmr, 2), avg_ft_score=round(avg_ft, 1),
            avg_cycles_to_5pips=round(avg_to5, 1) if avg_to5 else None,
            detail=detail,
        )

    def get_live_summary(self) -> dict:
        """Current open trades being tracked."""
        return {
            tid: {
                'symbol':   e.symbol,
                'direction': e.direction,
                'current_pips': round(e.current_pips, 1),
                'mfe_pips': round(e.mfe_pips, 1),
                'mae_pips': round(e.mae_pips, 1),
                'cycles':   e.cycles_elapsed,
            }
            for tid, e in self._live.items()
        }

    # ── Scoring ───────────────────────────────────────────────

    def _score_follow_through(self, exc: LiveExcursion, final_pips: float) -> float:
        """
        Follow-through score 0-100. Higher = cleaner, well-behaved trade.

        Components:
          30 pts — MFE/MAE ratio (clean entry)
          25 pts — reached +5 pips quickly (< 6 cycles = 30 min)
          25 pts — MFE reached at least 80% of TP1 distance
          20 pts — final result positive
        """
        score = 0.0
        tp1_pips = exc.pip_distance(exc.entry_price, exc.tp1_price)

        # MFE/MAE ratio (0-30)
        mae = max(exc.mae_pips, 0.1)
        mmr = exc.mfe_pips / mae
        if mmr >= 4.0:
            score += 30
        elif mmr >= 2.5:
            score += 22
        elif mmr >= 1.5:
            score += 14
        elif mmr >= 1.0:
            score += 7

        # Speed to +5 pips (0-25)
        if exc.cycles_to_5pips is not None:
            if exc.cycles_to_5pips <= 3:    # within 15 min
                score += 25
            elif exc.cycles_to_5pips <= 6:  # within 30 min
                score += 18
            elif exc.cycles_to_5pips <= 12: # within 1 hour
                score += 10
            else:
                score += 5
        # Never hit +5 pips at all = 0 for this component

        # MFE vs TP1 distance (0-25)
        if tp1_pips > 0:
            mfe_pct = exc.mfe_pips / tp1_pips
            if mfe_pct >= 1.0:    # hit TP1
                score += 25
            elif mfe_pct >= 0.8:
                score += 20
            elif mfe_pct >= 0.5:
                score += 12
            elif mfe_pct >= 0.25:
                score += 5

        # Final result positive (0-20)
        if final_pips > 0:
            score += 20
        elif final_pips > -3:
            score += 8   # near breakeven is still ok

        return min(score, 100.0)

    # ── Persistence ───────────────────────────────────────────

    def _write(self, record: TradeQualityRecord):
        with open(self.log_path, 'a') as f:
            f.write(json.dumps(record.to_dict()) + '\n')
