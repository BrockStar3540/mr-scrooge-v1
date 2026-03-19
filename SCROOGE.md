# SCROOGE.md — Mr. Scrooge Strategy Reference

## Overview

Mr. Scrooge is an OANDA forex mean-reversion bot. It trades the prior-day high/low box on 5-minute candles, entering on zone-and-signal confirmation, exiting via staggered harvester tiers or native TP2.

---

## Live Configuration

These are the current values from `main.py` CONFIG block.

### Risk and Sizing
| Parameter | Value | Notes |
|-----------|-------|-------|
| `margin_utilization_pct` | 0.50 | Deploy 50% of equity as margin per trade |
| `max_concurrent_positions` | 1 | One position at a time |
| `max_daily_loss_pct` | 0.075 | 7.5% daily halt |
| `min_risk_reward` | 1.60 | Minimum RR to enter |
| `max_trade_risk_pct` | 0.015 | Kept for daily-loss reference; sizing is margin-based |
| `target_profit_pct` | 0.0025 | 0.25% of equity per trade (profit-anchored sizing target) |
| `tp1_allocation_pct` | 0.40 | 40% of position closes at TP1 |
| `green_exit_min_pct` | 0.00125 | 0.125% equity minimum for green exit |
| `max_eur_positions` | 2 | EUR exposure cap |

### Timing
| Parameter | Value | Notes |
|-----------|-------|-------|
| `scan_interval_seconds` | 300 | 5-minute candle cycle |
| `rescan_interval_minutes` | 30 | Universe rescan frequency |
| `open_wait_minutes` | 15 | Wait after market open before entering |
| `max_hold_minutes` | 360 | 6-hour max hold |
| `hourly_green_exit_enabled` | True | Close net-green positions to recycle capital |
| `hourly_green_exit_minutes` | 60 | Green exit check frequency |
| `hourly_green_exit_min_pnl` | 0.002 | 0.2% net profit required to trigger |
| `limit_timeout_minutes` | 5 | Cancel unfilled limit orders after 5 min |
| `time_exit_cooldown_minutes` | 15 | Cooldown after time-based exit |

### Box and Zone Parameters
| Parameter | Value | Notes |
|-----------|-------|-------|
| `zone_proximity_pct` | 0.0025 | Must be within 0.25% of PDH/PDL to qualify as "in zone" |
| `sl_floor_pct` | 0.0050 | 0.50% minimum SL distance from entry |
| `atr_period` | 14 | ATR lookback period |
| `atr_high_confidence_pct` | 0.60 | 60% ATR threshold for high-confidence sessions |
| `atr_valid_pct` | 0.60 | Minimum ATR% to trade (filters dead-market hours) |
| `atr_minimum_pct` | 0.15 | Below 15% ATR = skip entirely |

### Spread and OTR Gates
| Parameter | Value | Notes |
|-----------|-------|-------|
| `max_spread_pct_of_half` | 0.30 | Pause entries if spread > 30% of half-box |
| `spread_spike_threshold` | 4.0 | Spike = spread > 4× London baseline |
| `otr_excellent` | 0.05 | < 5% OTR = all gates pass |
| `otr_acceptable` | 0.15 | 5–15% OTR = standard gate requirements |
| `otr_marginal` | 0.25 | 15–25% OTR = all three gates must align |
| `min_efficiency` | 2.0 | Minimum R-multiple efficiency score |
| `min_net_profit_pips` | 7 | Minimum net profit in pips (after spread) |

### Universe
| Parameter | Value | Notes |
|-----------|-------|-------|
| `instrument_tiers` | `['majors']` | Majors only |
| `universe_mode` | `'balanced'` | balanced / standard / aggressive |
| `apply_probation_size` | True | Apply size reduction for probation pairs |
| `probation_size_multiplier` | 0.65 | 65% size for probation pairs |

---

## Strategy: Box Detection

The "box" is defined by the prior trading day's high (PDH) and low (PDL). These are computed once per day from hourly candles.

**Zone classification:**
- Price is "in zone" if it is within `zone_proximity_pct` (0.25%) of PDH or PDL
- Price above PDH zone = upper breach territory
- Price below PDL zone = lower breach territory
- Price between zones = middle of box (no signal)

The `zone_detector.py` module pulls prior-day hourly bars from OANDA and extracts the 24-hour high and low for the prior calendar day (UTC). The PDH and PDL are recalculated once per day at UTC midnight reset.

---

## Strategy: Signal Engine and Box State Machine

Each instrument runs its own `BoxStateMachine` instance that tracks:
1. **IDLE** — no active zone approach
2. **APPROACHING** — price within zone proximity of PDH or PDL
3. **BREACH** — price has crossed the PDH/PDL wall
4. **PENDING** — breach confirmed, waiting for reversal signal candle
5. **SIGNAL** — reversal pattern detected, entry eligible

The `signal_engine.py` detects reversal candle patterns (pin bars, engulfing, inside bars) on the 5-minute chart. A signal is only valid when the box state machine is in PENDING state.

---

## Entry Logic

When a signal fires, the entry passes through a multi-gate check:

1. **Zone gate** — price within proximity of PDH/PDL
2. **ATR gate** — current volatility above `atr_valid_pct` threshold
3. **OTR gate** — open-to-range ratio within acceptable bounds
4. **Spread gate** — spread below `max_spread_pct_of_half` of half-box
5. **RR gate** — calculated risk/reward meets `min_risk_reward` (1.60)
6. **Net profit gate** — net profit after spread ≥ `min_net_profit_pips` (7 pips)
7. **Portfolio gate** — `max_concurrent_positions` not exceeded; EUR cap not exceeded

All gates must pass. Entry is a limit order placed at zone edge (not market). If the limit does not fill within `limit_timeout_minutes` (5 min), it is cancelled.

---

## Stop Loss and Take Profit

**Stop loss**: placed just beyond the PDH/PDL wall. Minimum distance enforced by `sl_floor_pct` (0.50% from entry). No ATR-based stop — the box wall is the stop anchor.

**Take profit:**
- TP1 = partial close target (40% of position at TP1)
- TP2 = native OANDA order to the opposite box wall (or calculated target)

Actual position management is shared between Scrooge (native TP2 order) and the harvester (partial closes at tiers before TP2).

---

## Position Sizing

Sizing is **margin-utilization-based**, not risk-anchored.

Formula: deploy `margin_utilization_pct` (50%) of current equity as the margin amount for the trade. The position size in units is back-calculated from this margin target using the current instrument price and OANDA margin rate.

Probation pairs get 65% of the calculated size (`probation_size_multiplier: 0.65`).

This approach means position size scales proportionally with account equity — larger accounts deploy proportionally more. It does not target a fixed percentage loss on the stop.

---

## Harvester Integration

Mr. Harvester runs as a separate process alongside Scrooge, polling OANDA every 30 seconds.

**Default tiers (equity %):**

| Tier | Equity Gain Trigger | Action | SL Advance |
|------|---------------------|--------|------------|
| T1 | +1.5% | Close 25% of original position | SL → break-even |
| T2 | +3.0% | Close another 25% | SL → +T1 level |
| T3 | +4.5% | Close another 25% | SL → +T2 level |
| Runner | — | Remaining 25% rides to native TP2 | — |

Equity-% thresholds are translated to pip equivalents per-pair using live pip dollar values, so all pairs target the same dollar profit per tier regardless of quote currency.

Per-pair manual pip overrides can be set from the Scrooge dashboard and take effect on the next Harvester poll cycle without restarting.

Harvester state is persisted to `harvester_state.json`. Restarts are safe — already-harvested tiers are not re-triggered.

---

## Risk Controls

**Daily halt**: when cumulative realized + unrealized loss exceeds `max_daily_loss_pct` (7.5%), no new entries are taken. Existing positions are held to their normal exits. Resets at UTC midnight.

**Spread gate**: entries paused when spread exceeds 30% of half-box. Spike detection (4× London baseline) flags abnormal spread widening.

**OTR gate**: pairs are classified as excellent (< 5%), acceptable (5–15%), or marginal (15–25%) based on the open-to-range ratio. Marginal pairs require all three secondary gates (zone, ATR, pitchfork) to align simultaneously.

**Session filter**: universe screener selects active pairs based on current session (London, New York, Tokyo, Sydney). Pairs outside their active session window are excluded.

**EUR exposure cap**: maximum 2 EUR pairs open at once (`max_eur_positions: 2`).

---

## Operations

### Systemd (EC2)
```bash
# Start
systemctl --user start mr-scrooge.service

# Stop
systemctl --user stop mr-scrooge.service

# Restart
systemctl --user restart mr-scrooge.service

# Status
systemctl --user status mr-scrooge.service
```

### Logs
```
logs/mrscrooge.log   — main runtime (stdout captured by systemd)
logs/decisions.log   — per-signal entry/exit decisions
logs/trades.log      — trade fills
```

Log location on EC2: `~/.openclaw/workspace/agents/mr-scrooge/logs/mrscrooge.log`

### Dashboard
Running on port 8081. HTTP Basic Auth required. Behind Tailscale — not publicly reachable.

### Companion Services
- `mr-harvester.service` — must be running alongside Scrooge for profit extraction
- Both services auto-restart on crash (Restart=always, 15s delay)
