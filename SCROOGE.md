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

**Sizing:**
- `margin_utilization_pct = 0.80` — deploy 80% of cash balance as margin
- Equity source: `balance` (not NAV — excludes unrealized P&L)
- Sizing uses OANDA's actual `marginRate` per instrument (JPY pairs = 5%/20:1, others = 3%/33:1)
- `max_concurrent_positions = 1`
- `max_eur_positions = 2`

**SL/TP (fixed pips from fill price):**
- `sl_pips = 10`
- `tp1_pips = 5` — ratchet trigger: SL locks to entry+5 when hit
- `tp2_pips = 30` — OANDA native TP, full position exit

**Ratchet stages:**
1. +2.5 pips → SL advances to break-even
2. +5 pips (TP1) → SL locks at entry+5, rides to TP2
3. Above TP1 → hybrid trailing stop (80% profit lock, hourly candle trail, 1.5×ATR)

**Risk gates:**
- `min_risk_reward = 1.60`
- `max_daily_loss_pct = 0.075` (7.5% daily halt)
- `otr_excellent = 0.05`, `otr_acceptable = 0.15`, `otr_marginal = 0.25`
- `min_net_profit_pips = 7`
- `min_efficiency = 2.0`

**Timing:**
- `scan_interval_seconds = 300` (5 min)
- `rescan_interval_minutes = 30`
- `open_wait_minutes = 15`
- `max_hold_minutes = 360` (6 hours)
- `time_exit_cooldown_minutes = 15`

**Box/Zone:**
- `box_timeframe` — toggleable via dashboard: `'30m'` (rolling 24-bar window) or `'daily'` (1H mode)
- `zone_proximity_pct = 0.0025`
- `atr_valid_pct = 0.60`, `atr_minimum_pct = 0.15`
- `max_spread_pct_of_half = 0.30`
- `spread_spike_threshold = 4.0`

## Quick Ops
```bash
# Status
systemctl --user status mr-scrooge.service

# Restart (clears state, re-reconciles positions from OANDA)
systemctl --user restart mr-scrooge.service

# Single process check
pgrep -af 'python3 main.py'

# Tail decisions
tail -f logs/decisions.log

# Tail main log
tail -f logs/mrscrooge.log
```

## Known Instrument Notes
- JPY pairs have wider spreads during Tokyo/Sydney — OTR gate filters these naturally
- All JPY pairs use pip_size = 0.01 (not 0.0001)
- Duplicate log line appearance is a pre-existing dual-handler cosmetic issue — not a runtime bug
