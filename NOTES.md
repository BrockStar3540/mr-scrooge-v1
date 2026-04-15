
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

## 2026-04-10 — 4-Actor Trade Coordination (Phase 1)

### What changed
- New : TradeCoordinator class — file-backed cross-process state at 
- : Bot registers trades at entry, marks dead on vanished detection. Fast loop checks alive gate before SL limit placement. Ratchet checks sl_owner before pushing SL. Ratchet reads tiers_hit from coordination file to sync partial_1_taken.
- : Alive gate on process_trade. Updates tiers_hit, sl_owner, current_sl after each tier close. Hands off sl_owner to ratchet after final tier.
- : place_opposing_limit detects NO_POSITION_TO_REDUCE cancels (fixes 46-order spam bug found in today's audit).

### Why
Trade audit found 4 independent actors (bot, ratchet, SL-limit, harvester) modifying the same OANDA trade with no shared state. SL-limit spammed 46 API calls on a dead position. Ratchet and harvester raced on SL moves. Ratchet didn't know harvester tier state.

### What to watch
- Grep logs for 'TradeCoordinator' / 'sl_owner' / 'is_trade_alive' / 'NO_POSITION_TO_REDUCE'
- Verify no SL limit spam on position close
- Verify harvester tier completions sync to ratchet (Stage 2 fires after T1)
- Coordination file: 

## 2026-04-10 — 4-Actor Trade Coordination (Phase 1)

### What changed
- New core/trade_state.py: TradeCoordinator class for cross-process state
- main.py: Bot registers trades, marks dead, alive gate in fast loop, sl_owner check, tiers_hit sync
- harvester/main.py: Alive gate, tier updates to coordination file, SL ownership handoff
- oanda_client.py: Detect NO_POSITION_TO_REDUCE (fixes 46-order spam bug)

### Why
4 actors modifying same OANDA trade with no shared state. SL-limit spammed 46 API calls on dead position. Ratchet and harvester raced on SL.

### Watch for
- Grep logs for TradeCoordinator / sl_owner / is_trade_alive
- Verify no SL limit spam on position close
- Coordination file: data/trade_coordination.json

## 2026-04-10 — Phase 3A: Entry Safety Gates

### What changed
- main.py: Added 4 hard gates before entry candidate ranking
  - Score >= 20 (blocks score=1 junk)
  - Spread ratio <= 3.0 (blocks toxic spread conditions)
  - Trigger freshness (blocks if price already past trigger level)
  - Trigger distance <= 0.3 ATR_5m (blocks stale/far triggers)
- All blocked trades logged as SCORE_BLOCKED / SPREAD_BLOCKED / TRIGGER_STALE / TRIGGER_FAR

### Why
Trade audit showed score=1 entries lost -40.41 (V1) and -940.54 (V2). Theory says do not chase.

### Watch for
- Grep decision logs for SCORE_BLOCKED, SPREAD_BLOCKED, TRIGGER_STALE, TRIGGER_FAR
- Fewer trades is expected and correct

## 2026-04-12 — V1/V2 Convergence + Beta Epoch

### What changed
- regime/supervisor.py: Merged correlation map — full 26-pair universe (was EUR/GBP/USD majors only). Added _MAX_BREADTH_PEERS=4 cap to control trigger frequency.

### Why
Dashboard comparison revealed V1 and V2 producing divergent regime scores. Root cause: V1 correlation map only covered EUR/GBP/USD, so breadth_negative never fired for AUD/NZD/JPY crosses. V2 had the opposite gap. Both now use identical merged map.

### Context from investigation
- Manual USD/JPY analysis revealed the alternative box (session_cheat_code) produces trade opportunities that may not align with the Master Theory. The Master Theory says the box is prior-day high/low — the alternative box uses session range after a breach. Jury is still out: not enough post-fix data to determine if alt-box trades have edge. Need clean runtime data to evaluate.
- Parity harness deployed as systemd service (parity-harness.service). Compares V1 and V2 status.json every 90s, logs drift with severity levels (CRITICAL/WARNING/INFO), saves snapshots on mismatch.
- Pre-beta data archived to logs/archive_2026-04-12_beta-start/. All logs cleared for clean data collection.
- Beta epoch: 2026-04-13 02:41 UTC (Sunday 10:41 PM ET).

### Open questions for future analysis
1. Alternative box (session_cheat_code): Does it have edge? The Master Theory doesn't describe rebuilding boxes from session data after breach. Need 100+ trades on fixed code to evaluate.
2. USD_MXN: Effectively choked by spread pause + OTR gate + score floor. Should it remain in the universe or be removed entirely? Keep it for observation during beta.
3. Win/loss asymmetry: Harvester takes small partials (1-8 pips), losses hit full stop (10-15 pips). Need 75%+ win rate to be net positive. Current post-fix V1 data suggests ~75% — monitor during beta.
4. BB position threshold: V1 and V2 diverge by 4-8 points when bb_pos is near 0.85 boundary. Pure timing noise from 30-second fetch gaps. Not actionable but explains parity harness warnings on CHF_JPY etc.

### Watch for
- Parity harness: ~/parity_harness/parity.log — should show mostly OK with occasional timing WARNINGs
- No CRITICAL session mismatches (session bug is fixed)
- Alternative box trades: track which entries fire on using_alternative=True vs normal box


---

## 2026-04-15 — V3 Scoring System Deployed

### Changes Applied This Session

1. **SL floor**: 0.25% to 0.10% (recipe analysis: tighter stops win more)
2. **Min RR**: 1.35 to 1.0 (RR doesn't separate W from L)
3. **Spread threshold**: 30% to 10% of half box (above 10% = 20% WR)
4. **Regime floor REMOVED** — was blocking at score below -1, now only BLOCKED state suppresses
5. **BB exhaustion gate REMOVED** — V3 scorer handles BB state scoring
6. **V3 Hybrid Scorer deployed** (v3_scorer.py) — overrides old scoring
7. **Dashboard updated** — shows V3 layered breakdown

### V3 Scoring Formula

- 5 hard blocks: RSI neutral 45-55, BB STRETCH, Duration 12h+, OTR below 8% with NORM, Stale box with EMA20
- 17 single-factor scores weighted by proportional gap from 50-trade analysis
- 10 positive pairs + 8 negative pairs (winner/loser clusters from raw data)
- Out-of-sample validated: 100% WR on 20-trade test set
- Separation: +80.3 pts (was +0.5 in old formula)

### Architecture Change

OLD: Regime gate then BB exhaustion gate then Scoring then Entry
NEW: Regime (only BLOCKED) then V3 Scorer (43 factors + pairs) then Entry

Regime = broad filter (only macro meltdowns).
V3 points = precision filter (each trade judged individually on 43 factors).

### Data Source

All analysis from raw OANDA candle data, not bot logs.
Scripts and raw data in V3 repo: github.com/BrockStar3540/mr-scrooge-v3
- data/raw_candles/ — 50 trade candle files
- data/trades_features_verified.csv — feature table
- scripts/pull_raw_data.py — transparent extraction
- docs/FACTOR_BIBLE.md — 31 factors analyzed
- docs/MASTER_FINDINGS.md — 43 factors with verdicts
- docs/SCORING_V3_LIVE.md — complete formula spec

### Key Findings

- Box age below 12h = 83% WR (strongest new factor)
- Entry hour 0-7 UTC = 82% WR
- Volume ratio 0.9-1.1 = 82% WR
- OTR below 8% = 17% WR (INVERTED — removed from positive scoring)
- EMA50 below 20% of box = 20% WR
- Directional candles 4+ = 83-100% WR
- BB STRETCH = 0% WR (hard blocked)
- Wall proximity shows NO signal on V1 live (W=0.39, L=0.39)
- Wick ratio inconsistent between V1 and V2 (unreliable)
