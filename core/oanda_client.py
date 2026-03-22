# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — OANDA Client
OANDA v20 REST API. Fetches bars, spreads, account state, executes orders.
Health monitoring: registers as 'oanda_client' with HealthMonitor.
"""

import os
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from typing import Optional, Dict, List
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger('mr-scrooge-v2.oanda')

# Health monitor — imported lazily to avoid circular at module load
def _monitor():
    from health.monitor import HealthMonitor
    return HealthMonitor.get()


class OANDAClient:
    """
    OANDA v20 REST API client for mr-scrooge-v2.
    Records success/failure on every API call via HealthMonitor.
    Circuit breaker auto-disables after 5 consecutive failures.
    """

    _COMPONENT = 'oanda_client'

    def __init__(self):
        self.api_key    = os.getenv('OANDA_API_KEY')
        self.account_id = os.getenv('OANDA_ACCOUNT_ID')
        env = os.getenv('OANDA_ENVIRONMENT', 'practice')

        if env == 'live':
            self.base_url = 'https://api-fxtrade.oanda.com'
        else:
            self.base_url = 'https://api-fxpractice.oanda.com'

        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type':  'application/json',
            'Accept-Datetime-Format': 'RFC3339',
        }
        self._instrument_cache: Dict = {}
        _monitor().register(self._COMPONENT, failure_threshold=5, recovery_seconds=120)
        self._load_instruments()

    # ── Internal helpers ─────────────────────────────────────

    def _get(self, url: str, params: dict = None, timeout: int = 10) -> Optional[dict]:
        """GET with health tracking. Returns parsed JSON or None."""
        t0 = time.monotonic()
        try:
            r = requests.get(url, headers=self.headers, params=params, timeout=timeout)
            r.raise_for_status()
            ms = (time.monotonic() - t0) * 1000
            _monitor().record_success(self._COMPONENT, ms)
            return r.json()
        except Exception as exc:
            ms = (time.monotonic() - t0) * 1000
            _monitor().record_failure(self._COMPONENT, str(exc), ms)
            log.warning(f'GET {url}: {exc}')
            return None

    def _post(self, url: str, body: dict, timeout: int = 10) -> Optional[dict]:
        """POST with health tracking. Returns parsed JSON or None."""
        t0 = time.monotonic()
        try:
            r = requests.post(url, headers=self.headers, json=body, timeout=timeout)
            r.raise_for_status()
            ms = (time.monotonic() - t0) * 1000
            _monitor().record_success(self._COMPONENT, ms)
            return r.json()
        except Exception as exc:
            ms = (time.monotonic() - t0) * 1000
            _monitor().record_failure(self._COMPONENT, str(exc), ms)
            log.error(f'POST {url}: {exc}')
            return None

    def _put(self, url: str, body: dict, timeout: int = 10) -> Optional[dict]:
        """PUT with health tracking. Returns parsed JSON or None."""
        t0 = time.monotonic()
        try:
            r = requests.put(url, headers=self.headers, json=body, timeout=timeout)
            r.raise_for_status()
            ms = (time.monotonic() - t0) * 1000
            _monitor().record_success(self._COMPONENT, ms)
            return r.json()
        except Exception as exc:
            ms = (time.monotonic() - t0) * 1000
            _monitor().record_failure(self._COMPONENT, str(exc), ms)
            log.error(f'PUT {url}: {exc}')
            return None

    # ── Instruments ──────────────────────────────────────────

    def _load_instruments(self):
        """Cache instrument metadata (pip sizes, margin rates)."""
        url = f'{self.base_url}/v3/accounts/{self.account_id}/instruments'
        data = self._get(url)
        if data:
            for inst in data.get('instruments', []):
                self._instrument_cache[inst['name']] = inst
            log.info(f'Instrument metadata loaded: {len(self._instrument_cache)} instruments')

    # ── Pricing ──────────────────────────────────────────────

    def get_prices(self, instruments: List[str]) -> Dict[str, Dict]:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/pricing'
        data = self._get(url, params={'instruments': ','.join(instruments)}, timeout=5)
        if not data:
            return {}
        result = {}
        for p in data.get('prices', []):
            result[p['instrument']] = {
                'bid': float(p['bids'][0]['price']),
                'ask': float(p['asks'][0]['price']),
            }
        return result

    def get_live_spread(self, instrument: str) -> Optional[float]:
        prices = self.get_prices([instrument])
        p = prices.get(instrument)
        if p:
            return p['ask'] - p['bid']
        return None

    def get_pip_value_per_unit(self, instrument: str, current_mid: float,
                                account_currency: str = 'USD') -> float:
        inst = self._instrument_cache.get(instrument)
        if inst is None:
            self._load_instruments()
            inst = self._instrument_cache.get(instrument)
        if inst is None:
            fallback = (0.01 / current_mid) if 'JPY' in instrument and current_mid > 0 else 0.0001
            log.warning(f'{instrument}: no metadata — fallback pip value {fallback:.8f}')
            return fallback

        pip_size = 10 ** int(inst.get('pipLocation', -4))
        parts = instrument.split('_')
        base_ccy  = parts[0] if len(parts) >= 1 else ''
        quote_ccy = parts[1] if len(parts) >= 2 else ''
        pip_value_quote = pip_size

        if quote_ccy == account_currency:
            return pip_value_quote
        if base_ccy == account_currency:
            return pip_value_quote / current_mid if current_mid > 0 else pip_value_quote

        # Cross pair — fetch conversion rate
        _USD_BASE = {'JPY','CHF','CAD','HKD','SGD','DKK','NOK','SEK','ZAR','MXN'}
        def _mid(pair: str) -> Optional[float]:
            px = self.get_prices([pair]).get(pair)
            return (px['bid'] + px['ask']) / 2.0 if px else None

        if quote_ccy in _USD_BASE:
            p1, p2 = f'{account_currency}_{quote_ccy}', f'{quote_ccy}_{account_currency}'
        else:
            p1, p2 = f'{quote_ccy}_{account_currency}', f'{account_currency}_{quote_ccy}'

        mid = _mid(p1)
        if mid:
            rate = mid if p1.startswith(quote_ccy) else 1.0 / mid
            return pip_value_quote * rate
        mid2 = _mid(p2)
        if mid2 and mid2 > 0:
            rate = mid2 if p2.startswith(quote_ccy) else 1.0 / mid2
            return pip_value_quote * rate

        log.warning(f'{instrument}: could not convert {quote_ccy}→{account_currency}')
        return pip_value_quote

    # ── Bar data ─────────────────────────────────────────────

    def get_candles(self, instrument: str, granularity: str,
                    count: int = 50) -> Optional[pd.DataFrame]:
        url = f'{self.base_url}/v3/instruments/{instrument}/candles'
        data = self._get(url, params={'granularity': granularity, 'count': count, 'price': 'M'})
        if not data:
            return None
        candles = data.get('candles', [])
        if not candles:
            return None
        rows = [
            {
                'time':   pd.to_datetime(c['time']),
                'open':   float(c['mid']['o']),
                'high':   float(c['mid']['h']),
                'low':    float(c['mid']['l']),
                'close':  float(c['mid']['c']),
                'volume': int(c.get('volume', 0)),
            }
            for c in candles
        ]
        df = pd.DataFrame(rows)
        df.set_index('time', inplace=True)
        return df

    def get_daily_bars(self, instrument: str, count: int = 20) -> Optional[pd.DataFrame]:
        return self.get_candles(instrument, 'D', count)

    def get_hourly_bars(self, instrument: str, count: int = 50) -> Optional[pd.DataFrame]:
        return self.get_candles(instrument, 'H1', count)

    def get_5m_bars(self, instrument: str, count: int = 50) -> Optional[pd.DataFrame]:
        return self.get_candles(instrument, 'M5', count)

    def get_30m_bars(self, instrument: str, count: int = 100) -> Optional[pd.DataFrame]:
        return self.get_candles(instrument, 'M30', count)

    # ── Account state ────────────────────────────────────────

    def get_account_summary(self) -> Optional[Dict]:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/summary'
        data = self._get(url)
        return data.get('account') if data else None

    def get_equity(self) -> Optional[float]:
        s = self.get_account_summary()
        return float(s['balance']) if s else None

    def get_free_margin(self) -> Optional[float]:
        s = self.get_account_summary()
        return float(s['marginAvailable']) if s else None

    def get_margin_used(self) -> Optional[float]:
        s = self.get_account_summary()
        return float(s['marginUsed']) if s else None

    def get_combined_margin_pct(self) -> float:
        s = self.get_account_summary()
        if s:
            balance = float(s.get('balance', 1))
            return float(s.get('marginUsed', 0)) / balance if balance > 0 else 1.0
        return 0.0

    def get_open_trades(self) -> List[Dict]:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/openTrades'
        data = self._get(url)
        return data.get('trades', []) if data else []

    def get_trade_realized_pl(self, trade_id: str) -> Optional[float]:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/trades/{trade_id}'
        data = self._get(url)
        if data:
            rpl = data.get('trade', {}).get('realizedPL')
            return float(rpl) if rpl is not None else None
        return None

    def get_daily_realized_pl(self) -> float:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%dT00:00:00.000000000Z')
        url = (f'{self.base_url}/v3/accounts/{self.account_id}/transactions'
               f'?from={today}&type=ORDER_FILL')
        data = self._get(url)
        if not data:
            return 0.0
        total = 0.0
        for page_url in data.get('pages', []):
            pd_data = self._get(page_url)
            if pd_data:
                for tx in pd_data.get('transactions', []):
                    total += float(tx.get('pl', 0))
        return round(total, 2)

    def get_alltime_realized_pl(self) -> float:
        s = self.get_account_summary()
        return round(float(s.get('pl', 0)), 2) if s else 0.0

    # ── Order execution ──────────────────────────────────────

    def place_entry_order(self, instrument: str, side: str, units: float,
                           take_profit: float, stop_loss: float,
                           client_trade_id: str) -> Dict:
        """Market order for entries. Fills immediately at bid/ask."""
        signed_units = int(round(units)) if side == 'BUY' else -int(round(units))
        decimals = 3 if 'JPY' in instrument else 5
        body = {
            'order': {
                'type': 'MARKET',
                'instrument': instrument,
                'units': str(signed_units),
                'timeInForce': 'FOK',
                'positionFill': 'DEFAULT',
                'takeProfitOnFill': {'price': f'{take_profit:.{decimals}f}'},
                'stopLossOnFill':   {'price': f'{stop_loss:.{decimals}f}'},
                'tradeClientExtensions': {'id': client_trade_id, 'comment': 'mr-scrooge-v2'},
            }
        }
        url = f'{self.base_url}/v3/accounts/{self.account_id}/orders'
        result = self._post(url, body)
        if result is None:
            return {'success': False, 'error': 'POST failed'}

        fill_tx = result.get('orderFillTransaction')
        if not fill_tx:
            cancel = result.get('orderCancelTransaction', {})
            reject = result.get('orderRejectTransaction', {})
            reason = cancel.get('reason') or reject.get('rejectReason') or 'unknown'
            log.error(f'Entry NOT filled: {instrument} {side} | {reason}')
            return {'success': False, 'error': reason, 'raw': result}

        fill_price     = float(fill_tx.get('price', 0))
        oanda_trade_id = str(fill_tx.get('tradeOpened', {}).get('tradeID', ''))
        log.info(f'Entry filled: {instrument} {side} {units:.0f} @ {fill_price:.5f} | ID: {oanda_trade_id}')
        return {'success': True, 'fill_price': fill_price, 'oanda_trade_id': oanda_trade_id, 'raw': result}

    def place_market_order(self, instrument: str, side: str, units: float,
                            stop_loss: float, client_trade_id: str,
                            reason: str = 'exit') -> Dict:
        """Emergency market exit."""
        log.warning(f'MARKET ORDER (exit): {instrument} {side} {units:.0f} | {reason}')
        signed_units = int(round(units)) if side == 'BUY' else -int(round(units))
        body = {
            'order': {
                'type': 'MARKET',
                'instrument': instrument,
                'units': str(signed_units),
                'timeInForce': 'FOK',
                'positionFill': 'REDUCE_ONLY',
            }
        }
        url = f'{self.base_url}/v3/accounts/{self.account_id}/orders'
        result = self._post(url, body)
        if result is None:
            return {'success': False, 'error': 'POST failed'}

        fill_tx = result.get('orderFillTransaction', {}) or {}
        fill_price = float(fill_tx.get('price', 0))
        try:
            realized_pl = (float(fill_tx.get('pl', 0))
                           + float(fill_tx.get('financing', 0))
                           - float(fill_tx.get('commission', 0)))
        except Exception:
            realized_pl = None
        return {'success': True, 'fill_price': fill_price, 'realized_pl': realized_pl, 'raw': result}

    def cancel_order(self, order_id: str) -> bool:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/orders/{order_id}/cancel'
        result = self._put(url, {})
        return result is not None

    def modify_trade_stop(self, trade_id: str, new_stop: float) -> bool:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/trades/{trade_id}/orders'
        result = self._put(url, {'stopLoss': {'price': str(round(new_stop, 5)), 'timeInForce': 'GTC'}})
        if result is not None:
            log.info(f'Stop moved: trade {trade_id} → {new_stop:.5f}')
            return True
        return False

    def modify_trade_tp(self, trade_id: str, tp_price: float) -> bool:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/trades/{trade_id}/orders'
        result = self._put(url, {'takeProfit': {'price': str(round(tp_price, 5)), 'timeInForce': 'GTC'}})
        if result is not None:
            log.info(f'TP set: trade {trade_id} → {tp_price:.5f}')
            return True
        return False

    def close_trade_partial(self, trade_id: str, units: float) -> Dict:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/trades/{trade_id}/close'
        result = self._put(url, {'units': str(units)})
        if result is None:
            return {'success': False, 'error': 'PUT failed'}
        fill_tx = result.get('orderFillTransaction', {}) or {}
        fill = float(fill_tx.get('price', 0))
        try:
            realized_pl = (float(fill_tx.get('pl', 0))
                           + float(fill_tx.get('financing', 0))
                           - float(fill_tx.get('commission', 0)))
        except Exception:
            realized_pl = None
        log.info(f'Partial close: trade {trade_id} | {units:.0f} units @ {fill:.5f}')
        return {'success': True, 'fill_price': fill, 'realized_pl': realized_pl, 'raw': result}

    def check_order_filled(self, order_id: str) -> Dict:
        url = f'{self.base_url}/v3/accounts/{self.account_id}/orders/{order_id}'
        data = self._get(url, timeout=5)
        if not data:
            return {'filled': False, 'state': 'ERROR'}
        state = data.get('order', {}).get('state', 'UNKNOWN')
        return {'filled': state == 'FILLED', 'cancelled': state == 'CANCELLED',
                'pending': state == 'PENDING', 'state': state}
