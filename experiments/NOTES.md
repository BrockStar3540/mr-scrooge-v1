# experiments/ — NOTES

## Purpose
Per-gate, per-pair A/B testing framework.
Assigns deterministic variant per (experiment, symbol) pair.
Records outcomes to JSONL. Runs Fisher exact test for significance.

## Usage Pattern
```python
tracker = ABTestingTracker.get()
tracker.register(Experiment(
    name='zone_proximity_test',
    gate_name='zone_gate',
    variants=['control', 'tighter_0.15pct'],
    description='Test tightening zone proximity from 0.25% to 0.15%',
    min_samples=30,
))
variant = tracker.assign_variant('zone_proximity_test', symbol)
# use variant config in gate
tracker.record(TradeOutcome(...))
```

## JSONL Log Location
`logs/experiments/<experiment_name>.jsonl`
One line per trade outcome, append-only.

## Significance Testing
Requires `scipy` (already in requirements).
Runs Fisher exact test after each new outcome once min_samples reached.
Reports p-value and winner to main log.

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial implementation | Claude Code |
