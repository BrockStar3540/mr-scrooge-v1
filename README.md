# Mr. Scrooge — OANDA Forex Box Reversal Bot

Automated forex trading bot that trades mean-reversion setups using the prior day high/low range. Runs on a 5-minute candle cycle against the OANDA v20 REST API.

---

## Strategy Overview

Each pair gets a "box" defined by the prior day high (PDH) and prior day low (PDL). The bot monitors for price entering a zone near a box wall, waits for a pitchfork reversal signal, then enters with a fixed-pip stop and rides to a 30-pip take-profit.

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
| `core/oanda_client.py` | OANDA v20 REST wrapper (quotes, orders, account, stop/TP modification) |
| `core/execution_router.py` | Order routing — limit entry, market fallback, stop movement |
| `core/fee_calculator.py` | Spread-adjusted net profit calculations |
| `strategy/zone_detector.py` | PDH/PDL box computation, percentile filter, gap detection, zone classification |
| `strategy/box_state_machine.py` | Per-pair box state: ranging / breach pending / broken / new box / shelved |
| `strategy/signal_engine.py` | 4-gate signal evaluation + pitchfork detection + opportunity scoring |
| `strategy/universe_screener.py` | Session-aware pair selection (London, New York, Tokyo, Sydney) |
| `portfolio/trade_manager.py` | Position tracking, ratchet stop management (stages 1-3), P&L |
| `dashboard.py` | Web dashboard backend (stdlib HTTP server, no Flask) |
| `dashboard.html` | Dashboard UI — positions, scanner, box states, config, logs |
| `status.py` | Writes `data/status.json` for external monitoring |

**Separate service:** Mr. Harvester (`~/.openclaw/workspace/agents/mr-harvester/`) — profit extraction daemon with equity-tier partials. Runs as its own systemd unit.

---

## Signal Flow (per 5-min cycle)

```
For each active instrument:
  1. Update box state machine (breach detection, age confidence)
  2. Manage open positions (ratchet stop stages, time exits, green exits)
  3. Evaluate signal (4 gates: zone -> ATR -> pitchfork -> OTR)
  4. If all gates pass -> queue as entry candidate with score

After all instruments scanned:
  5. Sort candidates by score (descending)
  6. Fire best candidate if position count < max (RR sanity check at current price)
  7. Fall through to next candidate if top pick is stale
  8. On fill: recalculate SL/TP from actual fill price, update OANDA orders
  9. Write dashboard status
```

---

## Position Sizing

Margin-based sizing using OANDA actual per-instrument margin rate.

```
target_margin = cash_balance x 0.80
units         = target_margin / (base_price_usd x oanda_margin_rate)
```

- Equity source: `balance` (cash only — excludes unrealized P&L)
- No decompressors — full 80% margin at all times
- JPY pairs: 5% margin (20:1), CHF pairs: ~3% (33:1), EUR/GBP: 2% (50:1)
- `base_price_usd` derived from pip_value (fixes cross-pair sizing like CAD/JPY)
- Probation pairs: 65% size multiplier

---

## SL / TP — Fixed Pip Structure

All SL/TP values are measured from the actual fill price (recalculated after fill to correct for slippage).

| Level | Pips | Purpose |
|-------|------|---------|
| Stop Loss | 10 | Beyond the zone wall |
| TP1 — Ratchet Trigger | 10 | SL management trigger |
| TP2 — Full Exit | 30 | OANDA native TP closes position |

**Ratchet stages (wired to OANDA via execution_router.move_stop -> oanda_client.modify_trade_stop):**
1. At **+5 pips** (50% to TP1): SL advances to break-even
2. At **+10 pips** (TP1 hit): SL locks to entry+10, position rides to TP2
3. Above TP1: hybrid trailing stop (80% profit lock, hourly candle trail, 1.5x ATR)
4. At **+30 pips** (TP2): OANDA native TP closes full position

---

## Harvester (Separate Service)

Mr. Harvester runs as `mr-harvester.service` at `~/.openclaw/workspace/agents/mr-harvester/`.

- Polls OANDA every 30 seconds
- Equity-tier partial closes: T1=3% (close 25%, SL->BE), T2=5% (close 25%, SL->+T1), T3=7% (close 25%, SL->+T2)
- Runner: remaining 25% rides to OANDA native TP2
- Manages OANDA stops directly
- State persisted to `harvester_state.json`
- Per-pair pip overrides available via dashboard

---

## Box Construction

Daily box from prior trading day hourly bars:
1. Raw PDH/PDL from prior-day high/low
2. Percentile filter (98th/2nd) trims spike wicks
3. Gap detection adjusts box when today open gaps beyond PDH/PDL
4. Touch counting validates wall tests

Rebuilt at UTC midnight and when new instruments enter the universe.

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

## Universe

26 candidate pairs across dollar majors, JPY crosses, EUR crosses, GBP crosses, and commodity crosses. Session-aware filtering in `balanced` mode suppresses probation pairs during thin sessions (Sydney/Tokyo only).

---

## Dashboard

Available at port 8081 (HTTP Basic Auth, Tailscale only). Features:

- Positions — live P&L, ratchet stage, time in trade
- Scanner — active pairs with box dimensions, OTR, spread
- Box States — per-pair state machine status, ON DECK badge for best-scored candidate
- Gate chips on box cards
- Config — live CONFIG values + runtime overrides
- Logs — filterable viewer (NO_TRADE, Universe scan, BREACH PENDING)
- Alltime PNL and daily PNL from OANDA transactions

---

## Deployment

Three systemd services on EC2:

```bash
systemctl --user {start|stop|restart|status} mr-scrooge.service          # trading bot
systemctl --user {start|stop|restart|status} mr-scrooge-dashboard.service # dashboard
systemctl --user {start|stop|restart|status} mr-harvester.service         # profit extraction
```

All bind to 127.0.0.1, exposed via Tailscale Serve. Auto-restart on crash (15s delay).

Must clear `strategy/__pycache__/*.pyc` after editing `.py` files.

---

## Lessons Learned

1. **30-minute box experiment**: Rolling box was too unstable. Reverted to daily PDH/PDL.
2. **Fixed pip stops**: Zone-anchored stops inside the box caused premature stop-outs. Fixed pip from fill gives consistent risk.
3. **Sizing formula**: `current_price * margin_rate` is wrong for cross pairs. Must use `base_price_usd * margin_rate`.
4. **Ratchet tuning**: TP1 at 5 pips meant breakeven at +2.5 pips — too tight. TP1=10 means breakeven at +5 pips.
5. **Fill-anchored recalculation**: Must recalculate SL/TP from actual fill price and update OANDA orders.
6. **Decompressors vs simplicity**: Box-age/off-peak/open-wait multipliers were unpredictable. Full 80% is simpler.
7. **NAV vs balance**: NAV fluctuates with unrealized P&L. Use cash balance.
8. **Pycache**: Must clear `strategy/__pycache__/*.pyc` after editing `.py` files.
