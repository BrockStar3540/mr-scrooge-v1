# positions/ — NOTES

## Purpose
Post-entry position management. Three stop-loss advancement stages.
OANDA native TP at TP2 closes remaining position at limit price.

## Stage Logic
1. **BE stop**: price 50% to TP1 → move SL to entry price
2. **Lock TP1**: price passes TP1 → SL locked at TP1
3. **Hybrid trail**: after BE — max(1H candle trail, 80% profit lock, 1.5x ATR trail)

## Re-entry Protocol
After stopout: allowed if still at zone AND (sweep detected | Tier 1/2 | ≥3 tests).
Max 3 re-entries per setup per day.

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Phase 5 — extracted from v1 trade_manager | Claude Code |
