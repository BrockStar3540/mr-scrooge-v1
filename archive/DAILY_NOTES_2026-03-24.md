# Daily Notes — 2026-03-24

## Session Summary

Two significant changes on this date: a silent evaluation bug fix in V2 (critical), and the
addition of an entry context logger for post-trade analysis.

---

## Bug: coordinator.py `return None` Indentation — V2 Silently Skipped All Signals ~4.5 Hours

**Commit:** `3203cfe` — 2026-03-24 10:27 EDT

### What happened

V2's `coordinator.py` `process_instrument()` contained a `return None` at 8-space indent
(outside the `if not tradeable:` guard block), when it should have been at 12-space indent
(inside it). This caused the bot to return immediately after the tradeable check for every
symbol, silently skipping all signal evaluation on every 5-minute cycle.

No errors were raised. The bot appeared healthy. Log entries continued. Positions were
still managed. But zero new signals were evaluated for approximately 4.5 hours before
the bug was caught.

### Fix

Re-indented the `return None` and its surrounding stub block from 8 to 12 spaces — inside
the `if not tradeable:` guard where it belongs. The fix is a single indentation change
with no logic modifications.

### Why it was silent

Python interprets indentation as block structure. The `return None` at 8 spaces was parsed
as an unconditional return at the function level (after the tradeable guard), not as a
conditional early exit. The function returned before reaching any gate logic, every call,
for every symbol.

### Impact

- Duration: ~4.5 hours of missed signal evaluation (exact window unknown, detected on
  morning review 2026-03-24)
- Account effect: no trades that should have fired were missed (regime/SL conditions
  may have blocked them anyway, cannot be confirmed retroactively)
- V1 was unaffected (not modular, no coordinator.py)

---

## Feature: Entry Context Logger — 30+ Fields Per Trade for Post-Analysis

**Commit:** `dfdfd8e` — 2026-03-24 15:23 EDT

### What was added

A passive data collection system for building a quality feedback loop. Every trade fill
now records a rich snapshot to `data/quality-trades.jsonl` for later analysis.

**New files:**
- `quality/indicators.py` — pure functions: RSI, EMA, MACD, Bollinger bands, Z-score,
  realized volatility, trend direction, ADR consumed, touch count, volume ratio,
  entry distance from PDH/PDL
- `quality/entry_context.py` — EntryContextLogger: record() on fill, patch_outcome()
  on close; writes JSONL
- `core/oanda_client.py` — added get_position_book() and get_order_book_summary()

**Fields recorded per trade:**
- Trade identity: trade_id, symbol, direction, entry_time/close_time
- Price levels: entry_price, close_price, sl_price, tp1_price, tp2_price
- Signal scoring: signal_score, zone tier, gate pass/fail state
- Market microstructure: position book bias, order book imbalance, spread at entry
- Technicals at entry: RSI, MACD histogram, Bollinger Z-score, EMA alignment,
  realized vol, ADR consumed %, volume ratio, touch count at wall
- Outcome (patched on close): mfe_pips, mae_pips, mfe_mae_ratio,
  follow_through score (0-100), hit_sl, hit_tp1, hit_tp2, close_reason,
  cycles_elapsed, cycles_to_5pips, cycles_to_tp1

### Why passive first

No filters are active yet. The system logs everything and waits for 50+ trades before
drawing conclusions. The feedback analysis script in quality/NOTES.md outlines how
to identify: whether MAE predicts SL hits, what signal score threshold matters, and
what F/T score separates good entries from bad.

**Wire-up in coordinator.py:**
- boot(): initializes EntryContextLogger
- fire_entry(): calls record() immediately after fill confirmation
- _handle_close(): calls patch_outcome() on every close event
- run(): passes logger instance through the cycle

---

## Also: V2 README Added

**Commit:** `3203cfe` — same commit as indentation fix

Initial README.md committed to V2 GitHub repo. Documents: architecture, all 6 gates,
regime filter, probe system, health monitor, quality tracker, key design decisions.

---

## Status at End of Day

- V2 coordinator indentation bug fixed, bot evaluating signals correctly
- Entry context logger live and recording
- V1 unchanged (no commits on V1-only side for March 24)
- Both bots running on EC2 under systemd
