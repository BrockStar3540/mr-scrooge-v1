# trend_gate — NOTES

## Purpose
Gate 2. Classifies the higher-timeframe trend using 50H EMA slope.
Only blocks counter-trend entries at Tier 3+ zones.
Tier 1/2 zones at box extremes are strong enough to trade against trend.

## Logic
- 50-period EMA on 1H closes
- Slope over last 10 EMA values (≈10 hours)
- threshold: ±0.05% per 10 bars = uptrend/downtrend
- Counter-trend at Tier 3/4 → BLOCKED
- Counter-trend at Tier 1/2 → ALLOWED (logged)
- score: 1.0 if with-trend, 0.6 if counter-trend but allowed

## History
- Original threshold was 0.001 over 5 bars — missed slow EUR downtrends
- Switched to 10-bar lookback at 0.0005 to catch sustained trends (2026-03)

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial extraction from SignalEngine | Claude Code |
