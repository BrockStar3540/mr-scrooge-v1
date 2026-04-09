# Mr. Scrooge Daily Notes — 2026-03-11

## Strategy Realignment — MASTER_THEORY audit
Full audit of all code against the source transcripts (Doug & Sylvia's Liquidity Sweep
Reversal Framework). Prior implementation had invented quantitative rules not in the
transcripts (~55-65% rule fidelity, ~30-40% numerical fidelity). All misalignments fixed.

### New reference document
- Created `MASTER_THEORY.md` — raw, unbiased quotes and rules from source transcripts only.
  No interpretation. This is the single source of truth for strategy decisions.
- Deleted old `BOX_THEORY.md` (was over-engineered interpretation).

### `strategy/signal_engine.py` changes
- **Sucker move** (`_check_sucker_move`): Replaced strict consecutive candle counting
  (breaks on first non-directional candle) with flexible window approach. Uses
  `search_window = max_candles + 2`, counts directional candles within the window,
  requires `>= min_candles` AND net price movement in correct direction. Per transcripts:
  sucker moves can be "messy/choppy" (red green green red green pattern).
- **Stop placement — all entry types**: Changed from mixed box-wall / candle-extreme stops
  to signal candle extreme only. John Wick long: `l * (1 - sl_floor_pct)`. Short: `h * (1 + sl_floor_pct)`.
  Power of Towers and standard pitchfork same treatment. Per transcripts: stop goes at
  the extreme of the signal candle, not the box wall.
- **Trigger prices**: All entry triggers use candle high/low (`h * 1.0001` / `l * 0.9999`),
  not zone_price. Per transcripts: "break of that candle's high" is the entry.
- **ATR gate (Gate 3)**: Demoted to advisory — always passes, logs warning only.
  MASTER_THEORY has no ATR consumption requirement.
- **Trend filter (Gate 2)**: Made tier-aware. Tier 1/2 zones (box extremes) override trend
  block — per "because we're at the lower part of the box I would prefer to trade long."
  Tier 3/4 counter-trend entries still blocked.
- **OTR gate (Gate 6)**: Demoted to advisory — always passes, logs warning only.
  MASTER_THEORY has no overhead-to-range gate.

### `portfolio/trade_manager.py` changes
- **Time exit**: Changed from hard 30-min exit to 4-hour backstop that only fires if price
  has moved AGAINST the trade. Per transcripts: "as long as trade is valid and stop not
  hit, no reason to exit."
- **`max_hold_minutes`**: Raised from 30 to 240.
- **`max_reentries_per_setup`**: Raised from 1 to 3. Transcripts: re-enter as long as zone
  holds and signal rebuilds; no hard limit stated.
- **Hybrid trailing stop** (`_calculate_hybrid_trail`): Replaced simple hourly candle trail
  with three-component ratchet: `max(candle_trail, profit_lock, atr_trail)`.
  - Candle trail: last completed hourly low/high (always active)
  - Profit lock: `entry + 70% × (peak - entry)` — locks 70% of best-ever gain
  - ATR trail: `peak - 1.5 × atr_at_entry` — volatility breathing room
  - Hybrid components 2+3 activate only after profit > 1 ATR (avoids tightening early)
  - Added `highest_price` / `lowest_price` fields to `OpenPosition` — tracked every cycle.

### `main.py` changes
- **`time_exit_cooldown_minutes`**: Reduced from 360 to 15. Transcripts: "as soon as I
  got stopped out, I was looking for re-entry."
- **EUR currency cap**: Removed entirely. Trend filter handles directional EUR exposure.
- **Probation sizing**: Removed. Transcripts treat all valid setups equally.
- **Kill zone**: Softened from hard block to 50% size reduction outside 07:00-22:00 UTC.
  Transcripts do not describe a kill zone.

### `strategy/zone_detector.py` changes
- **`min_level_touches`**: Lowered from 2 to 1. One prior high/low is a valid magic line.
- **`pdh_percentile`**: Raised 95 → 98 (less aggressive spike trimming)
- **`pdl_percentile`**: Lowered 5 → 2
- **`buy_zone_pct`** / **`sell_zone_pct`**: Widened 20% → 30% of box. Per transcripts:
  "lower part of the box."

## Deployment
- All changes deployed to EC2: `~/.openclaw/workspace/agents/mr-scrooge/`
- All changed files synced to Alien Device: `/Volumes/Alien Device/mr-scrooge/`
- Restarted cleanly via `systemctl --user restart mr-scrooge.service`

## Watch List / Open Questions
- **`min_total_touches` in box validation** (Scrooge zone_detector): Currently 4 combined
  touches required for 24h box to be considered valid. Several pairs consistently sitting
  at 3 touches. MASTER_THEORY says the prior day H/L IS the box — no touch count implied.
  Consider lowering to 3 if setups continue to be filtered unnecessarily. Watching.
- **Trend filter Tier boundary**: Tier 1/2 override is based on `signal.zone_tier` from
  signal_engine. Confirm this is correctly populated for all entry types under live conditions.
- **ATR = 0 on reconciled positions**: Positions loaded from OANDA on restart have
  `atr_at_entry = 0.0`. Hybrid trail falls back to candle-only trail for these — acceptable.
