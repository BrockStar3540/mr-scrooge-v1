# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Entry Context Logger

Records a rich technical snapshot at the moment of each entry.
Used for post-analysis — no trading logic, observation only.
Outcomes (pnl, mfe_pips, mae_pips) are filled in when the trade closes.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .indicators import (
    compute_rsi, compute_ema, compute_macd, compute_bollinger,
    compute_z_score, compute_realized_vol, compute_trend_1h,
    compute_adr_consumed, compute_touch_count, compute_volume_ratio,
    compute_entry_distance_pips, ema_aligned,
)

log = logging.getLogger('mr-scrooge-v2.entry_context')


class EntryContextLogger:
    """
    Writes one JSONL record per entry with full technical context.
    Call record() on fill, patch_outcome() on close.
    """

    def __init__(self, log_path: str):
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    def record(
        self,
        trade_id:    str,
        symbol:      str,
        direction:   str,       # 'long' or 'short'
        entry_price: float,
        signal,                 # SignalResult
        zone_map,               # ZoneMap
        bars_5m:     pd.DataFrame,
        bars_1h:     pd.DataFrame,
        bars_d:      pd.DataFrame,
        oanda_client,
        regime_assessment=None,
    ) -> None:
        pip_size = 0.01 if 'JPY' in symbol else 0.0001
        pdh = zone_map.daily_box.PDH if zone_map else 0.0
        pdl = zone_map.daily_box.PDL if zone_map else 0.0

        closes_1h = bars_1h['close'] if bars_1h is not None else pd.Series(dtype=float)

        # ── 1H indicators ────────────────────────────────────
        rsi    = compute_rsi(closes_1h)
        ema20  = compute_ema(closes_1h, 20)
        ema50  = compute_ema(closes_1h, 50)
        macd   = compute_macd(closes_1h)
        bb     = compute_bollinger(closes_1h)
        z      = compute_z_score(closes_1h)
        rvol   = compute_realized_vol(closes_1h)
        trend  = compute_trend_1h(bars_1h)

        # ── 5M / computed ────────────────────────────────────
        vol_ratio = compute_volume_ratio(bars_5m)
        adr_pct   = compute_adr_consumed(bars_5m, bars_d)
        touches   = compute_touch_count(bars_5m, pdh, pdl, pip_size)
        dist      = compute_entry_distance_pips(entry_price, pdh, pdl, direction, pip_size)

        # ── OANDA books ──────────────────────────────────────
        pos_book   = _safe(lambda: oanda_client.get_position_book(symbol))
        order_book = _safe(lambda: oanda_client.get_order_book_summary(
            symbol, entry_price, pdh, pdl, pip_size))

        # ── Regime snapshot ──────────────────────────────────
        regime_state   = regime_assessment.state.value if regime_assessment else 'unknown'
        regime_score   = regime_assessment.score       if regime_assessment else None
        regime_signals = (
            [s.name for s in regime_assessment.signals]
            if regime_assessment else []
        )

        entry = {
            'ts':           datetime.now(timezone.utc).isoformat(),
            'trade_id':     trade_id,
            'symbol':       symbol,
            'direction':    direction,
            'entry_price':  entry_price,
            'signal_type':  (signal.pitchfork.signal_type if signal.pitchfork else 'unknown'),
            'signal_score': signal.score,
            'zone_tier':    signal.zone_tier,

            # Regime
            'regime_state':   regime_state,
            'regime_score':   regime_score,
            'regime_signals': regime_signals,

            # 1H technical
            'rsi_1h':          rsi,
            'ema20_1h':        ema20,
            'ema50_1h':        ema50,
            'ema20_aligned':   ema_aligned(ema20, entry_price, direction),
            'ema50_aligned':   ema_aligned(ema50, entry_price, direction),
            'macd_direction':  macd.get('direction')  if macd else None,
            'macd_histogram':  macd.get('histogram')  if macd else None,
            'macd_crossed':    macd.get('crossed')    if macd else None,
            'bb_position_1h':  bb.get('position')     if bb   else None,
            'realized_vol_1h': rvol,
            'z_score_1h':      z,
            'trend_1h':        trend,

            # 5M / computed
            'volume_ratio_5m':     vol_ratio,
            'adr_consumed_pct':    adr_pct,
            'pdh_touches':         touches.get('pdh_touches'),
            'pdl_touches':         touches.get('pdl_touches'),
            'entry_distance_pips': dist,

            # OANDA books
            'oanda_pct_long':          pos_book.get('pct_long')       if pos_book   else None,
            'oanda_pct_short':         pos_book.get('pct_short')      if pos_book   else None,
            'order_book_near_pdh_pct': order_book.get('near_pdh_pct') if order_book else None,
            'order_book_near_pdl_pct': order_book.get('near_pdl_pct') if order_book else None,

            # Outcomes — filled in on close
            'outcome_pnl':      None,
            'outcome_sl':       None,
            'outcome_mfe_pips': None,
            'outcome_mae_pips': None,
        }

        with open(self.log_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

        log.info(
            f'{symbol}: context logged | rsi={rsi} ema20_aligned={entry["ema20_aligned"]} '
            f'bb={entry["bb_position_1h"]} z={z} vol_ratio={vol_ratio} '
            f'adr={adr_pct} long%={entry["oanda_pct_long"]}'
        )

    def patch_outcome(
        self,
        trade_id:  str,
        pnl:       float,
        hit_sl:    bool,
        mfe_pips:  Optional[float] = None,
        mae_pips:  Optional[float] = None,
    ) -> None:
        """Fill in outcome fields when the trade closes."""
        if not os.path.exists(self.log_path):
            return
        try:
            with open(self.log_path, 'r') as f:
                lines = f.readlines()
            updated = []
            for line in lines:
                try:
                    rec = json.loads(line)
                    if rec.get('trade_id') == trade_id:
                        rec['outcome_pnl']      = pnl
                        rec['outcome_sl']       = hit_sl
                        rec['outcome_mfe_pips'] = mfe_pips
                        rec['outcome_mae_pips'] = mae_pips
                        line = json.dumps(rec) + '\n'
                except Exception:
                    pass
                updated.append(line)
            with open(self.log_path, 'w') as f:
                f.writelines(updated)
        except Exception as e:
            log.warning(f'patch_outcome failed for {trade_id}: {e}')


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None
