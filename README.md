# Mr. Scrooge — OANDA Forex Box Reversal Bot

Mr. Scrooge is an automated forex trading bot that trades mean-reversion setups on major forex pairs using the prior day's high/low range as a reference framework, running on a 5-minute candle cycle against the OANDA v20 REST API.

---

## Strategy Overview

The core idea is simple: forex pairs tend to oscillate around the prior day's high and low rather than trend through them indefinitely. When price breaches the prior day's high or low and then stalls or reverses, that's the setup.

Each pair gets a "box" defined by the prior day's high (PDH) and prior day's low (PDL). The bot monitors for price entering a zone near one of those walls, waits for a reversal signal, then enters with a stop just beyond the wall and a take-profit targeting the opposite side of the box.

In plain English: if today's EUR/USD pushes up through yesterday's high and then shows a reversal candle pattern, the bot sells short with the expectation it returns toward the middle of the box.

---

## Requirements

- Python 3.10+
- OANDA account (practice or live — see note below)
- Minimum account size: **$2,000 USD**. Below this, the margin-based position sizing produces very small position sizes that may look like bot malfunctions. The bot will still function, but the economics become marginal.

### Python packages
```
requests
pandas
python-dotenv
```

---

## Installation

```bash
git clone <repo-url> mr-scrooge
cd mr-scrooge
pip install -r requirements.txt
```

---

## Configuration

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```
OANDA_API_KEY=your_oanda_api_key_here
OANDA_ACCOUNT_ID=your_oanda_account_id_here
OANDA_BASE_URL=https://api-fxtrade.oanda.com/v3
# Use https://api-fxpractice.oanda.com/v3 for practice accounts
```

To get your OANDA API key: log into your OANDA account → My Account → API Access → Generate Token.

Your account ID is the numeric string shown in the top-right of the OANDA trade portal (format: `001-001-XXXXXXX-001`).

---

## Running

### Direct
```bash
./start.sh
```

### As a systemd service (recommended for always-on deployment)
```bash
# Copy the service file to ~/.config/systemd/user/
systemctl --user start mr-scrooge.service
systemctl --user status mr-scrooge.service
```

### Logs
```
logs/mrscrooge.log   — main runtime log
logs/decisions.log   — entry/exit decision log
logs/trades.log      — trade fill log
```

---

## Architecture

| Component | Description |
|-----------|-------------|
| `main.py` | Boot sequence, main loop, daily reset, shutdown handling |
| `core/oanda_client.py` | OANDA v20 REST API wrapper (quotes, orders, positions) |
| `core/execution_router.py` | Order routing — limit entry, market fallback, partial close |
| `core/fee_calculator.py` | Spread-adjusted net profit calculations |
| `strategy/zone_detector.py` | Pulls prior-day OHLC, computes PDH/PDL, classifies zone proximity |
| `strategy/box_state_machine.py` | Per-pair state machine tracking box breach and reversal confirmation |
| `strategy/signal_engine.py` | Candlestick reversal pattern detection |
| `strategy/universe_screener.py` | Selects active pairs by session, ATR, and OTR gates |
| `portfolio/trade_manager.py` | Open position tracking, TP1/TP2 management, daily P&L |
| `dashboard.py` | Web dashboard backend (Flask) |
| `dashboard.html` | Dashboard UI — live positions, recent trades, config view |
| `status.py` | Structured status file writer (for Frank monitoring agent) |

---

## Mr. Harvester

Mr. Scrooge runs alongside a companion process called **mr-harvester**, which handles staged profit extraction on open positions.

Harvester tiers (calculated as equity % — self-adjusting as account grows):
- **Tier 1** (1.5% equity gain, ~10 pips): close 25% of position, advance stop to break-even
- **Tier 2** (3.0% equity gain, ~20 pips): close another 25%, advance stop to +Tier 1 level
- **Tier 3** (4.5% equity gain, ~30 pips): close another 25%, advance stop to +Tier 2 level
- **Runner**: remaining 25% rides to Scrooge's native TP2

Harvester polls OANDA every 30 seconds. State is persisted to disk — restarts do not double-harvest.

Per-pair pip overrides can be configured via the dashboard UI (stored in `harvester_config.json`).

---

## Dashboard

The dashboard is available at `http://localhost:8081` once `dashboard.py` is running. It shows live positions, recent trades, log viewer with filters, and harvester configuration controls.

Access requires HTTP Basic Auth. Set credentials in the dashboard config section of `main.py` or via environment variables.

---

## Key Config Values (in `main.py`)

| Parameter | Value | Description |
|-----------|-------|-------------|
| `margin_utilization_pct` | 0.50 | Deploy 50% of equity as margin per trade |
| `max_concurrent_positions` | 1 | One position at a time |
| `max_daily_loss_pct` | 0.075 | 7.5% daily halt |
| `min_risk_reward` | 1.60 | Minimum RR ratio to enter |
| `scan_interval_seconds` | 300 | 5-minute cycle |
| `max_hold_minutes` | 360 | 6-hour max hold |

---

## Deployment — The Bot Must Run 24/7

Trading bots miss setups and can leave positions unmanaged if the host goes offline. **The machine running Mr. Scrooge must stay on continuously.** You have two options:

---

### Option A — AWS EC2 (Recommended)

A cloud server is the cleanest solution. It runs 24/7 with no dependency on your home internet or power.

**Recommended instance:** `t3.small` or `t3.medium` (Ubuntu 22.04 LTS). Costs roughly $15–30/month.

**Setup steps:**

```bash
# 1. Launch an EC2 instance (Ubuntu 22.04) from the AWS console.
#    Open port 22 (SSH) in the security group. Keep all other ports closed.

# 2. SSH into the instance
ssh -i your-key.pem ubuntu@<your-ec2-ip>

# 3. Install Python and dependencies
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

# 4. Clone the repo
git clone <repo-url> mr-scrooge
cd mr-scrooge
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 5. Configure your .env
cp .env.example .env
nano .env   # fill in OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_BASE_URL

# 6. Run directly to verify
./start.sh
```

**Running as a persistent systemd service** (survives reboots, auto-restarts on crash):

```bash
# Create the service file
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/mr-scrooge.service << EOF
[Unit]
Description=Mr. Scrooge Forex Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/mr-scrooge
ExecStart=/home/ubuntu/mr-scrooge/start.sh
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF

# Enable lingering so the service runs without an active login session
loginctl enable-linger ubuntu

# Start and enable
systemctl --user daemon-reload
systemctl --user enable --now mr-scrooge.service

# Check status
systemctl --user status mr-scrooge.service
```

Do the same for mr-harvester if you want the profit extraction companion running.

---

### Option B — Always-On Local Machine

If you have a machine that stays on permanently (Mac Mini, NUC, old laptop), you can run it locally. Requirements: stable internet, no sleep mode, and ideally a UPS for power outages.

On macOS, disable sleep in System Settings → Energy Saver. Use `launchctl` or a similar service manager to keep the bot running on reboot.

---

## Viewing the Dashboard Remotely (Tailscale)

The dashboard binds to `localhost` only — it is not exposed to the public internet. To access it remotely, use **Tailscale**, a zero-config VPN that connects your devices securely.

**Setup:**

```bash
# On the server (EC2 or local machine):
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# On your laptop/phone:
# Install Tailscale from tailscale.com and sign in with the same account.
```

Once connected, both devices appear on the same private network. Access the dashboard by navigating to `http://<tailscale-ip-of-server>:8081` in your browser.

You can find the Tailscale IP with:
```bash
tailscale ip -4   # on the server
```

The dashboard requires HTTP Basic Auth — credentials are set in `dashboard.py`.

---

## Running Without OpenClaw

Mr. Scrooge runs **fully standalone** — it does not require OpenClaw, the gateway, or any other infrastructure. `start.sh` and `main.py` are all you need.

OpenClaw is a separate AI agent orchestration framework. If you are using it, you can register the bot as a monitored agent and access it via Slack or Telegram. If you are not using it, simply ignore any references to it in the logs or config.

---

## Notes

- The bot uses **margin-based sizing**, not fixed-lot sizing. It deploys 50% of available margin equity as the notional margin per trade. This means position size scales with your account.
- Only one position is held at a time (`max_concurrent_positions: 1`). Capital concentrates in the single best qualifying setup.
- The daily halt at 7.5% loss is hard — once triggered, no new entries are taken until the next UTC midnight reset.
- Practice accounts are fully supported. Use the `api-fxpractice.oanda.com` base URL.
