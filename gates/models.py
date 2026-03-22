# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — Gate Models
BaseGate abstract class and result dataclasses shared by all gate subfolders.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List
import logging


@dataclass
class GateResult:
    """
    Result from a single gate evaluation.
    passed=True  → signal cleared this gate.
    passed=False → signal blocked; reason explains why.
    score        → 0.0–1.0 confidence when passed.
    """
    gate_name: str
    passed: bool
    reason: str = ''
    score: float = 0.0          # 0.0 = barely passed, 1.0 = perfect
    latency_ms: float = 0.0     # time spent evaluating (for monitoring)
    metadata: dict = field(default_factory=dict)  # gate-specific debug data

    def __bool__(self) -> bool:
        return self.passed


@dataclass
class GatePipelineResult:
    """
    Aggregated result from running all gates in sequence.
    Stops at first failure (ordered broadest → strictest).
    """
    symbol: str
    all_passed: bool
    gates: List[GateResult] = field(default_factory=list)
    rejection_reason: str = ''    # reason from the first failing gate
    aggregate_score: float = 0.0  # mean score of all passing gates

    def append(self, result: GateResult):
        self.gates.append(result)
        if not result.passed and self.all_passed:
            self.all_passed = False
            self.rejection_reason = f'[{result.gate_name}] {result.reason}'
        if self.gates:
            passing = [g for g in self.gates if g.passed]
            self.aggregate_score = (
                sum(g.score for g in passing) / len(passing) if passing else 0.0
            )

    def gate(self, name: str) -> Optional[GateResult]:
        """Retrieve a specific gate result by name."""
        return next((g for g in self.gates if g.gate_name == name), None)


class BaseGate(ABC):
    """
    Abstract base for all gate implementations.
    Each concrete gate lives in its own subfolder:
      gates/<name>_gate/gate.py  — implements evaluate()
      gates/<name>_gate/NOTES.md — observations and change log
    """

    name: str = 'unnamed_gate'

    def __init__(self, config, health_monitor=None):
        self.config = config
        self._monitor = health_monitor
        self._log = logging.getLogger(f'mr-scrooge-v2.gate.{self.name}')
        if health_monitor is not None:
            health_monitor.register(
                f'gate_{self.name}',
                failure_threshold=3,
                recovery_seconds=120,
            )

    @abstractmethod
    def evaluate(self, symbol: str, **kwargs) -> GateResult:
        """
        Evaluate the gate for one symbol.
        Returns GateResult(gate_name, passed, reason, score).
        Must never raise — catch all exceptions and return a failed GateResult.
        """
        ...

    def safe_evaluate(self, symbol: str, **kwargs) -> GateResult:
        """
        Wraps evaluate() with timing, error capture, and health reporting.
        Call this instead of evaluate() from the pipeline runner.
        """
        import time
        start = time.monotonic()
        try:
            result = self.evaluate(symbol, **kwargs)
            elapsed_ms = (time.monotonic() - start) * 1000
            result.latency_ms = elapsed_ms
            if self._monitor:
                self._monitor.record_success(f'gate_{self.name}', elapsed_ms)
            return result
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            reason = f'Gate exception: {exc}'
            self._log.exception(f'{symbol}: gate exception — {exc}')
            if self._monitor:
                self._monitor.record_failure(f'gate_{self.name}', reason, elapsed_ms)
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=reason,
                latency_ms=elapsed_ms,
            )
