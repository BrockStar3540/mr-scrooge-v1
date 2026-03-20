# Mr. Scrooge — OANDA Forex Box Reversal Bot

Mr. Scrooge is an automated forex trading bot that trades mean-reversion setups on major forex pairs using the prior day's high/low range as a reference framework, running on a 5-minute candle cycle against the OANDA v20 REST API.

---

## Strategy Overview

Each pair gets a "box" defined by the prior day's high (PDH) and prior day's low (PDL). The bot monitors for price entering a zone near one of those walls, waits for a pitchfork reversal signal, then enters with a fixed-pip stop just beyond the wall and a take-profit targeting the opposite side of the box.

All setups must pass four gates before an entry is queued:
- **Zone** — price within proximity % of PDH or PDL
- **ATR** — session has sufficient momentum (not dead market)
- **Pitchfork** — 3–5 directional candles followed by a reversal candle
- **OTR** — spread cost is acceptable relative to box size

When multiple symbols qualify simultaneously, an opportunity scoring system (0–100 pts) ranks them and fires only the best setup each cycle.

---

## Requirements

- Python 3.10+
- OANDA account (practice or live)
- Recommended minimum account size: **$2,000 USD**

### Python packages
```
requests
pandas
python-dotenv
```

---

## Installation

```bash
git clone <repo-url> mr-scrooge
cd mr-scrooge
pip install -r requirements.txt
cp .env.example .env
# Edit .env with OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_BASE_URL
```

---

## Running

```bash
# Direct
./start.sh

# As a systemd user service (recommended — survives reboots, auto-restarts)
systemctl --user start mr-scrooge.service
systemctl --user status mr-scrooge.service
```

Logs:
```
logs/mrscrooge.log    — main runtime log
logs/decisions.log    — entry/exit decision log (gate results, rankings)
logs/trades.log       — fill log
```

---

## Architecture

| File | Description |
|------|-------------|
| `main.py` | Boot, main loop, daily reset, `_fire_entry_candidate()` |
| `core/oanda_client.py` | OANDA v20 REST wrapper (quotes, orders, account) |
| `core/execution_router.py` | Order routing — limit entry, market fallback |
| `core/fee_calculator.py` | Spread-adjusted net profit calculations |
| `strategy/zone_detector.py` | PDH/PDL computation, zone proximity classification |
| `strategy/box_state_machine.py` | Per-pair breach/reversal state machine |
| `strategy/signal_engine.py` | Pitchfork signal detection + opportunity scoring |
| `strategy/universe_screener.py` | Session-aware pair selection |
| `portfolio/trade_manager.py` | Position tracking, ratchet stop management, P&L |
| `dashboard.py` | Web dashboard backend (stdlib only, no Flask) |
| `dashboard.html` | Dashboard UI — positions, scanner, config, logs |
| `status.py` | Writes `data/status.json` for external monitoring |

---

## Position Sizing

Sizing is **margin-based** — it uses OANDA's actual per-instrument margin rate, not a hardcoded leverage value.

```
target_margin = equity_balance × margin_utilization_pct   (80%)
units         = target_margin / (base_price_usd × oanda_margin_rate)
```

OANDA margin rates vary by instrument:
- **JPY crosses** (USD/JPY, AUD/JPY, etc.): 5% margin = 20:1 leverage
- **Other majors** (AUD/USD, NZD/USD, etc.): 3% margin = ~33:1 leverage

`equity_balance` is the **cash balance** (not NAV) — position sizing does not fluctuate with unrealized P&L.

---

## SL / TP — Fixed Pip Structure

All SL/TP values are measured from the actual fill price.

| Level | Pips | Purpose |
|-------|------|---------|
| Stop Loss | 10 | Beyond the zone wall |
| TP1 — Ratchet Trigger | 5 | SL locks to entry+5 when hit |
| TP2 — Full Exit | 30 | OANDA native TP closes position |

**Ratchet stages:**
1. At +2.5 pips (50% to TP1): SL advances to break-even
2. At +5 pips (TP1 hit): SL locks to entry+5, position rides to TP2
3. Above TP1: hybrid trailing stop (80% profit lock, hourly candle trail, 1.5×ATR)

---

## Opportunity Scoring (0–100 pts)

When multiple symbols qualify in the same cycle, the best setup fires:

| Factor | Points |
|--------|--------|
| Zone tier 1 / 2 / 3 / other | 30 / 20 / 10 / 5 |
| ATR strength: strong / valid / weak | 20 / 15 / 5 |
| OTR: excellent / acceptable / marginal | 15 / 10 / 5 |
| R:R ≥3 / ≥2 / ≥1.5 | 15 / 10 / 5 |
| Signal: john_wick / power_of_towers | 10 |
| Signal: other pitchfork | 5 |
| Pitchfork confidence HIGH / MODERATE | 5 / 3 |

The highest-scoring symbol is shown as **ON DECK** on the dashboard (gold badge) even when the position limit is full.

---

## Key Config Values (`main.py` → `CONFIG`)

| Parameter | Value | Description |
|-----------|-------|-------------|
| `margin_utilization_pct` | 0.80 | Deploy 80% of equity balance as margin |
| `max_concurrent_positions` | 1 | One position at a time |
| `max_eur_positions` | 2 | EUR exposure cap |
| `max_daily_loss_pct` | 0.075 | 7.5% daily halt |
| `min_risk_reward` | 1.60 | Minimum RR to enter |
| `sl_pips` | 10 | Fixed stop loss |
| `tp1_pips` | 5 | Ratchet trigger |
| `tp2_pips` | 30 | Full exit |
| `scan_interval_seconds` | 300 | 5-minute cycle |
| `max_hold_minutes` | 360 | 6-hour max hold |
| `otr_marginal` | 0.25 | Max OTR — spread cannot exceed 25% of half-box |

Runtime overrides are written to `config.json` by the dashboard (timeframe toggle, trading pause).

---

## Dashboard

Available at `http://localhost:8081` (requires `dashboard.py` to be running). Access is HTTP Basic Auth.

Tabs: **Positions** · **Scanner** · **Signals** · **Box States** · **Config** · **Harvester** · **Logs**

The Config tab reflects live values from `main.py` + any `config.json` overrides — no manual refresh needed.

---

## Deployment

The bot runs as a `systemd --user` service on EC2 with `Restart=always`. It does not require OpenClaw or any other infrastructure — `start.sh` and `main.py` are self-contained.

Access the dashboard remotely via Tailscale (dashboard binds to `127.0.0.1` only).
