# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — Health Models
Data structures for per-component health tracking.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List


class HealthStatus(str, Enum):
    GREEN  = 'GREEN'   # Operating normally
    YELLOW = 'YELLOW'  # Degraded but functional
    RED    = 'RED'     # Failed / circuit-broken


@dataclass
class ComponentHealth:
    """
    Health record for one named component.
    Written by each component after every operation.
    Read by dashboard and HealthMonitor.
    """
    name: str
    status: HealthStatus = HealthStatus.GREEN
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    last_failure_reason: str = ''
    consecutive_failures: int = 0
    total_calls: int = 0
    total_failures: int = 0
    latency_ms_last: float = 0.0
    latency_ms_p95: float = 0.0          # rolling p95 of last 100 calls
    notes: str = ''                       # human-readable summary

    def record_success(self, latency_ms: float = 0.0):
        self.last_success = datetime.now(timezone.utc)
        self.consecutive_failures = 0
        self.total_calls += 1
        self.latency_ms_last = latency_ms
        if self.status == HealthStatus.RED:
            # Auto-recover after first success
            self.status = HealthStatus.YELLOW  # Stays YELLOW until N consecutive successes
        elif self.status == HealthStatus.YELLOW and self.consecutive_failures == 0:
            self.status = HealthStatus.GREEN

    def record_failure(self, reason: str, latency_ms: float = 0.0):
        self.last_failure = datetime.now(timezone.utc)
        self.last_failure_reason = reason
        self.consecutive_failures += 1
        self.total_calls += 1
        self.total_failures += 1
        self.latency_ms_last = latency_ms
        if self.consecutive_failures >= 3:
            self.status = HealthStatus.RED
        elif self.consecutive_failures >= 1:
            self.status = HealthStatus.YELLOW

    @property
    def failure_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_failures / self.total_calls

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'status': self.status.value,
            'last_success': self.last_success.isoformat() if self.last_success else None,
            'last_failure': self.last_failure.isoformat() if self.last_failure else None,
            'last_failure_reason': self.last_failure_reason,
            'consecutive_failures': self.consecutive_failures,
            'total_calls': self.total_calls,
            'total_failures': self.total_failures,
            'failure_rate': round(self.failure_rate, 4),
            'latency_ms_last': self.latency_ms_last,
            'latency_ms_p95': self.latency_ms_p95,
            'notes': self.notes,
        }


@dataclass
class CircuitBreaker:
    """
    Wraps a ComponentHealth. Auto-disables a component after N consecutive
    failures; auto-recovers after a cooldown period.
    """
    component: ComponentHealth
    failure_threshold: int = 5        # trips after this many consecutive failures
    recovery_seconds: int = 300       # wait this long before trying again
    _tripped: bool = field(default=False, init=False)
    _tripped_at: Optional[datetime] = field(default=None, init=False)

    @property
    def is_open(self) -> bool:
        """True means circuit is open (disabled) — calls should be skipped."""
        if not self._tripped:
            return False
        if self._tripped_at is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._tripped_at).total_seconds()
        if elapsed >= self.recovery_seconds:
            # Allow one probe attempt
            self._tripped = False
            return False
        return True

    def record_success(self, latency_ms: float = 0.0):
        self._tripped = False
        self.component.record_success(latency_ms)

    def record_failure(self, reason: str, latency_ms: float = 0.0):
        self.component.record_failure(reason, latency_ms)
        if self.component.consecutive_failures >= self.failure_threshold:
            self._tripped = True
            self._tripped_at = datetime.now(timezone.utc)

    @property
    def name(self) -> str:
        return self.component.name

    @property
    def status(self) -> HealthStatus:
        if self.is_open:
            return HealthStatus.RED
        return self.component.status


@dataclass
class HealthReport:
    """Snapshot of all components for the dashboard."""
    timestamp: datetime
    components: List[ComponentHealth] = field(default_factory=list)

    @property
    def overall_status(self) -> HealthStatus:
        if any(c.status == HealthStatus.RED for c in self.components):
            return HealthStatus.RED
        if any(c.status == HealthStatus.YELLOW for c in self.components):
            return HealthStatus.YELLOW
        return HealthStatus.GREEN

    def to_dict(self) -> dict:
        return {
            'timestamp': self.timestamp.isoformat(),
            'overall': self.overall_status.value,
            'components': [c.to_dict() for c in self.components],
        }
