# zone_gate — NOTES

## Purpose
Gate 1 (broadest). Checks if current price is within proximity of a known magic line zone.
Blocks any trade not at a valid zone — prevents random mid-box entries.

## Logic
- Checks all magic lines in sell_zones + buy_zones
- Also checks PDH/PDL directly (always Tier 1)
- Also checks prior hour high/low (Tier 2)
- JPY pairs: 2x proximity to account for wider normal range
- Returns tier of best matching zone (1=highest quality)

## Health
Component: gate_zone | Circuit: 3 failures → RED | Recovery: 120s

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial extraction from SignalEngine | Claude Code |
