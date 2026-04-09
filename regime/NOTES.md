# Regime Supervisor — NOTES

## What this is

A **passive observer**. It watches market conditions and the bot's own performance.
It logs a "protest" whenever it *would* have intervened, but never actually does.

## Why passive first

We don't know yet whether regime detection adds value or just noise.
After 3 months of logged protests, query the log:

```python
# Would restricted/blocked regime have saved money?
protests = [p for p in log if p['regime'] in ('RESTRICTED', 'BLOCKED')]
entries   = [p for p in protests if p['bot_action'] in ('ENTERED_LONG', 'ENTERED_SHORT')]

losers  = [e for e in entries if e['outcome_sl'] == True]
winners = [e for e in entries if e['outcome_sl'] == False]

print(f"When supervisor would have blocked: {len(losers)} losers, {len(winners)} winners")
print(f"Loss rate: {len(losers)/len(entries):.0%}")
```

If the loss rate in blocked windows is materially higher than baseline → **activate it**.
If it matches baseline → the detector is noise, discard it.

## The four signals

### 1. box_acceptance (-1 to -2)
Price closing and holding outside PDH/PDL.
2+ consecutive closes outside = market accepting a new range = bad for fading.

### 2. rejection_decay (-1 to -2, or +2 if clean)
Quality of recent wall touches.
Clean rejection = touched wall, closed well away.
Weak = touched, lingered near it.
Failed = accepted through it.

### 3. expansion_pressure (-1 to -2)
ATR expanding directionally without mean reversion.
Not ATR high = bad. Directional persistence + expansion = bad for fading.

### 4. qualified_failures (-1 to -3) — most important
Our own gated setups hitting SL recently.
The market is directly telling us our framework isn't being honored.
2+ SL hits, 0 winners in 6 hours = regime is hostile to current strategy.

## Score thresholds

| Score  | State      | Would have done              |
|--------|------------|------------------------------|
| >= +2  | NORMAL     | full trading                 |
| 0..+1  | CAUTION    | require top-tier score only  |
| -1..-2 | RESTRICTED | reduce size, tier-1 only     |
| <= -3  | BLOCKED    | halt new entries             |

## Protest log format

`data/regime-protests.jsonl` — one JSON object per line.

```json
{
  "ts": "2026-03-22T04:30:00Z",
  "symbol": "USD_JPY",
  "regime": "RESTRICTED",
  "score": -2,
  "signals": [
    {"name": "box_acceptance_above", "delta": -2, "detail": "2 closes above PDH..."},
    {"name": "single_failure", "delta": -1, "detail": "1 qualified setup hit SL..."}
  ],
  "bot_action": "ENTERED_LONG",
  "signal_score": 72.5,
  "trade_id": "USD_JPY_20260322_0430",
  "outcome_pnl": -18.40,
  "outcome_sl": true
}
```

`outcome_pnl` and `outcome_sl` are null at write time, filled in when the trade closes.
