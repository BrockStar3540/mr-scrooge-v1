# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Trade Coordination — Cross-process shared state for 4-actor sync.

Provides a single source of truth for:
  - Is the trade still alive on OANDA?
  - Who currently owns the stop loss? (ratchet vs harvester)
  - Which harvester tiers have been completed?
  - What is the authoritative SL price?

Backed by data/trade_coordination.json with atomic writes
and fcntl file locking for cross-process safety.

Actors: bot (slow loop), ratchet (trade manager), SL-limit (fast loop), harvester (separate daemon).
"""

import fcntl
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

log = logging.getLogger(__name__)

_DEFAULT_RECORD = {
    'oanda_trade_id': '',
    'client_id': '',
    'alive': True,
    'sl_owner': 'ratchet',
    'current_sl': 0.0,
    'tp1': 0.0,
    'tiers_hit': [],
    'last_updated_by': '',
    'last_updated_at': '',
}


class TradeCoordinator:
    """
    File-backed trade coordination state.

    Usage:
        coord = TradeCoordinator('/path/to/bot/data')
        coord.register_trade('6004', 'scrooge_xxx', sl=0.78382)
        if coord.is_trade_alive('6004'):
            rec = coord.get_trade('6004')
            if rec['sl_owner'] == 'ratchet':
                coord.update_trade('6004', current_sl=0.789, last_updated_by='ratchet')
    """

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, 'trade_coordination.json')
        self._cache: Dict[str, dict] = {}
        self._cache_mtime: float = 0.0
        self._load()

    # ── Read ──────────────────────────────────────────────────

    def _load(self) -> Dict[str, dict]:
        """Load state from disk. Uses mtime check to avoid redundant reads."""
        try:
            if os.path.exists(self._path):
                mtime = os.path.getmtime(self._path)
                if mtime != self._cache_mtime:
                    with open(self._path, 'r') as f:
                        self._cache = json.load(f)
                    self._cache_mtime = mtime
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f'TradeCoordinator: load failed ({e}), using cache')
        return self._cache

    def is_trade_alive(self, oanda_trade_id: str) -> bool:
        """Check if a trade is still considered alive. Fast, cache-aware.
        Unknown trades (not yet registered) are treated as alive for backward compatibility."""
        data = self._load()
        rec = data.get(str(oanda_trade_id))
        if rec is None:
            return True  # Unknown trade = assume alive (pre-coordination legacy trades)
        return rec.get('alive', True)

    def get_trade(self, oanda_trade_id: str) -> Optional[dict]:
        """Get full coordination record for a trade. Returns None if not tracked."""
        data = self._load()
        return data.get(str(oanda_trade_id))

    def get_all_alive(self) -> Dict[str, dict]:
        """Return all alive trade records."""
        data = self._load()
        return {k: v for k, v in data.items() if v.get('alive', False)}

    # ── Write (atomic, locked) ────────────────────────────────

    def _write(self, data: Dict[str, dict]):
        """Atomic write with file locking."""
        tmp = self._path + '.tmp'
        lock_path = self._path + '.lock'
        lock_fh = None
        try:
            lock_fh = open(lock_path, 'w')
            # Try lock with 3 retries, 50ms apart
            for attempt in range(3):
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if attempt == 2:
                        log.warning('TradeCoordinator: write lock contention after 3 attempts')
                        return
                    time.sleep(0.05)
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
            self._cache = data
            self._cache_mtime = os.path.getmtime(self._path)
        except OSError as e:
            log.error(f'TradeCoordinator: write failed ({e})')
        finally:
            if lock_fh:
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                    lock_fh.close()
                except Exception:
                    pass

    def register_trade(self, oanda_trade_id: str, client_id: str,
                       sl: float, tp1: float = 0.0, actor: str = 'bot') -> dict:
        """Register a new trade at entry time. Returns the new record."""
        data = self._load()
        rec = dict(_DEFAULT_RECORD)
        rec.update({
            'oanda_trade_id': str(oanda_trade_id),
            'client_id': client_id,
            'alive': True,
            'sl_owner': 'ratchet',
            'current_sl': sl,
            'tp1': tp1,
            'tiers_hit': [],
            'last_updated_by': actor,
            'last_updated_at': datetime.now(timezone.utc).isoformat(),
        })
        data[str(oanda_trade_id)] = rec
        self._write(data)
        log.info(f'TradeCoordinator: registered {client_id} (OANDA {oanda_trade_id}) sl={sl:.5f} tp1={tp1:.5f}')
        return rec

    def update_trade(self, oanda_trade_id: str, **fields) -> bool:
        """Update specific fields on a trade record. Returns True if written."""
        data = self._load()
        key = str(oanda_trade_id)
        if key not in data:
            log.warning(f'TradeCoordinator: update on unknown trade {key}')
            return False
        for k, v in fields.items():
            if k in _DEFAULT_RECORD or k in ('last_updated_by', 'last_updated_at'):
                data[key][k] = v
        data[key]['last_updated_at'] = datetime.now(timezone.utc).isoformat()
        self._write(data)
        return True

    def mark_dead(self, oanda_trade_id: str, actor: str = 'unknown'):
        """Mark a trade as no longer alive. Called when any actor detects trade gone."""
        data = self._load()
        key = str(oanda_trade_id)
        if key in data:
            data[key]['alive'] = False
            data[key]['last_updated_by'] = actor
            data[key]['last_updated_at'] = datetime.now(timezone.utc).isoformat()
            self._write(data)
            log.info(f'TradeCoordinator: marked dead {data[key].get("client_id", key)} by {actor}')

    def cleanup_dead(self, max_age_hours: float = 24.0):
        """Remove dead records older than max_age_hours. Called periodically."""
        data = self._load()
        now = datetime.now(timezone.utc)
        to_remove = []
        for key, rec in data.items():
            if not rec.get('alive', True):
                try:
                    updated = datetime.fromisoformat(rec.get('last_updated_at', ''))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    if (now - updated).total_seconds() > max_age_hours * 3600:
                        to_remove.append(key)
                except (ValueError, TypeError):
                    to_remove.append(key)
        if to_remove:
            for key in to_remove:
                del data[key]
            self._write(data)
            log.info(f'TradeCoordinator: cleaned up {len(to_remove)} dead record(s)')
