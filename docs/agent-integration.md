# Using primecli from an AI agent

This document collects integration patterns for letting an LLM-driven agent operate `primecli` on a user's behalf, plus the trust/guardrail model that goes with it.

## Why primecli is agent-friendly

- **Preview by default.** No state-changing command broadcasts a transaction unless `--execute` is passed. An agent can call any command speculatively, parse the preview, and only re-run with `--execute` after a deliberate authorisation step.
- **Predictable stdout.** Read-only commands print fixed-format tables (humans + agents both parse them). `deltaprime defi --json` emits a full positions snapshot as JSON for one-shot ingestion.
- **No stack traces on config errors.** Missing key → `deltaprime: No signing key found. ...` to stderr, exit 1. Agents can detect the failure mode without scraping a traceback.
- **Hand-curated ABIs, no Etherscan key required.** The tool ships its own pinned ABIs and resolves proxy implementations via the EIP-1967 storage slot, so it works in any environment with just an EVM RPC.

## Shell-tooled agents (Claude Code, Cursor, Aider, OpenAI Codex CLI)

If the agent can run shell commands, you're done after install:

```bash
pip install git+https://github.com/Mnemosyne-quest/primecli.git
export DELTAPRIME_PRIVATE_KEY=0x...
```

The agent calls `deltaprime pool-info usdc`, `deltaprime my-positions`, etc., parses the stdout, and decides on next steps. For writes, it runs the command WITHOUT `--execute` first to get the preview, surfaces the preview to the operator, and only adds `--execute` after explicit go-ahead.

## Claude Code skill

Drop a SKILL.md into your project's `.claude/skills/deltaprime/SKILL.md`. Starter content:

```markdown
---
name: deltaprime
description: Operate the DeltaPrime lending and leverage protocol on Avalanche via the `deltaprime` CLI — pool reads, Prime Account creation and funding, borrow/repay/fund, swaps via YieldYak or ParaSwap, GMX V2 LP, TraderJoe V2 LB, sJOE staking, PRIME leverage tiers, delayed collateral withdrawals.
---

# DeltaPrime

A lending/leverage protocol on Avalanche C-chain. Two layers: **savings pools** (deposit an asset to earn yield, done directly from the wallet) and **Prime Accounts** (per-user smart-contract accounts for leveraged borrowing — create one, fund it with collateral, then borrow / swap / LP / stake from inside it).

## Safety

- Every state-changing command **previews by default**. Add `--execute` only with the operator's explicit go-ahead for that exact transaction.
- The signing key is read from `DELTAPRIME_PRIVATE_KEY` (or `DELTAPRIME_KEY_FILE`, or `--key`). It controls a real wallet with real funds.
- The tool prints `Wallet:` on every command — verify the address matches the intended operator wallet before any `--execute`.

## Commands

Lending:
- `deltaprime pool-info [usdc|wavax|weth|btc|usdt|all]` — read-only.
- `deltaprime my-positions` — read-only.
- `deltaprime deposit --pool X --amount Y [--execute]`
- `deltaprime withdraw --pool X --amount Y [--execute]`
- `deltaprime borrow --pool X --amount Y [--execute]`
- `deltaprime repay --pool X --amount Y [--execute]`
- `deltaprime fund --pool X --amount Y [--execute]`

Prime Account:
- `deltaprime create-prime-account [--execute]` (alias `create-account`)
- `deltaprime create-prime-account --fund-pool X --fund-amount Y [--execute]`
- `deltaprime prime-summary` — read-only.
- `deltaprime defi --json` — full positions snapshot as JSON.
- `deltaprime withdraw-collateral --pool X --amount Y [--execute]`
- `deltaprime withdrawal-intents` — read-only.
- `deltaprime execute-withdrawal --pool X [--index N] [--execute]`

Swaps:
- `deltaprime swap --from S --to S --amount N [--via yak|paraswap] [--slippage P] [--execute]`
- `deltaprime swap-debt --from S --to S --amount N [--slippage P] [--execute]`

(See README + docs/deltaprime-capabilities.md for GMX, LB, sJOE, PRIME, zap commands.)

## Typical flows

- **Earn yield:** `deltaprime deposit --pool usdc --amount 100 --execute`.
- **Leverage:** `create-prime-account --execute` → `fund --pool X --amount Y --execute` → `borrow --pool X --amount Y --execute` → later `repay` → `withdraw`. ERC20 collateral can collapse the first two steps with `create-prime-account --fund-pool X --fund-amount Y --execute`.
```

Same shape for `degenprime` if you want to drive Base too.

## MCP server

Not shipped in v0.1. If you have a use case (Claude Desktop, Claude Code's MCP plugin system, etc.) where a structured-tools MCP wrapper would beat shell-calling the CLI, please file an issue. The wrapper is a few hundred lines of FastMCP — one tool per command, JSON-schema args, the same preview-by-default model.

## Recommended guardrails when an agent drives primecli

1. **Never store `--execute` in a model-controlled string.** Treat `--execute` as a separate authorisation step the operator (or a deliberate policy layer) attaches after seeing the preview.
2. **Cap daily spend externally.** primecli has no built-in spending caps — that's the operator's responsibility. Wrap it with a budget check.
3. **Log the preview before broadcasting.** If the agent decided to swap 100 USDC and the preview says 100,000 USDC, the operator needs to see that. Don't swallow stdout.
4. **Don't let the model paste recipient addresses.** Swap routing is internal (Prime/Degen Account → Prime/Degen Account) so this is mostly moot for the standard flows, but if you ever wire a custom send/withdraw flow, the destination address must be allowlisted out of band.
5. **Watch for RPC tampering.** A compromised RPC can return false pool/account state and fool a preview. Use a trusted RPC (your own node, or a reputable provider) for high-stakes operations. The tool defaults to public RPCs which are fine for reading but worth replacing for serious money.
6. **Verify the wallet line.** Every command prints `Wallet: 0x...`. A misconfigured key resolution (e.g. agent loaded the wrong env file) will silently operate the wrong wallet. The agent — and ideally the operator on the other side of the preview — should sanity-check this every time.

## What primecli intentionally does NOT do

- It does not implement key management beyond reading the configured key. Use a hardware wallet, an HSM, or an agent-side signing service if your threat model requires it.
- It does not protect against smart-contract bugs in DeltaPrime / DegenPrime themselves.
- It does not protect against oracle manipulation if RedStone is compromised at the signer level.
- It does not implement transaction simulation beyond the preview's call construction (no full eth_call against a forked chain). For high-stakes operations, simulate the tx in a fork (Tenderly, Foundry's `cast`) before broadcasting.

## Feedback

If you wire primecli into an agent and hit rough edges — output formats that didn't parse, error messages that confused your agent, missing JSON shapes, anything — file an issue. The agent-friendliness goals are a v1 commitment, not a marketing line.
