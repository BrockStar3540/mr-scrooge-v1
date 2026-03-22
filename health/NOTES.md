# health/ — NOTES

## Purpose
Per-component health tracking. Every module in the bot registers itself with
`HealthMonitor.get().register('component_name')` and calls `record_success()`
or `record_failure()` on each operation. The dashboard reads the live report.

## Components Registered (expected)
| Name | Owner | Circuit Threshold |
|------|-------|------------------|
| oanda_client | core/oanda_client.py | 5 |
| execution_router | core/execution_router.py | 3 |
| fee_calculator | core/fee_calculator.py | 10 |
| zone_detector | zones/detector.py | 5 |
| box_state_machine | box/state_machine.py | 5 |
| signal_engine | signals/engine.py | 5 |
| universe_screener | universe/screener.py | 5 |
| gate_zone | gates/zone_gate/ | 3 |
| gate_trend | gates/trend_gate/ | 3 |
| gate_atr | gates/atr_gate/ | 3 |
| gate_sucker_move | gates/sucker_move_gate/ | 3 |
| gate_pitchfork | gates/pitchfork_gate/ | 3 |
| gate_otr | gates/otr_gate/ | 3 |

## Status Definitions
- GREEN: all recent calls succeeded
- YELLOW: 1-2 consecutive failures, or recovering from RED
- RED: circuit breaker tripped (>= threshold consecutive failures)

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Initial implementation | Claude Code |
