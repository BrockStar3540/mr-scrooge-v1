# pitchfork_gate — NOTES

## Purpose
Gate 5. Detects the reversal candle signal that triggers entry.
Three named signals in priority order: John Wick → Power of Towers → Standard Pitchfork.

## Signals
- **John Wick**: Hammer/inverted hammer with wick ≥60% of candle range, confirmed by next candle
- **Power of Towers**: Full-range engulfing candle (including wicks)
- **Standard Pitchfork**: 3-5 directional candles → first reversal candle

## MASTER_THEORY
"Entry above the high of that candle, stop-loss placed slightly below."
Trigger = candle extreme ± tiny buffer (0.01%)

## Timeframe Selection
- Normal: 5M bars
- High volatility (last 1H range > 2x hourly ATR): switch to 1H bars

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial extraction from SignalEngine | Claude Code |
