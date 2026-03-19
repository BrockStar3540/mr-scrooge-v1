# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
# ============================================================
# MR. HARVESTER — PROFIT EXTRACTION DAEMON
#
# Monitors open Scrooge positions and locks in profit via
# staggered partial closes.
#
# Tier thresholds are expressed as % of equity by default —
# self-adjusting as the account grows. Each pair gets its own
# pip threshold calculated from live pip dollar value so that
# all pairs target the same dollar profit per tier regardless
# of the quote currency.
#
# Manual pip overrides per pair/tier can be set via the
# Scrooge dashboard (stored in harvester_config.json) and
# take effect on the next poll cycle — no restart needed.
#
#   Default tiers (equity %):
#     T1: 1.5% → close 25%, SL → BE
#     T2: 3.0% → close 25%, SL → +T1 pips
#     T3: 4.5% → close 25%, SL → +T2 pips
#   Runner: remaining 25% rides to OANDA native TP2
#
# Polls OANDA every 30 seconds.
# State persisted to disk — restarts don't double-harvest.
# ============================================================

import os
import sys
import json
import time
import logging
import signal
from datetime import datetime, timezone
from typing import Dict, List, Optional

# When running from harvester/ subdirectory inside the repo, parent dir is the bot root
SCROOGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, SCROOGE_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCROOGE_DIR, '.env'))

from core.oanda_client import OANDAClient


# ============================================================
# PATHS
# ============================================================

HARVESTER_DIR       = os.path.dirname(os.path.abspath(__file__))
STATE_FILE          = os.path.join(HARVESTER_DIR, 'harvester_state.json')
HARVESTER_CONFIG    = os.path.join(HARVESTER_DIR, 'harvester_config.json')

POLL_INTERVAL = 30  # seconds


# ============================================================
# DEFAULT CONFIG
# ============================================================

DEFAULT_CONFIG = {
    # Tier thresholds as % of equity.
    # threshold_pips = (equity × pct) / (pip_value_per_unit × original_units)
    # This keeps dollar profit per tier constant regardless of account size.
    'tier_equity_pcts':    [0.015, 0.030, 0.045],  # T1=1.5%, T2=3.0%, T3=4.5%
    'sl_lock_equity_pcts': [0.0,   0.015, 0.030],  # SL offset from entry per tier
    'close_pcts':          [0.25,  0.25,  0.25],   # fraction of original to close

    # Per-pair manual pip overrides (populated via dashboard UI).
    # Key: OANDA instrument string (e.g. "EUR_USD").
    # Value: {"pips": [10,20,30], "sl_lock_pips": [0,10,20]}
    # When set, overrides the equity-% calculation for that pair.
    'pair_pip_overrides': {}
}


# ============================================================
# CONFIG I/O
# ============================================================

def load_config() -> dict:
    try:
        if os.path.exists(HARVESTER_CONFIG):
            with open(HARVESTER_CONFIG) as f:
                stored = json.load(f)
            # Merge with defaults so new keys are always present
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(stored)
            return cfg
    except Exception as e:
        log.warning(f'Config load failed: {e} — using defaults')
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    tmp = HARVESTER_CONFIG + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, HARVESTER_CONFIG)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger('mr-harvester')

os.makedirs(os.path.join(HARVESTER_DIR, 'logs'), exist_ok=True)
harvest_log = logging.getLogger('mr-harvester.actions')
_hh = logging.FileHandler(os.path.join(HARVESTER_DIR, 'logs', 'harvests.log'))
_hh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
harvest_log.addHandler(_hh)


# ============================================================
# SHUTDOWN
# ============================================================

_running = True


def _handle_shutdown(signum, frame):
    global _running
    log.warning(f'Signal {signum} received — shutting down')
    _running = False


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT,  _handle_shutdown)


# ============================================================
# STATE PERSISTENCE
# ============================================================

def load_state() -> Dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        log.warning(f'State load failed: {e}')
    return {}


def save_state(state: Dict):
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f'State save failed: {e}')


# ============================================================
# PIP HELPERS
# ============================================================

def get_pip_size(instrument: str, oanda: OANDAClient) -> float:
    inst = oanda._instrument_cache.get(instrument, {})
    pip_loc = int(inst.get('pipLocation', -4))
    return 10 ** pip_loc


def pips_profit(entry: float, current: float, direction: str, pip_size: float) -> float:
    if direction == 'long':
        return (current - entry) / pip_size
    return (entry - current) / pip_size


def compute_tier_pips(
    equity: float,
    original_units: float,
    instrument: str,
    mid: float,
    oanda: OANDAClient,
    cfg: dict,
) -> tuple:
    """
    Returns (tier_pips: List[float], sl_lock_pips: List[float], source: str).

    If a manual override exists for this instrument, uses those pip values.
    Otherwise, calculates from equity % targets using live pip dollar value.
    """
    overrides = cfg.get('pair_pip_overrides', {}).get(instrument)
    if overrides:
        return (
            overrides.get('pips',        [10, 20, 30]),
            overrides.get('sl_lock_pips', [0, 10, 20]),
            'override',
        )

    # Dynamic: dollar_target = equity × pct; threshold = dollar_target / pip_dollar_value
    pip_value_per_unit = oanda.get_pip_value_per_unit(instrument, mid)
    pip_dollar_value   = pip_value_per_unit * original_units

    tier_equity_pcts    = cfg.get('tier_equity_pcts',    [0.015, 0.030, 0.045])
    sl_lock_equity_pcts = cfg.get('sl_lock_equity_pcts', [0.0,   0.015, 0.030])

    if pip_dollar_value > 0:
        tier_pips    = [round((equity * p) / pip_dollar_value, 1) for p in tier_equity_pcts]
        sl_lock_pips = [round((equity * p) / pip_dollar_value, 1) for p in sl_lock_equity_pcts]
    else:
        tier_pips    = [10.0, 20.0, 30.0]
        sl_lock_pips = [0.0, 10.0, 20.0]

    return tier_pips, sl_lock_pips, 'dynamic'


# ============================================================
# CORE: PROCESS ONE TRADE
# ============================================================

def process_trade(trade: Dict, state: Dict, oanda: OANDAClient,
                  equity: float, cfg: dict) -> bool:
    oanda_id  = str(trade.get('id', ''))
    client_id = (
        trade.get('tradeClientExtensions', {}).get('id', '')
        or trade.get('clientExtensions', {}).get('id', '')
    )

    if not (client_id.startswith('scrooge_') or client_id.startswith('scrooge-')):
        return False

    current_units_raw = float(trade.get('currentUnits', 0))
    current_units     = abs(current_units_raw)
    if current_units < 1:
        return False

    direction   = 'long' if current_units_raw > 0 else 'short'
    entry_price = float(trade.get('price', 0))
    current_sl  = float(trade.get('stopLossOrder', {}).get('price', 0))

    if oanda_id not in state:
        state[oanda_id] = {
            'client_id':      client_id,
            'instrument':     trade.get('instrument', ''),
            'direction':      direction,
            'entry_price':    entry_price,
            'original_units': current_units,
            'tiers_hit':      [],
            'current_sl':     current_sl,
            'first_seen':     datetime.now(timezone.utc).isoformat(),
        }
        log.info(
            f'Tracking: {client_id} | {trade.get("instrument")} {direction} '
            f'{current_units:.0f} units @ {entry_price:.5f}'
        )
        save_state(state)
        return True

    ts             = state[oanda_id]
    instrument     = ts['instrument']
    original_units = ts['original_units']
    tiers_hit      = ts['tiers_hit']
    pip_size       = get_pip_size(instrument, oanda)

    prices = oanda.get_prices([instrument])
    px     = prices.get(instrument)
    if not px:
        log.warning(f'{instrument}: price fetch failed — skipping')
        return False

    mid         = (px['bid'] + px['ask']) / 2.0
    profit_pips = pips_profit(entry_price, mid, direction, pip_size)

    # Compute thresholds — dynamic or override
    tier_pips, sl_lock_pips, source = compute_tier_pips(
        equity, original_units, instrument, mid, oanda, cfg
    )
    close_pcts = cfg.get('close_pcts', [0.25, 0.25, 0.25])

    log.debug(
        f'{client_id} | {instrument} | profit={profit_pips:.1f}p | '
        f'thresholds={tier_pips} ({source}) | tiers_hit={tiers_hit}'
    )

    state_changed = False

    for i in range(len(tier_pips)):
        if i in tiers_hit:
            continue
        if profit_pips < tier_pips[i]:
            break

        close_pct      = close_pcts[i] if i < len(close_pcts) else 0.25
        units_to_close = round(original_units * close_pct)
        units_to_close = max(1, min(units_to_close, int(current_units)))

        sl_offset = sl_lock_pips[i] * pip_size if i < len(sl_lock_pips) else 0.0
        if direction == 'long':
            new_sl = entry_price + sl_offset
        else:
            new_sl = entry_price - sl_offset

        tracked_sl   = ts.get('current_sl') or current_sl
        sl_is_better = (
            (direction == 'long'  and new_sl > tracked_sl) or
            (direction == 'short' and new_sl < tracked_sl)
        )

        log.info(
            f'HARVEST T{i+1}: {client_id} | {instrument} | '
            f'{profit_pips:.1f}p / {tier_pips[i]}p target [{source}] | '
            f'closing {units_to_close:.0f} units ({close_pct*100:.0f}% of {original_units:.0f}) | '
            f'SL → {new_sl:.5f}'
        )

        close_res = oanda.close_trade_partial(oanda_id, units_to_close)
        if not close_res.get('success'):
            log.error(f'Partial close FAILED: {client_id} T{i+1} | {close_res.get("error")}')
            break

        fill_price  = close_res.get('fill_price', mid)
        realized_pl = close_res.get('realized_pl') or 0.0

        harvest_log.info(
            f'HARVEST T{i+1} | {client_id} | {instrument} {direction} | '
            f'{profit_pips:.1f}p | closed {units_to_close:.0f}/{original_units:.0f} units '
            f'@ {fill_price:.5f} | realized_pl={realized_pl:+.4f} | new_sl={new_sl:.5f} | [{source}]'
        )

        if sl_is_better:
            sl_ok = oanda.modify_trade_stop(oanda_id, new_sl)
            if sl_ok:
                ts['current_sl'] = new_sl
                log.info(f'SL advanced: {client_id} → {new_sl:.5f}')
            else:
                log.warning(f'SL move failed: {client_id} T{i+1}')
        else:
            log.info(f'SL unchanged (already better): {client_id}')

        ts['tiers_hit'].append(i)
        current_units -= units_to_close
        state_changed  = True

    if state_changed:
        save_state(state)

    return state_changed


# ============================================================
# CLEANUP
# ============================================================

def cleanup_closed_trades(state: Dict, live_ids: set):
    to_remove = [oid for oid in state if oid not in live_ids]
    for oid in to_remove:
        ts = state[oid]
        log.info(
            f'Trade closed: {ts.get("client_id", oid)} | '
            f'tiers_hit={ts.get("tiers_hit", [])}'
        )
        del state[oid]
    if to_remove:
        save_state(state)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    log.info('=' * 60)
    log.info('Mr. Harvester — Profit Extraction Daemon — STARTING')
    log.info(f'Config: {HARVESTER_CONFIG}')
    log.info(f'Poll interval: {POLL_INTERVAL}s')
    log.info('=' * 60)

    # Ensure default config exists on first run
    if not os.path.exists(HARVESTER_CONFIG):
        save_config(DEFAULT_CONFIG)

    oanda = OANDAClient()
    state = load_state()
    log.info(f'Loaded state: {len(state)} known trade(s)')

    while _running:
        loop_start = time.time()
        try:
            cfg    = load_config()        # reload every cycle — picks up dashboard changes live
            equity = oanda.get_equity()

            trades   = oanda.get_open_trades()
            live_ids = {str(t.get('id', '')) for t in trades}
            cleanup_closed_trades(state, live_ids)

            scrooge_trades = [
                t for t in trades
                if (
                    t.get('tradeClientExtensions', {}).get('id', '')
                    or t.get('clientExtensions', {}).get('id', '')
                ).startswith('scrooge')
            ]

            if scrooge_trades:
                log.info(f'Monitoring {len(scrooge_trades)} Scrooge trade(s) | equity=${equity:.2f}')
                for trade in scrooge_trades:
                    try:
                        process_trade(trade, state, oanda, equity, cfg)
                    except Exception as e:
                        cid = (
                            trade.get('tradeClientExtensions', {}).get('id', '')
                            or trade.get('clientExtensions', {}).get('id', 'unknown')
                        )
                        log.error(f'Error on trade {cid}: {e}', exc_info=True)
            else:
                log.info('No Scrooge trades open — idle')

        except Exception as e:
            log.error(f'Poll cycle error: {e}', exc_info=True)

        elapsed    = time.time() - loop_start
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        end        = time.monotonic() + sleep_time
        while time.monotonic() < end and _running:
            time.sleep(1)

    log.info('Mr. Harvester — STOPPED')


if __name__ == '__main__':
    main()
