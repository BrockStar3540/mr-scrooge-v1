# Mr. Scrooge Daily Notes — 2026-03-12

## Green Exit TP Rejection Loop — Root Cause & Full Redesign

The bot was entering an infinite retry loop when attempting to set a LIMIT TP order via
OANDA after price had already reached or passed the target level. OANDA rejects LIMIT
orders where the limit price is already at/past the market, causing the `green_exit` /
`set_tp` handlers to fire on every cycle with no escape. The partial market-order fallback
was also costly: spread on each market exit erodes profit compared to a resting LIMIT.

**Solution**: Removed all reactive TP-setting and market-order exit logic entirely.
Trade management is now SL-advancement only. OANDA's native GTC limit TP (set at entry
as TP2) handles the limit exit without bot intervention. Bot never places market orders.

---

### `portfolio/trade_manager.py` changes

**Removed entirely**:
- `time_exit` — market order backstop exit
- Stop-hit close via market order
- `green_exit` / `set_tp` — reactive TP-setting after entry
- `partial_tp` (TP1 and TP2) — partial market/limit close logic
- Early break-even logic tied to the old partial framework

**Added — SL advancement, 3 stages (ratchet-only, never loosens)**:

- **Stage 1 (Break-even)**: When price reaches 50% of the distance from entry to TP1,
  advance SL to entry price. This locks out loss on a trade that has shown meaningful
  movement.
- **Stage 2 (TP1 lock)**: When price passes the TP1 level, lock SL to TP1 price.
  Uses `pos.partial_1_taken` as the sentinel flag (field already existed in the
  `OpenPosition` dataclass — no schema change needed).
- **Stage 3 (Hybrid trail)**: After break-even is achieved, apply hybrid trailing stop:
  `max(hourly_candle_low, entry + 80% × peak_profit, peak − 1.5×ATR)`.
  Ratchet-only — best move_stop selected as `max(candidates, key=lambda a: a['price'])`
  for longs, `min(...)` for shorts.

Peak tracking: `pos.highest_price` / `pos.lowest_price` updated every management cycle
(these fields were added 2026-03-11 — already present).

---

### `main.py` changes

**Removed**:
- `partial_tp` action handler — was calling `router.partial_close()` (market order)
- `set_tp` action handler — was calling `modify_trade_tp` + `partial_close` fallback
  (both market order paths)

**Kept**:
- `move_stop` handler — calls `router.move_stop(pos.trade_id, pos.oanda_trade_id, price, reason)`
  This is the only action the new trade_manager emits.

---

### Design rationale
- OANDA native GTC limit TP (set at entry as TP2) executes at the limit price —
  no slippage, no bot involvement needed.
- SL-only advancement means the bot can never make things worse by placing a bad order.
- Eliminates spread cost of all market exits.
- Eliminates the TP rejection loop permanently — bot no longer touches TP post-entry.

---

## Deployment
- All changes deployed to EC2: `~/.openclaw/workspace/agents/mr-scrooge/`
- All changed files synced to Alien Device: `/Volumes/Alien Device/mr-scrooge/`
- Restarted cleanly via `systemctl --user restart mr-scrooge.service`

## Watch List / Open Questions
- **TP2 set at entry**: Confirm that `signal_engine.py` / entry logic is correctly
  computing and passing TP2 as the OANDA native TP on order submission. The new design
  depends entirely on this being set correctly at entry time — if TP2 is 0.0 or missing
  at entry, the trade has no exit except SL.
- **ATR = 0 on reconciled positions**: Positions loaded from OANDA on restart have
  `atr_at_entry = 0.0`. Stage 3 hybrid trail's ATR component falls back gracefully
  (only candle trail + profit lock active). Acceptable short-term.
- **Stage 2 sentinel (`partial_1_taken`)**: Field repurposed — originally for partial
  close tracking, now used as "SL has been advanced to TP1" flag. Confirm no other code
  path reads `partial_1_taken` and misinterprets it as an actual partial fill.
- **`min_total_touches` in box validation**: Still at 4 combined touches (carried from
  Mar 11 watch list). Watching for unnecessary setup filtration.
