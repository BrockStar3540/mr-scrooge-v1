# atr_gate — NOTES

## Purpose
Gate 3 (advisory). Measures how much of the daily ATR has been consumed
by the directional move into the zone. Does NOT block trades — informs signal quality.

## Logic
- direction=short: measure session_high - current_price (push UP into resistance)
- direction=long: measure current_price - session_low (push DOWN into support)
- consumed_pct = directional_move / daily_ATR
- strength: strong (≥80%), valid (≥60%), weak (<60%)
- Gate always passes — strength stored in metadata for scoring

## Design Note
ATR is informational per MASTER_THEORY: "set your expectations" not a hard gate.
The sucker_move_gate implicitly enforces directional momentum.

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial extraction from SignalEngine | Claude Code |
