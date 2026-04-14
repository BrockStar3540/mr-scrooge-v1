#!/usr/bin/env python3
# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
# ============================================================
# MR. SCROOGE — MAIN RUNTIME LOOP
# OANDA forex box theory reversal bot.
#
# Boot sequence:
#   1. Load credentials from .env
#   2. Connect OANDA
#   3. Screen forex universe
#   4. Initialize box state machines
#   5. Run 5-minute loop
#
# Loop (every 5-min candle):
#   For each active instrument:
#     - Update box state
#     - Manage open positions
#     - Evaluate new signals
#     - Execute if all gates pass
#
# Daily reset at UTC midnight.
# Emergency shutdown on daily loss limit.
# ============================================================

import os
import sys
import json
import time
import logging
import logging.handlers
import signal
import fcntl
import threading
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from dotenv import load_dotenv

# Fix import paths — modules live in subdirectories
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.oanda_client import OANDAClient
from core.trade_state import TradeCoordinator
from core.fee_calculator import OANDAFeeCalculator
from core.execution_router import OANDAExecutionRouter
from gates import (
    ZoneDetector,
    SignalEngine,
    BoxStateMachine, BoxState,
    ForexUniverseScreener,
    BASE_PAIRS as SCROOGE_BASE_PAIRS,
    PROBATION_PAIRS as SCROOGE_PROBATION_PAIRS,
    get_active_sessions, get_session_pairs, SESSION_WINDOWS,
)
from portfolio.trade_manager import TradeManager
from status import write_status
from regime import RegimeSupervisor
from extreme_reversal import ExtremeReversalEngine
from trend_engine import TrendEngine
from quality import TradeQualityTracker, EntryContextLogger

# ============================================================
# LOGGING
# ============================================================

os.makedirs('logs', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
    handlers=[
        logging.StreamHandler(sys.stdout)  # systemd appends stdout -> mrscrooge.log
    ]
)
log = logging.getLogger('mr-scrooge')

# Trade decisions logged separately for analysis
decision_log = logging.getLogger('mr-scrooge.decisions')
decision_handler = logging.handlers.RotatingFileHandler('logs/decisions.log', maxBytes=10*1024*1024, backupCount=5)
decision_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
decision_log.addHandler(decision_handler)
decision_log.propagate = False  # prevent duplicate lines in mrscrooge.log

# Trade fills logged separately
trade_log = logging.getLogger('mr-scrooge.trades')
trade_handler = logging.handlers.RotatingFileHandler('logs/trades.log', maxBytes=10*1024*1024, backupCount=5)
trade_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
trade_log.addHandler(trade_handler)
trade_log.propagate = False  # prevent duplicate lines in mrscrooge.log

# ============================================================
# CONFIG
# ============================================================

CONFIG = {
    # Risk — bigger conviction, fewer trades
    'max_trade_risk_pct':      0.015,    # kept for daily-loss reference; sizing is now margin-based
    'margin_utilization_pct':  0.80,     # deploy 80% of equity as margin per trade
    # Trend participation engine (2026-04-14)
    'trend_enabled':             True,
    'trend_full_size_fraction':  0.50,
    'trend_stretch_size_fraction': 0.30,
    'trend_sma_proximity_pct':  0.0015,
    'trend_atr_stop_multiple':  1.0,
    'trend_fixed_score':        30,
    'max_risk_per_trade_pct':  0.04,     # 4% max account loss per trade — caps units when SL is wide
    'target_profit_pct':       0.0025,   # 0.25% of equity per trade — profit-anchored sizing target
    'tp1_allocation_pct':      0.40,     # 40% of position closes at TP1 — sizing anchors to this partial
    'green_exit_min_pct':      0.00125,  # 50% of target (0.125%) — equity % required to trigger green exit
    'max_concurrent_positions': 1,       # 1 max — concentrate capital in best setup
    'max_eur_positions':        2,       # EUR exposure cap — EUR trending; max 2 EUR pairs open
    'max_daily_loss_pct':       0.075,   # 7.5% daily halt — allows 5 full stops at 1.5% risk before shutdown
    'min_risk_reward':          1.35,    # Reduced to increase qualified setups while keeping RR > 1.5 floor

    # Timing
    'scan_interval_seconds':    300,     # 5 minutes
    'rescan_interval_minutes':  30,      # Universe rescan
    'open_wait_minutes':        15,      # Wait after market open
    'max_hold_minutes':         360,     # 6 hours (raised from 180 — forex trends need time to develop)
    'hourly_green_exit_enabled': True,   # Close net-green positions every cycle to recycle capital
    'hourly_green_exit_minutes': 60,     # Check once per hour (was 5 — too noisy, violated 1H management rule)
    'hourly_green_exit_min_pnl': 0.002,  # 0.2% net profit required (was 0.0 — fired on trivial moves)

    # Stop loss floor — minimum SL distance to prevent fakeout stops
    'sl_floor_pct':             0.0025,  # 0.25% min SL distance from entry

    # Execution
    'limit_timeout_minutes':    5,       # Cancel unfilled limit after 5 min
    'time_exit_cooldown_minutes': 15,    # MASTER_THEORY: re-enter as soon as zone still valid; 15 min breathing room only

    # OTR thresholds — widened 2026-03-02 to stop permanently blocking GBP_JPY (was 15% marginal cap)
    # Pairs at 15-25% OTR are "marginal" = all 3 gates (zone + ATR + pitchfork) must align
    'otr_excellent':   0.05,
    'otr_acceptable':  0.15,
    'otr_marginal':    0.25,
    'min_efficiency':  2.0,
    # Pair-consistent net floor:
    # - 7 pips across pairs via min_net_profit_pips.
    # - Legacy absolute value kept as fallback when pip-mode is disabled.
    'min_net_profit':  0.001,
    'min_net_profit_pips': 7,

    # Box parameters
    'zone_proximity_pct':      0.0025,  # 0.25% — must be within this % of magic line to qualify as "in zone" (raised from 0.10% — 2026-03-01)
    'atr_period':              14,
    'atr_high_confidence_pct': 0.60,    # 60% — London/NY sessions only; Frank's 0.40 "fix" still blocked Sydney/Tokyo (those sessions hit <20% ATR)
    'atr_valid_pct':           0.60,    # Restored from 0.40 — only trade sessions with real momentum
    'atr_minimum_pct':         0.15,    # Below 15% = ignore (lowered from 20% — Frank 2026-02-27)
    'max_spread_pct_of_half':  0.30,    # Pause entries if spread > 30% of half-box (raised from 15% — was blocking all Sydney/Sunday entries; OTR tiers are the real gate)
    # Spread spike thresholds — 24h forex has 3-4x London spreads during Sydney/Sunday; that's not a spike, it's normal thin liquidity
    'spread_spike_threshold':  4.0,     # Was 2.5 — raised because Sunday/Sydney legitimately runs 3-4x London baselines
    'high_volatility_atr_mult':    99,       # disabled — forces 5M bars for pitchfork (1H candle SL too wide)

    # Universe — no instrument cap; risk ceiling is max_concurrent_positions + max_trade_risk_pct
    'instrument_tiers':        ['majors'],
    'universe_mode':           'aggressive',  # balanced | standard | aggressive
}

# ============================================================
# PAIR EXECUTION PROFILES (hybrid SL limit + green exit)
# ============================================================

_PAIR_PROFILE = {
    'EUR_USD': ('tight',  2, 1.0, 3.0, 2.5, 0.5),
    'AUD_USD': ('tight',  2, 1.0, 3.0, 2.5, 0.5),
    'EUR_CAD': ('tight',  2, 1.0, 3.0, 2.5, 0.5),
    'GBP_USD': ('medium', 4, 1.5, 5.0, 2.0, 1.0),
    'USD_JPY': ('medium', 4, 1.5, 5.0, 2.0, 1.0),
    'NZD_USD': ('medium', 4, 1.5, 5.0, 2.0, 1.0),
    'USD_CHF': ('medium', 4, 1.5, 5.0, 2.0, 1.0),
    'USD_CAD': ('medium', 4, 1.5, 5.0, 2.0, 1.0),
    'EUR_JPY': ('medium', 4, 1.5, 5.0, 2.0, 1.0),
    'GBP_JPY': ('wide',   6, 2.0, 7.0, 1.8, 1.5),
    'CHF_JPY': ('wide',   6, 2.0, 7.0, 1.8, 1.5),
    'NZD_JPY': ('wide',   6, 2.0, 7.0, 1.8, 1.5),
    'AUD_JPY': ('wide',   6, 2.0, 7.0, 1.8, 1.5),
    'AUD_CHF': ('wide',   6, 2.0, 7.0, 1.8, 1.5),
}
_PAIR_PROFILE_DEFAULT = ('medium', 4, 1.5, 5.0, 2.0, 1.0)


def _get_pair_profile(symbol):
    """Returns (profile_name, trig_pips, offset_pips, fallback_pips, fast_mult, green_offset_pips)."""
    return _PAIR_PROFILE.get(symbol, _PAIR_PROFILE_DEFAULT)


def _is_fast_market(bars_5m, fast_mult):
    """True if most recent candle range >= fast_mult × 20-candle average range."""
    try:
        if bars_5m is None or len(bars_5m) < 20:
            return False
        recent = bars_5m.iloc[-1]
        candle_range = float(recent['high']) - float(recent['low'])
        avg_range = float((bars_5m['high'] - bars_5m['low']).iloc[-20:-1].mean())
        return avg_range > 0 and candle_range >= avg_range * fast_mult
    except Exception:
        return False


def _handle_green_exit(pos, symbol, current_price, bars_5m, oanda, router, trade_manager, ctx):
    """Place a limit close at profile-adjusted offset. Falls back to market on failure/fast market."""
    if not pos.oanda_trade_id:
        return False

    _, _, _, _, fast_mult, green_offset_pips = _get_pair_profile(symbol)
    pip = 0.01 if 'JPY' in symbol else 0.0001

    if _is_fast_market(bars_5m, fast_mult):
        log.info(f'{symbol} {pos.trade_id}: green exit — fast market, using market close')
        pos.sl_fallback_used = True
        exit_res = router.partial_close(pos.trade_id, pos.oanda_trade_id, pos.remaining_quantity, tp_level=0)
        realized_pnl = exit_res.get('realized_pl') if isinstance(exit_res, dict) else None
        if realized_pnl is None and pos.oanda_trade_id:
            realized_pnl = oanda.get_trade_realized_pl(pos.oanda_trade_id)
        close_pnl = trade_manager.close_position(pos.trade_id, current_price, 'Green exit (market — fast market)', pnl_override=realized_pnl) or 0.0
        ctx['daily_loss'] += max(0, -close_pnl)
        ctx['position_count'] = max(0, ctx['position_count'] - 1)
        trade_log.info(f'GREEN_EXIT {pos.trade_id} | {symbol} {pos.direction} | MARKET(fast) @ {current_price:.5f} | PnL: {close_pnl:+.4f}')
        return True

    prices = oanda.get_prices([symbol])
    px = prices.get(symbol, {})
    close_side = 'SELL' if pos.direction == 'long' else 'BUY'
    if pos.direction == 'long':
        limit_price = px.get('bid', current_price) - green_offset_pips * pip
    else:
        limit_price = px.get('ask', current_price) + green_offset_pips * pip

    result = oanda.place_opposing_limit(symbol, close_side, pos.remaining_quantity, limit_price, pos.trade_id, expire_seconds=300)

    if not result.get('success'):
        log.warning(f'{symbol} {pos.trade_id}: green exit limit failed — market fallback')
        pos.sl_fallback_used = True
        exit_res = router.partial_close(pos.trade_id, pos.oanda_trade_id, pos.remaining_quantity, tp_level=0)
        realized_pnl = exit_res.get('realized_pl') if isinstance(exit_res, dict) else None
        if realized_pnl is None and pos.oanda_trade_id:
            realized_pnl = oanda.get_trade_realized_pl(pos.oanda_trade_id)
        close_pnl = trade_manager.close_position(pos.trade_id, current_price, 'Green exit (market fallback)', pnl_override=realized_pnl) or 0.0
        ctx['daily_loss'] += max(0, -close_pnl)
        ctx['position_count'] = max(0, ctx['position_count'] - 1)
        trade_log.info(f'GREEN_EXIT {pos.trade_id} | {symbol} {pos.direction} | MARKET_FALLBACK @ {current_price:.5f} | PnL: {close_pnl:+.4f}')
        return True

    if result.get('filled'):
        fill_price = result.get('fill_price', limit_price)
        realized_pnl = result.get('realized_pl')
        close_pnl = trade_manager.close_position(pos.trade_id, fill_price, 'Green exit (limit fill)', pnl_override=realized_pnl) or 0.0
        ctx['daily_loss'] += max(0, -close_pnl)
        ctx['position_count'] = max(0, ctx['position_count'] - 1)
        trade_log.info(f'GREEN_EXIT {pos.trade_id} | {symbol} {pos.direction} | LIMIT_FILL @ {fill_price:.5f} | PnL: {close_pnl:+.4f}')
        return True

    order_id = result.get('order_id', '')
    log.info(f'{symbol} {pos.trade_id}: green exit limit PENDING @ {limit_price:.5f} | order {order_id}')
    return False


def _manage_sl_limit(pos, symbol, current_price, bars_5m, oanda, stop_moved=False):
    """Per-cycle hybrid SL limit management. OANDA native SL stays as hard backstop."""
    if not pos.oanda_trade_id:
        return
    if pos.sl_limit_filled:
        return  # Position already exited via SL limit — no further limit management needed

    _, trig_pips, offset_pips, fallback_pips, fast_mult, _ = _get_pair_profile(symbol)
    pip = 0.01 if 'JPY' in symbol else 0.0001
    trigger_buffer  = trig_pips * pip
    limit_offset    = offset_pips * pip
    fallback_buffer = fallback_pips * pip

    # Step 1: Check if active limit was filled
    if pos.sl_limit_active and pos.sl_limit_order_id:
        filled_info = oanda.check_order_filled(pos.sl_limit_order_id)
        if filled_info.get('filled'):
            fill_price = filled_info.get('fill_price', current_price)
            pips_saved = ((fill_price - pos.current_stop) if pos.direction == 'long'
                          else (pos.current_stop - fill_price)) / pip
            pos.sl_limit_filled = True
            pos.sl_limit_active = False
            pos.sl_limit_order_id = ''
            pos.sl_limit_pips_saved = round(max(0.0, pips_saved), 1)
            log.info(f'{symbol} {pos.trade_id}: SL limit FILLED @ {fill_price:.5f} | saved {pos.sl_limit_pips_saved:.1f}p vs SL {pos.current_stop:.5f}')
            trade_log.info(f'SL_LIMIT_FILL {pos.trade_id} | {symbol} {pos.direction} | fill={fill_price:.5f} sl={pos.current_stop:.5f} pips_saved={pos.sl_limit_pips_saved:.1f}')
            return
        elif filled_info.get('state') in ('CANCELLED', 'EXPIRED', 'ERROR'):
            pos.sl_limit_active = False
            pos.sl_limit_order_id = ''
            return  # Don't fall through to place a new order in the same cycle

    # Step 2: Cancel if stop ratcheted
    if stop_moved and pos.sl_limit_active and pos.sl_limit_order_id:
        oanda.cancel_order(pos.sl_limit_order_id)
        pos.sl_limit_active = False
        pos.sl_limit_order_id = ''
        log.info(f'{symbol} {pos.trade_id}: SL limit cancelled — stop ratcheted to {pos.current_stop:.5f}')

    # Step 3: Fallback — price past stop (runaway)
    if pos.sl_limit_active and pos.sl_limit_order_id:
        if pos.direction == 'long' and current_price < pos.current_stop - fallback_buffer:
            log.warning(f'{symbol} {pos.trade_id}: FAST_MOVE past SL — cancelling limit, OANDA SL is backstop')
            oanda.cancel_order(pos.sl_limit_order_id)
            pos.sl_limit_active = False
            pos.sl_limit_order_id = ''
            pos.sl_fallback_used = True
            return
        elif pos.direction == 'short' and current_price > pos.current_stop + fallback_buffer:
            log.warning(f'{symbol} {pos.trade_id}: FAST_MOVE past SL — cancelling limit, OANDA SL is backstop')
            oanda.cancel_order(pos.sl_limit_order_id)
            pos.sl_limit_active = False
            pos.sl_limit_order_id = ''
            pos.sl_fallback_used = True
            return

    if pos.sl_limit_active:
        return  # Already have one, nothing to do

    # Step 4: Place new limit if within trigger_buffer
    # Use coordination file SL if available (fresher than 5-min stale pos.current_stop)
    _auth_sl = pos.current_stop
    try:
        _tc = globals().get('_fast_loop_ctx', {}).get('trade_coord') or None
        if _tc and pos.oanda_trade_id:
            _coord_rec = _tc.get_trade(pos.oanda_trade_id)
            if _coord_rec and _coord_rec.get('current_sl', 0) > 0:
                _auth_sl = _coord_rec['current_sl']
    except Exception:
        pass
    if pos.direction == 'long':
        dist_to_stop = current_price - _auth_sl
        if dist_to_stop > trigger_buffer:
            return
        limit_price = _auth_sl + limit_offset
        close_side  = 'SELL'
    else:
        dist_to_stop = _auth_sl - current_price
        if dist_to_stop > trigger_buffer:
            return
        limit_price = _auth_sl - limit_offset
        close_side  = 'BUY'

    if _is_fast_market(bars_5m, fast_mult):
        log.info(f'{symbol} {pos.trade_id}: SL limit skipped — fast market (profile={_get_pair_profile(symbol)[0]})')
        return

    result = oanda.place_opposing_limit(symbol, close_side, pos.remaining_quantity, limit_price, pos.trade_id, expire_seconds=600)
    pos.sl_limit_attempted += 1

    if not result.get('success'):
        if result.get('no_position'):
            log.warning(f'{symbol} {pos.trade_id}: SL limit — NO_POSITION_TO_REDUCE, trade closed externally (harvester?)')
            pos.sl_limit_filled = True  # Prevent further SL limit attempts on this dead position
        else:
            log.warning(f'{symbol} {pos.trade_id}: SL limit placement failed — OANDA native SL is backstop')
        return

    if result.get('filled'):
        fill_price = result.get('fill_price', limit_price)
        pips_saved = ((fill_price - pos.current_stop) if pos.direction == 'long'
                      else (pos.current_stop - fill_price)) / pip
        pos.sl_limit_filled = True
        pos.sl_limit_pips_saved = round(max(0.0, pips_saved), 1)
        log.info(f'{symbol} {pos.trade_id}: SL limit IMMEDIATE_FILL @ {fill_price:.5f} | saved {pos.sl_limit_pips_saved:.1f}p')
        trade_log.info(f'SL_LIMIT_FILL {pos.trade_id} | {symbol} {pos.direction} | fill={fill_price:.5f} sl={pos.current_stop:.5f} pips_saved={pos.sl_limit_pips_saved:.1f} (immediate)')
    else:
        pos.sl_limit_order_id = result.get('order_id', '')
        pos.sl_limit_price    = limit_price
        pos.sl_limit_active   = True
        log.info(f'{symbol} {pos.trade_id}: SL limit PLACED @ {limit_price:.5f} | order {pos.sl_limit_order_id} | dist={dist_to_stop/pip:.1f}p | profile={_get_pair_profile(symbol)[0]}')


# ============================================================
# SHUTDOWN HANDLER
# ============================================================

_running = True
_INSTANCE_LOCK_FH = None
_exec_lock = threading.Lock()          # guards position management against fast loop reentrancy
_cached_bars: Dict[str, object] = {}  # bars_5m cache populated by slow loop, consumed by fast loop

def _acquire_instance_lock_or_exit():
    """Ensure only one Mr. Scrooge process can run in this working directory."""
    global _INSTANCE_LOCK_FH

    os.makedirs('logs', exist_ok=True)
    lock_path = os.path.join('logs', 'mr-scrooge.lock')
    lock_fh = open(lock_path, 'a+', encoding='utf-8')
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.critical(f'Another Mr. Scrooge instance is already running (lock: {lock_path}) — exiting')
        sys.exit(1)

    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()
    _INSTANCE_LOCK_FH = lock_fh

def _handle_shutdown(signum, frame):
    global _running
    log.warning(f'Signal {signum} received — initiating graceful shutdown')
    _running = False

signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT,  _handle_shutdown)

# ============================================================
# FRANK ALERTS (via Frank outbox → Telegram)
# ============================================================

_FRANK_OUTBOX = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'frank', 'outbox')
)


def _send_scrooge_alert(text: str):
    try:
        os.makedirs(_FRANK_OUTBOX, exist_ok=True)
        filename = f'{int(time.time() * 1000)}-scrooge-frank.json'
        path = os.path.join(_FRANK_OUTBOX, filename)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'text': text}, fh)
    except Exception as exc:
        log.warning(f'Failed to queue Frank alert: {exc}')


# ============================================================
# MARKET HOURS
# ============================================================

def is_weekend_utc() -> bool:
    """Returns True during OANDA weekend close (Sat 00:00 – Sun 22:00 UTC)."""
    now = datetime.now(timezone.utc)
    wd  = now.weekday()  # 0=Mon … 5=Sat, 6=Sun
    if wd == 5:
        return True
    if wd == 6 and now.hour < 22:
        return True
    return False


def _sleep(seconds: float):
    """Interruptible sleep — responds to SIGTERM within 1 second."""
    global _running
    end = time.monotonic() + seconds
    while time.monotonic() < end and _running:
        time.sleep(1)


# ============================================================
# SESSION MANAGEMENT
# ============================================================

def _update_sessions(ctx: dict, now: datetime) -> bool:
    """
    Checks for session opens/closes each cycle.
    Updates active_instruments to match currently open sessions.
    Returns True if session state changed (triggers log + rescan).
    """
    utc_hour = now.hour

    # Only re-evaluate when the hour changes
    if utc_hour == ctx.get('_last_session_hour', -1):
        return False
    ctx['_last_session_hour'] = utc_hour

    open_sessions  = {s['name'] for s in get_active_sessions(utc_hour)}
    prev_sessions  = ctx.get('_active_sessions', set())
    newly_opened   = open_sessions - prev_sessions
    newly_closed   = prev_sessions - open_sessions

    for name in sorted(newly_opened):
        log.info(f'════ {name.upper()} SESSION OPEN ════ {now.strftime("%H:%M")} UTC')
    for name in sorted(newly_closed):
        log.info(f'════ {name.upper()} SESSION CLOSED ════ {now.strftime("%H:%M")} UTC')

    ctx['_active_sessions'] = open_sessions

    if not (newly_opened or newly_closed):
        return False

    # Session changed — rescan with session-appropriate pairs
    session_pairs = get_session_pairs(utc_hour, mode=CONFIG.get('universe_mode', 'balanced'))
    if open_sessions:
        log.info(f'Open sessions: {", ".join(sorted(open_sessions))} | '
                 f'Scanning {len(session_pairs)} pairs: {", ".join(session_pairs)}')
    else:
        log.info('No session open — idling (no pairs to scan)')

    screen_results = ctx['screener'].screen_all(pairs=session_pairs if open_sessions else [])
    new_instruments = [r.instrument for r in screen_results if r.viable]

    # Initialize box state for any newly added instruments
    oanda         = ctx['oanda']
    zone_detector = ctx['zone_detector']
    box_machine   = ctx['box_machine']
    for symbol in new_instruments:
        if symbol not in box_machine.states:
            daily_bars  = oanda.get_daily_bars(symbol)
            hourly_bars = oanda.get_hourly_bars(symbol, count=200)
            if daily_bars is None or len(daily_bars) < 3:
                continue
            prices     = oanda.get_prices([symbol])
            price_data = prices.get(symbol, {})
            current_price = (
                (price_data.get('bid', 0) + price_data.get('ask', 0)) / 2
                or float(daily_bars.iloc[-1]['close'])
            )
            spread = (price_data.get('ask', 0) - price_data.get('bid', 0)) if price_data else 0.0002
            zone_map = zone_detector.build_zone_map(
                symbol, 'forex', daily_bars,
                hourly_bars if hourly_bars is not None else daily_bars,
                current_price
            )
            if zone_map:
                box_machine.initialize_instrument(symbol, zone_map.daily_box, spread)
                log.info(f'Box initialized (session open): {symbol}')

    ctx['active_instruments'] = new_instruments
    ctx["_last_screen"] = _serialize_screen(screen_results)
    ctx['last_rescan'] = now
    log.info(f'Active instruments: {", ".join(new_instruments) if new_instruments else "none viable yet"}')
    return True


# ============================================================
# SESSION START HELPER
# ============================================================

def _compute_session_start() -> datetime:
    """
    Returns the actual open time of the most recently started active session.
    A bot restart mid-session should NOT reset the open-wait timer — the market
    has already been open for some time.
    """
    now = datetime.now(timezone.utc)
    utc_hour = now.hour
    best_start = now  # fallback: bot start time

    for session in SESSION_WINDOWS:
        o = session['open_utc']
        c = session['close_utc']
        # Determine if this session is currently active
        if o < c:
            active = o <= utc_hour < c
        else:  # crosses midnight (e.g. Sydney 21–06)
            active = utc_hour >= o or utc_hour < c

        if active:
            # Compute when this session opened today (or yesterday for overnight)
            open_today = now.replace(hour=o, minute=0, second=0, microsecond=0)
            if o > utc_hour:
                # Session opened yesterday
                open_today -= timedelta(days=1)
            # Use the earliest start among active sessions (most time has passed)
            if open_today < best_start:
                best_start = open_today

    return best_start


# ============================================================
# BOOT
# ============================================================

def boot():
    """Initialize all components. Returns (oanda, router, screener, components_dict)."""
    load_dotenv()

    log.info('=' * 60)
    log.info('Mr. Scrooge — Box Theory FX Engine — STARTING')
    log.info('Exchange: OANDA forex only')
    log.info('=' * 60)

    # Validate credentials
    api_key    = os.getenv('OANDA_API_KEY')
    account_id = os.getenv('OANDA_ACCOUNT_ID')
    environment = os.getenv('OANDA_ENVIRONMENT', 'practice')

    if not api_key or not account_id:
        log.critical('OANDA_API_KEY or OANDA_ACCOUNT_ID missing from .env — cannot start')
        sys.exit(1)

    log.info(f'Account: {account_id} | Environment: {environment}')

    # Init OANDA client
    oanda = OANDAClient()

    # Verify connection
    equity = oanda.get_equity()
    if equity is None:
        log.critical('OANDA connection failed — cannot fetch equity. Check API key.')
        sys.exit(1)
    log.info(f'OANDA connected. Starting equity: {equity:.2f}')

    # Init components
    fee_calc = OANDAFeeCalculator(CONFIG)
    router   = OANDAExecutionRouter(oanda, fee_calc, CONFIG)
    screener = ForexUniverseScreener(oanda, fee_calc)

    zone_detector = ZoneDetector(CONFIG)        # needs CONFIG for thresholds
    signal_engine = SignalEngine(CONFIG)
    trade_manager = TradeManager(CONFIG)
    box_machine   = BoxStateMachine(CONFIG)     # single shared instance, manages all symbols

    # Run initial universe screen
    log.info('Running initial universe screen...')
    utc_hour = datetime.now(timezone.utc).hour
    session_pairs = get_session_pairs(utc_hour, mode=CONFIG.get('universe_mode', 'balanced'))
    screen_results = screener.screen_all(pairs=session_pairs)
    active_instruments = [r.instrument for r in screen_results if r.viable]

    if not active_instruments:
        log.warning('No viable instruments found on initial screen. Will retry on rescan.')

    log.info(f'Active instruments: {", ".join(active_instruments) if active_instruments else "none yet"}')

    # Initialize box state machine for each instrument
    for symbol in active_instruments:
        daily_bars  = oanda.get_daily_bars(symbol)
        hourly_bars = oanda.get_hourly_bars(symbol, count=200)
        if daily_bars is None or len(daily_bars) < 3:
            log.warning(f'{symbol}: insufficient daily bars — skipping box init')
            continue
        prices     = oanda.get_prices([symbol])
        price_data = prices.get(symbol, {})
        current_price = (
            (price_data.get('bid', 0) + price_data.get('ask', 0)) / 2
            or float(daily_bars.iloc[-1]['close'])
        )
        spread   = (price_data.get('ask', 0) - price_data.get('bid', 0)) if price_data else 0.0002
        zone_map = zone_detector.build_zone_map(
            symbol, 'forex', daily_bars,
            hourly_bars if hourly_bars is not None else daily_bars,
            current_price
        )
        if zone_map:
            box_machine.initialize_instrument(symbol, zone_map.daily_box, spread)
            log.info(f'Box initialized: {symbol}')

    _agent_dir = os.path.dirname(os.path.abspath(__file__))

    # TradeCoordinator must init before reconcile so TP1 can be recovered
    trade_coord = TradeCoordinator(
        os.path.join(_agent_dir, 'data')
    )
    trade_coord.cleanup_dead()
    log.info('TradeCoordinator initialized')

    # Reconcile existing OANDA positions into trade_manager — prevents stacking after restarts
    open_trades = oanda.get_open_trades()
    reconciled  = trade_manager.reconcile_from_oanda(open_trades, trade_coord=trade_coord)
    if reconciled:
        log.info(f'Reconciled {reconciled} open position(s) from OANDA on startup')
    else:
        log.info('No existing Scrooge positions found on OANDA — starting fresh')

    regime  = RegimeSupervisor(
        log_path=os.path.join(_agent_dir, 'data', 'regime-protests.jsonl')
    )

    quality = TradeQualityTracker(
        log_path=os.path.join(_agent_dir, 'data', 'quality-trades.jsonl')
    )
    entry_context = EntryContextLogger(
        log_path=os.path.join(_agent_dir, 'data', 'entry-context.jsonl')
    )

    return {
        'oanda':          oanda,
        'fee_calc':       fee_calc,
        'router':         router,
        'screener':       screener,
        'zone_detector':  zone_detector,
        'signal_engine':  signal_engine,
        'extreme_reversal': ExtremeReversalEngine(CONFIG),
'trend_engine':     TrendEngine(CONFIG),
        'trade_manager':  trade_manager,
        'box_machine':    box_machine,
        'active_instruments': active_instruments,
        'equity_start':   equity,
        'daily_loss':     0.0,
        'last_rescan':    datetime.now(timezone.utc),
        'session_start':  _compute_session_start(),
        'position_count': reconciled,
        'time_exit_cooldowns': {},
        '_active_sessions':    set(),
        '_last_session_hour':  -1,
        '_last_signals':       [],
        '_last_screen':        _serialize_screen(screen_results),
        '_cycle_count':        0,
        'regime':         regime,
        'quality':        quality,
        'entry_context':  entry_context,
        'trade_coord':   trade_coord,
    }

# ============================================================
# DAILY RESET
# ============================================================

def daily_reset(ctx: dict):
    """Called at UTC midnight. Refreshes boxes, resets daily counters."""
    log.info('=' * 60)
    log.info('DAILY RESET — refreshing boxes and counters')
    log.info('=' * 60)

    oanda = ctx['oanda']
    ctx['daily_loss'] = 0.0

    # Rebuild zone maps with new PDH/PDL — reinitialize box states
    zone_detector = ctx['zone_detector']
    box_machine   = ctx['box_machine']
    for symbol in ctx['active_instruments']:
        daily_bars  = oanda.get_daily_bars(symbol)
        hourly_bars = oanda.get_hourly_bars(symbol, count=200)
        if daily_bars is None or len(daily_bars) < 3:
            continue
        prices     = oanda.get_prices([symbol])
        price_data = prices.get(symbol, {})
        current_price = (
            (price_data.get('bid', 0) + price_data.get('ask', 0)) / 2
            or float(daily_bars.iloc[-1]['close'])
        )
        spread   = (price_data.get('ask', 0) - price_data.get('bid', 0)) if price_data else 0.0002
        zone_map = zone_detector.build_zone_map(
            symbol, 'forex', daily_bars,
            hourly_bars if hourly_bars is not None else daily_bars,
            current_price
        )
        if zone_map:
            box_machine.initialize_instrument(symbol, zone_map.daily_box, spread)
            log.info(f'Box reset: {symbol}')

    ctx['session_start'] = datetime.now(timezone.utc)

    # Clear stale zone debounce cache
    ctx['zone_cache'] = {}
    # D1+D3: Clear daily counters
    ctx['zone_entry_counts'] = {}
    ctx['ct_failure_counts'] = {}

    # Refresh equity baseline
    equity = oanda.get_equity()
    if equity:
        ctx['equity_start'] = equity
        log.info(f'Session equity baseline: {equity:.2f}')

# ============================================================
# INSTRUMENT PROCESSING
# ============================================================

def process_instrument(symbol: str, ctx: dict) -> Optional[dict]:
    """
    Processes one instrument per candle close.
    Returns trade event dict if something happened, else None.
    """
    oanda         = ctx['oanda']
    router        = ctx['router']
    fee_calc      = ctx['fee_calc']
    zone_detector = ctx['zone_detector']
    signal_engine = ctx['signal_engine']
    trade_manager = ctx['trade_manager']
    box_machine   = ctx['box_machine']

    # Fetch bars (single retry on failure)
    bars_5m = oanda.get_5m_bars(symbol, count=300)   # 25h window — ATR gate needs full session range
    bars_1h = oanda.get_hourly_bars(symbol, count=200) # 8+ trading days — covers key weekly levels
    bars_d  = oanda.get_daily_bars(symbol, count=20)

    if bars_5m is None or bars_1h is None or bars_d is None:
        time.sleep(2)
        if bars_5m is None: bars_5m = oanda.get_5m_bars(symbol, count=300)
        if bars_1h is None: bars_1h = oanda.get_hourly_bars(symbol, count=200)
        if bars_d  is None: bars_d  = oanda.get_daily_bars(symbol, count=20)
        if bars_5m is None or bars_1h is None or bars_d is None:
            log.warning(f'{symbol}: bar fetch failed after retry — skipping')
            return None

    # Candle debounce — skip if this 5M bar was already processed
    bar_time = str(bars_5m.index[-1])
    if ctx.get('last_candle', {}).get(symbol) == bar_time:
        return None
    ctx.setdefault('last_candle', {})[symbol] = bar_time
    _cached_bars[symbol] = bars_5m  # cache for fast execution loop

    current_price = float(bars_5m.iloc[-1]['close'])
    live_spread   = oanda.get_live_spread(symbol)
    if live_spread is None:
        log.warning(f'{symbol}: spread fetch failed — skipping')
        return None

    # Build zone map (also gives us the daily_box for the box machine)
    zone_map = zone_detector.build_zone_map(symbol, 'forex', bars_d, bars_1h, current_price)
    if zone_map is None:
        log.warning(f'{symbol}: zone map failed — skipping')
        return None

    half_box = zone_map.daily_box.half_box

    # Compute round-trip overhead in price units
    cost           = fee_calc.calculate(symbol, live_spread, half_box)
    total_overhead = cost.total_round_trip

    # Minutes since session start
    minutes_since_open = int(
        (datetime.now(timezone.utc) - ctx['session_start']).total_seconds() / 60
    )

    # Update box state machine
    if symbol not in box_machine.states:
        # Lazily initialize if not already done (e.g. new instrument from rescan)
        spread = live_spread or 0.0002
        box_machine.initialize_instrument(symbol, zone_map.daily_box, spread)

    current_bar = bars_5m.iloc[-1]
    box_record  = box_machine.update(
        symbol, current_price, current_bar, live_spread, total_overhead, minutes_since_open
    )

    # Handle BOX_BROKEN — draw alternative
    if box_record and box_record.state == BoxState.BOX_BROKEN_RECALCULATING:
        log.info(f'{symbol}: Box broken — drawing alternative')
        session_high = float(bars_5m['high'].max())
        session_low  = float(bars_5m['low'].min())
        alt = box_machine.draw_alternative_box(
            symbol, bars_5m, session_high, session_low,
            box_record.active_box, total_overhead
        )
        if not alt.viable:
            log.info(f'{symbol}: No viable alternative — shelving')
            return None
        log.info(f'{symbol}: Alternative box viable — continuing')

    # Manage any open positions for this symbol (always, even if not tradeable)
    with _exec_lock:
      for pos in trade_manager.get_all_open():
        if pos.symbol != symbol:
            continue
        try:
            entry_time = datetime.fromisoformat(pos.entry_time)
            now_utc    = datetime.now(timezone.utc)
            # Ensure both are tz-aware — naive datetimes (from open_position) are UTC on EC2
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            minutes_in_trade = int((now_utc - entry_time).total_seconds() / 60)
        except Exception as _te:
            log.warning(f'{symbol} {pos.trade_id}: entry_time parse error: {_te} — defaulting to 0 min')
            minutes_in_trade = 0
        ctx['quality'].update(pos.trade_id, current_price)
        # Sync remaining_quantity from OANDA (harvester may have closed partials)
        if pos.oanda_trade_id:
            for _lt in (ctx.get('live_oanda_trades') or []):
                if str(_lt.get('id', '')) == str(pos.oanda_trade_id):
                    _cu = abs(float(_lt.get('currentUnits', 0)))
                    if _cu > 0 and _cu != pos.remaining_quantity:
                        pos.remaining_quantity = _cu
                    break
        # Sync harvester tier state → ratchet flags via coordination file
        _tc = ctx.get('trade_coord')
        if _tc and pos.oanda_trade_id:
            _coord = _tc.get_trade(pos.oanda_trade_id)
            if _coord:
                if 0 in _coord.get('tiers_hit', []) and not pos.partial_1_taken:
                    pos.partial_1_taken = True
                    pos.break_even_achieved = True
                    log.info(f'{symbol} {pos.trade_id}: Harvester T1 via coordination — partial_1_taken=True')
        mgmt = trade_manager.manage_position(
            pos.trade_id, current_price, bars_5m, bars_1h, minutes_in_trade,
            zone_map=zone_map,
            account_equity=oanda.get_equity() or ctx['equity_start']
        )
        action = mgmt.get('action', 'hold')
        if action == 'close':
            was_tp1_hit    = pos.partial_1_taken
            reason_str     = mgmt.get('reason', '')
            stopped_at_be  = pos.break_even_achieved and 'stop' in reason_str.lower()

            # Track new box confidence — only count if using alternative box
            box_rec = box_machine.get_state(symbol)
            if box_rec and box_rec.using_alternative:
                box_machine.record_new_box_trade_result(symbol, was_tp1_hit)

            # Queue re-entry if stopped at break-even (liquidity sweep candidate)
            if stopped_at_be and not was_tp1_hit:
                ctx.setdefault('pending_reentry', {})[symbol] = pos

            exit_side = 'SELL' if pos.direction == 'long' else 'BUY'
            is_stop_hit_exit = 'Stop hit' in (reason_str or '')
            realized_pnl = None
            if is_stop_hit_exit:
                # OANDA's native stop loss order already closed this position.
                # Do NOT send a redundant market order — it would open a ghost reverse position.
                log.info(f'{symbol} {pos.trade_id}: stop hit detected — OANDA already closed, skipping emergency_exit')
                if pos.oanda_trade_id:
                    realized_pnl = ctx['oanda'].get_trade_realized_pl(pos.oanda_trade_id)
            elif mgmt.get('emergency', True) or not pos.oanda_trade_id:
                # True emergencies (or missing OANDA ID) — market order
                exit_res = router.emergency_exit(
                    symbol, exit_side, pos.remaining_quantity,
                    pos.current_stop, pos.trade_id, reason_str or 'manager'
                )
            else:
                # Non-emergency close (time exit, green exit) — use OANDA
                # native trade close to avoid slippage from opposing market order
                log.info(f'{symbol} {pos.trade_id}: closing via native trade close — {reason_str}')
                exit_res = router.partial_close(
                    pos.trade_id, pos.oanda_trade_id,
                    pos.remaining_quantity, tp_level=0
                )
                if isinstance(exit_res, dict):
                    rp = exit_res.get('realized_pl')
                    if rp is not None:
                        try:
                            realized_pnl = float(rp)
                        except Exception:
                            realized_pnl = None
                if realized_pnl is None and pos.oanda_trade_id:
                    realized_pnl = ctx['oanda'].get_trade_realized_pl(pos.oanda_trade_id)
            close_pnl = trade_manager.close_position(
                pos.trade_id,
                current_price,
                reason_str,
                pnl_override=realized_pnl
            ) or 0.0
            ctx['daily_loss'] += max(0, -close_pnl)
            ctx['position_count'] = max(0, ctx['position_count'] - 1)
            pnl_src = 'OANDA' if realized_pnl is not None else 'MODEL'
            trade_log.info(
                f'CLOSE {pos.trade_id} | {symbol} {pos.direction} | '
                f'PnL: {close_pnl:+.4f} [{pnl_src}] | Reason: {reason_str}'
            )
            ctx['regime'].record_outcome(
                trade_id=pos.trade_id, symbol=symbol,
                pnl=close_pnl, hit_sl=is_stop_hit_exit,
                entry_time=str(pos.entry_time),
            )
            # D1: Track CT failure on SL hit for zone exhaustion
            if is_stop_hit_exit and hasattr(pos, 'is_counter_trend') and pos.is_counter_trend:
                _pip_sz_ct = 0.01 if 'JPY' in symbol else 0.0001
                _rounded_ct = round(pos.entry_price / (_pip_sz_ct * 5)) * (_pip_sz_ct * 5)
                _ct_dir = 'ENTER_LONG' if pos.direction == 'long' else 'ENTER_SHORT'
                _ct_key = f'{symbol}_{_rounded_ct:.5f}_{_ct_dir}'
                _ct_fails = ctx.setdefault('ct_failure_counts', {})
                _ct_fails[_ct_key] = _ct_fails.get(_ct_key, 0) + 1
                decision_log.info(f'{symbol}: CT_SL_HIT — {_ct_key} now has {_ct_fails[_ct_key]} CT failures today')
            quality_rec = ctx['quality'].close_trade(
                trade_id=pos.trade_id, close_price=current_price,
                close_reason=reason_str, hit_sl=is_stop_hit_exit,
            )
            if quality_rec:
                pressure = ctx['quality'].get_quality_pressure(symbol)
                log.info(
                    f'Quality pressure {symbol}: {pressure.pressure} | '
                    f'{pressure.detail} | n={pressure.n_trades}'
                )
            # Always patch entry-context outcome (audit fix 2026-04-09)
            ctx['entry_context'].patch_outcome(
                trade_id=pos.trade_id,
                pnl=close_pnl,
                hit_sl=is_stop_hit_exit,
                mfe_pips=quality_rec.mfe_pips if quality_rec else 0.0,
                mae_pips=quality_rec.mae_pips if quality_rec else 0.0,
            )
            # Cooldown on ALL exits — prevents re-entering the same failed zone.
            # Break-even stops get a shorter cooldown (15 min) since the zone may still be valid,
            # but need a pause to avoid machine-gunning the same losing entry.
            is_time_exit = 'Time exit' in (reason_str or '')
            is_stop_hit  = 'Stop hit' in (reason_str or '')
            is_be_stop   = stopped_at_be
            if is_time_exit or is_stop_hit:
                cooldown_mins = 15 if is_be_stop else CONFIG.get('time_exit_cooldown_minutes', 60)
                ctx.setdefault('time_exit_cooldowns', {})[symbol] = (
                    datetime.now() + timedelta(minutes=cooldown_mins)
                )
                exit_type = 'BE-stop' if is_be_stop else ('Time-exit' if is_time_exit else 'Stop-hit')
                decision_log.info(f'{symbol}: {exit_type} cooldown set — {cooldown_mins}min')
        elif action == 'move_stop':
            # SL ownership: only push to OANDA if ratchet owns the SL
            _tc = ctx.get('trade_coord')
            _coord_sl = _tc.get_trade(pos.oanda_trade_id) if _tc and pos.oanda_trade_id else None
            if _coord_sl and _coord_sl.get('sl_owner') == 'harvester':
                log.debug(f'{symbol} {pos.trade_id}: SL move skipped — harvester owns SL')
            else:
                router.move_stop(
                    pos.trade_id,
                    pos.oanda_trade_id,
                    mgmt.get('price', pos.current_stop),
                    mgmt.get('reason', 'manager')
                )
                if _tc and pos.oanda_trade_id:
                    _tc.update_trade(
                        pos.oanda_trade_id, current_sl=mgmt.get('price', pos.current_stop),
                        last_updated_by='ratchet',
                    )
            _manage_sl_limit(pos, symbol, current_price, bars_5m, oanda, stop_moved=True)
            continue
        elif action == 'green_exit':
            _handle_green_exit(pos, symbol, current_price, bars_5m, oanda, router, trade_manager, ctx)
        # partial_tp and set_tp removed — OANDA native TP (TP2) handles limit exit.
        # SL advancement (Stage 1/2/3) locks profit as price climbs; no market orders.

        # Hybrid SL limit management (skip if position just closed/green-exited)
        if action not in ('close', 'green_exit'):
            _manage_sl_limit(pos, symbol, current_price, bars_5m, oanda, stop_moved=False)

    # Check pending limit fills
    fill_events = router.check_and_manage_pending()
    for event in fill_events:
        if event['event'] == 'FILLED':
            trade_log.info(f'{symbol} FILLED {event}')

    # Gate: is box in tradeable state?
    tradeable, reason = box_machine.is_tradeable(symbol)
    if not tradeable:
        decision_log.info(f'{symbol}: Not tradeable — {reason}')
        # Add stub signal so dashboard shows the blocker reason
        from gates import SignalResult
        stub = SignalResult(
            symbol=symbol, timestamp=datetime.now().isoformat(),
            action='NO_TRADE',
            zone_gate=False, zone_tier=0, zone_details='',
            sucker_move_gate=False, sucker_move=None,
            atr_gate=False, atr_consumed_pct=0, atr_strength='',
            pitchfork_gate=False, pitchfork=None,
            otr_gate=False, otr_ratio=0, otr_category='',
            entry_price=0, stop_price=0, tp1_price=0, tp2_price=0,
            risk_points=0, reward_points=0, risk_reward_ratio=0,
            rejection_reasons=[f'Not tradeable: {reason}'],
            all_gates_passed=False, wait_for=reason,
        )
        ctx.get('_last_signals', []).append(stub)
        ctx.setdefault('_last_signal_scores', {})[symbol] = 0
        ctx['_blockers'][symbol] = {'type': 'not_tradeable', 'message': reason}
        return None

    # Gate: already in a position on this specific symbol?
    pending_reentry = ctx.get('pending_reentry', {}).get(symbol)
    if not pending_reentry:
        existing = [p for p in trade_manager.get_all_open() if p.symbol == symbol]

        # OANDA ground-truth check — catches stacking after restarts or state loss
        live_trades = ctx.get('live_oanda_trades') or []
        live_scrooge = [
            t for t in live_trades
            if t.get('instrument') == symbol
            and (t.get('clientExtensions', {}).get('id', '') or t.get('tradeClientExtensions', {}).get('id', '')).startswith('scrooge')
        ]
        if live_scrooge and not existing:
            # State gap — reconcile missing position back into trade_manager
            trade_manager.reconcile_from_oanda(live_scrooge)
            log.info(f'{symbol}: reconciled {len(live_scrooge)} live OANDA trade(s) missing from state')

        if existing or live_scrooge:
            return None  # Already in this trade — don't stack

    # Gate: time-exit cooldown — don't re-enter same symbol for 60min after time exit
    cooldown_until = ctx.get('time_exit_cooldowns', {}).get(symbol)
    if cooldown_until and datetime.now() < cooldown_until:
        msg = f'Cooldown until {cooldown_until.strftime("%H:%M UTC")}'
        decision_log.info(f'{symbol}: Time-exit cooldown active until {cooldown_until.strftime("%H:%M UTC")}')
        ctx.setdefault('_blockers', {})[symbol] = {'type': 'cooldown', 'message': msg}
        return None

    # Gate: too many concurrent positions?
    # Exception: a pending re-entry is allowed to bypass the cap (same setup, break-even stop)
    at_max_positions = not pending_reentry and ctx['position_count'] >= CONFIG['max_concurrent_positions']
    if at_max_positions:
        decision_log.info(f'{symbol}: Max positions ({ctx["position_count"]})')

    # EUR currency cap removed — trend filter now handles directional EUR exposure correctly.

    # Evaluate signal (all gates)
    signal = signal_engine.evaluate(symbol, zone_map, bars_5m, bars_1h, total_overhead, minutes_since_open)

    reasons = ' | '.join(signal.rejection_reasons) if signal.rejection_reasons else signal.wait_for
    decision_log.info(
        f'{symbol}: {signal.action} | zone={signal.zone_gate} ATR={signal.atr_gate} '
        f'fork={signal.pitchfork_gate} OTR={signal.otr_gate}'
        + (f' | {reasons}' if reasons else '')
    )

    # Collect for dashboard status
    ctx.get('_last_signals', []).append(signal)
    # Always store score — even partial gate passes earn points for the dashboard
    ctx.setdefault('_last_signal_scores', {})[symbol] = signal.score

    if not signal.all_gates_passed or signal.action not in ('ENTER_LONG', 'ENTER_SHORT'):
        _no_sig_regime = ctx['regime'].observe(
            symbol, zone_map, bars_5m, bars_1h,
            bot_action='NO_SIGNAL', signal_score=float(signal.score),
            quality_pressure=ctx['quality'].get_quality_pressure(symbol),
        )
        ctx.get('pending_reentry', {}).pop(symbol, None)
        # ── EXTREME_REVERSAL: BB snap-back entry when main engine finds nothing ──
        er_sig = ctx['extreme_reversal'].evaluate(
            symbol, zone_map, bars_5m, current_price, _no_sig_regime.score,
        )
        if er_sig is not None:
            decision_log.info(
                f'{symbol}: EXTREME_REVERSAL queued | '
                f'entry={er_sig.entry_price:.5f} sl={er_sig.stop_price:.5f} '
                f'rr={er_sig.risk_reward_ratio:.2f} regime={_no_sig_regime.score:+.0f}'
            )
            ctx.setdefault('_cycle_entry_candidates', []).append({
                'symbol':        symbol,
                'signal':        er_sig,
                'current_price': current_price,
                'half_box':      half_box,
                'live_spread':   live_spread,
                'zone_map':      zone_map,
                'bars_5m':       bars_5m,
                'bars_1h':       bars_1h,
                'regime_assessment': _no_sig_regime,
                'entry_type':    'EXTREME_REVERSAL',
            })
            return
        
        # Trend participation fallback (no-signal path)
        _bb_trend = ctx['regime']._last_bb.get(symbol)
        if _bb_trend and _bb_trend.state.value in ('TREND_UP', 'TREND_DOWN', 'STRETCH_UP', 'STRETCH_DOWN'):
            _trend_sig2 = ctx['trend_engine'].evaluate(
                symbol, bars_5m, zone_map, _bb_trend,
                live_spread=live_spread, half_box=half_box,
            )
            if _trend_sig2 is not None:
                _t_type2 = 'TREND_FULL' if _bb_trend.state.value.startswith('TREND') else 'TREND_STRETCH'
                decision_log.info(
                    f'{symbol}: {_t_type2} queued (no-signal) | '
                    f'{_trend_sig2.action} @ {_trend_sig2.entry_price:.5f}'
                )
                ctx.setdefault('_cycle_entry_candidates', []).append({
                    'symbol':        symbol,
                    'signal':        _trend_sig2,
                    'current_price': current_price,
                    'half_box':      half_box,
                    'live_spread':   live_spread,
                    'total_overhead': total_overhead,
                    'zone_map':      zone_map,
                    'bars_5m':       bars_5m,
                    'bars_1h':       bars_1h,
                    'regime_assessment': _no_sig_regime,
                    'is_counter_trend': False,
                    'entry_type':    _t_type2,
                })
                return
        return None

    regime_assessment = ctx['regime'].observe(
        symbol, zone_map, bars_5m, bars_1h,
        bot_action='SIGNAL_QUALIFIED', signal_score=float(signal.score),
        quality_pressure=ctx['quality'].get_quality_pressure(symbol),
    )

    # Regime gate (rebalanced 2026-04-13): only BLOCKED suppresses entry.
    # Score floor and hysteresis removed — regime state IS the gate now.
    _regime_scores_dq = ctx.setdefault('_last_regime_scores', {}).setdefault(symbol, __import__('collections').deque(maxlen=2))
    _regime_scores_dq.append(regime_assessment.score)
    from regime import RegimeState as _RS
    if regime_assessment.state == _RS.BLOCKED:
        decision_log.info(f'{symbol}: REGIME_BLOCKED score={regime_assessment.score:+.1f} — entry suppressed')
        signal.action   = 'REGIME_BLOCKED'
        signal.wait_for = f'REGIME_BLOCKED ({regime_assessment.score:+.0f}) — {regime_assessment.summary()}'
        ctx.setdefault('_blockers', {})[symbol] = {'type': 'regime_blocked', 'message': f'Regime BLOCKED ({regime_assessment.score:+.0f}) — {regime_assessment.summary()}'}
        return None

    # FIX 2 (2026-04-14): Entry floor — require regime score >= -1
    if regime_assessment.score < -1:
        decision_log.info(
            f'{symbol}: REGIME_FLOOR score={regime_assessment.score:+.1f} — '
            f'entry suppressed (need >= -1)'
        )
        signal.action   = 'REGIME_FLOOR'
        signal.wait_for = f'REGIME_FLOOR ({regime_assessment.score:+.0f}) — need score >= -1'
        ctx.setdefault('_blockers', {})[symbol] = {
            'type': 'regime_floor',
            'message': f'REGIME_FLOOR ({regime_assessment.score:+.0f}) — need >= -1'
        }
        return None

    # FIX 3 (2026-04-14): BB exhaustion gate — reversals require EXTREME or REJECTION
    # This is the core filter: don't fade trends, only fade exhaustion.
    _bb_ctx = ctx['regime']._last_bb.get(symbol)
    if _bb_ctx:
        _bb_state = _bb_ctx.state.value
        _is_reversal = signal.action in ('ENTER_LONG', 'ENTER_SHORT')
        _exhaustion_states = ('EXTREME_UP', 'EXTREME_DOWN', 'REJECTION_SHORT', 'REJECTION_LONG')
        if _is_reversal and _bb_state not in _exhaustion_states:
            decision_log.info(
                f'{symbol}: BB_NO_EXHAUSTION — bb_state={_bb_state}, '
                f'reversal blocked (need EXTREME or REJECTION)'
            )
            signal.action   = 'BB_NO_EXHAUSTION'
            signal.wait_for = f'BB state {_bb_state} — no exhaustion, reversal blocked'
            ctx.setdefault('_blockers', {})[symbol] = {
                'type': 'bb_no_exhaustion',
                'message': f'BB {_bb_state} — need EXTREME/REJECTION for reversal'
            }
            
            # Trend participation fallback
            if _bb_state in ('TREND_UP', 'TREND_DOWN', 'STRETCH_UP', 'STRETCH_DOWN'):
                _trend_sig = ctx['trend_engine'].evaluate(
                    symbol, bars_5m, zone_map, _bb_ctx,
                    live_spread=live_spread, half_box=half_box,
                )
                if _trend_sig is not None:
                    _t_type = 'TREND_FULL' if _bb_state.startswith('TREND') else 'TREND_STRETCH'
                    decision_log.info(
                        f'{symbol}: {_t_type} queued | '
                        f'{_trend_sig.action} @ {_trend_sig.entry_price:.5f} '
                        f'SL={_trend_sig.stop_price:.5f} RR={_trend_sig.risk_reward_ratio:.2f}'
                    )
                    ctx.setdefault('_cycle_entry_candidates', []).append({
                        'symbol':        symbol,
                        'signal':        _trend_sig,
                        'current_price': current_price,
                        'half_box':      half_box,
                        'live_spread':   live_spread,
                        'total_overhead': total_overhead,
                        'zone_map':      zone_map,
                        'bars_5m':       bars_5m,
                        'bars_1h':       bars_1h,
                        'regime_assessment': regime_assessment,
                        'is_counter_trend': False,
                        'entry_type':    _t_type,
                    })
                    return None
            return None

    # ── Regime scoring adjustment (rebalanced 2026-04-13) ──
    # NORMAL: +10 pts, CAUTION: -5 pts, RESTRICTED: -10 pts
    from regime import RegimeState as _RS_score
    if regime_assessment.state == _RS_score.NORMAL:
        signal.score += 10
    elif regime_assessment.state == _RS_score.CAUTION:
        signal.score -= 5
    elif regime_assessment.state == _RS_score.RESTRICTED:
        signal.score -= 10
    # Ensure score doesn't go below 1 (0 means validity-rejected)
    if signal.score < 1:
        signal.score = 1

    # Zone debounce — prevent same zone from re-firing within 20 min (4 candles)
    # Round entry price to nearest 5 pips so micro-drift doesn't generate phantom new zone keys
    _pip_size = 0.01 if 'JPY' in symbol else 0.0001
    _rounded  = round(signal.entry_price / (_pip_size * 5)) * (_pip_size * 5)
    zone_key  = f"{symbol}_{_rounded:.5f}_{signal.action}"
    zone_cache = ctx.setdefault('zone_cache', {})
    last_fired = zone_cache.get(zone_key)
    if last_fired and (datetime.now() - last_fired).total_seconds() < 1200:
        decision_log.info(f'{symbol}: Zone {signal.entry_price:.5f} recently triggered — skipping')
        _remain = int((1200 - (datetime.now() - last_fired).total_seconds()) / 60)
        ctx.setdefault('_blockers', {})[symbol] = {'type': 'zone_debounce', 'message': f'Zone debounce — {_remain}m remaining'}
        return None
    # zone_cache is updated in _fire_entry_candidate (only on actual fire, not on ranking queue)

    # D3 fix: Daily zone entry cap — max 3 fresh entries per zone+direction per day
    _daily_zone_key = f"{symbol}_{_rounded:.5f}_{signal.action}"
    _zone_counts = ctx.setdefault('zone_entry_counts', {})
    if _zone_counts.get(_daily_zone_key, 0) >= 3:
        decision_log.info(f'{symbol}: ZONE_DAILY_CAP — {_daily_zone_key} already entered 3 times today')
        ctx.setdefault('_blockers', {})[symbol] = {'type': 'zone_daily_cap', 'message': f'Zone daily cap reached (3 entries)'}
        return None

    # D1 fix: CT failure memory — block CT entries at zones with 2+ SL hits today
    if signal.is_counter_trend:
        _ct_key = f"{symbol}_{_rounded:.5f}_{signal.action}"
        _ct_fails = ctx.setdefault('ct_failure_counts', {})
        if _ct_fails.get(_ct_key, 0) >= 2:
            decision_log.info(f'{symbol}: CT_ZONE_EXHAUSTED — {_ct_key} has {_ct_fails[_ct_key]} CT SL hits today')
            ctx.setdefault('_blockers', {})[symbol] = {'type': 'ct_exhausted', 'message': f'CT zone exhausted ({_ct_fails[_ct_key]} SL hits today)'}
            return None

    # Open wait gate — block new entries for first 15 min after session start
    # Exception: Tier 1 zone + 80%+ ATR + named signal → allow at 50% size
    open_wait_override = False
    if minutes_since_open < CONFIG['open_wait_minutes']:
        is_perfect_setup = (
            signal.zone_tier == 1 and
            signal.atr_strength == 'strong' and
            signal.pitchfork and
            signal.pitchfork.signal_type in ('john_wick', 'power_of_towers')
        )
        if not is_perfect_setup:
            decision_log.info(f'{symbol}: Open wait ({minutes_since_open}min) — setup quality too low')
            ctx.get('pending_reentry', {}).pop(symbol, None)
            return None
        open_wait_override = True
        decision_log.info(f'{symbol}: Open wait OVERRIDE — Tier 1 + 80%+ ATR + {signal.pitchfork.signal_type}, 50% size')

    # Validate pending re-entry if present
    if pending_reentry:
        reentry = trade_manager.assess_reentry(symbol, pending_reentry, zone_map, bars_5m)
        ctx['pending_reentry'].pop(symbol, None)
        if not reentry.allowed:
            decision_log.info(f'{symbol}: Re-entry rejected — {reentry.reason}')
            return None
        decision_log.info(f'{symbol}: Re-entry approved — {reentry.reason}')

    # RANKING: all gates passed -- queue as candidate, fire best-scored after full scan
    ctx.setdefault('_cycle_entry_candidates', []).append({
        'symbol':            symbol,
        'signal':            signal,
        'current_price':     current_price,
        'half_box':          half_box,
        'live_spread':       live_spread,
        'total_overhead':    total_overhead,
        'pending_reentry':   pending_reentry,
        'open_wait_override': open_wait_override,
        'zone_map':          zone_map,
        'bars_5m':           bars_5m,
        'bars_1h':           bars_1h,
        'bars_d':            bars_d,
        'regime_assessment': regime_assessment,
        'is_counter_trend': signal.is_counter_trend,
    })
    return None


def _fire_entry_candidate(cand: dict, ctx: dict):
    """Execute entry for the best-scored candidate after all symbols are scanned."""
    symbol           = cand['symbol']
    signal           = cand['signal']
    current_price    = cand['current_price']
    half_box         = cand['half_box']
    live_spread      = cand['live_spread']
    total_overhead   = cand['total_overhead']
    pending_reentry  = cand['pending_reentry']
    open_wait_override = cand['open_wait_override']
    zone_map         = cand['zone_map']
    bars_5m          = cand.get('bars_5m')
    bars_1h          = cand.get('bars_1h')
    bars_d           = cand.get('bars_d')

    # Zone cache stamp moved to after successful entry (was here, caused stale-signal loop)

    oanda         = ctx['oanda']
    router        = ctx['router']
    fee_calc      = ctx['fee_calc']
    trade_manager = ctx['trade_manager']
    box_machine   = ctx['box_machine']

    # Size the position — margin-utilization: deploy ~50% of equity as margin.
    # Harvester handles profit extraction via staggered partials (8/18/30 pip tiers).
    equity      = oanda.get_equity() or ctx['equity_start']
    inst_meta   = oanda._instrument_cache.get(symbol, {})
    pip_size    = 10 ** int(inst_meta.get('pipLocation', -4))
    pip_value   = float(oanda.get_pip_value_per_unit(symbol, current_price))
    margin_rate = float(inst_meta.get('marginRate', 0.0333))

    # Validate signal still computable
    risk_points = signal.risk_points
    if risk_points <= 0:
        return None

    # Base units from margin target
    # base_price_usd: USD value of 1 unit of base currency (correct for JPY crosses too)
    base_price_usd = pip_value * current_price / pip_size if pip_size > 0 else current_price
    target_margin  = equity * CONFIG['margin_utilization_pct']
    base_units     = target_margin / (base_price_usd * margin_rate) if base_price_usd > 0 else 0

    units = round(base_units)

    # Regime RESTRICTED — halve position size (trade allowed, but at reduced risk)
    from regime import RegimeState as _RS2
    _regime_cand = cand.get('regime_assessment')
    if _regime_cand and _regime_cand.state == _RS2.RESTRICTED:
        units = round(units * 0.5)
        decision_log.info(
            f'{symbol}: REGIME_RESTRICTED score={_regime_cand.score:+.1f} — '
            f'position size halved to {units} units'
        )

    # Trend entry size reduction
    _entry_type = cand.get('entry_type', '')
    if _entry_type == 'TREND_FULL':
        _tf = CONFIG.get('trend_full_size_fraction', 0.50)
        units = int(units * _tf)
        decision_log.info(f'{symbol}: TREND_FULL size — {_tf:.0%} ({units} units)')
    elif _entry_type == 'TREND_STRETCH':
        _tf = CONFIG.get('trend_stretch_size_fraction', 0.30)
        units = int(units * _tf)
        decision_log.info(f'{symbol}: TREND_STRETCH size — {_tf:.0%} ({units} units)')

    # Logging
    risk_pips  = risk_points / pip_size
    tp1_pips   = abs(signal.tp1_price - current_price) / pip_size
    pip_dollar = units * pip_value  # $ per pip on full position
    decision_log.info(
        f'{symbol}: Margin-based sizing — equity ${equity:.2f} | '
        f'margin target ${target_margin:.2f} ({CONFIG["margin_utilization_pct"]*100:.0f}%) | '
        f'margin_rate {margin_rate:.2%} | units {units:.0f} | '
        f'${pip_dollar:.2f}/pip | SL {risk_pips:.1f}p | TP1 {tp1_pips:.1f}p'
    )

    if units < 1:
        log.warning(f'{symbol}: Position too small ({units:.0f} units)')
        return None

    # Cap units to free margin — prevent INSUFFICIENT_MARGIN rejections
    # Leave a 20% buffer so existing trades aren't squeezed on spread moves
    margin_rate  = float(inst_meta.get('marginRate', 0.02))
    free_margin  = oanda.get_free_margin()
    if free_margin is not None:
        usable_margin    = free_margin * 0.80
        max_units_margin = round(usable_margin / (base_price_usd * margin_rate)) if base_price_usd > 0 else 0
        if max_units_margin < 1:
            log.warning(f'{symbol}: Insufficient free margin (${free_margin:.2f}) — skipping')
            return None
        if units > max_units_margin:
            log.info(f'{symbol}: Units capped by free margin: {units:.0f} → {max_units_margin:.0f} (free margin: ${free_margin:.2f})')
            # Skip if margin cap cuts position to less than 50% of risk-sized quantity —
            # the spread cost would dwarf any realistic profit on such a tiny position.
            if max_units_margin < units * 0.50:
                log.warning(
                    f'{symbol}: Skipping — margin cap too severe: '
                    f'{max_units_margin:.0f} units = {max_units_margin/units*100:.0f}% of intended {units:.0f} '
                    f'(free margin: ${free_margin:.2f})'
                )
                return None
            units = max_units_margin

    # Staleness check: current price must still be inside the valid entry range
    # (not already at or beyond the stop level)
    if signal.action == 'ENTER_SHORT' and current_price >= signal.stop_price:
        decision_log.info(
            f'{symbol}: Signal stale — price {current_price:.5f} at/above short stop {signal.stop_price:.5f}'
        )
        ctx.setdefault('_blockers', {})[symbol] = {'type': 'signal_stale', 'message': f'Stale — price {current_price:.5f} above stop {signal.stop_price:.5f}'}
        return None
    if signal.action == 'ENTER_LONG' and current_price <= signal.stop_price:
        decision_log.info(
            f'{symbol}: Signal stale — price {current_price:.5f} at/below long stop {signal.stop_price:.5f}'
        )
        ctx.setdefault('_blockers', {})[symbol] = {'type': 'signal_stale', 'message': f'Stale — price {current_price:.5f} below stop {signal.stop_price:.5f}'}
        return None

    direction  = 'BUY' if signal.action == 'ENTER_LONG' else 'SELL'
    trade_id   = f'scrooge_{symbol.lower().replace("_","")}{uuid.uuid4().hex[:8]}'
    signal_type = signal.pitchfork.signal_type if signal.pitchfork else 'unknown'

    result = router.enter_position(
        symbol=symbol,
        side=direction,
        quantity=units,
        limit_price=signal.entry_price,
        take_profit=signal.tp2_price,   # Native OANDA TP at opposite wall; trade manager handles TP1 partial
        stop_loss=signal.stop_price,
        trade_id=trade_id,
        live_spread=live_spread,
        half_box=half_box
    )

    if not result.get('success'):
        # On INSUFFICIENT_MARGIN or other rejections, set a cooldown to prevent
        # hammering OANDA with the same failing order every cycle
        error = result.get('error', '')
        if 'INSUFFICIENT_MARGIN' in str(error):
            cooldown_mins = CONFIG.get('time_exit_cooldown_minutes', 60)
            ctx.setdefault('time_exit_cooldowns', {})[symbol] = (
                datetime.now() + timedelta(minutes=cooldown_mins)
            )
            margin_str = f'${free_margin:.2f}' if free_margin else 'unknown'
            decision_log.info(
                f'{symbol}: INSUFFICIENT_MARGIN cooldown set — {cooldown_mins}min '
                f'(units: {units:.0f}, free margin: {margin_str})'
            )
        return None

    if result.get('success'):
        ctx['position_count'] += 1
        # Mark zone as recently triggered (only on successful fill, not on stale/rejected)
        _pip_size_zc = 0.01 if 'JPY' in symbol else 0.0001
        _rounded_zc  = round(signal.entry_price / (_pip_size_zc * 5)) * (_pip_size_zc * 5)
        _zone_key    = f'{symbol}_{_rounded_zc:.5f}_{signal.action}'
        ctx.setdefault('zone_cache', {})[_zone_key] = datetime.now()
        # D3: increment daily zone entry count
        _daily_zk = f'{symbol}_{_rounded_zc:.5f}_{signal.action}'
        _zc = ctx.setdefault('zone_entry_counts', {})
        _zc[_daily_zk] = _zc.get(_daily_zk, 0) + 1
        # Use actual fill price if available; fall back to signal entry price
        actual_fill = result.get('fill_price') or signal.entry_price
        if actual_fill <= 0:
            actual_fill = signal.entry_price

        # Use zone-based SL/TP from signal engine (not fixed pips)
        signal.entry_price = actual_fill
        _oanda_tid = result.get('oanda_trade_id', '')
        if _oanda_tid:
            # SL/TP already set via stopLossOnFill/takeProfitOnFill at entry — no need to re-modify
            # oanda.modify_trade_stop(_oanda_tid, signal.stop_price)
            # oanda.modify_trade_tp(_oanda_tid, signal.tp2_price)
            decision_log.info(
                f'{symbol}: Zone-anchored -- fill {actual_fill:.5f} | '
                f'SL {signal.stop_price:.5f} | '
                f'TP1 {signal.tp1_price:.5f} | '
                f'TP2 {signal.tp2_price:.5f}'
            )

        # Convert overhead from quote currency to USD (audit fix 2026-04-09)
        _pip_sz = 0.01 if 'JPY' in symbol else 0.0001
        overhead_cost_usd = total_overhead * units * (pip_value / _pip_sz) if _pip_sz > 0 else total_overhead * units
        trade_manager.register_entry(
            trade_id=trade_id,
            symbol=symbol,
            exchange='OANDA',
            signal=signal,
            quantity=units,
            actual_fill_price=actual_fill,
            overhead_cost=overhead_cost_usd,
            pip_value_per_unit=pip_value,
            atr=zone_map.daily_box.ATR,
            oanda_trade_id=result.get('oanda_trade_id', '')
        )
        # D1: Store CT flag on position for failure tracking at close
        _pos = trade_manager.positions.get(symbol)
        if _pos:
            _pos.is_counter_trend = cand.get('is_counter_trend', False)
        # Register in coordination file for harvester/SL-limit sync
        if _oanda_tid:
            ctx['trade_coord'].register_trade(
                oanda_trade_id=_oanda_tid, client_id=trade_id,
                sl=signal.stop_price, tp1=signal.tp1_price, actor='bot',
            )
        ctx['quality'].open_trade(
            trade_id=trade_id, symbol=symbol,
            direction=direction.lower().replace('buy', 'long').replace('sell', 'short'),
            entry_price=actual_fill,
            entry_time=datetime.now(timezone.utc).isoformat(),
            stop_loss=signal.stop_price, tp1_price=signal.tp1_price, tp2_price=signal.tp2_price,
            signal_score=float(signal.score),
        )
        ctx['entry_context'].record(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction.lower().replace('buy', 'long').replace('sell', 'short'),
            entry_price=actual_fill,
            signal=signal,
            zone_map=zone_map,
            bars_5m=bars_5m,
            bars_1h=bars_1h,
            bars_d=bars_d,
            oanda_client=oanda,
            regime_assessment=ctx['regime']._last.get(symbol),
        )
        ctx['regime'].observe(
            symbol, zone_map, bars_5m, bars_1h,
            bot_action='ENTERED_' + direction.upper(),
            trade_id=trade_id,
            signal_score=float(signal.score),
            quality_pressure=ctx['quality'].get_quality_pressure(symbol),
        )
        # Re-entry counter — must be incremented here (on actual fill) not on approval
        if pending_reentry:
            trade_manager.record_reentry(symbol)
        trade_log.info(
            f'ENTRY {trade_id} | {symbol} {direction} {units:.0f} units '
            f'@ {actual_fill:.5f} | SL: {signal.stop_price:.5f} | '
            f'TP1: {signal.tp1_price:.5f} | Signal: {signal_type}'
        )
        return {
            'type': 'ENTRY',
            'trade_id': trade_id,
            'symbol': symbol,
            'direction': direction
        }

    return None

# ============================================================
# SCREEN RESULT SERIALIZER
# ============================================================

def _serialize_screen(screen_results) -> list:
    """Serialize ScreenResult objects for dashboard consumption."""
    out = []
    for r in screen_results:
        out.append({
            'instrument': r.instrument,
            'viable': r.viable,
            'pdh': round(r.pdh, 5),
            'pdl': round(r.pdl, 5),
            'box_range_pips': round(r.box_range_pips, 1),
            'half_box_pips': round(r.half_box_pips, 1),
            'daily_atr_pips': round(r.daily_atr_pips, 1),
            'atr_consumed_pct': round(r.atr_consumed_pct, 3),
            'spread_pips': round(r.current_spread_pips, 1),
            'otr_ratio': round(r.otr_ratio, 3),
            'prior_day_ticks': r.prior_day_ticks,
            'rejection_reason': r.rejection_reason,
        })
    return out


# ============================================================
# UNIVERSE RESCAN
# ============================================================

def rescan_universe(ctx: dict):
    """Re-screens forex pairs and updates active instrument list.
    Session-aware: only scans pairs whose session is currently open,
    matching _update_sessions() behaviour. Audit gates (min box, min ticks,
    OTR) handle cost filtering — this just controls which pairs get scanned.
    """
    utc_hour = datetime.now(timezone.utc).hour
    session_pairs = get_session_pairs(utc_hour, mode=CONFIG.get('universe_mode', 'balanced'))
    open_sessions = {s['name'] for s in get_active_sessions(utc_hour)}
    log.info(f'Universe rescan — sessions: {", ".join(sorted(open_sessions)) if open_sessions else "none"} | '
             f'{len(session_pairs)} pairs')
    screen_results = ctx['screener'].screen_all(pairs=session_pairs if open_sessions else [])
    new_instruments = [r.instrument for r in screen_results if r.viable]

    # Add new instruments
    for symbol in new_instruments:
        if symbol not in ctx['active_instruments']:
            daily_bars  = ctx['oanda'].get_daily_bars(symbol)
            hourly_bars = ctx['oanda'].get_hourly_bars(symbol, count=50)
            if daily_bars is not None and len(daily_bars) >= 3:
                prices     = ctx['oanda'].get_prices([symbol])
                price_data = prices.get(symbol, {})
                current_price = (
                    (price_data.get('bid', 0) + price_data.get('ask', 0)) / 2
                    or float(daily_bars.iloc[-1]['close'])
                )
                spread   = (price_data.get('ask', 0) - price_data.get('bid', 0)) if price_data else 0.0002
                zone_map = ctx['zone_detector'].build_zone_map(
                    symbol, 'forex', daily_bars,
                    hourly_bars if hourly_bars is not None else daily_bars,
                    current_price
                )
                if zone_map:
                    ctx['box_machine'].initialize_instrument(symbol, zone_map.daily_box, spread)
                    log.info(f'Added to universe: {symbol}')

    ctx['active_instruments'] = new_instruments
    ctx['_last_screen'] = _serialize_screen(screen_results)
    ctx['last_rescan'] = datetime.now(timezone.utc)
    log.info(f'Universe updated: {", ".join(new_instruments)}')

# ============================================================
# FAST EXECUTION LOOP
# ============================================================

def _fast_loop_body(ctx: dict):
    """[FAST_LOOP] 3-second cycle: SL limit placement/fill monitoring for all open positions."""
    trade_manager = ctx.get('trade_manager')
    oanda         = ctx.get('oanda')
    if trade_manager is None or oanda is None:
        return
    open_positions = list(trade_manager.get_all_open())
    if not open_positions:
        return
    symbols = list({p.symbol for p in open_positions})
    try:
        all_prices = oanda.get_prices(symbols)
    except Exception:
        return
    with _exec_lock:
        for pos in open_positions:
            # Skip if position was closed by slow loop while we waited for the lock
            if pos.trade_id not in {p.trade_id for p in trade_manager.get_all_open()}:
                continue
            px = all_prices.get(pos.symbol, {})
            if not px:
                continue
            current_price = (px.get('bid', 0) + px.get('ask', 0)) / 2
            bars_5m = _cached_bars.get(pos.symbol)
            # Skip dead positions (harvester may have closed them)
            globals()['_fast_loop_ctx'] = ctx
            _tc = ctx.get('trade_coord')
            if _tc and pos.oanda_trade_id and not _tc.is_trade_alive(pos.oanda_trade_id):
                continue
            _manage_sl_limit(pos, pos.symbol, current_price, bars_5m, oanda, stop_moved=False)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    global _running

    ctx = boot()
    last_daily_reset = datetime.now(timezone.utc).date()

    # Start fast execution thread (SL limits + fill monitoring at 3s interval)
    def _exec_thread():
        while _running:
            try:
                _fast_loop_body(ctx)
            except Exception as exc:
                log.error(f'[FAST_LOOP] error: {exc}', exc_info=True)
            time.sleep(3)
    threading.Thread(target=_exec_thread, daemon=True, name='exec-fast-loop').start()
    log.info('[FAST_LOOP] Execution thread started (3s interval)')

    log.info('Main loop starting — waiting for 5-minute candle closes')

    _in_weekend = False

    while _running:
        loop_start = time.time()
        now = datetime.now(timezone.utc)

        # Daily reset check
        if now.date() > last_daily_reset:
            daily_reset(ctx)
            last_daily_reset = now.date()

        # Weekend idle — OANDA closes Sat 00:00 – Sun ~22:00 UTC
        if is_weekend_utc():
            if not _in_weekend:
                log.info('── WEEKEND — OANDA closed. Idling until ~Sun 22:00 UTC ──')
                _in_weekend = True
            _sleep(CONFIG['scan_interval_seconds'])
            continue
        if _in_weekend:
            log.info('── MARKET OPEN — resuming normal scan ──')
            _in_weekend = False

        # Session management — update active pairs when sessions open/close
        _update_sessions(ctx, now)

        # Universe rescan
        elapsed_rescan = (now - ctx['last_rescan']).total_seconds() / 60
        if elapsed_rescan >= CONFIG['rescan_interval_minutes']:
            rescan_universe(ctx)

        # Daily loss limit check
        equity = ctx['oanda'].get_equity() or ctx['equity_start']
        daily_loss_pct = ctx['daily_loss'] / ctx['equity_start'] if ctx['equity_start'] > 0 else 0

        if daily_loss_pct >= CONFIG['max_daily_loss_pct']:
            log.warning(
                f'DAILY LOSS LIMIT HIT: {daily_loss_pct:.1%} >= {CONFIG["max_daily_loss_pct"]:.1%} — '
                f'halting new entries for the session'
            )
            _send_scrooge_alert(
                f'⚠️ Mr. Scrooge daily loss limit hit: {daily_loss_pct:.1%}. No new entries until reset.'
            )
            # Don't exit — still manage open positions until daily reset
            _sleep(CONFIG['scan_interval_seconds'])
            continue


        # Process each active instrument
        cycle_start = datetime.now(timezone.utc)
        log.info(f'── Cycle {now.strftime("%H:%M")} ── Equity: {equity:.2f} | '
                 f'Positions: {ctx["position_count"]} | '
                 f'Instruments: {len(ctx["active_instruments"])}')

        ctx['_blockers'] = {}  # Reset entry blockers for dashboard

        # Fetch OANDA live trades once per cycle — used as ground truth for entry guard
        ctx['live_oanda_trades'] = ctx['oanda'].get_open_trades()

        # Detect positions closed by OANDA native SL between cycles.
        # When OANDA's stop-loss fires server-side, the trade vanishes from live trades
        # but remains in trade_manager state. Without this check, cooldown never fires
        # and the bot immediately re-enters the same losing zone.
        # NOTE: get_open_trades() returns None on API error — skip vanished detection
        # entirely in that case to avoid false-positive native-close fires (503 outage bug).
        # NOTE: also index by OANDA trade ID as a secondary match — client extensions are
        # not always present in the response (propagation delay after fill), causing false
        # vanished detection even when the trade is still genuinely open.
        live_oanda_ids = set()       # client extension IDs
        live_oanda_trade_ids = set() # OANDA internal trade IDs
        if ctx['live_oanda_trades'] is not None:
            for t in ctx['live_oanda_trades']:
                cid = (t.get('clientExtensions', {}).get('id', '')
                       or t.get('tradeClientExtensions', {}).get('id', ''))
                if cid:
                    live_oanda_ids.add(cid)
                tid_raw = t.get('id', '')
                if tid_raw:
                    live_oanda_trade_ids.add(str(tid_raw))

        vanished = []
        if ctx['live_oanda_trades'] is not None:
            for tid, pos in list(ctx['trade_manager'].open_positions.items()):
                if tid not in live_oanda_ids and pos.oanda_trade_id:
                    if str(pos.oanda_trade_id) not in live_oanda_trade_ids:
                        vanished.append(pos)

        for pos in vanished:
            realized_pl = None
            if pos.oanda_trade_id:
                realized_pl = ctx['oanda'].get_trade_realized_pl(pos.oanda_trade_id)

            log.warning(
                f'OANDA NATIVE CLOSE detected: {pos.trade_id} | {pos.symbol} '
                f'{pos.direction} | OANDA closed between cycles (native SL/TP)'
            )
            if pos.oanda_trade_id:
                ctx['trade_coord'].mark_dead(pos.oanda_trade_id, actor='bot')
            # Exit price is not available in vanished-trade reconciliation path.
            # Use entry price to avoid bogus extreme PnL from a zero price placeholder.
            ctx['trade_manager'].close_position(
                pos.trade_id,
                pos.entry_price,
                'OANDA native close (SL/TP/manual)',
                pnl_override=realized_pl
            )
            if realized_pl is not None:
                ctx['daily_loss'] += max(0, -float(realized_pl))
            if realized_pl is not None:
                trade_log.info(
                    f'{pos.symbol} CLOSE {pos.trade_id}: OANDA native close '
                    f'| RealizedPL: {realized_pl:+.4f}'
                )
            else:
                trade_log.info(f'{pos.symbol} CLOSE {pos.trade_id}: OANDA native close')
            ctx['position_count'] = max(0, ctx['position_count'] - 1)
            # Cooldown after ALL native closes — prevents immediate re-entry.
            # TP hit (profitable) gets 15 min: zone may still be valid but needs a breather.
            # Loss/unknown gets full cooldown: zone is likely broken.
            # Audit finding: EUR/USD TP exit 03:51 -> re-entry 03:53 (-$5.83). Root cause:
            # profitable native close was popping the cooldown entirely.
            if realized_pl is not None and realized_pl > 0:
                tp_cooldown = 15  # Short cooldown — TP hit, zone may still be valid later
                ctx.setdefault('time_exit_cooldowns', {})[pos.symbol] = (
                    datetime.now() + timedelta(minutes=tp_cooldown)
                )
                decision_log.info(
                    f'{pos.symbol}: OANDA-native-close (TP hit) cooldown — {tp_cooldown}min '
                    f'({realized_pl:+.4f})'
                )
            else:
                cooldown_mins = CONFIG.get('time_exit_cooldown_minutes', 60)
                ctx.setdefault('time_exit_cooldowns', {})[pos.symbol] = (
                    datetime.now() + timedelta(minutes=cooldown_mins)
                )
                if realized_pl is None:
                    decision_log.info(
                        f'{pos.symbol}: OANDA-native-close cooldown set — {cooldown_mins}min '
                        f'(PnL unknown; conservative hold)'
                    )
                else:
                    decision_log.info(
                        f'{pos.symbol}: OANDA-native-close cooldown set — {cooldown_mins}min '
                        f'(net {realized_pl:+.4f})'
                    )

        # Log any non-Scrooge positions on the account — visibility into ghosts or manual trades
        unknown = [
            t for t in (ctx['live_oanda_trades'] or [])
            if not (t.get('clientExtensions', {}).get('id', '') or '').startswith('scrooge')
        ]
        if unknown:
            for t in unknown:
                cid  = t.get('clientExtensions', {}).get('id', 'no-id')
                inst = t.get('instrument', '?')
                upl  = t.get('unrealizedPL', '?')
                log.warning(f'UNTRACKED POSITION on OANDA: {inst} {t.get("currentUnits")} units | uPL={upl} | id={cid}')

        # Base pairs first — ensures they get capital priority over extended pairs
        seen_symbols = set()
        ordered = (
            [s for s in ctx['active_instruments'] if s in SCROOGE_BASE_PAIRS] +
            [s for s in ctx['active_instruments'] if s not in SCROOGE_BASE_PAIRS]
        )
        # CRITICAL: also process any symbol with an open position that fell out of the
        # active universe — position management MUST run for ALL tracked trades, not
        # just the current session's universe (otherwise green/time exits never fire).
        tracked_symbols = {pos.symbol for pos in ctx['trade_manager'].get_all_open()}
        for _orphan in tracked_symbols:
            if _orphan not in ordered:
                ordered.append(_orphan)
                log.info(f'Management-only scan added: {_orphan} (open position outside universe)')
        for symbol in ordered:
            if not _running:
                break
            if symbol in seen_symbols:
                log.debug(f'{symbol}: duplicate in active_instruments — skipping')
                continue
            seen_symbols.add(symbol)
            try:
                process_instrument(symbol, ctx)
            except Exception as e:
                log.error(f'{symbol}: Unhandled error — {e}', exc_info=True)

        # ── Ranking: fire best-scored entry candidate ──
        # Phase 3A safety gates: score threshold, spread hard-block, stale trigger, freshness
        MIN_ENTRY_SCORE = 20
        SPREAD_HARD_BLOCK = 3.0
        STALE_TRIGGER_ATR_FRAC = 0.3
        _raw_candidates = ctx.pop('_cycle_entry_candidates', [])
        _candidates = []
        for _c in _raw_candidates:
            _sig = _c['signal']
            _sym = _c['symbol']
            _cp  = _c['current_price']
            _spread = _c.get('live_spread', 0)
            _hbox = _c.get('half_box', 0)
            # Score gate
            if _sig.score < MIN_ENTRY_SCORE:
                decision_log.info(f'{_sym}: SCORE_BLOCKED score={_sig.score} < {MIN_ENTRY_SCORE}')
                ctx.setdefault('_blockers', {})[_sym] = {'type': 'score_blocked', 'message': f'Score {_sig.score} < {MIN_ENTRY_SCORE}'}
                continue
            # Spread hard-block (spread_ratio = live_spread / half_box as proxy)
            _spread_ratio = (_spread / _hbox) if _hbox > 0 else 0
            if _spread_ratio > SPREAD_HARD_BLOCK:
                decision_log.info(f'{_sym}: SPREAD_BLOCKED ratio={_spread_ratio:.1f} > {SPREAD_HARD_BLOCK}')
                ctx.setdefault('_blockers', {})[_sym] = {'type': 'spread_blocked', 'message': f'Spread ratio {_spread_ratio:.1f} > {SPREAD_HARD_BLOCK}'}
                continue
            # Trigger freshness — price already past the entry level
            if _sig.action == 'ENTER_LONG' and _cp >= _sig.entry_price:
                decision_log.info(f'{_sym}: TRIGGER_STALE price {_cp:.5f} already past long trigger {_sig.entry_price:.5f}')
                ctx.setdefault('_blockers', {})[_sym] = {'type': 'trigger_stale', 'message': f'Price already past trigger'}
                continue
            if _sig.action == 'ENTER_SHORT' and _cp <= _sig.entry_price:
                decision_log.info(f'{_sym}: TRIGGER_STALE price {_cp:.5f} already past short trigger {_sig.entry_price:.5f}')
                ctx.setdefault('_blockers', {})[_sym] = {'type': 'trigger_stale', 'message': f'Price already past trigger'}
                continue
            # Stale trigger distance — too far from current price
            _pip = 0.01 if 'JPY' in _sym else 0.0001
            _bars = _c.get('bars_5m')
            if _bars is not None and len(_bars) >= 14:
                _atr_5m = _bars['high'].iloc[-14:].sub(_bars['low'].iloc[-14:]).mean()
                _trig_dist = abs(_cp - _sig.entry_price)
                if _trig_dist > STALE_TRIGGER_ATR_FRAC * _atr_5m:
                    decision_log.info(f'{_sym}: TRIGGER_FAR dist={_trig_dist/_pip:.1f}p > {STALE_TRIGGER_ATR_FRAC}*ATR5m={_atr_5m/_pip:.1f}p')
                    ctx.setdefault('_blockers', {})[_sym] = {'type': 'trigger_far', 'message': f'Trigger too far ({_trig_dist/_pip:.1f}p)'}
                    continue
            _candidates.append(_c)
        if _candidates:
            _candidates.sort(key=lambda c: c['signal'].score, reverse=True)
            ctx['_firing_symbols'] = {c['symbol'] for c in _candidates}
            _best = _candidates[0]
            _others = ', '.join(
                f'{c["symbol"]}={c["signal"].score}' for c in _candidates[1:]
            ) or 'none'
            ctx['_on_deck_symbol'] = _best['symbol']
            ctx['_on_deck_score']  = _best['signal'].score
            decision_log.info(
                f'RANKING: {len(_candidates)} candidate(s) | on_deck={_best["symbol"]} '
                f'score={_best["signal"].score} | others: {_others}'
            )
            if ctx['position_count'] < CONFIG['max_concurrent_positions']:
                for _cand in _candidates:
                    try:
                        _entry_event = _fire_entry_candidate(_cand, ctx)
                        if _entry_event:
                            break
                    except Exception as _e:
                        log.error(f'_fire_entry_candidate error: {_e}', exc_info=True)
                        break
            else:
                # Max positions reached — record blocker for all candidates
                for _cand in _candidates:
                    ctx.setdefault('_blockers', {})[_cand['symbol']] = {
                        'type': 'max_positions',
                        'message': f'Max positions ({ctx["position_count"]}/{CONFIG["max_concurrent_positions"]})'
                    }
        else:
            ctx['_on_deck_symbol'] = None
            ctx['_on_deck_score']  = 0
            ctx['_firing_symbols'] = set()

        # ── Write dashboard status ──
        ctx['_cycle_count'] = ctx.get('_cycle_count', 0) + 1
        try:
            box_states = {}
            for _sym in ctx['active_instruments']:
                _bs = ctx['box_machine'].get_state(_sym)
                if _bs:
                    # Show alternative box when it's active — the original may be inverted/broken
                    _ab = (_bs.alternative_box if _bs.using_alternative and _bs.alternative_box
                           else _bs.active_box)
                    box_states[_sym] = {
                        'state': _bs.state.value,
                        'pdh': _ab.PDH,
                        'pdl': _ab.PDL,
                        'half_box': _ab.half_box,
                        'box_range_pct': round(_ab.box_range_pct, 4),
                        'atr': round(_ab.ATR, 5),
                        'box_age_hours': round(_bs.box_age_hours, 1),
                        'age_confidence_pct': round(_bs.age_confidence_pct, 1),
                        'entry_paused': _bs.entry_paused,
                        'pause_reason': _bs.pause_reason,
                        'spread': _bs.last_spread,
                        # Validation fields
                        'raw_pdh': getattr(_ab, 'raw_PDH', _ab.PDH),
                        'raw_pdl': getattr(_ab, 'raw_PDL', _ab.PDL),
                        'pdh_touches': getattr(_ab, 'pdh_touches', 0),
                        'pdl_touches': getattr(_ab, 'pdl_touches', 0),
                        'percentile_filtered': getattr(_ab, 'percentile_filtered', False),
                        'validated': getattr(_ab, 'validated', True),
                        # Breach tracking
                        'breach_direction': _bs.breach_direction,
                        'breach_candles': _bs.breach_candles_confirmed,
                        'using_alternative': _bs.using_alternative,
                        'gates_required': _bs.confirmation_gates_required,
                        'signal_score': ctx.get('_last_signal_scores', {}).get(_sym, 0),
                        'on_deck': ctx.get('_on_deck_symbol') == _sym,
                        'firing': _sym in ctx.get('_firing_symbols', set()),
                    }
            _eq = ctx['oanda'].get_equity() or ctx['equity_start']
            _daily_realized = ctx['oanda'].get_daily_realized_pl()
            _alltime_pl = ctx['oanda'].get_alltime_realized_pl()
            # Enrich screen_results with signal/blocker reasons for viable instruments
            _screen = ctx.get('_last_screen', [])
            _sigs = {s.symbol: s for s in ctx.get('_last_signals', [])}
            _blk = ctx.get('_blockers', {})
            for sr in _screen:
                if sr.get('viable') and not sr.get('rejection_reason'):
                    sym = sr['instrument']
                    if sym in _blk:
                        sr['rejection_reason'] = _blk[sym].get('message', '')
                    elif sym in _sigs:
                        sig = _sigs[sym]
                        if sig.rejection_reasons:
                            sr['rejection_reason'] = sig.rejection_reasons[0]
                        elif sig.wait_for:
                            sr['rejection_reason'] = sig.wait_for

            write_status(
                cycle=ctx['_cycle_count'],
                equity=_eq,
                daily_pnl=_daily_realized,
                daily_loss_pct=ctx['daily_loss'] / ctx['equity_start'] if ctx['equity_start'] > 0 else 0,
                positions=ctx['trade_manager'].get_all_open(),
                signal_results=ctx.get('_last_signals', []),
                box_states=box_states,
                active_sessions=list(ctx.get('_active_sessions', set())),
                active_instruments=ctx['active_instruments'],
                extra={
                    'trading_enabled': True,
                    'alltime_pl': _alltime_pl,
                    'screen_results': ctx.get('_last_screen', []),
                    'blockers': ctx.get('_blockers', {}),
                    'bb_states': ctx['regime'].get_bb_states(),
                },
            )
        except Exception as _e:
            log.warning(f'[STATUS] write_status error: {_e}')
        finally:
            ctx['_last_signals'] = []

        # Sleep until next 5-minute boundary
        elapsed = time.time() - loop_start
        sleep_time = max(0, CONFIG['scan_interval_seconds'] - elapsed)
        log.debug(f'Cycle complete in {elapsed:.1f}s — sleeping {sleep_time:.0f}s')

        if _running:
            _sleep(sleep_time)

    log.info('=' * 60)
    log.info('Mr. Scrooge — ENGINE STOPPED')
    log.info('=' * 60)


if __name__ == '__main__':
    _acquire_instance_lock_or_exit()
    main()
