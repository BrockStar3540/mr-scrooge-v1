# Mr. Scrooge — Operational Reference

## Identity
- Bot: Mr. Scrooge
- Venue: OANDA (forex, live account)
- EC2 path: `~/.openclaw/workspace/agents/mr-scrooge`
- Entry: `main.py` via `start.sh`
- Service: `systemctl --user {start|stop|restart|status} mr-scrooge.service`
- Logs: `logs/mrscrooge.log`, `logs/decisions.log`, `logs/trades.log`
- Dashboard: `http://<tailscale-ip>:8081` (HTTP Basic Auth)

## Performance Cutoff
Valid performance analysis starts: **2026-03-04 23:00:00 UTC**
Data before this date contains pre-fix noise (manual closes, duplicate processes) and should be excluded from strategy scoring.

## Current Configuration (as of 2026-03-20)
All values live in `CONFIG` dict in `main.py`. Runtime overrides written to `config.json` by dashboard.

### Sizing
- `margin_utilization_pct = 0.80` — deploy 80% of cash balance as margin
- Equity source: `balance` (not NAV — excludes unrealized P&L)
- Sizing uses OANDA's actual `marginRate` per instrument:
  - JPY pairs: 5% margin = 20:1 leverage
  - CHF pairs: ~3% margin = 33:1 leverage
  - EUR/GBP majors: 2% margin = 50:1 leverage
- No decompressors — full 80% at all times (no box-age, off-peak, or open-wait reductions)
- `max_concurrent_positions = 1`

### SL / TP — Fixed Pip Structure
All SL/TP values are measured from the actual fill price (recalculated after OANDA fill).

| Level | Pips | Purpose |
|-------|------|---------|
| Stop Loss | 10 | Beyond the zone wall |
| TP1 — Ratchet Trigger | 10 | At 50% (+5p) SL moves to BE; at TP1 SL locks to entry+10 |
| TP2 — Full Exit | 30 | OANDA native TP closes position |

### Ratchet Stages
1. **+5 pips** (50% of TP1): SL advances to break-even (entry price)
2. **+10 pips** (TP1 hit): SL locks to entry+10, position rides to TP2
3. **Above TP1**: hybrid trailing stop (80% profit lock, hourly candle trail, 1.5x ATR)
4. **+30 pips** (TP2): OANDA native TP closes full position

### Risk Gates
- `min_risk_reward = 1.60`
- `max_daily_loss_pct = 0.075` (7.5% daily halt)
- `otr_excellent = 0.05`, `otr_acceptable = 0.15`, `otr_marginal = 0.25`
- `min_net_profit_pips = 7`
- `min_efficiency = 2.0`
- RR sanity check at current price (blocks entries where price has drifted from trigger)

### Timing
- `scan_interval_seconds = 300` (5 min candle cycle)
- `rescan_interval_minutes = 30` (universe rescan)
- `open_wait_minutes = 15` (exception: Tier 1 + 80% ATR + named signal)
- `max_hold_minutes = 360` (6 hours)
- `time_exit_cooldown_minutes = 15`
- `hourly_green_exit_enabled = True` (close net-green positions hourly)

### Box / Zone
- Box mode: **daily** (PDH/PDL from prior day, with percentile filter + gap detection)
- `zone_proximity_pct = 0.0025`
- `atr_valid_pct = 0.60`, `atr_minimum_pct = 0.15`
- `high_volatility_atr_mult = 2.0` (switches to 1H bars when volatility exceeds 2x hourly ATR)
- `max_spread_pct_of_half = 0.30`
- `spread_spike_threshold = 4.0`
- Universe mode: `balanced` (session-aware pair selection)

## Opportunity Scoring (0-100 pts)
When multiple symbols qualify in the same cycle, the best setup fires:

| Factor | Points |
|--------|--------|
| Zone tier 1 / 2 / 3 / other | 30 / 20 / 10 / 5 |
| ATR strength: strong / valid / weak | 20 / 15 / 5 |
| OTR: excellent / acceptable / marginal | 15 / 10 / 5 |
| R:R >= 3.0 / >= 2.0 / >= 1.5 | 15 / 10 / 5 |
| Signal: john_wick / power_of_towers | 10 |
| Signal: standard pitchfork | 5 |
| Pitchfork confidence: HIGH / MODERATE | 5 / 3 |

Highest-scoring symbol shown as **ON DECK** on dashboard (gold badge) even when position limit is full.
Candidates fall through in score order if top pick goes stale.

## Quick Ops
```bash
# Status
systemctl --user status mr-scrooge.service

# Restart (clears state, re-reconciles positions from OANDA)
systemctl --user restart mr-scrooge.service

# Must clear pycache after editing .py files
rm -rf strategy/__pycache__/*.pyc

# Tail decisions
tail -f logs/decisions.log

# Tail fills
tail -f logs/trades.log
```

## Known Instrument Notes
- JPY pairs have wider spreads during Tokyo/Sydney — OTR gate filters naturally
- All JPY pairs use pip_size = 0.01 (not 0.0001)
- Duplicate log line appearance is a pre-existing dual-handler cosmetic issue — not a runtime bug
- OANDA margin rates vary by instrument — always use `inst_meta['marginRate']`, never hardcode
