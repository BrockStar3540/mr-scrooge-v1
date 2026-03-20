# Mr. Scrooge Audit -- 2026-03-05

## Scope
- Live EC2 path: `/home/ubuntu/.openclaw/workspace/agents/mr-scrooge`
- Backup path: `/Volumes/Alien Device/mr-scrooge`
- Performance cutoff: `2026-03-04 23:00:00 UTC` (March 4, 2026 6:00 PM New York)

## Critical Findings
1. Multiple live bot instances were running concurrently.
2. Duplicate log lines confirmed concurrent engines writing at the same time.
3. Post-cutover no-trade periods were primarily gate-driven (RR/OTR/spread), not a total engine freeze.

## Fixes Applied
1. `main.py`
- Added non-blocking file lock (`logs/mr-scrooge.lock`) to prevent second runtime instance.
2. `start.sh`
- Added startup lock (`logs/start.lock`).
- Added orphan process sweep scoped to this agent working directory.
- Added clean stop/force-stop flow before restart.
- Keeps PID file management explicit.
3. Documentation
- Replaced stale phase-heavy docs with concise operational references.
- Standardized all audit guidance to post-cutover window.

## Drift Check (Live vs Backup)
- Functional code drift in key files was not found; prior hash mismatches were header/comment differences.
- Backup now treated as clean source of truth for this audit set.

## Current Observed Gate Pressure (Post-cutover)
- Frequent rejects from:
  - `RR < 1.75`
  - `OTR > 25%`
  - spread threshold pauses in thin sessions
- Intermittent OANDA `502 Bad Gateway` candle fetch errors were also observed.

## Audit Policy Going Forward
- Profitability and regression checks must use data >= cutoff only.
- Pre-cutover rows are retained only for forensic context, never for strategy scoring.

## Change Log (2026-03-06)
- Reduced `min_risk_reward` in `main.py`:
  - `1.75 -> 1.60`
- Deployed to EC2 and restarted Scrooge.
- AUD/CAD cooldown note:
  - Cooldown is set to `360` minutes (`time_exit_cooldown_minutes`).
  - Latest trigger came from native close event:
    - `2026-03-06 01:50:05 UTC` close of `scrooge_audcad11f51e26`
    - cooldown active until `07:50 UTC`.

## Change Log (2026-03-06, native-close cooldown policy)
- Updated native-close reconciliation policy:
  - If OANDA native close has `realizedPL > 0`: **no cooldown**.
  - If `realizedPL <= 0`: keep standard cooldown (`time_exit_cooldown_minutes`).
  - If PnL cannot be fetched: keep conservative cooldown.
- Added OANDA helper in `core/oanda_client.py`:
  - `get_trade_realized_pl(trade_id)` for closed-trade realized PnL lookup.

## Change Log (2026-03-06, consolidated daily notes)
- Added full-day operations + strategy summary:
  - `DAILY_NOTES_2026-03-06.md`
- Includes naming/process standardization context (`mr-scrooge` / `mr-sprite`), hourly green-capital release behavior, cooldown policy status, and EC2/Alien Device sync state.

## Change Log (2026-03-07, Net Floor Normalization)
- Replaced single absolute net-floor logic with pip-normalized floor support:
  - New config: `min_net_profit_pips = 7`
  - Legacy `min_net_profit` retained only as fallback.
- Implementation in `strategy/box_state_machine.py`:
  - Added symbol-aware pip sizing (`JPY=0.01`, others `0.0001`).
  - Alternative-box viability now checks `net >= min_net_profit_pips * pip_size(symbol)`.
- Rationale:
  - Prevents inconsistent strictness across JPY vs non-JPY pairs.

## Change Log (2026-03-07, Faster Green Recycle)
- Increased green-capital recycle frequency:
  - `hourly_green_exit_minutes`: `60 -> 30`
- Intent:
  - Close net-green holds sooner and free slots more often for re-entry opportunities.

## 2026-03-08 Shark Audit Transfer (2026-03-08 05:21 UTC)

### What transferred from Sprite (relevant to OANDA)
- Not fee-table transfer. Scrooge cost driver is spread/slippage, not Kraken fee schedule.
- Transfer is execution-accounting rigor:
  - use exchange-realized close PnL when available
  - avoid model-only close accounting in risk controls

### Code hardening applied
- `portfolio/trade_manager.py`:
  - `close_position()` now accepts `pnl_override` and returns final close PnL.
- `core/oanda_client.py`:
  - `place_market_order()` now returns `realized_pl` from OANDA fill transaction components (`pl`, `financing`, `commission`) when present.
  - `close_trade_partial()` now returns same `realized_pl` field.
- `main.py`:
  - Close path now prefers OANDA realized PnL and passes it through `pnl_override`.
  - `daily_loss` now updates from actual `close_pnl` (not pre-close model value).
  - Native OANDA-close reconciliation path now also applies `pnl_override=realized_pl` and updates `daily_loss` from realized values.

### Why this matters for red baskets
- Daily loss controls now reflect actual close outcomes more accurately.
- Exit logs can distinguish exchange-realized vs modeled fallback paths.
- Red exposure management is less likely to be distorted by model drift.

---

## Change Log (2026-03-19 → 2026-03-20, Major Overhaul)

### Bugs Fixed

**1. Timeframe toggle buttons missing from dashboard**
- Added 30M / 1H toggle buttons to dashboard header.
- `fetchTimeframe()`, `_updateTfButtons()`, `setTimeframe()` JS functions added.
- `TIMEFRAME_PRESETS` in `dashboard.py` updated to use fixed pip values (SL/TP1/TP2) rather than deriving them from RR ratios.

**2. `authHeaders()` ReferenceError on timeframe toggle**
- `setTimeframe()` and `fetchTimeframe()` called `authHeaders()` which was never defined.
- Fixed: removed the call — browser handles HTTP Basic Auth natively.

**3. Rogue duplicate bot processes on timeframe switch**
- `_set_timeframe()` used `pkill -f 'python3 main.py'` + `subprocess.Popen(['python3', 'main.py'])`.
- With `Restart=always` in systemd, when the bot was killed systemd immediately relaunched it AND Popen launched another — creating 6+ competing processes.
- Fixed: replaced with `subprocess.run(['systemctl', '--user', 'restart', 'mr-scrooge.service'])`.

**4. SL/TP pip values corrected**
- Migrated from equity-% based targets to fixed-pip targets.
- Final values: `sl_pips=10`, `tp1_pips=5` (ratchet trigger), `tp2_pips=30` (full exit).
- All sizing, logging, and config presets updated consistently.

**5. Dead code removed (code audit)**
Files cleaned: `main.py`, `trade_manager.py`, `box_state_machine.py`, `fee_calculator.py`, `signal_engine.py`.
Removed:
- `max_trade_risk_pct`, `target_profit_pct`, `tp1_allocation_pct` from CONFIG
- `probation_size_multiplier`, `hourly_green_exit_*`, `green_exit_min_pct` from CONFIG
- `hourly_green_checks_done`, `green_exit_tp_set` from `OpenPosition`
- `_is_tp_reached()` from `trade_manager.py` (never called)
- `get_position_size_multiplier()` from `box_state_machine.py` (never called)
- `otr_acceptable` default corrected from `0.10` → `0.15` in `fee_calculator.py`

**6. Opportunity scoring system added**
- `SignalResult` dataclass gains `score: int = 0` field (placed last — has default value).
- `_calculate_score()` method added to `signal_engine.py`.
- Scores 0–100 based on: zone tier, ATR strength, OTR category, R:R ratio, signal type, pitchfork confidence.
- All qualifying candidates stored in `ctx['_cycle_entry_candidates']` per cycle.
- Post-loop ranking: candidates sorted by score, best fires, others discarded.
- `_fire_entry_candidate()` extracted as module-level function.

**7. ON DECK was never set when a position was open**
- `at_max_positions` check caused early return from `process_instrument()` before candidates were stored.
- Fixed: removed early return — qualifying signals continue to candidate queue regardless of position count.
- Post-loop firing still gates on `position_count < max_concurrent_positions`.

**8. Zone cache poisoning at max positions**
- Zone cache write (`zone_cache[zone_key] = datetime.now()`) was inside `process_instrument()`.
- When a signal was ranked but not fired (position already open), it still poisoned the zone cache for 20 minutes.
- Fixed: moved zone cache write into `_fire_entry_candidate()` — only marks zones on actual order placement.

**9. Margin sizing bug — all JPY pair trades blocked**
- `get_equity()` in `oanda_client.py` returned `NAV` (includes unrealized P&L) instead of `balance`.
- Primary sizing used hardcoded `CONFIG['leverage'] = 50` (2% margin) but OANDA requires 5% margin (20:1) for JPY pairs.
- Result: bot sized to ~56,711 units for USD/JPY but OANDA's margin cap allowed only ~22,684.
- Cap formula compounded the error: used `current_price` (e.g., 157.69) instead of `base_price_usd` (1.0) — making the effective cap 157× too small → 143 units.
- Severity check (`max_units < 50% of intended`) then killed every JPY trade.
- Fixed:
  - Primary sizing now uses `margin_rate` from `inst_meta` (OANDA's actual rate per instrument).
  - Cap formula changed from `usable_margin / (current_price × margin_rate)` → `usable_margin / (base_price_usd × margin_rate)`.
  - Removed duplicate `margin_rate` re-fetch (was using different default on second fetch).

**10. Equity source switched from NAV to balance**
- `get_equity()` was returning `NAV` (net asset value including unrealized P&L).
- Position sizing fluctuated with open trade P&L — sized up when winning, down when losing.
- Fixed: `get_equity()` now returns `balance` (cash only).
- `get_combined_margin_pct()` also updated to use `balance` as denominator.

**11. Config tab showed obsolete fields**
- Dashboard Config tab rendered `max_trade_risk_pct`, `probation_size_multiplier`, `Hourly Green Exit` section — all removed in code audit.
- Replaced with current parameters: Position Sizing section (margin utilization, leverage, EUR cap), SL/TP section (pip values), corrected Timing values, Box & Zone section (timeframe, lookback).

### Commit References
- `2389afe` — opportunity scoring, ON DECK, dead code audit, SL/TP pip values, TF toggle
- `6ecd754` — margin sizing bug fix, cap formula fix, Config tab update
- `fbb0285` — equity source: NAV → balance
