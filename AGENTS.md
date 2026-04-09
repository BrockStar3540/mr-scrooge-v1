# MR. SCROOGE -- AGENT PROFILE (CLEAN)

## Identity
- Name: Mr. Scrooge
- Type: OANDA forex box-theory reversal engine
- Runtime path: `~/.openclaw/workspace/agents/mr-scrooge`
- Start script: `start.sh`
- Entry point: `main.py`

## Non-Negotiables
- Single process only (enforced by `start.sh` and `main.py` lock).
- Use post-cutover audit window by default:
  - cutoff = `2026-03-04 23:00:00 UTC` (March 4, 2026 6:00 PM New York)
- Do not claim profitability using pre-cutover data.

## Strategy Gates (Current)
- Zone proximity + ATR regime + sucker move + pitchfork + OTR + RR.
- Key economics:
  - `min_risk_reward = 1.75`
  - `otr_marginal = 0.25`
  - `max_spread_pct_of_half = 0.30`

## Files To Check First In Any Audit
1. `main.py`
2. `core/fee_calculator.py`
3. `core/execution_router.py`
4. `strategy/universe_screener.py`
5. `portfolio/trade_manager.py`
6. `logs/launchd.log`
7. `logs/decisions.log`
8. `logs/trades.log`

## Quick Triage Commands
- Process sanity: `ps -eo pid,ppid,lstart,cmd | grep -E 'mr-scrooge|main.py'`
- Duplicate-log symptom: `tail -n 120 logs/mrscrooge.log`
- Gate pressure: `tail -n 200 logs/decisions.log`
- Recent trade flow: `tail -n 200 logs/trades.log`
