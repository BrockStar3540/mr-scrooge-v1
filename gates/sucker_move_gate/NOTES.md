# sucker_move_gate — NOTES

## Purpose
Gate 4. Detects the "sucker move": fast, explosive directional push into the zone.
This is the mechanism that traps retail traders going the wrong way.

## Logic
- Shorts: 3-5+ consecutive/mostly-green candles pushing UP into sell zone
- Longs: 3-5+ consecutive/mostly-red candles pushing DOWN into buy zone
- Flexible window: allows intermixed non-directional candles (real moves are choppy)
- terminating_at_zone: did the move end within 0.5% of the target magic line?
- Both detected AND terminating_at_zone must be True to pass

## MASTER_THEORY
"Sometimes it can look a little choppier... red green green red green."
Net price direction is what matters, not perfect consecutive colors.

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial extraction from SignalEngine | Claude Code |
