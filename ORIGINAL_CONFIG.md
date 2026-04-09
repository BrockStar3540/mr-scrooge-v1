# ORIGINAL CONFIG — Mr. Scrooge V1
# These are the original settings that were performing well before the March 25-26 changes.
# Stored for reference. If the system needs to be rolled back, use these values.

## Risk and Sizing
| Parameter | Value | Notes |
|-----------|-------|-------|
| margin_utilization_pct | 0.50 | Deploy 50% of equity as margin per trade |
| max_concurrent_positions | 1 | One position at a time |
| max_daily_loss_pct | 0.075 | 7.5% daily halt |
| min_risk_reward | 1.60 | Minimum RR to enter |
| max_trade_risk_pct | 0.015 | Kept for daily-loss reference; sizing is margin-based |
| target_profit_pct | 0.0025 | 0.25% of equity per trade (profit-anchored sizing target) |
| tp1_allocation_pct | 0.40 | 40% of position closes at TP1 |
| green_exit_min_pct | 0.00125 | 0.125% equity minimum for green exit |
| max_eur_positions | 2 | EUR exposure cap |

## Timing
| Parameter | Value | Notes |
|-----------|-------|-------|
| scan_interval_seconds | 300 | 5-minute candle cycle |
| rescan_interval_minutes | 30 | Universe rescan frequency |
| open_wait_minutes | 15 | Wait after market open before entering |
| max_hold_minutes | 360 | 6-hour max hold |
| hourly_green_exit_enabled | True | Close net-green positions to recycle capital |
| hourly_green_exit_minutes | 60 | Green exit check frequency |
| hourly_green_exit_min_pnl | 0.002 | 0.2% net profit required to trigger |
| limit_timeout_minutes | 5 | Cancel unfilled limit orders after 5 min |
| time_exit_cooldown_minutes | 15 | Cooldown after time-based exit |

## Box and Zone Parameters
| Parameter | Value | Notes |
|-----------|-------|-------|
| zone_proximity_pct | 0.0025 | Must be within 0.25% of PDH/PDL to qualify as in zone |
| sl_floor_pct | 0.0050 | 0.50% minimum SL distance from entry |
| atr_period | 14 | ATR lookback period |
| atr_high_confidence_pct | 0.60 | 60% ATR threshold for high-confidence sessions |
| atr_valid_pct | 0.60 | Minimum ATR% to trade |
| atr_minimum_pct | 0.15 | Below 15% ATR = skip entirely |

## Spread and OTR Gates
| Parameter | Value | Notes |
|-----------|-------|-------|
| max_spread_pct_of_half | 0.30 | Pause entries if spread > 30% of half-box |
| spread_spike_threshold | 4.0 | Spike = spread > 4x London baseline |
| otr_excellent | 0.05 | < 5% OTR = all gates pass |
| otr_acceptable | 0.15 | 5-15% OTR = standard gate requirements |
| otr_marginal | 0.25 | 15-25% OTR = all three gates must align |
| min_efficiency | 2.0 | Minimum R-multiple efficiency score |
| min_net_profit_pips | 7 | Minimum net profit in pips (after spread) |

## Universe
| Parameter | Value | Notes |
|-----------|-------|-------|
| instrument_tiers | majors | Majors only |
| universe_mode | balanced | balanced / standard / aggressive |
| apply_probation_size | True | Apply size reduction for probation pairs |
| probation_size_multiplier | 0.65 | 65% size for probation pairs |

## Harvester (Original)
| Parameter | Value | Notes |
|-----------|-------|-------|
| T1 | 1.5% equity (~4.5p) | Close 25%, SL stays |
| T2 | 3.0% equity (~9p) | Close 25%, SL to +1.5% |
| T3 | 4.5% equity (~13p) | Close 25%, SL to +3.0% |
| Runner | 25% | Rides to TP2 (OANDA native limit) |
| Stagnant | 6hr + 2p profit | Limit TP at bid/ask |

## Ratchet (Original)
| Stage | Trigger | Action |
|-------|---------|--------|
| Stage 0 | Reconciled trade +5p | SL to entry |
| Stage 1 | 50% of way to TP1 | SL to entry (breakeven) |
| Stage 2 | Price passes TP1 | SL to TP1 level |
| Stage 3 | After BE | Hybrid trail: max(1H candle, 80% lock, peak-1.5xATR) |

## Strategy: Box Detection
The box is defined by the prior trading day high (PDH) and low (PDL), computed once per day from hourly candles.
Price in zone if within zone_proximity_pct (0.25%) of PDH or PDL.
PDH/PDL recalculated at UTC midnight reset.
