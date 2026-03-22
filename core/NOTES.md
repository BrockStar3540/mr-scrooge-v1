# core/ — NOTES

## Purpose
External API wrappers. All OANDA communication flows through here.
No trading logic lives here — pure I/O with health monitoring.

## Components

### oanda_client.py
- All HTTP calls go through `_get()`, `_put()`, `_post()` helpers
- Each helper records success/failure latency to HealthMonitor
- Circuit breaker trips at 5 consecutive failures, recovers after 120s
- Instrument cache loaded on init, reloaded on miss

### execution_router.py
- Routes entries (market orders) and exits (OANDA native close or emergency market)
- Pending order dict tracks any limit orders (legacy path)
- Circuit breaker: 3 failures → RED, 60s recovery

### fee_calculator.py
- Calculates OTR ratio and spread viability before every entry
- Accepts both Config dataclass (v2) and plain dict (backward compat)
- Circuit breaker: 10 failures → RED (shouldn't fail in normal operation)

## Health Component Names
| Component | HealthMonitor Key |
|-----------|------------------|
| OANDAClient | `oanda_client` |
| OANDAExecutionRouter | `execution_router` |
| OANDAFeeCalculator | `fee_calculator` |

## Change Log
| Date | Change | Author |
|------|--------|--------|
| 2026-03-21 | Phase 2 build — health hooks added to all three components | Claude Code |
