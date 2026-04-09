# Mr. Scrooge Daily Notes -- 2026-03-06

## Ops and Naming
- Standardized process/agent naming to:
  - `mr-scrooge` (forex bot)
  - `mr-sprite` (crypto bot)
- Updated OpenClaw agent references so orchestrators can find the correct process names (including Lestat, Frank, Egor context updates done during this cycle).

## Strategy/Runtime Changes
- Added hourly green-capital release behavior:
  - Open positions that are net-green are eligible for close on hourly timing checks to free capital.
- Updated cooldown policy for native closes:
  - If realized PnL is positive: no cooldown.
  - If realized PnL is zero/negative: standard cooldown remains.
  - If realized PnL cannot be fetched: conservative cooldown remains.

## Deployment and Sync
- Changes deployed to EC2 live path:
  - `~/.openclaw/workspace/agents/mr-scrooge`
- Backup/source path kept aligned:
  - `/Volumes/Alien Device/mr-scrooge`
- Start script and runtime behavior validated after restart.
