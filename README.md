# primecli

> Command-line tools for the **DeltaPrime** (Avalanche) and **DegenPrime** (Base) lending and leverage protocols.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

`primecli` is two `pip`-installable console commands — `deltaprime` and `degenprime` — that drive the lending and leverage protocols by the DeltaPrimeLabs team on Avalanche C-chain and Base. It exposes the full surface most people use day to day: savings pools, per-user Prime / Degen Accounts, ParaSwap and YieldYak swaps, debt refinancing, GMX V2 LP, TraderJoe V2 LB, sJOE staking, PRIME leverage tiers, delayed collateral withdrawals, and a leveraged-long zap macro.

Built to be agent-friendly: preview by default, structured stdout, hand-curated ABIs, no Etherscan / Snowtrace / Basescan API key required, RedStone-signed solvency math handled internally, ParaSwap calldata validated client-side before broadcast.

## Security and trust

**This tool moves real on-chain funds.** Read this before using.

- You manage your own private key. The tool reads it from `DELTAPRIME_PRIVATE_KEY` / `DEGENPRIME_PRIVATE_KEY` (or a file path you point at). It never writes the key anywhere.
- Every state-changing command **previews by default**. You must pass `--execute` to broadcast a transaction. Don't pass `--execute` until you understand what the preview is about to do.
- The tool's RedStone payload, ParaSwap executor allowlist, and facet ABIs are pinned to specific on-chain state at the dates noted in the source. If DeltaPrime or DegenPrime upgrade their diamond facets, the tool may need updating. Open an issue.
- The DeltaPrime team is not affiliated with this project. This is community-maintained tooling.

Full threat model and trust assumptions: [docs/security.md](docs/security.md).

## Install

Requires Python 3.10+.

```bash
pip install git+https://github.com/Mnemosyne-quest/primecli.git
```

Or from a local clone (handy for development):

```bash
git clone https://github.com/Mnemosyne-quest/primecli.git
cd primecli
pip install -e .
```

A PyPI release (`pip install primecli`) will follow once the project has been smoke-tested in the wild for a bit. Track [#1](https://github.com/Mnemosyne-quest/primecli/issues/1) if you want a ping.

## Quickstart

```bash
# Set your key (or pass --key on each command for one-off use).
export DELTAPRIME_PRIVATE_KEY=0xabc...

# Read live pool state (no key needed for read-only commands).
deltaprime pool-info usdc

# Preview a deposit.
deltaprime deposit --pool usdc --amount 100

# Broadcast it.
deltaprime deposit --pool usdc --amount 100 --execute
```

Same shape for DegenPrime on Base:

```bash
# Same EVM key works on both chains; the DegenPrime env var falls back to DELTAPRIME_PRIVATE_KEY if unset.
export DEGENPRIME_PRIVATE_KEY=0xabc...

degenprime pool-info usdc
degenprime my-positions
```

## Commands

### DeltaPrime (Avalanche C-chain)

**Lending core.** `pool-info`, `my-positions`, `deposit`, `withdraw`, `borrow`, `repay`, `fund`.

**Prime Account.** `create-prime-account` (alias `create-account`), `prime-summary`, `defi --json` (full positions dump in a DeBank-like shape), `withdraw-collateral`, `withdrawal-intents`, `execute-withdrawal`.

**Swaps.** `swap --from S --to S --amount N [--via yak|paraswap] [--slippage P]`, `swap-debt --from S --to S --amount N [--slippage P]`.

**GMX V2 LP** (async, keeper-executed). `gmx-positions`, `gmx-deposit --market M --amount N [--side long|short]`, `gmx-withdraw --market M --amount N`. Markets: `avax-usdc`, `btc-usdc`, `eth-usdc` (two-sided GM); `avax+`, `btc+`, `eth+` (single-sided GM+).

**TraderJoe V2 LB** (concentrated liquidity). `lb-positions`, `lb-add --pair P --amount-x N --amount-y N [--shape spot|curve|bidask] [--range R]`, `lb-remove --pair P`. Pairs: `avax-usdc`, `avax-usdc-20`, `btc-usdc`, `eth-avax`, `btc-avax`, `avax-btc`, `eurc-usdc`, `usdt-usdc`, `joe-avax`.

**sJOE staking.** `sjoe-position`, `sjoe-stake --amount N`, `sjoe-unstake --amount N`, `sjoe-claim`.

**PRIME leverage tiers.** `prime-tier`, `prime-needed --borrow X [--tier premium|basic]`, `prime-deposit --amount N`, `prime-activate [--amount N]`, `prime-deactivate [--withdraw]`, `prime-unstake --amount N`, `prime-repay --amount N`.

**Zaps.** `zap --market M --collateral P --collateral-amount N --borrow-amount N --deposit-amount N [--side long|short] [--swap]` — leveraged-long macro composing fund → borrow → optional swap → GMX GM deposit.

Every state-changing command previews by default; add `--execute` to broadcast.

Full per-command reference: [docs/deltaprime-reference.md](docs/deltaprime-reference.md). Per-capability build spec: [docs/deltaprime-capabilities.md](docs/deltaprime-capabilities.md).

### DegenPrime (Base)

**Lending core.** `pool-info`, `my-positions`, `deposit`, `withdraw`, `borrow`, `repay`, `fund`.

**Degen Account.** `create-account`, `summary`, `withdraw-collateral`, `withdrawal-intents`, `execute-withdrawal`, `cancel-withdrawal`.

**Swaps.** `swap --from S --to S --amount N [--slippage P]` (ParaSwap v6), `swap-debt --from S --to S --amount N`.

**Aerodrome** (read-only in v1). `aerodrome-positions` — lists owned/staked Aerodrome NFT tokenIds. Composition and write paths are deferred to v2.

Pools (v1): `usdc`, `weth`, `cbbtc`, `aero`, `brett`, `kaito`, `cbdoge`, `cbxrp`. Collateral assets beyond the pools (memecoins, LSTs) work for `swap` / `swap-debt` via the on-chain TokenManager.

Full per-command reference: [docs/degenprime-reference.md](docs/degenprime-reference.md). Per-capability build spec: [docs/degenprime-capabilities.md](docs/degenprime-capabilities.md).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `DELTAPRIME_PRIVATE_KEY` | — | Your Avalanche signing key. Raw `0x...` hex. |
| `DELTAPRIME_KEY_FILE` | — | Path to a file containing the key. Use this if you don't want the key in process env. |
| `DELTAPRIME_RPC` | `https://api.avax.network/ext/bc/C/rpc` | Avalanche C-chain RPC. Override with a paid Alchemy / QuickNode / Infura endpoint for high-throughput use. |
| `DEGENPRIME_PRIVATE_KEY` | falls back to `DELTAPRIME_PRIVATE_KEY` | Your Base signing key. Same EVM key works on both chains. |
| `DEGENPRIME_KEY_FILE` | falls back to `DELTAPRIME_KEY_FILE` | Path to key file for Base. |
| `DEGENPRIME_RPC` | `https://base.publicnode.com` | Base RPC. |

The CLI also accepts a per-command `--key <0xhex>` override that takes precedence over all env vars — handy for one-off operations from a shell where you don't want to persist the key.

A copy-paste template is at [examples/env.example](examples/env.example).

## What's covered

### DeltaPrime

| Area | Status |
|---|---|
| Savings pools (USDC, WAVAX, WETH, BTC, USDT) | full read + write |
| Prime Account creation + funding | full |
| Borrow / repay / fund / withdraw-collateral | full |
| Solvency views (health ratio, total value, debt, solvent) | full (RedStone-gated reads) |
| Swap (YieldYak + ParaSwap) | full |
| Swap-debt (debt refinancing) | full |
| GMX V2 LP (GM + GM+, 6 markets) | full (async; account freezes until keeper callback) |
| TraderJoe V2 LB (9 whitelisted pairs) | full (max 80 bins/account) |
| sJOE staking | full |
| PRIME leverage tiers (BASIC / PREMIUM) | full |
| Leveraged-long zap macro | full (GM-terminal) |
| Wombat / GLP / Pangolin LP | not yet (specced in docs) |

### DegenPrime

| Area | Status |
|---|---|
| Savings pools (8 v1 pools) | full read + write |
| Degen Account creation + funding | full |
| Borrow / repay / fund | full |
| Universal 24h delayed collateral withdrawal | full (3-step: create / list / execute, plus cancel) |
| Solvency views | full (RedStone-gated reads, with BaseOracle TWAP fallback for non-RedStone symbols) |
| Swap (ParaSwap v6) | full |
| Swap-debt | full (both legs must have RedStone feeds) |
| Aerodrome positions | read-only (tokenId listing) |
| Aerodrome write paths (add/remove/stake/claim) | deferred to v2 |
| $DgP staking | not deployed on-chain yet |

## Documentation

- [DeltaPrime reference](docs/deltaprime-reference.md) — protocol model, addresses, facet map, RedStone integration, full command table, GMX/LB/PRIME flows.
- [DeltaPrime capabilities](docs/deltaprime-capabilities.md) — per-command build spec with exact function signatures, parameter encoding, approve targets, slippage/oracle/exec-fee requirements.
- [DegenPrime reference](docs/degenprime-reference.md) — Base-side equivalent.
- [DegenPrime capabilities](docs/degenprime-capabilities.md) — Base-side per-command build spec.
- [Security model](docs/security.md) — key handling, preview-by-default, ParaSwap executor allowlist, RedStone trust model, threat model.

## Using from an AI agent

`primecli` is built for autonomous and semi-autonomous use. Three properties that matter for agents:

1. **Preview by default.** Every state-changing command prints a structured preview and stops unless `--execute` is passed. An agent can run any command speculatively, parse the preview, decide whether to broadcast, and only then re-run with `--execute`.
2. **Predictable, parseable stdout.** Read-only commands (`pool-info`, `my-positions`, `prime-summary`, `summary`, `withdrawal-intents`, `lb-positions`, `gmx-positions`, `aerodrome-positions`, `sjoe-position`, `prime-tier`, `defi --json`) emit either fixed-format tables or JSON. `defi --json` is a one-shot full positions snapshot.
3. **Clean failure modes.** No stack traces on configuration errors — a missing key prints `deltaprime: No signing key found. Set DELTAPRIME_PRIVATE_KEY ...` to stderr and exits 1.

### Drop-in patterns

**Shell-tooled agents (Claude Code, Cursor agent mode, Aider, OpenAI Codex CLI, custom bash-using agents).** Install with `pip install git+https://github.com/Mnemosyne-quest/primecli.git`, set the env var, and the agent can call `deltaprime` / `degenprime` as normal CLI commands. No further wiring.

**Claude Code skills.** Drop a `SKILL.md` into `.claude/skills/deltaprime/` (or `degenprime/`) that describes when to invoke the tool and which commands are read-only vs state-changing. A starter template is in [docs/agent-integration.md](docs/agent-integration.md).

**MCP server.** Not shipped in v0.1; if there's interest, file an issue and the wrapper is a few hundred lines of FastMCP.

### Recommended agent guardrails

- Never store `--execute` in a model-controlled string. Treat `--execute` as a separate authorisation step the operator (or a deliberate policy) attaches.
- Cap daily spend with an external budget check (the tool has no built-in caps — that's the operator's responsibility).
- Always log the preview output before broadcasting. If the agent decided to swap 100 USDC and the preview says 100,000 USDC, the operator needs to see that.

## Contributing

PRs welcome. Open an issue first if you're planning anything non-trivial (new facet support, new chain, write paths for Aerodrome, etc.) — pinning ABIs and verifying on-chain shapes takes a real probe pass, and it's worth aligning before doing the work.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Built on the work of the [DeltaPrime team](https://deltaprime.io/) and the broader DeFi tooling ecosystem (web3.py, eth-account, RedStone, ParaSwap, YieldYak, TraderJoe / LFJ, GMX).
