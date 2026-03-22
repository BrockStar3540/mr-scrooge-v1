# sizing/ — NOTES

## Purpose
Margin-utilization position sizing. Extracted from v1 _fire_entry_candidate().

## Formula
target_margin = equity × margin_utilization_pct (default 80%)
units = target_margin / (base_price_usd × margin_rate)

## Caps
- Free margin cap: max 80% of available margin
- If margin cap cuts position below 50% of intended → skip trade

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Phase 5 — extracted from v1 main.py | Claude Code |
