# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Technical indicator computations for entry context logging.
Pure functions — no side effects. All inputs are already-fetched DataFrames.
"""
import numpy as np
import pandas as pd
from typing import Optional, Dict


def compute_rsi(series: pd.Series, period: int = 14) -> Optional[float]:
    if len(series) < period + 1:
        return None
    delta = series.diff().dropna()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
    avg_loss = losses.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_ema(series: pd.Series, period: int) -> Optional[float]:
    if len(series) < period:
        return None
    return round(float(series.ewm(span=period, adjust=False).mean().iloc[-1]), 5)


def compute_macd(series: pd.Series) -> Optional[Dict]:
    if len(series) < 35:
        return None
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    hist_now  = float(histogram.iloc[-1])
    hist_prev = float(histogram.iloc[-2])
    return {
        'macd':      round(float(macd_line.iloc[-1]), 6),
        'signal':    round(float(signal_line.iloc[-1]), 6),
        'histogram': round(hist_now, 6),
        'direction': 'bullish' if hist_now > 0 else 'bearish',
        'crossed':   (hist_now >= 0) != (hist_prev >= 0),
    }


def compute_bollinger(series: pd.Series, period: int = 20) -> Optional[Dict]:
    if len(series) < period:
        return None
    sma   = series.rolling(period).mean().iloc[-1]
    std   = series.rolling(period).std().iloc[-1]
    upper = sma + 2 * std
    lower = sma - 2 * std
    price = float(series.iloc[-1])
    width = upper - lower
    pos   = (price - lower) / width if width > 0 else 0.5
    return {
        'upper':    round(float(upper), 5),
        'lower':    round(float(lower), 5),
        'mid':      round(float(sma), 5),
        'position': round(float(pos), 3),   # 0.0 = at lower band, 1.0 = at upper band
    }


def compute_z_score(series: pd.Series, window: int = 20) -> Optional[float]:
    if len(series) < window:
        return None
    mean = series.rolling(window).mean().iloc[-1]
    std  = series.rolling(window).std().iloc[-1]
    if std == 0:
        return 0.0
    return round(float((series.iloc[-1] - mean) / std), 3)


def compute_realized_vol(series: pd.Series, window: int = 20) -> Optional[float]:
    """Realized volatility in pips (std of log returns * 10000)."""
    if len(series) < window + 1:
        return None
    log_returns = np.log(series / series.shift(1)).dropna()
    vol = log_returns.rolling(window).std().iloc[-1]
    return round(float(vol * 10000), 2)


def compute_trend_1h(bars_1h: pd.DataFrame, lookback: int = 4) -> str:
    """Trend direction: are the last N 1H closes moving up or down?"""
    if bars_1h is None or len(bars_1h) < lookback + 1:
        return 'unknown'
    closes = bars_1h['close'].values
    delta  = closes[-1] - closes[-(lookback + 1)]
    if delta > 0:
        return 'bullish'
    elif delta < 0:
        return 'bearish'
    return 'flat'


def compute_adr_consumed(bars_5m: pd.DataFrame, bars_d: pd.DataFrame) -> Optional[float]:
    """Fraction of 14-day average daily range consumed by today's intraday range."""
    if bars_5m is None or bars_d is None or len(bars_d) < 15:
        return None
    daily_ranges = (bars_d['high'] - bars_d['low']).iloc[-15:-1]
    adr = float(daily_ranges.mean())
    if adr <= 0:
        return None
    today_range = float(bars_5m['high'].max()) - float(bars_5m['low'].min())
    return round(today_range / adr, 3)


def compute_touch_count(bars_5m: pd.DataFrame, pdh: float, pdl: float,
                        pip_size: float, proximity_pct: float = 0.05) -> Dict:
    """Count of 5M bars where price tested PDH or PDL today."""
    if bars_5m is None:
        return {'pdh_touches': 0, 'pdl_touches': 0}
    box_range  = pdh - pdl
    proximity  = max(box_range * proximity_pct, pip_size * 5)
    pdh_touches = int((bars_5m['high'] >= pdh - proximity).sum())
    pdl_touches = int((bars_5m['low']  <= pdl + proximity).sum())
    return {'pdh_touches': pdh_touches, 'pdl_touches': pdl_touches}


def compute_volume_ratio(bars_5m: pd.DataFrame, candle_index: int = -1,
                         lookback: int = 20) -> Optional[float]:
    """Volume of the specified candle relative to the rolling average."""
    if bars_5m is None or len(bars_5m) < lookback + 1:
        return None
    vols = bars_5m['volume'].values
    candle_vol = float(vols[candle_index])
    avg_vol    = float(np.mean(vols[-lookback - 1:-1]))
    if avg_vol <= 0:
        return None
    return round(candle_vol / avg_vol, 2)


def compute_entry_distance_pips(entry_price: float, pdh: float, pdl: float,
                                 direction: str, pip_size: float) -> float:
    """Pips from entry to the nearest box wall."""
    dist = (entry_price - pdl) if direction == 'long' else (pdh - entry_price)
    return round(abs(dist) / pip_size, 1)


def ema_aligned(ema_val: Optional[float], entry_price: float, direction: str) -> Optional[bool]:
    """True if EMA is on the correct side for the trade direction (trend-aligned)."""
    if ema_val is None:
        return None
    return entry_price > ema_val if direction == 'long' else entry_price < ema_val
