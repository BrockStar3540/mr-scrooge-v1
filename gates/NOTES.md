# gates/ — NOTES

## Purpose
Ordered signal-validation pipeline. Each gate is a self-contained subfolder
with its own `gate.py`, `NOTES.md`, and (future) `experiments/` log.
Gates run broadest → strictest. First failure stops the pipeline.

## Gate Order (broadest → strictest)
1. **zone_gate** — Is price at a valid magic-line zone? (Tier 1/2/3)
2. **trend_gate** — Is the signal direction aligned with the higher-timeframe trend?
3. **atr_gate** — Does current volatility support this trade? (ATR % of range)
4. **sucker_move_gate** — Did price make a sucker move (false break above/below)?
5. **pitchfork_gate** — Is there a named candle signal (John Wick, Power of Towers, Pitchfork)?
6. **otr_gate** — Is the overhead-to-range ratio within acceptable bounds?

## Health Indicators
Each gate registers a CircuitBreaker with the HealthMonitor.
Dashboard shows GREEN/YELLOW/RED per gate.

## A/B Testing
Each gate subfolder will have an experiments/ directory for variant testing.
Use experiments/tracker.py to log outcomes and compute Fisher exact test significance.

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial models and folder structure | Claude Code |
