# Mr. Scrooge — Operational Reference

## Identity
- Bot: Mr. Scrooge (V1 — Live)
- Venue: OANDA (forex, live account [REDACTED-ACCOUNT])
- EC2 path: `~/.openclaw/workspace/agents/mr-scrooge`
- Entry: `main.py` via `start.sh`
- Service: `systemctl --user {start|stop|restart|status} mr-scrooge.service`
- Logs: `logs/mrscrooge.log`, `logs/decisions.log`, `logs/trades.log`
- Dashboard: `https://[REDACTED-HOST]:8081` (HTTP Basic Auth, Tailscale only)

## Performance Cutoff
Valid performance analysis starts: **2026-03-04 23:00:00 UTC**
Data before this date contains pre-fix noise and should be excluded.

## Current Configuration (as of 2026-03-25)
All values live in `CONFIG` dict in `main.py`. Runtime overrides via dashboard.

### Sizing — Dual-Constraint Model (March 25 upgrade)
Position size = `min(margin_units, risk_cap_units)`

| Constraint | Formula | Purpose |
|------------|---------|---------|
| Margin target | `(equity × 0.80) / (base_price_usd × margin_rate)` | 80% capital deployment |
| **Risk cap** | `(equity × 0.04) / (SL_pips × pip_value_per_unit)` | **4% max loss per trade** |

- Whichever produces fewer units wins
- Tight SL → full 80% exposure (margin-dominant)
- Wide SL → reduced size (risk-dominant)
- `max_risk_per_trade_pct = 0.04` — no single trade can lose more than 4% of equity
- Equity source: `balance` (cash only, excludes unrealized P&L)
- `max_concurrent_positions = 1`

### SL / TP — Box-Derived Targets
SL and TP are derived from the daily box structure, not fixed pips.

| Level | Source | Purpose |
|-------|--------|---------|
| Stop Loss | Pitchfork stop (beyond zone wall) | Structural stop |
| TP1 | Daily box center | Ratchet trigger + harvester gate |
| TP2 | Opposite box wall (PDH for longs, PDL for shorts) | OANDA native TP — full exit |

**TP2 market-price guard** (March 25): Orders blocked if TP2 is within 2 pips of current market. Prevents 400 Bad Request when filtered box wall is already consumed. Checked at signal generation AND pre-submit.

### Ratchet Stages (March 25 upgrades)
Stop movements wired to OANDA via `execution_router.move_stop()`.

1. **+5 pips profit**: SL → entry + 1 pip buffer (spread-safe breakeven)
2. **TP1 hit** (center of box): SL → TP1 level
3. **Above TP1**: Hybrid trailing stop with **adaptive lock**:
   - Pre-TP1: **55% profit lock** (loose — let trade breathe)
   - Post-TP1: **75% profit lock** (tighter — protect confirmed move)
   - Plus: hourly candle trail, 1.5× ATR trail
   - Lock + ATR activate when profit > 1× ATR
4. **TP2**: OANDA native TP closes full remaining position (limit fill)

### Harvester (Separate Daemon — March 25 upgrades)
Service: `mr-harvester.service`
Path: `~/.openclaw/workspace/agents/mr-scrooge/harvester/`

| Setting | Value | Notes |
|---------|-------|-------|
| T1 | 3.0% of equity (~8-10p) | Close 25%, SL stays |
| T2 | 5.0% of equity (~15-18p) | Close 25%, SL → +T1 |
| T3 | **Removed** | Was 4.5% — killed runner too early |
| Runner | 50% rides to TP2 | Was 25% with T3 active |
| **BE-gate** | Must achieve breakeven before any harvest | Inferred from OANDA SL ≥ entry |
| Stagnant timeout | **90 min** + profit < T1 | Was 6hr blanket. Trades above T1 run freely |
| Poll interval | 30 seconds | |
| Close method | Limit first (90s), market fallback | |

Config: `harvester/harvester_config.json`
State: `harvester/harvester_state.json`

### Risk Gates
- `min_risk_reward = 1.60`
- `max_daily_loss_pct = 0.075` (7.5% daily halt)
- `otr_excellent = 0.05`, `otr_acceptable = 0.15`, `otr_marginal = 0.25`
- `min_net_profit_pips = 7`

### Timing
- `scan_interval_seconds = 300` (5 min candle cycle)
- `rescan_interval_minutes = 30`
- `open_wait_minutes = 15`
- `max_hold_minutes = 360` (6 hours)
- `time_exit_cooldown_minutes = 15`

### Box / Zone
- Box mode: daily PDH/PDL with percentile filter + gap detection
- `zone_proximity_pct = 0.0025`
- `atr_valid_pct = 0.60`, `atr_minimum_pct = 0.15`
- `max_spread_pct_of_half = 0.30`
- Universe mode: `standard` (session-aware)

## Opportunity Scoring (0-100 pts)

| Factor | Points |
|--------|--------|
| Zone tier 1 / 2 / 3 / other | 30 / 20 / 10 / 5 |
| ATR strength: strong / valid / weak | 20 / 15 / 5 |
| OTR: excellent / acceptable / marginal | 15 / 10 / 5 |
| R:R >= 3.0 / >= 2.0 / >= 1.5 | 15 / 10 / 5 |
| Signal: john_wick / power_of_towers | 10 |
| Standard pitchfork | 5 |
| Pitchfork confidence: HIGH / MODERATE | 5 / 3 |

## Quick Ops
```bash
# Status (all 3 services)
systemctl --user status mr-scrooge.service mr-scrooge-dashboard.service mr-harvester.service

# Restart bot
systemctl --user restart mr-scrooge.service

# Restart harvester (separate daemon)
systemctl --user restart mr-harvester.service

# Tail decisions
tail -f logs/decisions.log

# Tail fills
tail -f logs/trades.log

# Tail harvester actions
tail -f harvester/logs/harvests.log
```

## Log Rotation
Managed by `/etc/logrotate.d/mr-scrooge-logs`:
- Daily rotation, 20 days retained, compressed (`.gz`)
- `copytruncate` — safe for running processes
- Covers: mrscrooge.log, decisions.log, trades.log, launchd.log, dashboard.log, harvester logs

---

## 2026-03-26 — sl_floor_pct Investigation + Fix

### Problem
Both V1 and V2 went silent for extended periods including the entire NY session on 2026-03-25.
Investigation showed all signal gates (zone, ATR, sucker move, pitchfork, OTR) were passing
cleanly — the only blocker was `RR < 1.6` across all active pairs.

### Root Cause
`sl_floor_pct = 0.0050` (0.50%) was enforcing a minimum stop of ~54 pips on EUR pairs at 1.08.
This inflated the RR denominator, making it nearly impossible to reach 1.6x reward on normal
intraday setups. The best pairs on the March 25 NY session (USD selloff / tariff news day) were
EUR_AUD and EUR_NZD at RR 1.35 — 16% short of the 1.6 minimum, all session.

The floor had been raised from 0.30% to 0.50% at some earlier point with a comment "too tight",
but the 0.50% overcorrected and was the dominant constraint crushing trade frequency.

### Fix
Lowered `sl_floor_pct` from 0.50% → 0.25% (`0.0025`).
At EUR/USD 1.08 that is ~27 pip minimum stop (was ~54 pips).

### Immediate Effect (2026-03-26 03:56 UTC)
On the first cycle after restarting at 0.25%:
- **V1**: ENTERED USD_JPY SHORT @ 159.444 | SL 159.881 (43.7p) | TP1 158.877 | Signal: power_of_towers
- **V2**: Signal QUALIFIED (all gates passed), but REGIME_BLOCKED (score -4.0)
  - Reason: 6 consecutive closes above PDH 159.181 — price holding outside box
  - Reason: 8.1w failed rejections at that wall in last 24H
  - This is correct regime behavior — V2 identified the PDH wall had been failing all day
  - V1 is regime observer-only so it took the trade; V2 actively enforces

### Monitoring
Watching the next few days to confirm 0.25% floor isn't too loose (stops getting hit before
trades have room to develop). If stop-hit rate increases, step back up to 0.30%.

### V2 Logging Bug Also Fixed (same session)
V2's decisions log was showing `sucker=False fork=False otr=False` even when those gates passed
and RR was the only blocker. The `_no_trade()` call at the RR check point wasn't forwarding the
preceding gate kwargs — causing False display values. Fixed in `signals/engine.py`. Gate states
now correctly show True when RR is the final blocker.

---

## 2026-03-31 — Data Analysis + Major Config Overhaul

### Background
After collecting trade data from March 29–31, we ran a full analysis of what was working and what
wasn't. Both bots were producing 43% WR with negative expectancy under the prior settings.

### Analysis Findings

**Best day: March 24**
- V2 was REGIME_BLOCKED on all losing setups (GBP_CAD, GBP_AUD, AUD_NZD, EUR_USD — all with
  7–10 week rejection failure chains)
- The one winning trade (AUD_CAD) had `box_contained(+1)` + `compression(+1)` = score +2
  (NORMAL). Price sitting quietly inside box + ATR compressed = highest-quality reversal setup.
- That is the setup the regime filter was designed to protect.

**Worst trades post-March 29**
- USD/MXN (V2): Regime BLOCKED 8+ times in a row (rejection_failed: 10.2 weeks).
  When price briefly touched PDH, box_acceptance cleared for one candle, score moved from -4
  to -2 (no longer BLOCKED under old rules), and V2 entered. Lost.
  Root cause: old threshold only blocked on BLOCKED state (score ≤ -3); RESTRICTED (score -2)
  was allowed through.
- EUR/USD (V2): Entered on regime score = 0 (CAUTION/neutral). EUR was in clear uptrend all
  day. Score 0 means no positive signals at all — nothing actually argued FOR a reversal.
  Root cause: no score floor, CAUTION was treated as acceptable.

**Simulation of fixes (127 trades, March 29–31 window)**
- Current settings: -$163.81 (43.3% WR)
- Regime proxy + balanced universe + original margin (0.50): +$32.04 (48.9% WR)
- Key driver: regime filter blocking the structural losers

### Changes Made

#### 1. Universe Mode → Aggressive (both bots)
- `universe_mode: 'aggressive'` — all 26 CANDIDATE_PAIRS scanned every cycle, all sessions
- Removed: `apply_probation_size`, `probation_size_multiplier` — static probation list replaced
  by dynamic regime scoring. Probation pairs still defined in code but have no special treatment.

#### 2. Harvester Tiers Restored (both V1 + V2 harvesters)
Original ORIGINAL_CONFIG.md tiers restored. Previous tiers (3.0/5.0/7.0%) were too wide —
trades were getting stopped out before ever reaching T1.

| Tier | Old | New | Action |
|------|-----|-----|--------|
| T1 | 3.0% equity | **1.5% equity** | Close 25%, SL stays |
| T2 | 5.0% equity | **3.0% equity** | Close 25%, SL → +1.5% |
| T3 | 7.0% equity | **4.5% equity** | Close 25%, SL → +3.0% |
| Runner | 25% | **25%** | Rides to TP2 (OANDA native) |

Files changed: `harvester/main.py` (V1), `mr-scrooge-v2/harvester/main.py` (V2)

#### 3. Spread Consistency Gate (both bots)
Old: spread exceeding threshold on any single scan → entries immediately paused.
New: spread must exceed threshold on **2 consecutive scans** (10 minutes) before pausing.
Prevents single-candle spread spikes (common in Sydney/Sunday) from blocking valid setups.

Files changed: `gates.py` (V1), `mr-scrooge-v2/box/models.py` + `box/state_machine.py` (V2)

#### 4. Regime Score Floor (both bots)
Old: only BLOCKED state (score ≤ -3) suppressed entries. RESTRICTED and CAUTION allowed through.
New: entry requires **score ≥ +1**. CAUTION (score = 0) = wait. At least one positive signal
must be present.

Log tag: `REGIME_FLOOR` when blocked due to score < +1 with no recent bad history.

#### 5. Regime Hysteresis (both bots)
New: if regime score was ≤ -2 (RESTRICTED or BLOCKED) in either of the last 2 scans (10 min),
entry is still blocked even if current score has improved, until score ≥ +1 is confirmed.

Log tag: `REGIME_HYSTERESIS` distinguishes recoveries from clean floor blocks.
Implemented via `_last_regime_scores` per-symbol deque (maxlen=2).

Addresses the USD/MXN incident where a one-candle improvement in box_acceptance allowed
entry into a 10-week rejection-failure regime.

#### 6. V1 Regime Filter Activated
V1 was observer-only — it logged "protests" but never blocked anything.
V1 regime is now an **active blocker**, using the same logic as V2:
- BLOCKED → suppress entry
- Score < +1 → suppress entry (score floor)
- RESTRICTED → halve position size (trade allowed)

Files changed: `main.py` (V1)

### Current Settings Summary (both bots)

| Parameter | V1 | V2 |
|-----------|----|----|
| margin_utilization_pct | 0.80 | 0.80 |
| max_concurrent_positions | 1 | 1 |
| universe_mode | aggressive | aggressive |
| probation_size_multiplier | removed | removed |
| regime filter | **ACTIVE** (was observer-only) | active |
| regime entry threshold | score ≥ +1 | score ≥ +1 |
| regime hysteresis | 2-scan lookback | 2-scan lookback |
| spread gate | 2 consecutive scans | 2 consecutive scans |
| T1 harvester | 1.5% equity | 1.5% equity |
| T2 harvester | 3.0% equity | 3.0% equity |
| T3 harvester | 4.5% equity | 4.5% equity |

### What to Watch
- Regime log tags: `REGIME_BLOCKED`, `REGIME_RESTRICTED`, `REGIME_FLOOR`, `REGIME_HYSTERESIS`
  in `logs/decisions.log` — confirm they're firing on the expected pairs
- Trade frequency may drop initially as the score floor filters CAUTION setups
- If WR improves but frequency drops too much, consider whether score ≥ 0 is sufficient
  (removing the floor) while keeping hysteresis
- Harvester: T1 at 1.5% should trigger much earlier — watch `harvester/logs/harvests.log`

---

## 2026-04-09 — OANDA Truth Audit + Scoring Recalibration

### Background
Two independent audits cross-referenced OANDA API trade data (511 V1 trades, 58 V2 trades) against
bot logs. OANDA candle data was used to classify trend/ranging/counter-trend independently of the
bot's own classification. Key finding: **bot logs lied about what was counter-trend vs ranging.**

### Critical Findings

**1. Scoring system had zero predictive power (r≈0 with P/L)**
Higher scores predicted worse outcomes. The 70-79 score bracket was the biggest loser.

**2. Ranging markets are the #1 profit killer (not counter-trend)**
| Trend | V1 Trades | V1 P/L | V1 PF | V2 PF |
|-------|-----------|--------|-------|-------|
| With-trend | 46 | +$90 | 1.91 | — |
| Counter-trend | 240 | +$34 | 1.03 | 0.97 |
| **Ranging** | **225** | **-$344** | **0.33** | **0.33** |

**3. CT Tier 1 = worst, CT Tier 2 = best (inverted from theory)**
| CT Zone | Trades | WR | P/L |
|---------|--------|----|-----|
| CT Tier 1 (PDH/PDL) | 70 | 45.7% | -$537 |
| CT Tier 2 (PHH/PHL) | 15 | 66.7% | +$140 |

**4. john_wick profitable, power_of_towers bleeds**
- john_wick: +$1,267 (V2), near breakeven (V1)
- power_of_towers: -$4,086 (V2), -$288 (V1)

**5. NORMAL regime = edge, CAUTION = bleed**
- NORMAL: 77.8% WR, +$5,037
- CAUTION: -$5,719

**6. R:R against TP1 (r=0.68) is the strongest single predictor**

### Scoring Changes Applied

**Signal reweighting (Layer 2):**
- john_wick: +15 (was +10)
- power_of_towers: +5 (was +10)

**New Layer 3b modifiers:**
| Modifier | Pts | Evidence |
|----------|-----|----------|
| Ranging market | -20 | PF 0.33 both bots |
| CT + power_of_towers | -15 | -$339 V1, -$4,086 V2 |
| CT + john_wick | -5 | Near breakeven |
| CT Tier 1 | -10 | 45.7% WR, -$537 |
| CT Tier 2 | +5 | 66.7% WR, +$140 |
| z-score alignment | +5 | r=+0.38 |

**Regime scoring (in main.py):**
- NORMAL: +10 pts
- CAUTION: -10 pts

**R:R scoring:** Uses TP1 (not TP2) for bonus buckets. TP2 hard gate (1.60 min) unchanged.
Buckets recalibrated: >= 2.0: +15, >= 1.5: +10, >= 1.0: +5

### Bug Fixes Applied

1. **P/L currency conversion** — `_calculate_partial_pnl()` now converts from quote currency to USD
   for non-USD-quote pairs. `pip_value_per_unit` stored on OpenPosition at entry.
2. **Overhead cost fix** — `overhead_cost_usd` converted using pip_value.
3. **OANDA 404 fallback** — `get_trade_realized_pl()` falls back to transactions API when trade
   endpoint returns 404 for closed trades.
4. **TP sanity check** — TP2 clamped at 3x daily ATR to prevent absurd targets from stale box data.
5. **Dead config removed** — `sl_pips`, `tp1_pips`, `tp2_pips` deleted from CONFIG (zone-anchored
   targets replaced them long ago).
6. **Entry-context null outcomes** — `patch_outcome()` moved outside `quality_rec` guard.
7. **Log duplicates fixed** — `propagate=False` on child loggers.
8. **Log rotation** — `RotatingFileHandler(10MB, 5 backups)` for decisions.log and trades.log.
9. **Dashboard credentials** — moved to env vars.

### Updated Scoring Table

| Factor | Points | Notes |
|--------|--------|-------|
| Zone tier 1/2/3/other | 30/20/12/6 | |
| ATR strength | 20/12/6/4 | |
| OTR ratio | 15/10/5 | |
| R:R (TP1-based) >= 2.0/1.5/1.0 | 15/10/5 | Recalibrated for TP1 |
| john_wick + confidence | 15 + 5/3 | Was +10 |
| power_of_towers + confidence | 5 + 5/3 | Was +10 |
| z-score edge | 10/6/3 | |
| BB position extreme | 8/4 | |
| Structure touches | 8/4 | |
| Gap fade alignment | 6 | |
| **Ranging penalty** | **-20** | NEW |
| **CT + power_of_towers** | **-15** | NEW |
| **CT + john_wick** | **-5** | NEW |
| **CT Tier 1 penalty** | **-10** | NEW |
| **CT Tier 2 bonus** | **+5** | NEW |
| **z-score alignment** | **+5** | NEW |
| **Regime NORMAL** | **+10** | Applied in main.py |
| **Regime CAUTION** | **-10** | Applied in main.py |

### Data Files
- `data/audit/scrooge_v1_audit.csv` — 344 trades with OANDA P/L + bot decision factors
- `data/audit/oanda_v1_fills.json` — 1,077 ORDER_FILL transactions, raw OANDA API data
