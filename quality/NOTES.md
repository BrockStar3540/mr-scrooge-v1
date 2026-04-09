# Trade Quality Feedback Loop — NOTES

## What this tracks

Every trade that fires gets a quality record written to `data/quality-trades.jsonl`.

### MFE — Maximum Favorable Excursion
How far the trade went **in our favor** before closing.
Measures entry quality and market cooperation.

### MAE — Maximum Adverse Excursion
How far the trade went **against us** before recovering (or stopping out).
High MAE = bad entry timing or wrong direction. The trade fought us before cooperating.

### MFE/MAE Ratio
The single most useful number.
- Ratio > 2.5 = clean entry, market moved with us immediately
- Ratio < 1.2 = fought us hard, barely recovered if at all
- Good trades tend to have low MAE relative to their MFE

### Follow-Through Score (0-100)
Composite quality rating. Components:
- 30 pts: MFE/MAE ratio (clean entry, market cooperated)
- 25 pts: reached +5 pips quickly (< 6 candles = 30 min)
- 25 pts: MFE reached at least 80% of TP1 distance
- 20 pts: final result positive

### Quality Pressure (per symbol, recent N trades)
- **strong**: avg F/T >= 65, MFE/MAE >= 2.5 — market respecting our entries
- **normal**: middle range
- **weak**: avg F/T <= 40 or MFE/MAE <= 1.2 — entries are fighting the market

## Why passive first

We don't know yet:
- What score threshold separates good entries from bad
- Whether slowness (many cycles to +5 pips) predicts worse outcomes
- Whether high MAE at entry correlates with eventual SL hits

The log answers all of these. After 50+ trades per symbol, run:

```python
import json, statistics

records = [json.loads(l) for l in open('data/quality-trades.jsonl')]

# Does high MAE predict SL hits?
sl_trades  = [r for r in records if r['hit_sl']]
win_trades = [r for r in records if not r['hit_sl']]
print(f"SL avg MAE:  {statistics.mean(r['mae_pips'] for r in sl_trades):.1f}")
print(f"Win avg MAE: {statistics.mean(r['mae_pips'] for r in win_trades):.1f}")

# Does F/T score predict outcome?
strong = [r for r in records if r['follow_through'] >= 65]
weak   = [r for r in records if r['follow_through'] <= 40]
print(f"Strong F/T win rate: {sum(not r['hit_sl'] for r in strong)/len(strong):.0%}")
print(f"Weak F/T win rate:   {sum(not r['hit_sl'] for r in weak)/len(weak):.0%}")

# What signal score actually matters?
for threshold in [50, 60, 65, 70, 75]:
    above = [r for r in records if r['signal_score'] >= threshold]
    if above:
        wr = sum(not r['hit_sl'] for r in above) / len(above)
        print(f"Score >= {threshold}: {len(above)} trades, {wr:.0%} win rate")
```

Once you see a pattern, activate the filter.

## Log format

```json
{
  "trade_id": "scroogev2_usdjpy_abc123",
  "symbol": "USD_JPY",
  "direction": "long",
  "entry_time": "2026-03-24T08:30:00Z",
  "close_time": "2026-03-24T11:15:00Z",
  "entry_price": 151.250,
  "close_price": 151.380,
  "signal_score": 72.5,
  "mfe_pips": 18.2,
  "mae_pips": 3.1,
  "final_pips": 13.0,
  "cycles_elapsed": 33,
  "cycles_to_5pips": 4,
  "cycles_to_10pips": 11,
  "cycles_to_tp1": 18,
  "mfe_mae_ratio": 5.87,
  "follow_through": 90.0,
  "hit_sl": false,
  "hit_tp1": true,
  "hit_tp2": false,
  "close_reason": "TP1 partial close"
}
```
