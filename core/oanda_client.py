# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
# ============================================================
# MR. SCROOGE — OANDA CLIENT
# OANDA v20 REST API client.
# Handles bar fetching (5M, 1H, D), live spreads, order placement,
# account summary, and open trade reconciliation.
# ============================================================

import os
import time
import math
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger('mr-scrooge.oanda')


class OANDAClient:
    """
    OANDA v20 REST API client for Mr. Scrooge.

    Used for:
    - Fetching daily bars (PDH/PDL box)
    - Fetching hourly bars (magic lines)
    - Fetching 5-minute bars (signal confirmation)
    - Getting live spread
    - Placing limit orders (primary)
    - Placing market orders (emergency exit only)
    - Querying open positions and margin
    """

    def __init__(self):
        self.api_key = os.getenv('OANDA_API_KEY')
        self.account_id = os.getenv('OANDA_ACCOUNT_ID')
        env = os.getenv('OANDA_ENVIRONMENT', 'practice')

        if env == 'live':
            self.base_url = 'https://api-fxtrade.oanda.com'
            self.stream_url = 'https://stream-fxtrade.oanda.com'
        else:
            self.base_url = 'https://api-fxpractice.oanda.com'
            self.stream_url = 'https://stream-fxpractice.oanda.com'

        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Accept-Datetime-Format': 'RFC3339'
        }

        self._instrument_cache: Dict = {}
        self._load_instruments()

    # ----------------------------------------------------------
    # INSTRUMENTS & PRICING
    # ----------------------------------------------------------

    def _load_instruments(self):
        """Cache instrument metadata (pip sizes, min trade sizes)."""
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/instruments"
            r = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            for inst in r.json().get('instruments', []):
                self._instrument_cache[inst['name']] = inst
            log.info(f"Instrument metadata loaded: {len(self._instrument_cache)} instruments cached")
        except Exception as e:
            log.error(f"Failed to load instruments: {e}")

    def get_prices(self, instruments: List[str]) -> Dict[str, Dict]:
        """Get current bid/ask prices for multiple instruments at once."""
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/pricing"
            params = {'instruments': ','.join(instruments)}
            r = requests.get(url, headers=self.headers, params=params, timeout=5)
            r.raise_for_status()
            result = {}
            for p in r.json().get('prices', []):
                result[p['instrument']] = {
                    'bid': float(p['bids'][0]['price']),
                    'ask': float(p['asks'][0]['price']),
                }
            return result
        except Exception as e:
            log.warning(f"Multi-price fetch failed: {e}")
            return {}

    def get_pip_value_per_unit(self,
                                instrument: str,
                                current_mid: float,
                                account_currency: str = 'USD') -> float:
        """
        Returns pip value in account currency per unit held.

        Critical for correct position sizing on non-USD-quote pairs:
          USD/JPY: pip = 0.01 JPY → must convert to USD (÷ current price)
          EUR/GBP: pip = 0.0001 GBP → must fetch GBP/USD and multiply
          EUR/USD: pip = 0.0001 USD → no conversion needed (quote IS USD)

        Without this, USD/JPY sizing is off by ~100x.
        """
        inst = self._instrument_cache.get(instrument)
        if inst is None:
            # Reload and retry once
            self._load_instruments()
            inst = self._instrument_cache.get(instrument)

        if inst is None:
            # Last-resort fallback — better than crashing
            pip_val = (0.01 / current_mid) if "JPY" in instrument and current_mid and current_mid > 0 else 0.0001
            log.warning(f'{instrument}: no metadata — using fallback pip value {pip_val:.8f}')
            return pip_val

        pip_loc        = int(inst.get('pipLocation', -4))
        pip_size       = 10 ** pip_loc
        parts          = instrument.split('_')
        base_currency  = parts[0] if len(parts) >= 1 else ''
        quote_currency = parts[1] if len(parts) >= 2 else ''

        # Pip value in quote currency (always pip_size per unit)
        pip_value_quote = pip_size

        # Case 1: quote IS the account currency (EUR/USD, GBP/USD, etc.)
        if quote_currency == account_currency:
            return pip_value_quote

        # Case 2: base IS the account currency (USD/JPY, USD/CAD, etc.)
        # Pip is in quote; convert back to USD via 1/price
        if base_currency == account_currency:
            if current_mid <= 0:
                return pip_value_quote
            return pip_value_quote / current_mid

        # Case 3: cross pair (EUR/GBP, EUR/CHF, etc.)
        # Fetch quote_currency/USD conversion rate
        _USD_BASE_QUOTES = {'JPY', 'CHF', 'CAD', 'HKD', 'SGD', 'DKK', 'NOK', 'SEK', 'ZAR', 'MXN'}

        def _mid(pair: str) -> Optional[float]:
            try:
                prices = self.get_prices([pair])
                px = prices.get(pair)
                if px:
                    return (px['bid'] + px['ask']) / 2.0
            except Exception as exc:
                log.debug(f'Conversion lookup failed for {pair}: {exc}')
            return None

        # OANDA quotes USD_X for currencies where USD is the base
        if quote_currency in _USD_BASE_QUOTES:
            first_pair  = f'{account_currency}_{quote_currency}'
            second_pair = f'{quote_currency}_{account_currency}'
        else:
            first_pair  = f'{quote_currency}_{account_currency}'
            second_pair = f'{account_currency}_{quote_currency}'

        mid = _mid(first_pair)
        if mid:
            rate = mid if first_pair.startswith(quote_currency) else 1.0 / mid
            return pip_value_quote * rate

        mid_inv = _mid(second_pair)
        if mid_inv and mid_inv > 0:
            rate = mid_inv if second_pair.startswith(quote_currency) else 1.0 / mid_inv
            return pip_value_quote * rate

        log.warning(f'{instrument}: could not convert {quote_currency} → {account_currency}, using raw pip value')
        return pip_value_quote

    def get_live_spread(self, instrument: str) -> Optional[float]:
        """
        Returns current bid/ask spread in price units.
        Used by signal engine to calculate OTR ratio.
        """
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/pricing"
            params = {'instruments': instrument}
            r = requests.get(url, headers=self.headers, params=params, timeout=5)
            r.raise_for_status()
            prices = r.json().get('prices', [])
            if prices:
                asks = float(prices[0]['asks'][0]['price'])
                bids = float(prices[0]['bids'][0]['price'])
                return asks - bids
        except Exception as e:
            log.warning(f"Spread fetch failed {instrument}: {e}")
        return None

    # ----------------------------------------------------------
    # BAR DATA — THE CORE NEED FOR MR. SCROOGE
    # ----------------------------------------------------------

    def get_candles(self,
                     instrument: str,
                     granularity: str,
                     count: int = 50) -> Optional[pd.DataFrame]:
        """
        Fetches OHLCV bars for any timeframe.

        granularity options:
          'M5'  → 5-minute bars  (signal confirmation)
          'H1'  → 1-hour bars    (magic lines)
          'D'   → daily bars     (PDH/PDL box + ATR)

        Returns DataFrame with columns: open, high, low, close, volume
        Index is datetime (UTC).
        """
        try:
            url = f"{self.base_url}/v3/instruments/{instrument}/candles"
            params = {
                'granularity': granularity,
                'count': count,
                'price': 'M'  # midpoint candles
            }
            r = requests.get(url, headers=self.headers, params=params, timeout=10)
            r.raise_for_status()

            candles = r.json().get('candles', [])
            if not candles:
                return None

            rows = []
            for c in candles:
                rows.append({
                    'time': pd.to_datetime(c['time']),
                    'open': float(c['mid']['o']),
                    'high': float(c['mid']['h']),
                    'low': float(c['mid']['l']),
                    'close': float(c['mid']['c']),
                    'volume': int(c.get('volume', 0))
                })

            if not rows:
                return None

            df = pd.DataFrame(rows)
            df.set_index('time', inplace=True)
            return df

        except Exception as e:
            log.error(f"Candle fetch failed {instrument} {granularity}: {e}")
            return None

    def get_daily_bars(self, instrument: str, count: int = 20) -> Optional[pd.DataFrame]:
        """Daily bars for PDH/PDL box and ATR calculation."""
        return self.get_candles(instrument, 'D', count)

    def get_hourly_bars(self, instrument: str, count: int = 50) -> Optional[pd.DataFrame]:
        """Hourly bars for magic line detection."""
        return self.get_candles(instrument, 'H1', count)

    def get_30m_bars(self, instrument: str, count: int = 100) -> Optional[pd.DataFrame]:
        """30-minute bars for experimental rolling box."""
        return self.get_candles(instrument, 'M30', count)

    def get_5m_bars(self, instrument: str, count: int = 50) -> Optional[pd.DataFrame]:
        """5-minute bars for pitchfork/signal confirmation."""
        return self.get_candles(instrument, 'M5', count)

    def get_recent_bars(self,
                         instrument: str,
                         count: int = 50,
                         granularity: str = 'M5') -> Optional[pd.DataFrame]:
        """Generic recent bars fetch — alias for compatibility."""
        return self.get_candles(instrument, granularity, count)

    # ----------------------------------------------------------
    # ACCOUNT STATE
    # ----------------------------------------------------------

    def get_account_summary(self) -> Optional[Dict]:
        """Returns full account summary including equity, margin, NAV."""
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/summary"
            r = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json().get('account', {})
        except Exception as e:
            log.error(f"Account summary failed: {e}")
            return None

    def get_equity(self) -> Optional[float]:
        """Returns account balance (cash only, excludes unrealized P&L)."""
        summary = self.get_account_summary()
        if summary:
            return float(summary.get('balance', 0))
        return None

    def get_margin_used(self) -> Optional[float]:
        summary = self.get_account_summary()
        if summary:
            return float(summary.get('marginUsed', 0))
        return None

    def get_free_margin(self) -> Optional[float]:
        """Returns available margin (marginAvailable) from OANDA account summary."""
        summary = self.get_account_summary()
        if summary:
            return float(summary.get('marginAvailable', 0))
        return None

    def get_open_trades(self) -> Optional[List[Dict]]:
        """Returns all open trades on the OANDA account, or None on API error.
        Callers must treat None as unknown — not as an empty list."""
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/openTrades"
            r = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json().get('trades', [])
        except Exception as e:
            log.error(f"Open trades fetch failed: {e}")
            return None

    def get_trade_realized_pl(self, trade_id: str) -> Optional[float]:
        """
        Returns realized PnL for a specific trade from OANDA.
        Useful when a trade is closed natively (SL/TP/manual) between bot cycles.
        Falls back to transactions API if trade endpoint returns 404 (closed trades).
        """
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/trades/{trade_id}"
            r = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            trade = r.json().get('trade', {})
            realized = trade.get('realizedPL')
            if realized is None:
                return None
            return float(realized)
        except Exception as e:
            # Fallback: search recent ORDER_FILL transactions for this trade (audit fix 2026-04-09)
            try:
                from datetime import datetime, timezone, timedelta
                since = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%S.000000000Z')
                tx_url = f"{self.base_url}/v3/accounts/{self.account_id}/transactions"
                tx_r = requests.get(tx_url, headers=self.headers, params={
                    'from': since, 'type': 'ORDER_FILL'
                }, timeout=15)
                if tx_r.status_code == 200:
                    pages = tx_r.json().get('pages', [])
                    for page_url in pages:
                        page_r = requests.get(page_url, headers=self.headers, timeout=15)
                        if page_r.status_code != 200:
                            continue
                        for tx in page_r.json().get('transactions', []):
                            closed = tx.get('tradesClosed', [])
                            for tc in closed:
                                if str(tc.get('tradeID')) == str(trade_id):
                                    pl = tx.get('pl')
                                    if pl is not None:
                                        log.info(f"Trade {trade_id} PnL recovered from transactions: {pl}")
                                        return float(pl)
            except Exception as e2:
                log.warning(f"Trade {trade_id} PnL fallback also failed: {e2}")
            log.warning(f"Trade realized PnL fetch failed for {trade_id}: {e}")
            return None

    def get_daily_realized_pl(self) -> float:
        """Returns total realized P/L for today (since midnight UTC) from OANDA transactions."""
        from datetime import datetime, timezone
        try:
            today = datetime.now(timezone.utc).strftime('%Y-%m-%dT00:00:00.000000000Z')
            url = (f"{self.base_url}/v3/accounts/{self.account_id}/transactions"
                   f"?from={today}&type=ORDER_FILL")
            r = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            pages = r.json().get('pages', [])
            total_pl = 0.0
            for page_url in pages:
                pr = requests.get(page_url, headers=self.headers, timeout=10)
                pr.raise_for_status()
                for tx in pr.json().get('transactions', []):
                    pl = tx.get('pl', '0')
                    total_pl += float(pl)
            return round(total_pl, 2)
        except Exception as e:
            log.warning(f"Daily realized PL fetch failed: {e}")
            return 0.0

    def get_alltime_realized_pl(self) -> float:
        """Returns all-time realized P/L from OANDA account summary."""
        summary = self.get_account_summary()
        if summary:
            return round(float(summary.get('pl', 0)), 2)
        return 0.0

    def get_combined_margin_pct(self) -> float:
        """
        Returns margin used as % of equity.
        Used to check overall account leverage before new entries.
        """
        try:
            summary = self.get_account_summary()
            if summary:
                margin_used = float(summary.get('marginUsed', 0))
                balance = float(summary.get('balance', 1))
                return margin_used / balance if balance > 0 else 1.0
        except:
            pass
        return 0.0

    # ----------------------------------------------------------
    # ORDER EXECUTION
    # ----------------------------------------------------------

    def place_limit_order(self,
                           instrument: str,
                           side: str,
                           units: float,
                           price: float,
                           take_profit: float,
                           stop_loss: float,
                           client_trade_id: str) -> Dict:
        """
        Places a limit order — primary execution method.
        Mr. Scrooge always tries limit orders first to avoid slippage.

        side: 'BUY' or 'SELL'
        units: positive number (sign determined by side)
        price: limit price
        """
        signed_units = units if side == 'BUY' else -units

        order_body = {
            "order": {
                "type": "LIMIT",
                "instrument": instrument,
                "units": str(signed_units),
                "price": str(round(price, 5)),
                "timeInForce": "GTC",
                "takeProfitOnFill": {
                    "price": str(round(take_profit, 5))
                },
                "stopLossOnFill": {
                    "price": str(round(stop_loss, 5))
                },
                "clientExtensions": {
                    "id": client_trade_id,
                    "comment": "mr-scrooge"
                }
            }
        }

        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/orders"
            r = requests.post(url, headers=self.headers, json=order_body, timeout=10)
            r.raise_for_status()
            result = r.json()
            order_id = result.get('orderCreateTransaction', {}).get('id', 'unknown')
            log.info(
                f"Limit order placed: {instrument} {side} {units:.4f} @ {price:.5f} "
                f"| TP: {take_profit:.5f} SL: {stop_loss:.5f} | Order ID: {order_id}"
            )
            return {'success': True, 'order_id': order_id, 'raw': result}
        except Exception as e:
            log.error(f"Limit order failed {instrument}: {e}")
            return {'success': False, 'error': str(e)}

    def place_entry_order(self,
                           instrument: str,
                           side: str,
                           units: float,
                           take_profit: float,
                           stop_loss: float,
                           client_trade_id: str) -> Dict:
        """
        Market order for entries — fills immediately at current bid/ask.
        Used instead of limit orders: OANDA cancels limit orders placed at/near current price
        (WOULD_TRIGGER_IMMEDIATELY). Market orders fill instantly; cost is just the spread,
        which is already factored into the OTR calculation.
        """
        signed_units = int(round(units)) if side == 'BUY' else -int(round(units))
        decimals = 3 if (stop_loss > 50 or 'JPY' in instrument) else 5

        order_body = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(signed_units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "takeProfitOnFill": {
                    "price": f"{take_profit:.{decimals}f}"
                },
                "stopLossOnFill": {
                    "price": f"{stop_loss:.{decimals}f}"
                },
                "tradeClientExtensions": {
                    "id": client_trade_id,
                    "comment": "mr-scrooge"
                }
            }
        }

        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/orders"
            r = requests.post(url, headers=self.headers, json=order_body, timeout=10)
            r.raise_for_status()
            result = r.json()

            fill_tx = result.get('orderFillTransaction')
            if not fill_tx:
                # Order was created but not filled — extract cancellation reason
                cancel_tx = result.get('orderCancelTransaction', {})
                reject_tx = result.get('orderRejectTransaction', {})
                reason = (
                    cancel_tx.get('reason')
                    or reject_tx.get('rejectReason')
                    or 'unknown'
                )
                log.error(
                    f"Entry order NOT filled: {instrument} {side} | "
                    f"Reason: {reason} | Raw: {result}"
                )
                return {'success': False, 'error': reason, 'raw': result}

            fill_price     = float(fill_tx.get('price', 0))
            oanda_trade_id = str(fill_tx.get('tradeOpened', {}).get('tradeID', ''))
            log.info(
                f"Entry order filled: {instrument} {side} {units:.4f} @ {fill_price:.5f} "
                f"| TP: {take_profit:.5f} SL: {stop_loss:.5f} | ID: {client_trade_id} "
                f"| OANDA trade ID: {oanda_trade_id}"
            )
            return {'success': True, 'fill_price': fill_price, 'oanda_trade_id': oanda_trade_id, 'raw': result}
        except Exception as e:
            body = ""
            if hasattr(e, "response") and e.response is not None:
                try:
                    body = e.response.text
                except Exception:
                    pass
            log.error(f"Entry order failed {instrument}: {e} | body: {body}")
            return {'success': False, 'error': str(e), 'body': body}

    def place_market_order(self,
                            instrument: str,
                            side: str,
                            units: float,
                            stop_loss: float,
                            client_trade_id: str,
                            reason: str = 'emergency') -> Dict:
        """
        Emergency market order — used for exits only.
        Logs cost penalty vs limit order for post-analysis.
        """
        log.warning(
            f"MARKET ORDER (emergency) — {instrument} {side} {units:.4f} | "
            f"Reason: {reason} | Elevated taker fees apply"
        )

        signed_units = int(round(units)) if side == 'BUY' else -int(round(units))
        decimals = 3 if (stop_loss > 50 or 'JPY' in instrument) else 5

        order_body = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(signed_units),
                "timeInForce": "FOK",
                "positionFill": "REDUCE_ONLY",
            }
        }

        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/orders"
            r = requests.post(url, headers=self.headers, json=order_body, timeout=10)
            r.raise_for_status()
            result = r.json()
            fill_tx = result.get('orderFillTransaction', {}) or {}
            fill_price = float(fill_tx.get('price', 0))
            realized_pl = None
            try:
                # OANDA includes realized PnL components on fill transactions for closes.
                pl = float(fill_tx.get('pl', 0.0))
                financing = float(fill_tx.get('financing', 0.0))
                commission = float(fill_tx.get('commission', 0.0))
                realized_pl = pl + financing - commission
            except Exception:
                realized_pl = None
            log.warning(
                f"Market order filled @ {fill_price:.5f} | "
                f"Note: taker cost vs limit cost should be analyzed post-session"
            )
            return {'success': True, 'fill_price': fill_price, 'realized_pl': realized_pl, 'raw': result}
        except Exception as e:
            log.error(f"Market order failed {instrument}: {e}")
            return {'success': False, 'error': str(e)}

    def cancel_order(self, order_id: str) -> bool:
        """Cancels an unfilled limit order (e.g. price left zone)."""
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/orders/{order_id}/cancel"
            r = requests.put(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            log.info(f"Order {order_id} cancelled — price left zone")
            return True
        except Exception as e:
            log.error(f"Cancel failed for order {order_id}: {e}")
            return False

    def modify_trade_stop(self, trade_id: str, new_stop: float, instrument: str = '') -> bool:
        """Moves stop-loss on an open trade (for trailing stop and break-even)."""
        try:
            decimals = 3 if (new_stop > 50 or 'JPY' in instrument) else 5
            url = (f"{self.base_url}/v3/accounts/{self.account_id}"
                   f"/trades/{trade_id}/orders")
            body = {
                "stopLoss": {
                    "price": f"{new_stop:.{decimals}f}",
                    "timeInForce": "GTC"
                }
            }
            r = requests.put(url, headers=self.headers, json=body, timeout=10)
            r.raise_for_status()
            log.info(f"Stop moved: trade {trade_id} → {new_stop:.5f}")
            return True
        except Exception as e:
            log.error(f"Stop modify failed trade {trade_id}: {e}")
            return False

    def modify_trade_tp(self, trade_id: str, tp_price: float) -> bool:
        """Sets / updates take-profit on an open trade (green exit LIMIT order)."""
        try:
            url = (f"{self.base_url}/v3/accounts/{self.account_id}"
                   f"/trades/{trade_id}/orders")
            body = {
                "takeProfit": {
                    "price": str(round(tp_price, 5)),
                    "timeInForce": "GTC"
                }
            }
            r = requests.put(url, headers=self.headers, json=body, timeout=10)
            r.raise_for_status()
            log.info(f"TP set: trade {trade_id} → {tp_price:.5f}")
            return True
        except Exception as e:
            log.error(f"TP modify failed trade {trade_id}: {e}")
            return False

    def place_opposing_limit(self, instrument: str, close_side: str,
                              units: float, price: float,
                              client_id: str, expire_seconds: int = 90) -> dict:
        """Place opposing limit order to close part of a position (maker fill)."""
        from datetime import timedelta
        gtd = (datetime.now(timezone.utc) + timedelta(seconds=expire_seconds)).strftime(
            '%Y-%m-%dT%H:%M:%S.000000000Z')
        signed = int(round(units)) if close_side == 'BUY' else -int(round(units))
        decimals = 3 if (price > 50 or 'JPY' in instrument) else 5
        body = {"order": {
            "type": "LIMIT",
            "instrument": instrument,
            "units": str(signed),
            "price": f"{price:.{decimals}f}",
            "timeInForce": "GTD",
            "gtdTime": gtd,
            "positionFill": "REDUCE_ONLY",
            "clientExtensions": {"id": f"harvest_{client_id[:20]}", "comment": "mr-scrooge-harvest"}
        }}
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/orders"
            r = requests.post(url, headers=self.headers, json=body, timeout=10)
            r.raise_for_status()
            result = r.json()
            oid = result.get('orderCreateTransaction', {}).get('id', '')
            fill_tx = result.get('orderFillTransaction')
            cancel_tx = result.get('orderCancelTransaction')
            if fill_tx:
                fp = float(fill_tx.get('price', 0))
                rpl = float(fill_tx.get('pl', 0)) + float(fill_tx.get('financing', 0))
                log.info(f"Limit close IMMEDIATE fill: {instrument} {close_side} {units:.0f} @ {fp:.5f}")
                return {'success': True, 'filled': True, 'order_id': oid,
                        'fill_price': fp, 'realized_pl': rpl, 'raw': result}
            if cancel_tx:
                cancel_reason = cancel_tx.get('reason', 'UNKNOWN')
                no_position = cancel_reason == 'NO_POSITION_TO_REDUCE'
                log.warning(f"Limit close CANCELLED immediately: {instrument} {close_side} {units:.0f} | reason={cancel_reason}")
                return {'success': False, 'error': cancel_reason, 'no_position': no_position, 'raw': result}
            log.info(f"Limit close placed: {instrument} {close_side} {units:.0f} @ {price:.{decimals}f} | order {oid}")
            return {'success': True, 'filled': False, 'order_id': oid, 'raw': result}
        except Exception as e:
            log.error(f"Opposing limit failed {instrument}: {e}")
            return {'success': False, 'error': str(e)}

    def close_trade_partial(self,
                             trade_id: str,
                             units: float) -> Dict:
        """Partially closes an open trade (for partial take-profit)."""
        try:
            url = (f"{self.base_url}/v3/accounts/{self.account_id}"
                   f"/trades/{trade_id}/close")
            body = {"units": str(units)}
            r = requests.put(url, headers=self.headers, json=body, timeout=10)
            r.raise_for_status()
            result = r.json()
            fill_tx = result.get('orderFillTransaction', {}) or {}
            fill = float(fill_tx.get('price', 0))
            realized_pl = None
            try:
                pl = float(fill_tx.get('pl', 0.0))
                financing = float(fill_tx.get('financing', 0.0))
                commission = float(fill_tx.get('commission', 0.0))
                realized_pl = pl + financing - commission
            except Exception:
                realized_pl = None
            log.info(f"Partial close: trade {trade_id} | {units:.4f} units @ {fill:.5f}")
            return {'success': True, 'fill_price': fill, 'realized_pl': realized_pl, 'raw': result}
        except Exception as e:
            log.error(f"Partial close failed trade {trade_id}: {e}")
            return {'success': False, 'error': str(e)}

    def check_order_filled(self, order_id: str) -> Dict:
        """Polls a pending limit order to see if it filled."""
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}/orders/{order_id}"
            r = requests.get(url, headers=self.headers, timeout=5)
            r.raise_for_status()
            order = r.json().get('order', {})
            state = order.get('state', 'UNKNOWN')
            return {
                'filled': state == 'FILLED',
                'cancelled': state == 'CANCELLED',
                'pending': state == 'PENDING',
                'state': state
            }
        except Exception as e:
            log.error(f"Order status check failed {order_id}: {e}")
            return {'filled': False, 'state': 'ERROR'}

    def get_position_book(self, instrument: str):
        """Returns % of OANDA retail clients long/short on this instrument."""
        try:
            url  = f'{self.base_url}/v3/instruments/{instrument}/positionBook'
            r    = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            buckets     = r.json().get('positionBook', {}).get('buckets', [])
            total_long  = sum(float(b.get('longCountPercent',  0)) for b in buckets)
            total_short = sum(float(b.get('shortCountPercent', 0)) for b in buckets)
            total = total_long + total_short
            if total <= 0:
                return None
            return {
                'pct_long':  round(total_long  / total, 3),
                'pct_short': round(total_short / total, 3),
            }
        except Exception:
            return None

    def get_order_book_summary(self, instrument: str, current_price: float,
                                pdh: float, pdl: float, pip_size: float,
                                proximity_pips: float = 20.0):
        """Returns % of clustered pending orders near PDH and PDL."""
        try:
            url  = f'{self.base_url}/v3/instruments/{instrument}/orderBook'
            r    = requests.get(url, headers=self.headers, timeout=10)
            r.raise_for_status()
            buckets   = r.json().get('orderBook', {}).get('buckets', [])
            proximity = proximity_pips * pip_size
            near_pdh = near_pdl = total = 0.0
            for b in buckets:
                price      = float(b.get('price', 0))
                bucket_pct = float(b.get('longCountPercent', 0)) + float(b.get('shortCountPercent', 0))
                total     += bucket_pct
                if abs(price - pdh) <= proximity:
                    near_pdh += bucket_pct
                if abs(price - pdl) <= proximity:
                    near_pdl += bucket_pct
            if total <= 0:
                return None
            return {
                'near_pdh_pct': round(near_pdh / total, 4),
                'near_pdl_pct': round(near_pdl / total, 4),
            }
        except Exception:
            return None
