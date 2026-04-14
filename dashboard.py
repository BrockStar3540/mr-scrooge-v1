#!/usr/bin/env python3
# ============================================================
# Copyright (c) Carl Brock 2026. All rights reserved.
# Use, reproduction, modification, or distribution of this
# software is permitted only with the express written
# permission of Carl Brock.
# ============================================================
"""
Scrooge Dashboard — Web server for monitoring Mr. Scrooge.
No external dependencies — uses Python stdlib only.

Run:  python3 dashboard.py
Open: http://<host>:8081
"""

import os
import sys
import json
import subprocess
import http.server
import socketserver
from urllib.parse import urlparse

PORT = 8081
_DASH_USER = os.environ.get('DASH_USER', 'brock')
_DASH_PASS = os.environ.get('DASH_PASS', '')

BOT_DIR          = os.path.dirname(os.path.abspath(__file__))
HARVESTER_CONFIG = os.path.join(BOT_DIR, '..', 'mr-harvester', 'harvester_config.json')

sys.path.insert(0, BOT_DIR)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'  # force Connection: close — prevents FD exhaustion

    def log_message(self, fmt, *args):
        pass  # Suppress default access logs

    def do_GET(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path

        if path in ('/', '/dashboard'):
            self._serve_file('dashboard.html', 'text/html')

        elif path == '/api/status':
            self._serve_json_file('data/status.json')

        elif path == '/api/config':
            self._serve_config()

        elif path == '/api/timeframe':
            self._serve_timeframe()

        elif path == '/api/logs':
            self._serve_logs()

        elif path == '/api/harvester-config':
            self._serve_harvester_config()

        elif path == '/api/regime-summary':
            self._serve_regime_summary()

        elif path == '/api/execution-audit':
            self._serve_execution_audit()

        elif path == '/api/trade-quality':
            self._serve_trade_quality()

        else:
            self.send_error(404)

    def do_POST(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path

        if path == '/api/restart':
            self._restart_bot()

        elif path == '/api/toggle':
            self._toggle_trading()

        elif path == '/api/timeframe':
            self._set_timeframe()

        elif path == '/api/harvester-config':
            self._save_harvester_config()

        else:
            self.send_error(404)

    # ── helpers ──────────────────────────────────────────────

    def _require_auth(self):
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Basic '):
            try:
                import base64
                creds = base64.b64decode(auth[6:]).decode()
                if creds == f'{_DASH_USER}:{_DASH_PASS}':
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="OpenClaw"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def _serve_file(self, name, ctype):
        fpath = os.path.join(BOT_DIR, name)
        try:
            with open(fpath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_json_file(self, name):
        fpath = os.path.join(BOT_DIR, name)
        try:
            with open(fpath) as f:
                data = json.load(f)
            self._json(data)
        except (FileNotFoundError, json.JSONDecodeError):
            self._json({})

    def _serve_config(self):
        """Return CONFIG dict from main.py by AST-parsing its source (no import side-effects)."""
        try:
            import ast
            main_path = os.path.join(BOT_DIR, 'main.py')
            with open(main_path) as f:
                source = f.read()
            tree = ast.parse(source)
            config = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == 'CONFIG':
                            try:
                                config = ast.literal_eval(node.value)
                            except Exception:
                                pass
                            break

            # Apply config.json runtime overrides
            json_path = os.path.join(BOT_DIR, 'config.json')
            if os.path.exists(json_path):
                with open(json_path) as f2:
                    overrides = json.load(f2)
                if overrides.get('globals'):
                    config.update(overrides['globals'])

            self._json(config)
        except Exception as e:
            self._json({'error': str(e)})

    def _serve_logs(self):
        log_path = os.path.join(BOT_DIR, 'logs', 'mrscrooge.log')
        try:
            with open(log_path) as f:
                lines = f.readlines()
            self._json({'lines': lines[-150:]})
        except Exception:
            self._json({'lines': []})

    def _toggle_trading(self):
        """Flip trading_enabled in config.json and return new state."""
        try:
            json_path = os.path.join(BOT_DIR, 'config.json')
            data = {}
            if os.path.exists(json_path):
                with open(json_path) as f:
                    data = json.load(f)
            if 'globals' not in data:
                data['globals'] = {}
            current = data['globals'].get('trading_enabled', True)
            data['globals']['trading_enabled'] = not current
            with open(json_path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            state = 'LIVE' if not current else 'PAUSED'
            self._json({'status': 'ok', 'trading_enabled': not current,
                        'message': f'Trading {state}'})
        except Exception as e:
            self._json({'status': 'error', 'message': str(e)})

    def _serve_harvester_config(self):
        try:
            if os.path.exists(HARVESTER_CONFIG):
                with open(HARVESTER_CONFIG) as f:
                    self._json(json.load(f))
            else:
                self._json({
                    'tier_equity_pcts':    [0.015, 0.030, 0.045],
                    'sl_lock_equity_pcts': [0.0,   0.015, 0.030],
                    'close_pcts':          [0.25,  0.25,  0.25],
                    'pair_pip_overrides':  {}
                })
        except Exception as e:
            self._json({'error': str(e)})

    def _save_harvester_config(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            cfg    = json.loads(body)
            tmp    = HARVESTER_CONFIG + '.tmp'
            os.makedirs(os.path.dirname(HARVESTER_CONFIG), exist_ok=True)
            with open(tmp, 'w') as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, HARVESTER_CONFIG)
            self._json({'status': 'ok'})
        except Exception as e:
            self._json({'status': 'error', 'message': str(e)})

    def _restart_bot(self):
        try:
            # Kill existing bot
            subprocess.run(['pkill', '-f', 'python3 main.py'], cwd=BOT_DIR)
            import time; time.sleep(2)
            # Clear caches and lock
            subprocess.run(['find', '.', '-name', '__pycache__', '-exec',
                            'rm', '-rf', '{}', '+'],
                           cwd=BOT_DIR, stderr=subprocess.DEVNULL)
            lock = os.path.join(BOT_DIR, 'logs', 'scrooge.lock')
            if os.path.exists(lock):
                os.remove(lock)
            # Start fresh
            log_file = os.path.join(BOT_DIR, 'logs', 'mrscrooge.log')
            log_fd = open(log_file, 'a')
            subprocess.Popen(
                [os.path.expanduser('~/mr_scrooge_env/bin/python3'), 'main.py'],
                cwd=BOT_DIR,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            log_fd.close()
            self._json({'status': 'ok', 'message': 'Bot restarted'})
        except Exception as e:
            self._json({'status': 'error', 'message': str(e)})

    TIMEFRAME_PRESETS = {
        '30m': {'box_timeframe': '30m', 'box_30m_lookback': 24,
                'sl_pips': 10, 'tp1_pips': 5, 'tp2_pips': 30},
        '1h':  {'box_timeframe': 'daily', 'box_30m_lookback': 8,
                'sl_pips': 10, 'tp1_pips': 5, 'tp2_pips': 30},
    }

    def _serve_timeframe(self):
        json_path = os.path.join(BOT_DIR, 'config.json')
        tf = '30m'
        try:
            if os.path.exists(json_path):
                with open(json_path) as f:
                    data = json.load(f)
                bt = data.get('globals', {}).get('box_timeframe', '30m')
                tf = '30m' if bt == '30m' else '1h'
        except Exception:
            pass
        self._json({'timeframe': tf})

    def _set_timeframe(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            incoming = json.loads(body)
            tf = incoming.get('timeframe', '30m')
            if tf not in self.TIMEFRAME_PRESETS:
                self._json({'status': 'error', 'message': f'Unknown timeframe: {tf}'})
                return
            preset = self.TIMEFRAME_PRESETS[tf]
            json_path = os.path.join(BOT_DIR, 'config.json')
            data = {}
            if os.path.exists(json_path):
                with open(json_path) as f:
                    data = json.load(f)
            if 'globals' not in data:
                data['globals'] = {}
            data['globals'].update(preset)
            with open(json_path, 'w') as f:
                json.dump(data, f, indent=2)
            # Restart bot via systemd (avoids duplicate processes)
            import subprocess
            subprocess.run(['systemctl', '--user', 'restart', 'mr-scrooge.service'])
            self._json({'status': 'ok', 'timeframe': tf,
                        'message': f'Switched to {tf.upper()} mode — bot restarting'})
        except Exception as e:
            self._json({'status': 'error', 'message': str(e)})

    def _serve_regime_summary(self):
        """Aggregate regime-protests.jsonl into summary stats."""
        path = os.path.join(BOT_DIR, 'data', 'regime-protests.jsonl')
        try:
            with open(path) as f:
                lines = f.readlines()
            # Only look at recent entries (last 200)
            recent = []
            for line in lines[-200:]:
                try:
                    recent.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

            # Aggregate by latest state per symbol
            by_sym = {}
            for r in recent:
                sym = r.get('symbol', '')
                by_sym[sym] = {
                    'state': r.get('regime', 'UNKNOWN'),
                    'score': r.get('score', 0),
                    'signals': [s.get('name', '') for s in r.get('signals', [])],
                }

            blocked = sum(1 for v in by_sym.values() if v['state'] == 'BLOCKED')
            restricted = sum(1 for v in by_sym.values() if v['state'] == 'RESTRICTED')
            clean = max(0, len(by_sym) - blocked - restricted)
            scores = [v['score'] for v in by_sym.values()]
            avg_score = sum(scores) / len(scores) if scores else 0

            # Dominant signal
            from collections import Counter
            all_sigs = []
            for v in by_sym.values():
                all_sigs.extend(v['signals'])
            sig_counts = Counter(all_sigs)
            dominant = sig_counts.most_common(1)[0][0] if sig_counts else 'none'

            self._json({
                'blocked': blocked, 'restricted': restricted, 'clean': clean,
                'avg_score': round(avg_score, 1), 'dominant_signal': dominant,
                'by_symbol': by_sym,
            })
        except FileNotFoundError:
            self._json({'blocked': 0, 'restricted': 0, 'clean': 0, 'avg_score': 0, 'dominant_signal': 'none', 'by_symbol': {}})

    def _serve_trade_quality(self):
        """Compute trade quality stats from entry-context.jsonl."""
        path = os.path.join(BOT_DIR, 'data', 'entry-context.jsonl')
        try:
            with open(path) as f:
                entries = [json.loads(line) for line in f if line.strip()]

            with_outcome = [e for e in entries if e.get('outcome_pnl') is not None]
            wins = [e for e in with_outcome if e['outcome_pnl'] > 0]
            mfes = [e['outcome_mfe_pips'] for e in with_outcome if e.get('outcome_mfe_pips') is not None]
            maes = [e['outcome_mae_pips'] for e in with_outcome if e.get('outcome_mae_pips') is not None]

            total = len(with_outcome)
            win_rate = len(wins) / total * 100 if total > 0 else 0
            avg_mfe = sum(mfes) / len(mfes) if mfes else 0
            avg_mae = sum(maes) / len(maes) if maes else 0
            ratio = avg_mfe / avg_mae if avg_mae > 0 else 0

            self._json({
                'total': total, 'wins': len(wins), 'win_rate': round(win_rate, 1),
                'avg_mfe': round(avg_mfe, 1), 'avg_mae': round(avg_mae, 1),
                'mfe_mae_ratio': round(ratio, 2),
                'total_entries': len(entries),
                'recent': entries[-10:],
            })
        except FileNotFoundError:
            self._json({'total': 0, 'wins': 0, 'win_rate': 0, 'avg_mfe': 0, 'avg_mae': 0, 'mfe_mae_ratio': 0, 'total_entries': 0, 'recent': []})

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    with ReusableTCPServer(('127.0.0.1', PORT), Handler) as httpd:
        print(f'Scrooge Dashboard on http://127.0.0.1:{PORT}')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nDashboard stopped')
