# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — A/B Testing Tracker
Records trade outcomes per variant; runs Fisher exact test for significance.
Persists to JSONL files in logs/experiments/.
"""

from __future__ import annotations
import json
import logging
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional

from .models import Experiment, VariantStats, TradeOutcome, ExperimentResults

log = logging.getLogger('mr-scrooge-v2.experiments')


class ABTestingTracker:
    """
    Singleton. Thread-safe.

    Usage:
        tracker = ABTestingTracker.get()
        tracker.register(Experiment('zone_tier_test', 'zone_gate', ['control', 'v2']))
        variant = tracker.assign_variant('zone_tier_test', symbol)
        # ... run gate with variant config ...
        tracker.record(TradeOutcome(..., variant=variant, experiment='zone_tier_test'))
        results = tracker.results('zone_tier_test')
    """

    _instance: Optional[ABTestingTracker] = None
    _lock = threading.Lock()
    _LOG_DIR = 'logs/experiments'

    def __init__(self):
        self._experiments: Dict[str, Experiment] = {}
        self._stats: Dict[str, Dict[str, VariantStats]] = {}
        self._assignments: Dict[str, str] = {}  # symbol → variant (per-experiment hash)
        self._rlock = threading.RLock()
        os.makedirs(self._LOG_DIR, exist_ok=True)

    @classmethod
    def get(cls) -> 'ABTestingTracker':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── Registration ─────────────────────────────────────────

    def register(self, experiment: Experiment):
        with self._rlock:
            self._experiments[experiment.name] = experiment
            self._stats[experiment.name] = {
                v: VariantStats(variant=v)
                for v in experiment.variants
            }
            log.info(f'A/B experiment registered: {experiment.name} '
                     f'variants={experiment.variants}')

    # ── Variant Assignment ────────────────────────────────────

    def assign_variant(self, experiment_name: str, symbol: str) -> str:
        """
        Deterministically assigns a variant to a (experiment, symbol) pair.
        Same symbol always gets the same variant within one experiment.
        Uses hash(symbol + experiment_name) mod num_variants.
        """
        exp = self._experiments.get(experiment_name)
        if exp is None or not exp.active:
            return 'control'
        key = f'{experiment_name}:{symbol}'
        with self._rlock:
            if key not in self._assignments:
                idx = hash(key) % len(exp.variants)
                self._assignments[key] = exp.variants[idx]
            return self._assignments[key]

    # ── Recording ────────────────────────────────────────────

    def record(self, outcome: TradeOutcome):
        """Record one trade outcome and update running stats."""
        with self._rlock:
            exp_stats = self._stats.get(outcome.experiment)
            if exp_stats is None:
                log.warning(f'Unknown experiment: {outcome.experiment}')
                return
            variant_stats = exp_stats.get(outcome.variant)
            if variant_stats is None:
                log.warning(f'Unknown variant {outcome.variant} in {outcome.experiment}')
                return
            variant_stats.record(outcome)

        # Persist to JSONL
        self._append_log(outcome)

        # Check significance after each new outcome
        self._maybe_compute_significance(outcome.experiment)

    def _append_log(self, outcome: TradeOutcome):
        path = os.path.join(self._LOG_DIR, f'{outcome.experiment}.jsonl')
        try:
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps({
                    'ts': outcome.timestamp.isoformat(),
                    'experiment': outcome.experiment,
                    'variant': outcome.variant,
                    'symbol': outcome.symbol,
                    'gate': outcome.gate_name,
                    'direction': outcome.direction,
                    'pnl': outcome.pnl,
                    'win': outcome.win,
                    'tp1': outcome.tp1_hit,
                    'tp2': outcome.tp2_hit,
                    'hold_min': outcome.hold_minutes,
                    'notes': outcome.notes,
                }) + '\n')
        except Exception as exc:
            log.warning(f'Failed to write experiment log: {exc}')

    def _maybe_compute_significance(self, experiment_name: str):
        exp = self._experiments.get(experiment_name)
        if exp is None or len(exp.variants) != 2:
            return  # Fisher exact test only for 2-variant experiments
        stats = self._stats.get(experiment_name, {})
        v_a = stats.get(exp.variants[0])
        v_b = stats.get(exp.variants[1])
        if v_a is None or v_b is None:
            return
        if v_a.n < exp.min_samples or v_b.n < exp.min_samples:
            return

        try:
            from scipy.stats import fisher_exact
            table = [[v_a.wins, v_a.losses], [v_b.wins, v_b.losses]]
            _, p_value = fisher_exact(table, alternative='two-sided')
            significant = p_value < 0.05
            winner = None
            if significant:
                winner = exp.variants[0] if v_a.win_rate > v_b.win_rate else exp.variants[1]
            log.info(
                f'A/B {experiment_name}: p={p_value:.4f} '
                f'{"SIGNIFICANT" if significant else "not significant"} '
                f'winner={winner or "none"}'
            )
        except ImportError:
            pass  # scipy optional; significance testing disabled

    # ── Results ──────────────────────────────────────────────

    def results(self, experiment_name: str) -> Optional[ExperimentResults]:
        with self._rlock:
            exp = self._experiments.get(experiment_name)
            if exp is None:
                return None
            stats = self._stats.get(experiment_name, {})
            return ExperimentResults(
                experiment=experiment_name,
                gate_name=exp.gate_name,
                variants=dict(stats),
            )

    def all_results(self) -> List[ExperimentResults]:
        with self._rlock:
            return [self.results(n) for n in self._experiments]
