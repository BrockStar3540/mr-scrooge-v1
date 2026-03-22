# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# ============================================================
"""
mr-scrooge-v2 — Coordinator
Main loop orchestrator. Owns all runtime state.
Extracts logic that was inline in v1 main.py into a proper class.
"""
import fcntl
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from config import Config
from health.monitor import HealthMonitor
from core.oanda_client import OANDAClient
from core.execution_router import OANDAExecutionRouter
from core.fee_calculator import OANDAFeeCalculator
from universe.screener import ForexUniverseScreener, ScreenResult, BASE_PAIRS, get_session_pairs
from zones.detector import ZoneDetector
from box.state_machine import BoxStateMachine
from box.models import BoxState
from signals.engine import SignalEngine
from positions.manager import PositionManager
from sizing.calculator import SizingCalculator
from alerts import send_slack_alert
from status import write_status

log = logging.getLogger('mr-scrooge-v2')
decision_log = logging.getLogger('mr-scrooge-v2.decisions')
trade_log    = logging.getLogger('mr-scrooge-v2.trades')


class Coordinator:
    """
    Owns all runtime state and runs the 5-minute main loop.
    Single instance per process (enforced by file lock).
    """

    def __init__(self, config: Config):
        self.config  = config
        self.monitor = HealthMonitor.get()
        self._running = True

        # Register SIGTERM/SIGINT
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

        # Runtime state (populated in boot())
        self.oanda:   Optional[OANDAClient]         = None
        self.router:  Optional[OANDAExecutionRouter] = None
        self.screener: Optional[ForexUniverseScreener] = None
        self.zone_detector: Optional[ZoneDetector]  = None
        self.box_machine:   Optional[BoxStateMachine] = None
        self.signal_engine: Optional[SignalEngine]   = None
        self.positions:     Optional[PositionManager] = None
        self.sizing:        Optional[SizingCalculator] = None
        self.fee_calc:      Optional[OANDAFeeCalculator] = None

        self.active_instruments: List[str] = []
        self.equity_start: float = 0.0
        self.daily_loss:   float = 0.0
        self.position_count: int = 0
        self.last_rescan:  datetime = datetime.now(timezone.utc)
        self.session_start: datetime = datetime.now(timezone.utc)
        self.time_exit_cooldowns: Dict[str, datetime] = {}
        self.pending_reentry: Dict = {}
        self.last_candle:    Dict[str, str] = {}
        self.zone_cache:     Dict[str, datetime] = {}
        self._active_sessions = set()
        self._last_session_hour = -1
        self._last_signals: List = []
        self._last_screen:  List = []
        self._last_signal_scores: Dict[str, int] = {}
        self._cycle_count:  int = 0
        self._on_deck_symbol: Optional[str] = None
        self._on_deck_score:  int = 0
        self._lock_fh = None

    # ── Lifecycle ─────────────────────────────────────────────

    def _acquire_lock(self):
        os.makedirs('logs', exist_ok=True)
        lock_path = os.path.join('logs', 'mr-scrooge-v2.lock')
        fh = open(lock_path, 'a+', encoding='utf-8')
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.critical(f'Another mr-scrooge-v2 instance is running ({lock_path}) — exiting')
            sys.exit(1)
        fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
        self._lock_fh = fh

    def _shutdown(self, signum, frame):
        log.warning(f'Signal {signum} — initiating graceful shutdown')
        self._running = False

    def _sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end and self._running:
            time.sleep(1)

    # ── Boot ─────────────────────────────────────────────────

    def boot(self):
        from dotenv import load_dotenv
        load_dotenv()

        log.info('=' * 60)
        log.info('mr-scrooge-v2 — Box Theory FX Engine — STARTING')
        log.info('=' * 60)

        from core.oanda_client import OANDAClient
        self.oanda = OANDAClient()

        equity = self.oanda.get_equity()
        if equity is None:
            log.critical('OANDA connection failed — exiting')
            sys.exit(1)
        self.equity_start = equity
        log.info(f'OANDA connected. Equity: {equity:.2f}')

        cfg_dict = self.config.as_dict()
        self.fee_calc     = OANDAFeeCalculator(self.config)
        self.router       = OANDAExecutionRouter(self.oanda, self.fee_calc, self.config)
        self.screener     = ForexUniverseScreener(self.oanda, self.fee_calc)
        self.zone_detector = ZoneDetector(cfg_dict)
        self.box_machine  = BoxStateMachine(cfg_dict)
        self.signal_engine = SignalEngine(self.config, self.monitor)
        self.positions    = PositionManager(self.config)
        self.sizing       = SizingCalculator(self.config, self.oanda)

        # Initial universe screen
        utc_hour = datetime.now(timezone.utc).hour
        session_pairs = get_session_pairs(utc_hour, mode=self.config.universe_mode)
        screen_results = self.screener.screen_all(pairs=session_pairs)
        self.active_instruments = [r.instrument for r in screen_results if r.viable]
        self._last_screen = self._serialize_screen(screen_results)
        log.info(f'Active instruments: {", ".join(self.active_instruments) or "none yet"}')

        # Initialize box states
        for symbol in self.active_instruments:
            self._init_box(symbol)

        # Reconcile open positions
        open_trades = self.oanda.get_open_trades()
        reconciled  = self.positions.reconcile_from_oanda(open_trades)
        self.position_count = reconciled
        log.info(f'Reconciled {reconciled} open positions from OANDA')

        self.session_start = datetime.now(timezone.utc)
        self.last_rescan   = datetime.now(timezone.utc)
        log.info('Boot complete.')

    def _init_box(self, symbol: str):
        daily_bars  = self.oanda.get_daily_bars(symbol)
        hourly_bars = self.oanda.get_hourly_bars(symbol, count=200)
        if daily_bars is None or len(daily_bars) < 3:
            return
        prices     = self.oanda.get_prices([symbol])
        price_data = prices.get(symbol, {})
        current_price = (
            (price_data.get('bid', 0) + price_data.get('ask', 0)) / 2
            or float(daily_bars.iloc[-1]['close'])
        )
        spread   = (price_data.get('ask', 0) - price_data.get('bid', 0)) if price_data else 0.0002
        zone_map = self.zone_detector.build_zone_map(
            symbol, 'forex', daily_bars,
            hourly_bars if hourly_bars is not None else daily_bars, current_price,
        )
        if zone_map:
            self.box_machine.initialize_instrument(symbol, zone_map.daily_box, spread)
            log.info(f'Box initialized: {symbol}')

    # ── Daily reset ───────────────────────────────────────────

    def daily_reset(self):
        log.info('=' * 60)
        log.info('DAILY RESET')
        log.info('=' * 60)
        self.daily_loss = 0.0
        for symbol in self.active_instruments:
            self._init_box(symbol)
        self.session_start = datetime.now(timezone.utc)
        self.zone_cache.clear()
        equity = self.oanda.get_equity()
        if equity:
            self.equity_start = equity
            log.info(f'Session equity baseline: {equity:.2f}')

    # ── Universe rescan ───────────────────────────────────────

    def rescan_universe(self):
        utc_hour = datetime.now(timezone.utc).hour
        session_pairs = get_session_pairs(utc_hour, mode=self.config.universe_mode)
        screen_results = self.screener.screen_all(pairs=session_pairs)
        new_instruments = [r.instrument for r in screen_results if r.viable]
        for symbol in new_instruments:
            if symbol not in self.active_instruments:
                self._init_box(symbol)
                log.info(f'Added to universe: {symbol}')
        self.active_instruments = new_instruments
        self._last_screen = self._serialize_screen(screen_results)
        self.last_rescan = datetime.now(timezone.utc)
        log.info(f'Universe updated: {", ".join(new_instruments)}')

    # ── Instrument processing ─────────────────────────────────

    def process_instrument(self, symbol: str) -> Optional[dict]:
        """Process one instrument per 5M candle. Returns candidate dict or None."""
        bars_5m = self.oanda.get_5m_bars(symbol, count=300)
        bars_1h = self.oanda.get_hourly_bars(symbol, count=200)
        bars_d  = self.oanda.get_daily_bars(symbol, count=20)

        if any(b is None for b in [bars_5m, bars_1h, bars_d]):
            time.sleep(2)
            if bars_5m is None: bars_5m = self.oanda.get_5m_bars(symbol, count=300)
            if bars_1h is None: bars_1h = self.oanda.get_hourly_bars(symbol, count=200)
            if bars_d  is None: bars_d  = self.oanda.get_daily_bars(symbol, count=20)
            if any(b is None for b in [bars_5m, bars_1h, bars_d]):
                log.warning(f'{symbol}: bar fetch failed — skipping')
                return None

        # Candle debounce
        bar_time = str(bars_5m.index[-1])
        if self.last_candle.get(symbol) == bar_time:
            return None
        self.last_candle[symbol] = bar_time

        current_price = float(bars_5m.iloc[-1]['close'])
        live_spread   = self.oanda.get_live_spread(symbol)
        if live_spread is None:
            log.warning(f'{symbol}: spread fetch failed — skipping')
            return None

        zone_map = self.zone_detector.build_zone_map(symbol, 'forex', bars_d, bars_1h, current_price)
        if zone_map is None:
            return None

        half_box = zone_map.daily_box.half_box
        cost     = self.fee_calc.calculate(symbol, live_spread, half_box)
        total_overhead = cost.total_round_trip
        minutes_since_open = int((datetime.now(timezone.utc) - self.session_start).total_seconds() / 60)

        # Update box state machine
        if symbol not in self.box_machine.states:
            self.box_machine.initialize_instrument(symbol, zone_map.daily_box, live_spread or 0.0002)
        current_bar = bars_5m.iloc[-1]
        box_record  = self.box_machine.update(
            symbol, current_price, current_bar, live_spread, total_overhead, minutes_since_open
        )

        if box_record and box_record.state == BoxState.BOX_BROKEN_RECALCULATING:
            session_high = float(bars_5m['high'].max())
            session_low  = float(bars_5m['low'].min())
            alt = self.box_machine.draw_alternative_box(
                symbol, bars_5m, session_high, session_low, box_record.active_box, total_overhead,
            )
            if not alt.viable:
                return None

        # Manage open positions for this symbol
        for pos in self.positions.get_all_open():
            if pos.symbol != symbol:
                continue
            try:
                entry_time = datetime.fromisoformat(pos.entry_time)
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                minutes_in_trade = int((datetime.now(timezone.utc) - entry_time).total_seconds() / 60)
            except Exception:
                minutes_in_trade = 0

            mgmt = self.positions.manage_position(
                pos.trade_id, current_price, bars_5m, bars_1h, minutes_in_trade,
                zone_map=zone_map, account_equity=self.oanda.get_equity() or self.equity_start,
            )
            action = mgmt.get('action', 'hold')
            if action == 'close':
                self._handle_close(pos, symbol, current_price, mgmt, zone_map)
            elif action == 'move_stop':
                self.router.move_stop(
                    pos.trade_id, pos.oanda_trade_id,
                    mgmt.get('price', pos.current_stop), mgmt.get('reason', 'manager'),
                )

        # Check pending fills
        self.router.check_and_manage_pending()

        # Gate: box tradeable?
        tradeable, reason = self.box_machine.is_tradeable(symbol)
        if not tradeable:
            decision_log.info(f'{symbol}: Not tradeable — {reason}')
            return None

        # Gate: already in position?
        existing = [p for p in self.positions.get_all_open() if p.symbol == symbol]
        if existing:
            return None

        # Gate: cooldown?
        cooldown_until = self.time_exit_cooldowns.get(symbol)
        if cooldown_until and datetime.now() < cooldown_until:
            return None

        # Gate: at max positions?
        if self.position_count >= self.config.max_concurrent_positions:
            decision_log.info(f'{symbol}: Max positions ({self.position_count})')
            return None

        # Evaluate signal
        signal = self.signal_engine.evaluate(
            symbol, zone_map, bars_5m, bars_1h, total_overhead, minutes_since_open,
        )
        self._last_signals.append(signal)

        reasons = ' | '.join(signal.rejection_reasons) if signal.rejection_reasons else signal.wait_for
        decision_log.info(
            f'{symbol}: {signal.action} | zone={signal.zone_gate} zone_tier={signal.zone_tier} '
            f'sucker={signal.sucker_move_gate} atr={signal.atr_strength} '
            f'fork={signal.pitchfork_gate} otr={signal.otr_gate}'
            + (f' | {reasons}' if reasons else '')
        )

        if not signal.all_gates_passed or signal.action not in ('ENTER_LONG', 'ENTER_SHORT'):
            return None

        # Zone debounce (20 min / 4 candles)
        _pip = 0.01 if 'JPY' in symbol else 0.0001
        _rounded = round(signal.entry_price / (_pip * 5)) * (_pip * 5)
        zone_key = f'{symbol}_{_rounded:.5f}_{signal.action}'
        last_fired = self.zone_cache.get(zone_key)
        if last_fired and (datetime.now() - last_fired).total_seconds() < 1200:
            decision_log.info(f'{symbol}: Zone recently triggered — skipping')
            return None

        if signal.all_gates_passed:
            self._last_signal_scores[symbol] = signal.score

        return {
            'symbol': symbol, 'signal': signal, 'current_price': current_price,
            'half_box': half_box, 'live_spread': live_spread,
            'total_overhead': total_overhead, 'zone_map': zone_map,
        }

    def _handle_close(self, pos, symbol, current_price, mgmt, zone_map):
        reason_str = mgmt.get('reason', '')
        is_stop    = 'Stop hit' in reason_str
        exit_side  = 'SELL' if pos.direction == 'long' else 'BUY'
        realized_pnl = None

        if is_stop:
            log.info(f'{symbol} {pos.trade_id}: stop hit — OANDA already closed')
            if pos.oanda_trade_id:
                realized_pnl = self.oanda.get_trade_realized_pl(pos.oanda_trade_id)
        else:
            exit_res = self.router.partial_close(
                pos.trade_id, pos.oanda_trade_id, pos.remaining_quantity, tp_level=0,
            )
            if isinstance(exit_res, dict):
                realized_pnl = exit_res.get('realized_pl')
            if realized_pnl is None and pos.oanda_trade_id:
                realized_pnl = self.oanda.get_trade_realized_pl(pos.oanda_trade_id)

        close_pnl = self.positions.close_position(
            pos.trade_id, current_price, reason_str, pnl_override=realized_pnl,
        ) or 0.0
        self.daily_loss += max(0, -close_pnl)
        self.position_count = max(0, self.position_count - 1)
        trade_log.info(f'CLOSE {pos.trade_id} | {symbol} {pos.direction} | PnL: {close_pnl:+.4f} | {reason_str}')

        is_time_exit = 'Time exit' in reason_str
        if is_time_exit or is_stop:
            cooldown_mins = self.config.time_exit_cooldown_minutes
            self.time_exit_cooldowns[symbol] = datetime.now() + timedelta(minutes=cooldown_mins)

    def fire_entry(self, cand: dict):
        """Execute the best-scored entry candidate."""
        symbol         = cand['symbol']
        signal         = cand['signal']
        current_price  = cand['current_price']
        live_spread    = cand['live_spread']
        half_box       = cand['half_box']
        total_overhead = cand['total_overhead']
        zone_map       = cand['zone_map']

        # Mark zone
        _pip = 0.01 if 'JPY' in symbol else 0.0001
        _rounded = round(signal.entry_price / (_pip * 5)) * (_pip * 5)
        self.zone_cache[f'{symbol}_{_rounded:.5f}_{signal.action}'] = datetime.now()

        equity      = self.oanda.get_equity() or self.equity_start
        free_margin = self.oanda.get_free_margin()
        units = self.sizing.calculate(symbol, current_price, equity, free_margin)
        if units is None:
            return None

        # Staleness check
        if signal.action == 'ENTER_SHORT' and current_price >= signal.stop_price:
            return None
        if signal.action == 'ENTER_LONG' and current_price <= signal.stop_price:
            return None

        direction  = 'BUY' if signal.action == 'ENTER_LONG' else 'SELL'
        trade_id   = f'scroogev2_{symbol.lower().replace("_","")}{uuid.uuid4().hex[:8]}'

        result = self.router.enter_position(
            symbol=symbol, side=direction, quantity=units,
            limit_price=signal.entry_price, take_profit=signal.tp2_price,
            stop_loss=signal.stop_price, trade_id=trade_id,
            live_spread=live_spread, half_box=half_box,
        )
        if not result.get('success'):
            return None

        self.position_count += 1
        actual_fill = result.get('fill_price') or signal.entry_price
        if actual_fill <= 0:
            actual_fill = signal.entry_price

        # Recalculate SL/TP from actual fill
        inst_meta = self.oanda._instrument_cache.get(symbol, {})
        pip_size  = 10 ** int(inst_meta.get('pipLocation', -4))
        sl_pips, tp1_pips, tp2_pips = self.config.sl_pips, self.config.tp1_pips, self.config.tp2_pips
        if direction == 'BUY':
            sl_fill  = round(actual_fill - sl_pips  * pip_size, 5)
            tp1_fill = round(actual_fill + tp1_pips * pip_size, 5)
            tp2_fill = round(actual_fill + tp2_pips * pip_size, 5)
        else:
            sl_fill  = round(actual_fill + sl_pips  * pip_size, 5)
            tp1_fill = round(actual_fill - tp1_pips * pip_size, 5)
            tp2_fill = round(actual_fill - tp2_pips * pip_size, 5)

        signal.stop_price  = sl_fill
        signal.tp1_price   = tp1_fill
        signal.tp2_price   = tp2_fill
        signal.entry_price = actual_fill
        oanda_tid = result.get('oanda_trade_id', '')
        if oanda_tid:
            self.oanda.modify_trade_stop(oanda_tid, sl_fill)
            self.oanda.modify_trade_tp(oanda_tid, tp2_fill)

        pip_value = float(self.oanda.get_pip_value_per_unit(symbol, actual_fill))
        overhead_cost_usd = total_overhead * units
        self.positions.register_entry(
            trade_id=trade_id, symbol=symbol, exchange='OANDA',
            signal=signal, quantity=units, actual_fill_price=actual_fill,
            overhead_cost=overhead_cost_usd, atr=zone_map.daily_box.ATR,
            oanda_trade_id=oanda_tid,
        )
        signal_type = signal.pitchfork.signal_type if signal.pitchfork else 'unknown'
        trade_log.info(f'ENTRY {trade_id} | {symbol} {direction} {units:.0f} @ {actual_fill:.5f} | '
                       f'SL: {sl_fill:.5f} | TP1: {tp1_fill:.5f} | Signal: {signal_type}')
        return {'type': 'ENTRY', 'trade_id': trade_id, 'symbol': symbol, 'direction': direction}

    # ── Weekend check ─────────────────────────────────────────

    @staticmethod
    def is_weekend() -> bool:
        now = datetime.now(timezone.utc)
        wd  = now.weekday()
        return wd == 5 or (wd == 6 and now.hour < 22)

    # ── Main loop ─────────────────────────────────────────────

    def run(self):
        self._acquire_lock()
        self.boot()
        last_daily_reset = datetime.now(timezone.utc).date()
        in_weekend = False

        log.info('Main loop starting — 5-minute candle cadence')

        while self._running:
            loop_start = time.time()
            now = datetime.now(timezone.utc)

            if now.date() > last_daily_reset:
                self.daily_reset()
                last_daily_reset = now.date()

            if self.is_weekend():
                if not in_weekend:
                    log.info('── WEEKEND — OANDA closed. Idling ──')
                    in_weekend = True
                self._sleep(self.config.scan_interval_seconds)
                continue
            if in_weekend:
                log.info('── MARKET OPEN — resuming ──')
                in_weekend = False

            # Universe rescan
            elapsed_rescan = (now - self.last_rescan).total_seconds() / 60
            if elapsed_rescan >= self.config.rescan_interval_minutes:
                self.rescan_universe()

            # Daily loss check
            equity = self.oanda.get_equity() or self.equity_start
            daily_loss_pct = self.daily_loss / self.equity_start if self.equity_start > 0 else 0
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                log.warning(f'DAILY LOSS LIMIT {daily_loss_pct:.1%} — halting entries')
                send_slack_alert(f'mr-scrooge-v2 daily loss limit {daily_loss_pct:.1%}. No new entries until reset.')
                self._sleep(self.config.scan_interval_seconds)
                continue

            log.info(f'── Cycle {now.strftime("%H:%M")} ── Equity: {equity:.2f} | '
                     f'Positions: {self.position_count} | '
                     f'Instruments: {len(self.active_instruments)}')

            # OANDA native close detection (SL/TP closed between cycles)
            live_trades = self.oanda.get_open_trades()
            live_ids = set()
            for t in live_trades:
                cid = (t.get('clientExtensions', {}).get('id', '')
                       or t.get('tradeClientExtensions', {}).get('id', ''))
                if cid:
                    live_ids.add(cid)

            for tid, pos in list(self.positions.open_positions.items()):
                if tid not in live_ids and pos.oanda_trade_id:
                    realized_pl = self.oanda.get_trade_realized_pl(pos.oanda_trade_id)
                    log.warning(f'OANDA native close: {pos.trade_id} {pos.symbol}')
                    self.positions.close_position(
                        pos.trade_id, pos.entry_price,
                        'OANDA native close (SL/TP)', pnl_override=realized_pl,
                    )
                    if realized_pl is not None:
                        self.daily_loss += max(0, -float(realized_pl))
                    self.position_count = max(0, self.position_count - 1)
                    cooldown_mins = 15 if (realized_pl is not None and realized_pl > 0) else self.config.time_exit_cooldown_minutes
                    self.time_exit_cooldowns[pos.symbol] = datetime.now() + timedelta(minutes=cooldown_mins)

            # Process each instrument — collect entry candidates
            entry_candidates = []
            seen = set()
            ordered = (
                [s for s in self.active_instruments if s in BASE_PAIRS] +
                [s for s in self.active_instruments if s not in BASE_PAIRS]
            )
            tracked = {pos.symbol for pos in self.positions.get_all_open()}
            for orphan in tracked:
                if orphan not in ordered:
                    ordered.append(orphan)

            for symbol in ordered:
                if not self._running:
                    break
                if symbol in seen:
                    continue
                seen.add(symbol)
                try:
                    cand = self.process_instrument(symbol)
                    if cand:
                        entry_candidates.append(cand)
                except Exception as exc:
                    log.error(f'{symbol}: unhandled error — {exc}', exc_info=True)

            # Rank and fire best entry
            if entry_candidates:
                entry_candidates.sort(key=lambda c: c['signal'].score, reverse=True)
                best = entry_candidates[0]
                self._on_deck_symbol = best['symbol']
                self._on_deck_score  = best['signal'].score
                decision_log.info(
                    f'RANKING: {len(entry_candidates)} candidates | '
                    f'on_deck={best["symbol"]} score={best["signal"].score}'
                )
                if self.position_count < self.config.max_concurrent_positions:
                    for cand in entry_candidates:
                        try:
                            if self.fire_entry(cand):
                                break
                        except Exception as exc:
                            log.error(f'fire_entry error: {exc}', exc_info=True)
                            break
            else:
                self._on_deck_symbol = None
                self._on_deck_score  = 0

            # Health report
            health_report = self.monitor.report().to_dict()

            # Dashboard status
            self._cycle_count += 1
            try:
                write_status(
                    cycle=self._cycle_count, equity=equity,
                    daily_pnl=self.oanda.get_daily_realized_pl(),
                    daily_loss_pct=daily_loss_pct,
                    positions=self.positions.get_all_open(),
                    signal_results=self._last_signals,
                    box_states={}, active_sessions=list(self._active_sessions),
                    active_instruments=self.active_instruments,
                    health_report=health_report,
                    extra={'alltime_pl': self.oanda.get_alltime_realized_pl(),
                           'screen_results': self._last_screen,
                           'on_deck': self._on_deck_symbol},
                )
            except Exception as exc:
                log.warning(f'write_status error: {exc}')
            finally:
                self._last_signals = []

            elapsed = time.time() - loop_start
            sleep_time = max(0, self.config.scan_interval_seconds - elapsed)
            log.debug(f'Cycle {self._cycle_count} done in {elapsed:.1f}s — sleeping {sleep_time:.0f}s')
            self._sleep(sleep_time)

        log.info('mr-scrooge-v2 — STOPPED')

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _serialize_screen(screen_results) -> list:
        out = []
        for r in screen_results:
            try:
                out.append({
                    'instrument': r.instrument, 'viable': r.viable,
                    'pdh': round(r.pdh, 5), 'pdl': round(r.pdl, 5),
                    'box_range_pips': round(r.box_range_pips, 1),
                    'otr_ratio': round(r.otr_ratio, 3),
                    'rejection_reason': r.rejection_reason,
                })
            except Exception:
                pass
        return out
