
## 2026-04-03 — Fast Execution Loop + Dashboard Regime Visibility

### Changes Made

#### 1. Pair Execution Profiles (both bots)
Added `_PAIR_PROFILE` dict with per-pair tuned parameters for the hybrid SL limit and green exit
systems. Three tiers based on observed spread/wick behaviour:

| Tier | Pairs | Trig (pips) | Offset | Fallback | Fast Mult | Green Offset |
|------|-------|-------------|--------|----------|-----------|--------------|
| tight | EUR_USD, AUD_USD, EUR_CAD | 2 | 1.0 | 3.0 | 2.5x | 0.5 |
| medium | GBP_USD, USD_JPY, NZD_USD, USD_CHF, USD_CAD, EUR_JPY | 4 | 1.5 | 5.0 | 2.0x | 1.0 |
| wide | GBP_JPY, CHF_JPY, NZD_JPY, AUD_JPY, AUD_CHF | 6 | 2.0 | 7.0 | 1.8x | 1.5 |

Files changed: `coordinator.py` (V2), `main.py` (V1)

#### 2. Hybrid SL Limit System (both bots)
Proactive REDUCE_ONLY limit order placed when price approaches stop within `trigger_buffer` pips.
OANDA native SL kept as hard backstop. Full lifecycle: fill check, ratchet cancel, fallback,
placement. Fast market detection skips limit placement when candle range ≥ `fast_mult × avg`.

Key fields added to `OpenPosition`:
`sl_limit_order_id`, `sl_limit_price`, `sl_limit_active`, `sl_limit_attempted`,
`sl_limit_filled`, `sl_limit_pips_saved`, `sl_fallback_used`, `last_sl_price`

Files changed: `positions/models.py` (V2), `portfolio/trade_manager.py` (V1),
`coordinator.py` (V2), `main.py` (V1)

#### 3. Green Exit → Limit Order (both bots)
Hourly green exit now places a REDUCE_ONLY limit at `bid - green_offset_pips` (long) or
`ask + green_offset_pips` (short) with 300s expiry. Falls back to market on failure or
fast market. Green exit guard: skips if `sl_limit_active = True`. Only fires when no
stop-ratchet action pending this cycle (move_stops take priority).

Files changed: `positions/manager.py` (V2), `portfolio/trade_manager.py` (V1),
`coordinator.py` (V2), `main.py` (V1)

#### 4. Fast Execution Loop — 3-second daemon thread (both bots)
Dual-loop architecture: 5-min loop handles decisions, 3-sec loop handles SL limit
placement and fill monitoring (`manage_open_positions_fast` / `_fast_loop_body`).

Implementation:
- `threading.Lock` (`_exec_lock`) prevents race conditions between loops
- `_cached_bars` dict stores latest `bars_5m` from slow loop; fast loop reuses without refetch
- Fast loop fetches live prices via single batch `get_prices(symbols)` call per cycle
- Position loop in slow loop wrapped with `with _exec_lock:`
- Dead position guard: existence check before `_manage_sl_limit` in fast loop
- Log tag: `[FAST_LOOP]` on thread start and errors

Files changed: `coordinator.py` (V2), `main.py` (V1)

#### 5. Regime Visibility on Dashboard (V1 dashboard + both bots)
Before: regime blocked entries silently — dashboard showed `ENTER_SHORT`/`ENTER_LONG` with
no indicator. Signal card had blue glow, box card had no banner.

After: at all three regime block points (BLOCKED, FLOOR, HYSTERESIS), the bot now:
- Sets `signal.action` to `'REGIME_BLOCKED'`, `'REGIME_FLOOR'`/`'REGIME_HYSTERESIS'`
- Sets `signal.wait_for` with score + reason detail
- Writes to `_blockers[symbol]` with full message (feeds both signal card and box card banners)

Dashboard changes:
- `actionCls` = `'fail'` (red) for any `action.startsWith('REGIME_')`
- Blue `ready-card` glow suppressed for regime-blocked cards
- Badge shows `REGIME_BLOCKED` directly (no `READY —` prefix)
- Red blocker banner shown on both signal card and box card

Files changed: `main.py` (V1), `coordinator.py` (V2), `dashboard.html` (V1 only — V2 has no
separate dashboard UI)

### Architecture Note
System is now dual-loop: candle-based decisions (5 min) + real-time execution (3 sec).
This is the same pattern used in professional trading systems. The lock prevents simultaneous
`_manage_sl_limit` calls from both loops on the same position.

### What to Watch
- `[FAST_LOOP] Execution thread started` in logs on each restart ✓
- `[FAST_LOOP] error:` lines — indicates API issues in the fast loop
- Dashboard: regime-blocked pairs now show red `REGIME_BLOCKED` badge and blocker banner
- SL limit events: `SL limit placed`, `fallback triggered`, `skipped (fast market)` in logs
- Lock contention: if slow loop feels sluggish, fast loop may be holding lock too long
