# Install guide (for AI agents)

Drop-in installation and integration spec for an LLM-driven agent (Claude Code, Cursor, Aider, Codex CLI, MCP-tooled agent, custom bash-using agent, etc.) that needs to operate `primecli` on a user's behalf.

This document is the operational contract. For per-command semantics see [deltaprime-reference.md](deltaprime-reference.md) and [degenprime-reference.md](degenprime-reference.md). For trust and guardrail rationale see [agent-integration.md](agent-integration.md) and [security.md](security.md).

## TL;DR install

```bash
pip install primecli
```

That installs two console commands: `deltaprime` (Avalanche C-chain, chainId 43114) and `degenprime` (Base, chainId 8453).

Requires Python 3.10+. No additional system dependencies. No Etherscan / Snowtrace / Basescan API key required (the tool ships hand-curated ABIs and resolves proxy implementations via the EIP-1967 storage slot).

## Verify the install

```bash
deltaprime --help
degenprime --help
```

Both should print a docstring and exit 0. If either errors, the install failed; do not proceed.

```bash
deltaprime pool-info usdc --json
degenprime pool-info usdc --json
```

Both should print a valid JSON object describing the USDC pool on each chain. **No signing key is required for read-only commands.** If these succeed, the chain RPCs are reachable and the tool is operational.

## Configuration contract

```
key resolution precedence (first set wins):
  1. --key <0xhex>                       (per-command CLI flag)
  2. DELTAPRIME_PRIVATE_KEY               (env var, raw 0x... hex)
  3. DELTAPRIME_KEY_FILE                  (env var, path to file containing the key)

degenprime falls back to DELTAPRIME_PRIVATE_KEY / DELTAPRIME_KEY_FILE when its own
DEGENPRIME_* equivalents are unset. The same EVM key works on both chains.

RPC overrides (optional, for higher throughput):
  DELTAPRIME_RPC  (defaults to https://api.avax.network/ext/bc/C/rpc)
  DEGENPRIME_RPC  (defaults to https://base.publicnode.com)
```

Read-only commands work without any key. Write commands require a key.

## Command surface

| Command shape | State-changing? | Notes |
|---------------|-----------------|-------|
| `<tool> pool-info [<pool>\|all] [--json]` | no | Live pool state. `--json` for machine-parseable output. |
| `<tool> my-positions` | no | Wallet + per-pool deposit/borrow + account address. |
| `deltaprime prime-summary` / `degenprime summary [--json]` | no | Account assets, debts, live solvency. `--json` on `summary`. |
| `deltaprime defi --json` | no | Full positions snapshot, single JSON object (trimmed). |
| `<tool> deposit --pool X --amount Y [--execute]` | yes | Default: preview. With `--execute`: broadcasts. |
| `<tool> withdraw --pool X --amount Y [--execute]` | yes | **Step 1 of delayed lender withdraw (24h flow).** Registers an intent via `createWithdrawalIntent`. The pool's plain `withdraw(uint256)` reverts — this is the only path. Intent matures ~24h later for a 48h window. |
| `<tool> withdrawal-requests` | no | Lists pending lender pool-side intents. Distinct from `withdrawal-intents` (Prime/Degen Account collateral side). |
| `<tool> execute-withdrawal-request --pool X [--index N] [--execute]` | yes | Step 2 of delayed lender withdraw — pulls a matured intent to the wallet. |
| `<tool> cancel-withdrawal-request --pool X --index N [--execute]` | yes | Cancel a pending lender pool-side intent before maturity. |
| `<tool> borrow --pool X --amount Y [--execute]` | yes | RedStone-gated. |
| `<tool> repay --pool X --amount Y [--execute]` | yes | |
| `<tool> fund --pool X --amount Y [--execute]` | yes | |
| `<tool> withdraw-collateral --pool X --amount Y [--execute]` | yes | Step 1 of delayed Prime/Degen Account collateral withdrawal (separate flow). |
| `<tool> withdrawal-intents` | no | Lists pending Prime/Degen Account collateral intents. |
| `<tool> execute-withdrawal --pool X [--index N] [--execute]` | yes | Step 2 of delayed collateral withdrawal. |
| `<tool> swap --from S --to S --amount N [--via yak\|paraswap] [--slippage P] [--execute]` | yes | RedStone-gated. **`--via paraswap` is currently blocked upstream** — see [issue #2](https://github.com/Mnemosyne-quest/primecli/issues/2). Default `--via yak` works. |
| `<tool> swap-debt --from S --to S --amount N [--slippage P] [--execute]` | yes | **Currently blocked upstream** (same allowlist as paraswap). The tool refuses cleanly. Manual fallback: `borrow → swap --via yak → repay`. See [issue #2](https://github.com/Mnemosyne-quest/primecli/issues/2). |

Avalanche-only on top of the above: `create-prime-account`, `cmd_defi`, `gmx-*` (6 markets), `lb-*` (9 pairs), `sjoe-*`, `prime-tier` / `prime-needed` / `prime-deposit` / `prime-activate` / `prime-deactivate` / `prime-unstake` / `prime-repay`, `zap`.

Base-only: `create-account`, `cancel-withdrawal`, `aerodrome-positions` (read-only in v1).

Full per-command details: [deltaprime-reference.md](deltaprime-reference.md), [degenprime-reference.md](degenprime-reference.md).

## The safety contract (enforce this on every write)

```
1. Every state-changing command previews by default. The agent runs the command
   WITHOUT --execute first, captures the preview, and surfaces it to the operator
   (or to a deterministic policy layer).

2. --execute MUST be a separate authorisation step that the operator (or policy)
   adds AFTER reading the preview. NEVER include --execute in a model-controlled
   string template.

3. Every command prints a "Wallet: 0x..." line. Verify this matches the intended
   operator wallet before allowing --execute. A misconfigured key resolution will
   silently operate the wrong wallet.

4. Numeric arguments are taken literally. The tool does not auto-scale units. A
   "100" passed for USDC amount means 100 USDC, not 0.0001 USDC. If your agent
   parses an operator request like "deposit 100 dollars", it must convert to the
   correct units before invoking the tool.
```

## Output shapes (machine-readable contracts)

### `pool-info --json`

For a named pool: emits a single object.

For `all`: emits a `{pool_name: {...}}` dict.

Per-pool shape (all keys optional, omitted when the underlying read failed):

```json
{
  "symbol": "USDC",
  "proxy": "0x8027e004d80274FB320e9b8f882C92196d779CE8",
  "token": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
  "decimals": 6,
  "totalSupply": 1039875.25,
  "totalBorrowed": 656940.43,
  "utilization": 63.17,
  "depositRate": 6.82,
  "borrowingRate": 10.79,
  "tokenPrice": 1.0008,
  "tvl": 1040707.15,
  "myDeposit": 100.0
}
```

- `utilization`, `depositRate`, `borrowingRate` are percentages (`63.17` means 63.17%).
- `tokenPrice`, `tvl` are USD-denominated; omitted when KuCoin price lookup fails.
- `myDeposit` is in token units; omitted when no key is configured or balance is zero.

### `deltaprime defi --json`

Single object covering all DeltaPrime position types on the account. Keys whose value is `null`, `[]`, or `{}` are dropped. The decorative `url` key is dropped. Numeric `0` and boolean `false` are preserved.

### `degenprime summary --json`

Single object covering the Degen Account's lending state and solvency. Same trim contract.

```json
{
  "wallet": "0x...",
  "account": "0x...",
  "nativeBalance": 0.0123,
  "supplied": [{"symbol": "USDC", "amount": 100.0, "usd": 100.0}, ...],
  "borrowed": [{"symbol": "ETH", "amount": 0.01, "usd": 20.0}, ...],
  "totalValueUsd": 80.0,
  "debtUsd": 20.0,
  "healthRatio": 4.0,
  "solvent": true
}
```

When no Degen Account exists: `{"wallet": "...", "account": null}` (the `account: null` is intentionally preserved on this branch so a consumer can detect it).

## Failure modes (parse these without scraping a traceback)

Missing key:

```
deltaprime: No signing key found. Set DELTAPRIME_PRIVATE_KEY (raw 0x... key) or DELTAPRIME_KEY_FILE (path to a file with the key), or pass --key <0xhex>.
```

Exit code: 1.

Other failure modes that don't crash with a traceback:

- "No Prime Account yet. Create one with: deltaprime create-prime-account --execute"
- "No Degen Account yet. Create one with: degenprime create-account --execute"
- "Unknown pool 'X'. Choose from: usdc, wavax, ..."
- "RedStone gateway fetch failed: ..." (falls back to balances-only where possible)

Errors that DO crash with a traceback (because they shouldn't happen in normal use): contract reverts the tool didn't anticipate, malformed JSON from the ParaSwap API, multicall returning unexpected shape. If your agent sees a stack trace, that's a bug; capture it and surface to the operator.

## Performance characteristics

Multicall3 batches the per-asset reads, so on a heavy-positions account:

- `pool-info all` is roughly N HTTP requests (one per pool) plus one KuCoin call per pool. Sub-second on a decent RPC.
- `my-positions` is 1-2 HTTP requests total.
- `prime-summary` / `summary` is 3 batched eth_calls (multicall stages) + 1 RedStone gateway round-trip.
- `defi --json` is the heaviest read; 4-6 batched eth_calls + 1 RedStone gateway round-trip + 1 KuCoin call per pool.

If your agent polls these in a loop, set `DELTAPRIME_RPC` / `DEGENPRIME_RPC` to a paid endpoint (Alchemy / QuickNode / Infura) to avoid the public RPC's rate limits.

## Recommended drop-in patterns

### Shell-tooled agent

```bash
# Install
pip install primecli

# Configure (the operator provides the key out-of-band)
export DELTAPRIME_PRIVATE_KEY=0x...

# Read pool state
deltaprime pool-info usdc --json

# Read positions
deltaprime defi --json

# Preview a transaction
deltaprime deposit --pool usdc --amount 100  # captures preview, agent parses

# Operator-confirmed broadcast
deltaprime deposit --pool usdc --amount 100 --execute
```

### Claude Code skill

Drop a `SKILL.md` at `.claude/skills/deltaprime/SKILL.md` (or `degenprime/SKILL.md`). Starter template lives in [agent-integration.md](agent-integration.md).

### Custom MCP wrapper

Not shipped in v0.2.x. If you build one, please file an issue — the canonical implementation would be a few hundred lines of FastMCP exposing each command as a structured tool with JSON schemas, preserving the preview-by-default contract.

## What this tool does NOT do

- Key management. You provide the key; the tool reads it.
- Spending caps. The tool has no built-in budget gate. Wrap with an external budget check if the agent operates autonomously.
- Transaction simulation against a fork. The preview is the call construction, not a `cast call --trace` against Tenderly. For high-stakes operations, simulate externally before broadcasting.
- Protection against compromised RPCs, RedStone signer compromise, or smart-contract bugs in DeltaPrime/DegenPrime themselves. The trust model is documented in [security.md](security.md).

## Versioning

The 0.x line is pre-1.0; minor versions may include behaviour changes. Pin to a version if your agent depends on stable output shapes:

```bash
pip install primecli==0.2.1
```

Subscribe to releases at https://github.com/Mnemosyne-quest/primecli/releases for change notifications.

## Filing issues

Surprising failure modes, output shapes that didn't parse, error messages that confused your agent: file at https://github.com/Mnemosyne-quest/primecli/issues. Agent-friendliness is a v1 commitment, not a marketing line.
