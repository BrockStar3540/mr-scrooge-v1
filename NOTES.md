# mr-scrooge-v2 — Master Notes

## Architecture Overview

```
mr-scrooge-v2/
├── config.py              ← All parameters (typed dataclass, env overrides)
├── alerts.py              ← Claudia + Frank outbox integration
├── coordinator.py         ← Main orchestrator (Phase 6)
├── main.py                ← Boot + systemd entrypoint (Phase 6)
│
├── health/                ← GREEN/YELLOW/RED per component, circuit breakers
│   ├── models.py          ← ComponentHealth, CircuitBreaker, HealthReport
│   └── monitor.py         ← HealthMonitor singleton
│
├── core/                  ← External API wrappers (Phase 2)
│   ├── oanda_client.py    ← REST client (extracted from v1 core/)
│   ├── execution_router.py
│   └── fee_calculator.py
│
├── universe/              ← Pair screening (Phase 3)
│   ├── screener.py        ← ForexUniverseScreener
│   └── session_windows.py ← Session schedule
│
├── zones/                 ← Magic-line zone detection (Phase 3)
│   ├── models.py          ← MagicLine, DailyBox, ZoneMap
│   └── detector.py        ← ZoneDetector
│
├── box/                   ← Box state machine (Phase 3)
│   ├── models.py          ← BoxState, BoxRecord
│   └── state_machine.py   ← BoxStateMachine
│
├── gates/                 ← Signal validation pipeline (Phase 4)
│   ├── models.py          ← BaseGate, GateResult, GatePipelineResult
│   ├── zone_gate/         ← Gate 1: price at magic line?
│   ├── trend_gate/        ← Gate 2: higher-TF trend alignment?
│   ├── atr_gate/          ← Gate 3: volatility adequate?
│   ├── sucker_move_gate/  ← Gate 4: false break detected?
│   ├── pitchfork_gate/    ← Gate 5: named candle signal?
│   └── otr_gate/          ← Gate 6: overhead-to-range ok?
│
├── signals/               ← Signal evaluation + ranking (Phase 5)
│   └── engine.py          ← SignalEngine
│
├── positions/             ← Open position management (Phase 5)
│   ├── models.py          ← Position dataclass
│   └── manager.py         ← PositionManager
│
├── sizing/                ← Position sizing (Phase 5)
│   └── calculator.py      ← margin-based sizing
│
├── experiments/           ← A/B testing framework (Phase 1 ✓)
│   ├── models.py          ← Experiment, VariantStats, TradeOutcome
│   └── tracker.py         ← ABTestingTracker singleton
│
├── dashboard/             ← Flask health + experiments UI (Phase 7)
│   └── templates/
│
├── data/                  ← Runtime state files
└── logs/                  ← Timestamped logs, decisions, trades, experiments
```

## Design Principles
1. **Each component knows its own health** — register + record on every call
2. **Circuit breakers** auto-disable failing components
3. **Gates** run in order; first failure stops pipeline — no silent passes
4. **A/B testing** is opt-in per gate; never influences production until proven
5. **JSONL logs** — append-only, timestamped, machine-parseable
6. **No dead code** — v2 is a clean slate, nothing carried from v1 unless used

## Build Status
| Phase | Contents | Status |
|-------|----------|--------|
| 1 | config, alerts, health, gates/models, experiments | ✅ Complete |
| 2 | core/ (oanda_client, execution_router, fee_calculator) | ✅ Complete |
| 3 | universe/, zones/, box/ | ✅ Complete |
| 4 | All 6 gate subfolders | ✅ Complete |
| 5 | signals/, positions/, sizing/ | ✅ Complete |
| 6 | coordinator.py + main.py | ✅ Complete |
| 7 | dashboard/ | 🔲 Future |

## Change Log
| Date | Phase | Description | Author |
|------|-------|-------------|--------|
| 2026-03-21 | 1 | Initial build — infrastructure + models | Claude Code |
