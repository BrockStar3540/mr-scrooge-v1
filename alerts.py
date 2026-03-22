# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
mr-scrooge-v2 — Alerts
Drops JSON messages into Claudia's outbox for Slack delivery.
"""

import os
import json
import time
import logging

log = logging.getLogger('mr-scrooge-v2.alerts')

_CLAUDIA_OUTBOX = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'claudia', 'outbox')
)

_FRANK_OUTBOX = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'frank', 'outbox')
)


def send_slack_alert(text: str, channel: str = '#mr-scrooge') -> bool:
    """Drop a Slack message into Claudia's outbox."""
    try:
        os.makedirs(_CLAUDIA_OUTBOX, exist_ok=True)
        filename = f'{int(time.time() * 1000)}-scrooge-v2-alert.json'
        path = os.path.join(_CLAUDIA_OUTBOX, filename)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'text': text, 'channel': channel}, fh)
        return True
    except Exception as exc:
        log.warning(f'Claudia alert failed: {exc}')
        return False


def send_frank_alert(text: str) -> bool:
    """Drop a message into Frank's outbox (Telegram trading monitor)."""
    try:
        os.makedirs(_FRANK_OUTBOX, exist_ok=True)
        filename = f'{int(time.time() * 1000)}-scrooge-v2-frank.json'
        path = os.path.join(_FRANK_OUTBOX, filename)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'text': text}, fh)
        return True
    except Exception as exc:
        log.warning(f'Frank alert failed: {exc}')
        return False


def send_trade_alert(symbol: str, direction: str, units: int,
                     fill_price: float, sl: float, tp1: float, tp2: float,
                     signal_type: str = 'unknown'):
    """Convenience wrapper — formats and sends a trade entry alert."""
    text = (
        f'📈 *{symbol}* {direction} `{units:,}` units @ `{fill_price:.5f}`\n'
        f'SL `{sl:.5f}` | TP1 `{tp1:.5f}` | TP2 `{tp2:.5f}`\n'
        f'Signal: `{signal_type}`'
    )
    send_slack_alert(text)


def send_close_alert(symbol: str, direction: str, pnl: float, reason: str):
    """Convenience wrapper — formats and sends a trade close alert."""
    emoji = '✅' if pnl >= 0 else '🔴'
    text = (
        f'{emoji} *{symbol}* {direction} closed | PnL: `{pnl:+.4f}` | {reason}'
    )
    send_slack_alert(text)
