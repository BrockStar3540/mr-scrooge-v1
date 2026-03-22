# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — Health Monitor
Singleton registry for all ComponentHealth / CircuitBreaker instances.
Each module registers itself on import; dashboard reads from here.
"""

from __future__ import annotations
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

from .models import ComponentHealth, CircuitBreaker, HealthReport, HealthStatus

log = logging.getLogger('mr-scrooge-v2.health')


class HealthMonitor:
    """
    Singleton. Thread-safe.
    Usage:
        monitor = HealthMonitor.get()
        monitor.register('oanda_client')
        monitor.record_success('oanda_client', latency_ms=120)
        report = monitor.report()
    """

    _instance: Optional[HealthMonitor] = None
    _lock = threading.Lock()

    def __init__(self):
        self._components: Dict[str, ComponentHealth] = {}
        self._breakers:   Dict[str, CircuitBreaker]  = {}
        self._rlock = threading.RLock()

    @classmethod
    def get(cls) -> 'HealthMonitor':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── Registration ─────────────────────────────────────────

    def register(self, name: str,
                 failure_threshold: int = 5,
                 recovery_seconds: int = 300) -> CircuitBreaker:
        """Register a named component. Idempotent — safe to call on every import."""
        with self._rlock:
            if name not in self._components:
                comp = ComponentHealth(name=name)
                breaker = CircuitBreaker(
                    component=comp,
                    failure_threshold=failure_threshold,
                    recovery_seconds=recovery_seconds,
                )
                self._components[name] = comp
                self._breakers[name] = breaker
                log.debug(f'HealthMonitor: registered component "{name}"')
            return self._breakers[name]

    def get_breaker(self, name: str) -> Optional[CircuitBreaker]:
        return self._breakers.get(name)

    def get_component(self, name: str) -> Optional[ComponentHealth]:
        return self._components.get(name)

    # ── Recording ────────────────────────────────────────────

    def record_success(self, name: str, latency_ms: float = 0.0):
        with self._rlock:
            b = self._breakers.get(name)
            if b:
                b.record_success(latency_ms)

    def record_failure(self, name: str, reason: str, latency_ms: float = 0.0):
        with self._rlock:
            b = self._breakers.get(name)
            if b:
                b.record_failure(reason, latency_ms)
                if b.is_open:
                    log.error(f'CIRCUIT OPEN: {name} — {reason}')
                else:
                    log.warning(f'FAILURE: {name} — {reason}')

    def is_open(self, name: str) -> bool:
        """Returns True if the circuit breaker is tripped (calls should be skipped)."""
        b = self._breakers.get(name)
        return b.is_open if b else False

    # ── Reporting ────────────────────────────────────────────

    def report(self) -> HealthReport:
        with self._rlock:
            return HealthReport(
                timestamp=datetime.now(timezone.utc),
                components=list(self._components.values()),
            )

    def status_line(self) -> str:
        """One-line summary for logs. e.g. 'GREEN(8) YELLOW(1) RED(0)'"""
        with self._rlock:
            counts = {s: 0 for s in HealthStatus}
            for c in self._components.values():
                counts[c.status] += 1
            return ' '.join(f'{s.value}({counts[s]})' for s in HealthStatus)
