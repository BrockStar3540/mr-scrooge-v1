# otr_gate — NOTES

## Purpose
Gate 6 (strictest). Checks if the overhead-to-range ratio is within acceptable bounds.
OTR = total_round_trip_spread / half_box
High OTR means spread costs eat too much of the potential profit.

## Thresholds
- excellent:  OTR < 5%  → trade freely
- acceptable: OTR < 15% → normal rules
- marginal:   OTR < 25% → all other gates must be strongest tier
- skip:       OTR ≥ 25% → BLOCKED

## OTR + Zone Tier interaction
- Marginal OTR (15-25%) is allowed only when zone_tier ≤ 2 AND (sucker_move_strength == 'strong' OR pitchfork == john_wick/power_of_towers)
- If OTR is marginal but zone is Tier 3/4, block.

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial extraction from SignalEngine | Claude Code |
| 2026-03-21 | Added zone_tier × OTR interaction check | Claude Code |
