# Mr. Scrooge — OANDA Forex Box Reversal Bot

Mr. Scrooge is an automated forex trading bot that trades mean-reversion setups on major forex pairs using the prior day's high/low range as a reference framework, running on a 5-minute candle cycle against the OANDA v20 REST API.

---

## Strategy Overview

Each pair gets a "box" defined by the prior day's high (PDH) and prior day's low (PDL). The bot monitors for price entering a zone near one of those walls, waits for a pitchfork reversal signal, then enters with a fixed-pip stop and rides to a 30-pip take-profit.

All setups must pass four gates before an entry is queued:
- **Zone** — price within proximity % of PDH or PDL
- **ATR** — session has sufficient momentum (not dead market)
- **Pitchfork** — 3-5 directional candles followed by a reversal candle (John Wick, Power of Towers, or standard pitchfork)
- **OTR** — spread cost is acceptable relative to box size

When multiple symbols qualify simultaneously, an opportunity scoring system (0-100 pts) ranks them and fires only the best setup each cycle.

---

## Architecture

| File | Purpose |
|------|---------|
| `main.py` | Boot, main 5-min loop, daily reset, entry candidate ranking |
| `core/oanda_client.py` | OANDA v20 REST wrapper (quotes, orders, account) |
| `core/execution_router.py` | Order routing — limit entry, market fallback |
| `core/fee_calculator.py` | Spread-adjusted net profit calculations |
| `strategy/zone_detector.py` | PDH/PDL box computation, percentile filter, gap detection, zone classification |
| `strategy/box_state_machine.py` | Per-pair box state: ranging / breach pending / broken / new box / shelved |
| `strategy/signal_engine.py` | 4-gate signal evaluation + pitchfork detection + opportunity scoring |
| `strategy/universe_screener.py` | Session-aware pair selection (London, New York, Tokyo, Sydney) |
| `portfolio/trade_manager.py` | Position tracking, ratchet stop management, harvester, P&L |
| `harvester/` | Profit extraction daemon (staggered partials, trailing stops) |
| `dashboard.py` | Web dashboard backend (stdlib HTTP server, no Flask) |
| `dashboard.html` | Dashboard UI — positions, scanner, box states, config, logs |
| `status.py` | Writes `data/status.json` for external monitoring |

---

## Signal Flow (per 5-min cycle)

```
For each active instrument:
  1. Update box state machine (breach detection, age confidence)
  2. Manage open positions (harvester ratchet, time exits, green exits)
  3. Evaluate signal (4 gates: zone → ATR → pitchfork → OTR)
  4. If all gates pass → queue as entry candidate with score

After all instruments scanned:
  5. Sort candidates by score (descending)
  6. Fire best candidate if position count < max
  7. Fall through to next if top pick goes stale
  8. Write dashboard status
```

---

## Position Sizing

Sizing is **margin-based** using OANDA's actual per-instrument margin rate.

```
target_margin = equity_balance x 0.80
units         = target_margin / (base_price_usd x oanda_margin_rate)
```

- `equity_balance` = cash balance (not NAV — excludes unrealized P&L)
- No decompressors — full 80% margin at all times
- JPY pairs: 5% margin (20:1), CHF pairs: ~3% (33:1), EUR/GBP: 2% (50:1)

---

## SL / TP — Fixed Pip Structure

All SL/TP values are measured from the actual fill price.

| Level | Pips | Purpose |
|-------|------|---------|
| Stop Loss | 10 | Beyond the zone wall |
| TP1 — Ratchet Trigger | 10 | SL management trigger |
| TP2 — Full Exit | 30 | OANDA native TP closes position |

**Ratchet stages:**
1. At **+5 pips** (50% to TP1): SL advances to break-even
2. At **+10 pips** (TP1 hit): SL locks to entry+10, position rides to TP2
3. Above TP1: hybrid trailing stop (80% profit lock, hourly candle trail, 1.5x ATR)
4. At **+30 pips** (TP2): OANDA native TP closes full position

After fill, the bot recalculates SL/TP from the actual fill price and updates the OANDA orders. This corrects for any slippage between the signal's trigger price and the market fill.

---

## Box Construction

The daily box is built from the prior trading day's hourly bars:
1. Raw PDH/PDL extracted from prior-day high/low
2. **Percentile filter** (98th/2nd percentile) trims single-bar spike wicks
3. **Gap detection** adjusts box when today's open gaps above PDH or below PDL
4. **Touch counting** validates the box walls have been tested

The box is rebuilt at UTC midnight (daily reset) and when new instruments enter the universe.

---

## Opportunity Scoring (0-100 pts)

| Factor | Points |
|--------|--------|
| Zone tier 1 / 2 / 3 / other | 30 / 20 / 10 / 5 |
| ATR strength: strong / valid / weak | 20 / 15 / 5 |
| OTR: excellent / acceptable / marginal | 15 / 10 / 5 |
| R:R >= 3.0 / >= 2.0 / >= 1.5 | 15 / 10 / 5 |
| Named signal (john_wick / power_of_towers) | 10 |
| Standard pitchfork | 5 |
| Pitchfork confidence: HIGH / MODERATE | 5 / 3 |

---

## Dashboard

Available at `http://localhost:8081` (HTTP Basic Auth). Tabs:

- **Positions** — live P&L, ratchet stage, time in trade
- **Scanner** — all active pairs with box dimensions, OTR, spread
- **Signals** — last cycle's gate results per pair
- **Box States** — per-pair state machine status, ON DECK badge
- **Config** — live CONFIG values + runtime overrides
- **Harvester** — ratchet config, stagnant timeout settings
- **Logs** — filterable real-time log viewer

---

## Deployment

Runs as a `systemd --user` service on EC2 with `Restart=always` (auto-restarts on crash, 15s delay).

```bash
systemctl --user start mr-scrooge.service
systemctl --user status mr-scrooge.service
systemctl --user restart mr-scrooge.service
```

Dashboard accessible via Tailscale only (binds to 127.0.0.1, exposed via Tailscale Serve on port 8081).

---

## Lessons Learned

1. **30-minute box experiment (2026-03-19)**: Rolling 12-hour box was too unstable — zones shifted constantly, entries lost alignment with daily structure. Reverted to daily PDH/PDL.
2. **Fixed pip stops**: Zone-anchored stops placed inside the box caused premature stop-outs. Fixed pip stops from fill price give consistent risk regardless of box geometry.
3. **Sizing formula**: `current_price * margin_rate` is wrong for cross pairs (CAD/JPY). Must use `base_price_usd * margin_rate` where base_price_usd is derived from pip_value.
4. **Ratchet tuning**: With TP1 at 5 pips, the 50% breakeven trigger fired at +2.5 pips — too tight. Setting TP1 to 10 pips means breakeven triggers at +5 pips (the minimum target).
5. **Fill-anchored recalculation**: Signal engine computes SL/TP from trigger price, but fills happen at market. Must recalculate from actual fill and update OANDA orders.
6. **Decompressors vs simplicity**: Box-age multipliers, off-peak reductions, and open-wait half-sizing reduced position sizes unpredictably. Full 80% at OANDA's real margin rate is simpler and more capital-efficient.
7. **NAV vs balance**: Using NAV for sizing means position size fluctuates with unrealized P&L — sizes up when winning, down when losing. Use cash balance instead.
8. **Pycache**: Must clear `strategy/__pycache__/*.pyc` after editing `.py` files or changes don't take effect.
