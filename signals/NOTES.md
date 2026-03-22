# signals/ — NOTES

## Purpose
Signal evaluation: runs 6 gates in order, returns SignalResult.
SignalEngine is now a thin orchestrator — all gate logic lives in gates/<name>_gate/gate.py.

## Gate Order
1. zone_gate — price at magic line?
2. trend_gate — direction matches regime? (Tier 3+ only)
3. atr_gate — advisory, always passes
4. sucker_move_gate — explosive push into zone?
5. pitchfork_gate — reversal candle confirmed?
6. otr_gate — spread cost acceptable?

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Phase 5 — thin orchestrator using gate classes | Claude Code |
