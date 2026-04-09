# Mr. Scrooge Audit Log

## Performance Cutoff
- Live EC2 path: `/home/ubuntu/.openclaw/workspace/agents/mr-scrooge`
- Backup path: `/Volumes/Alien Device/mr-scrooge`
- Performance cutoff: `2026-03-04 23:00:00 UTC` (March 4, 2026 6:00 PM New York)
- Data before cutoff contains pre-fix noise and is excluded from strategy scoring.

---

## 2026-03-05 — Initial Audit

### Critical Findings
1. Multiple live bot instances were running concurrently.
2. Duplicate log lines confirmed concurrent engines writing at the same time.
3. Post-cutover no-trade periods were primarily gate-driven (RR/OTR/spread), not engine freeze.

### Fixes Applied
- `main.py`: Added non-blocking file lock (`logs/mr-scrooge.lock`) to prevent second instance.
- `start.sh`: Added startup lock, orphan process sweep, clean stop/force-stop flow.
- Documentation replaced stale phase-heavy docs with concise operational references.

---

## 2026-03-06 — RR Reduction + Cooldown Policy

- Reduced `min_risk_reward`: `1.75 -> 1.60`
- Native-close reconciliation policy: profitable closes skip cooldown, losses keep standard cooldown.
- Added `get_trade_realized_pl(trade_id)` to `core/oanda_client.py`.

---

## 2026-03-07 — Net Floor Normalization + Green Recycle

- `min_net_profit_pips = 7` — pip-normalized floor (was absolute value, inconsistent across JPY vs non-JPY).
- `hourly_green_exit_minutes`: `60 -> 30` (faster green-capital recycle).

---

## 2026-03-08 — Shark Audit Transfer

Execution-accounting rigor transferred from Sprite:
- `close_position()` accepts `pnl_override`, returns final close PnL.
- `place_market_order()` returns `realized_pl` from OANDA fill transaction components.
- Close path prefers OANDA realized PnL via `pnl_override`.
- `daily_loss` updates from actual `close_pnl` (not pre-close model value).

---

## 2026-03-19 to 2026-03-20 — Major Overhaul

### 30-Minute Box Experiment (failed, reverted)
Rolling 12-hour box was too unstable — zones shifted constantly, entries lost alignment with daily structure. 23 commits introduced. Bot stopped making money.

### What Was Reverted (back to profitable commit `1c99ae2` logic)
1. **30m rolling box** -> restored **daily PDH/PDL** with percentile filter + gap detection
2. **Fixed pip TPs from zone** -> restored **fixed pip TPs from fill** (SL=10, TP1=10, TP2=30)
3. **Zone-anchored entry/stop** -> restored **candle-based pitchfork detection** with fixed 10-pip stop override
4. **Decompressors** -> removed all (box-age, off-peak, open-wait multipliers) — full 80% margin
5. **Green exit removed** -> **restored** hourly green exit
6. **Volatility gate disabled** (`high_volatility_atr_mult = 99`) -> restored to **2.0**

### What Was Kept From the 23 Commits
- Opportunity scoring system (0-100 pts) on `SignalResult`
- Ranked entry selection (best-scored candidate fires, fallthrough on stale)
- `_fire_entry_candidate()` extraction as module-level function
- ON DECK dashboard display (gold badge)
- Dashboard improvements: gate chips, config tab cleanup, log filter fixes, alltime/daily PNL
- All dead code removal from the code audit
- `authHeaders()` console error fix in dashboard.html

### Bugs Fixed During Overhaul

**1-3. Dashboard fixes** — timeframe toggle buttons, `authHeaders()` ReferenceError, rogue duplicate processes from `pkill`+`Popen` (replaced with `systemctl --user restart`).

**4. SL/TP values** — migrated from equity-% to fixed-pip. Final: `sl_pips=10`, `tp1_pips=10`, `tp2_pips=30`.

**5. Dead code removed** — `max_trade_risk_pct` (sizing ref only), `target_profit_pct`, `tp1_allocation_pct`, `_is_tp_reached()`, `get_position_size_multiplier()` (still exists but uncalled).

**6. Opportunity scoring** — `SignalResult.score`, `_calculate_score()`, cycle candidate ranking.

**7. ON DECK at max positions** — removed early return so qualifying signals still score when position limit full.

**8. Zone cache poisoning** — moved zone cache write into `_fire_entry_candidate()` (only on actual order).

**9. JPY margin sizing** — formula was `current_price * margin_rate` (wrong for cross pairs). Fixed to `base_price_usd * margin_rate`.

**10. Equity source** — `get_equity()` changed from NAV to `balance`.

**11. Config tab** — removed obsolete fields, added current parameters.

**12. Stale candidate fallthrough** — ranking loop now iterates all candidates until one fires.

**13. Config tab cleanup** — removed dead `leverage` field, fixed duplicate Min Net Profit.

**14. Fill-anchored SL/TP** — after OANDA fill, recalculates SL/TP from actual fill price and updates OANDA orders via `modify_trade_stop()` / `modify_trade_tp()`.

**15. RR sanity check** — blocks entries where actual RR at current price is below `min_risk_reward`.

**16. TP1 changed 5 -> 10 pips** — 50% ratchet was triggering breakeven at +2.5 pips (too tight). Now triggers at +5 pips.

**17. Decompressors removed** — `get_position_size_multiplier()` no longer called. Sizing is straight 80%.

### External Code Review Assessment
1 of 3 recommendations actionable. The other 2 would have introduced new regressions against the profitable baseline.

### Key Commits
- `1c99ae2` — original profitable version (baseline)
- `2389afe` — scoring, ON DECK, dead code audit, SL/TP pip values, TF toggle
- `6ecd754` — margin sizing bug fix, cap formula fix
- `fbb0285` — equity source: NAV -> balance
- `536a460` — remove dead leverage key, fix Config tab
- `2a07bba` — candidate fallthrough on staleness
- `59e83b9` — revert to profitable daily box strategy; fixed pip SL/TP from fill
- `d48356d` — TP1 to 10 pips; restore 50% ratchet

---

## Current State (2026-03-20)

### Architecture
- **mr-scrooge.service** — main trading bot (5-min cycle)
- **mr-scrooge-dashboard.service** — web dashboard (port 8081)
- **mr-harvester.service** — profit extraction daemon (30s poll, equity-tier partials)

### Strategy
- Daily PDH/PDL box with percentile filter + gap detection
- Candle-based pitchfork triggers (John Wick, Power of Towers, standard)
- 4 gates: Zone, ATR, Pitchfork, OTR
- Opportunity scoring 0-100, best-scored fires after full scan
- Max 1 concurrent position

### Sizing & Risk
- 80% margin, no decompressors
- `base_price_usd * margin_rate` formula (JPY fix)
- SL=10p, TP1=10p, TP2=30p from actual fill price
- Ratchet: BE at 50% to TP1, lock at TP1, hybrid trail above
- Ratchet stop movements wired to OANDA via execution_router
- Harvester: T1=3%/T2=5%/T3=7% equity-tier partials, manages OANDA stops

### Universe
- 26 candidate pairs, balanced mode (session-aware)
- 5 base pairs (proven positive), 7 probation pairs (conservative sizing)

### Known Legacy Config Keys
These keys remain in CONFIG but are not used by core sizing/entry logic:
- `max_trade_risk_pct` — kept for daily-loss reference only
- `target_profit_pct`, `tp1_allocation_pct`, `green_exit_min_pct` — dead (from old partial framework)
- `sl_floor_pct` — dead (superseded by fixed pip SL)
- `apply_probation_size`, `probation_size_multiplier` — active for probation pair sizing

---

## 2026-03-25 — Risk + Exit Architecture Overhaul

### Root Cause Analysis
- 5 USD/CHF MARKET order rejections (400 Bad Request) — TP2 (filtered PDH) was at/below current ask. Signal engine validated tp2 > entry_price but not tp2 > current_ask.
- Trade history analysis (111 trades) revealed: tight SL (≤15p) + big size = 24% win rate, -$250.59 total. The 20-40p SL range = 86% win rate, +$74.17. Risk % per trade ranged from 3.5% to 13.5% — unconstrained.
- Harvester firing T1 at +4.4p (before ratchet BE at +10p) created control-ordering conflict: 75% of position removed before the trade proved itself.

### Fixes Applied (V1 + V2 simultaneously)

**1. TP2 market-price guard** — `gates.py` + `main.py`
- `_calculate_targets()` now receives `current_price`
- New guard: `tp2 <= current_price + 2_pips → reject` (after existing entry-price guard)
- Pre-submit guard in `main.py` re-validates TP2 vs live price before `router.enter_position()`

**2. Risk-capped sizing** — `main.py` CONFIG + sizing block
- New: `max_risk_per_trade_pct = 0.04` (4% max loss per trade)
- `risk_cap_units = (equity × 0.04) / (SL_pips × pip_value_per_unit)`
- `final_units = min(margin_units, risk_cap_units)`
- Log shows which constraint is binding (MARGIN vs RISK_CAP)
- Yesterday's -$59.51 loss (8% risk) would have been capped to ~-$30 (4% risk)

**3. Earlier breakeven** — `portfolio/trade_manager.py` Stage 1
- Changed: trigger from 50% of TP1 to **+5 pips profit**
- SL target: **entry + 1 pip buffer** (not naked entry — survives spread noise)

**4. Harvester tiers raised + T3 removed** — `harvester/harvester_config.json`
- T1: 1.5% → **3.0%** (~8-10p instead of ~4.4p)
- T2: 3.0% → **5.0%** (~15-18p instead of ~8.9p)
- T3: **removed** (runner now 50% of position, was 25%)

**5. Harvester BE-gated** — `harvester/main.py`
- No tiers fire until ratchet achieves breakeven
- Inferred from OANDA SL: long BE = SL >= entry, short BE = SL <= entry
- Prevents harvesting before protection is in place

**6. Conditional stagnant timeout** — `harvester/main.py`
- Changed: 6hr + 2p profit → **90min + profit < T1**
- Trades above T1 threshold run freely (no blind timeout)
- Prevents killing delayed breakouts

**7. Adaptive ratchet lock** — `portfolio/trade_manager.py` `_calculate_hybrid_trail()`
- Changed: fixed 80% → **55% pre-TP1, 75% post-TP1**
- Early trades get room to breathe; confirmed moves get tighter protection

### Log Rotation
- `/etc/logrotate.d/mr-scrooge-logs` — added `su ubuntu ubuntu` (was silently failing)
- Added harvester, patrick, morty, sprite-harvester logs
- Force-rotated to verify. 20-day retention, daily, compressed.

### Other Fixes
- Patrick CRM: zombie process on port 8091 → 37K restart crash-loop. Killed, restarted clean.
- Tailscale Serve: all dashboard routes (8080-8083, 9090, 24601) restored after wipe.
- V2 .venv: recreated from requirements.txt.
- Mr. Sprite: confirmed stopped and disabled (all logs zeroed). Not restarted this session.

---

## 2026-03-26 — sl_floor_pct Diagnosis + Logging Fix + V2 engine.py Restore

### Root Cause: sl_floor_pct Was Killing RR
- **Finding**: Both bots were silent for days including the entire NY session March 25. All
  signal gates were passing cleanly. The only blocker was `RR < 1.6` across all pairs.
- `sl_floor_pct = 0.0050` (0.50%) forced a minimum ~54 pip stop on EUR pairs at 1.08,
  inflating the RR denominator. EUR_AUD/EUR_NZD peaked at RR 1.35 all session — 16% short.
- Floor was previously raised from 0.30% to 0.50% with comment "too tight" but overcorrected.
- **Fix**: Lowered to `0.0025` (0.25%). First cycle after restart → USD_JPY qualified and
  V1 entered SHORT @ 159.444 (signal: power_of_towers).

### V2 Logging Bug Fixed (signals/engine.py)
- `_no_trade()` calls at the RR check point were not forwarding the preceding gate kwargs
  (`sucker_move_gate`, `pitchfork_gate`, `otr_gate`). All showed `False` in decisions log
  even when those gates passed. Display-only bug — no effect on trade decisions.
- Fixed by passing all gate state kwargs explicitly at both RR failure and TP wrong-side paths.

### V2 signals/engine.py Accidentally Left as .bak
- `signals/engine.py` had been renamed to `signals/engine.py.bak.20260325` in a prior session.
- Bot was surviving on stale `.pyc` cache — crashed on next restart with `ModuleNotFoundError`.
- Restored from git HEAD. `.pyc` cache cleared.

### V2 Regime Correctly Blocked USD_JPY (2026-03-26 03:56 UTC)
- V2 qualified USD_JPY SHORT (all gates, RR cleared 1.6) but REGIME_BLOCKED score=-4.0.
  - `box_acceptance_above(-2)`: 6 consecutive closes above PDH 159.18 — price holding outside box
  - `rejection_failed(-2)`: 8.1w failed rejections at that wall in last 24H
- V1 (observer-only regime) fired the same trade. Watching outcome to validate regime call.
- This is regime working as designed. V2 correctly identified the wall had been failing all day.

### Monitoring
- Watching next few days to confirm 0.25% floor is not too loose (stops getting hit early).
  Rollback point: step back to 0.30% if stop-hit rate rises.


### Session 2026-04-03 — Alert Routing + Venv Path Cleanup
- `main.py`: `_send_scrooge_alert()` was routing to Claudia outbox targeting `#mr-scrooge`
  (non-existent Slack channel, caused syslog flooding). Now routes to Frank outbox
  (`../frank/outbox/`) as JSON files delivered via Telegram.
- `harvester/start.sh`, `dashboard.py`, `mr-scrooge-dashboard.service`: hardcoded
  `mr_burns_env` paths replaced with `mr_scrooge_env` symlink. No functional change.
- V1 restarted and confirmed active.
