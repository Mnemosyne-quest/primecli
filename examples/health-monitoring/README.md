# Health Monitoring — Example Setup

These scripts demonstrate a complete health monitoring and auto-rebalancing
system using `primecli`. They track a Prime/Degen account's health, log it
every 5 minutes, and optionally rebalance within a configurable range.

## Architecture

- `account-health-monitor.sh` — runs every 5 min via cron
- `account-escalation-handler.sh` — reads escalation markers, spawns agent
- `strategy.json` — per-account strategy config (optional, rebalance mode only)

## Two Modes

### Observer mode (default)
Pure logging: reads account state, computes health, writes to history.
Escalates only on data failures or equity near zero.

### Rebalance mode (with strategy.json)
Place a `strategy.json` in `state/account-health/{prime,degen}/`:
```json
{
  "mode": "rebalance",
  "target_range": [30, 70],
  "center": 50,
  "cooldown_secs": 3600,
  "position": "gmx",
  "market": "avax-usdc",
  "side": "short"
}
```

## Health Formula

Bruno's 0-100% scale, where 0% = liquidation and 100% = no debt:

```
equity = total_supplied_usd - total_debt_usd
max_debt = equity × (tier - 1)    // tier = 10 for PREMIUM, 5 for BASIC
health% = (max_debt - debt) / max_debt × 100
```

This uses only the equity (not gross supplied value) as the borrowing base
because LP positions (GMX, LB, Aerodrome CL) don't count as full-rate collateral.

## Crontab Setup

```cron
# DeltaPrime (Avalanche) — every 5 min
*/5 * * * * /path/to/account-health-monitor.sh >> /var/log/health-prime.log 2>&1

# DegenPrime (Base) — every 5 min
*/5 * * * * /path/to/account-health-monitor.sh --chain base >> /var/log/health-degen.log 2>&1

# Escalation handler — every 5 min
*/5 * * * * /path/to/account-escalation-handler.sh >> /var/log/escalations.log 2>&1
```

## Escalations

The monitor writes a JSON file to `state/account-health/{prime,degen}/escalate.json`
when it encounters a situation it can't auto-resolve. The escalation handler
reads this file and spawns an isolated agent session with full context.
