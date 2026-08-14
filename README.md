# primecli

> Command-line tools for the **DeltaPrime** (Avalanche C-chain + Arbitrum One) and **DegenPrime** (Base) lending and leverage protocols.

[![PyPI](https://img.shields.io/pypi/v/primecli.svg)](https://pypi.org/project/primecli/)
[![Python](https://img.shields.io/pypi/pyversions/primecli.svg)](https://pypi.org/project/primecli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`primecli` installs four console commands. Three of them — `deltaprime`, `degenprime`, and `arbprime` — drive the lending and leverage protocols built by the DeltaPrimeLabs team on Avalanche C-chain, Base, and Arbitrum One respectively. The fourth, `bridge`, moves native or ERC-20 funds between those three chains (via the LiFi aggregator) for any wallet in the shared agent table. All share a per-user smart-account architecture (EIP-2535 diamond) and are operated through the same CLI shape: savings pools, per-user Prime / Degen Accounts, borrow / repay / fund, swaps, debt refinancing, delayed collateral withdrawals. The Avalanche side additionally exposes GMX V2 LP (GM and GM+), TraderJoe V2 LB, sJOE staking, PRIME leverage tiers, and a leveraged-long zap macro. The Arbitrum side carries the same leverage stack adapted to GMX's home chain — 10 GM + 3 GM+ markets, GLV vaults, 11 TraderJoe LB pairs, PRIME tiers, zap (no sJOE). The Base side ships a read-only Aerodrome position inventory; write paths are deferred to v2.

Built for agent use:

- Preview by default. Every state-changing command prints the plan and stops unless you pass `--execute`.
- Predictable stdout. Read-only commands emit fixed-format tables or JSON.
- No Etherscan, Snowtrace, or Basescan API key required. Hand-curated ABIs, proxy reads via the EIP-1967 storage slot.
- RedStone-signed solvency math handled internally, with a regression test pinning the half-boundary `toFixed(8)` encoding.
- ParaSwap calldata validated client-side against the on-chain executor allowlist before broadcast.

**Current version:** 0.14.8 The 0.x line is pre-1.0, so breaking changes are possible. See [Releases](https://github.com/Mnemosyne-quest/primecli/releases).

> **Breaking change in 0.5.0:** there is no longer a default signing key. Earlier versions silently fell back to a baked-in agent when no key was configured; that fallback has been removed. With no key configured, every command now fails closed with `No signing key found...`. Set a key explicitly (see [Configuration](#configuration)).

## Security and trust

**This tool moves real on-chain funds.** Read before using.

- You manage your own private key. The tool reads it from `<TOOL>_PRIVATE_KEY` (e.g. `DELTAPRIME_PRIVATE_KEY`), or a file path you point at via `<TOOL>_KEY_FILE`, or a one-shot `--key` flag. It never writes the key anywhere. There is no default key: with nothing configured, commands fail closed.
- Every state-changing command **previews by default**. You must pass `--execute` to broadcast. Don't pass `--execute` until you have read the preview and understand what it is about to do.
- The RedStone payload, ParaSwap executor allowlist, and facet ABIs are pinned to specific on-chain state at the dates noted in the source. If DeltaPrime or DegenPrime upgrade their diamonds, the tool may need updating. Open an issue.
- The DeltaPrimeLabs team is not affiliated with this project. This is community-maintained tooling.

Full threat model and trust assumptions: [docs/security.md](docs/security.md).

## Install

```bash
pip install primecli
```

Requires Python 3.10+. Step-by-step install + first-use walkthroughs:

- **Humans** new to Python CLI tools: [docs/install-humans.md](docs/install-humans.md).
- **AI agents** integrating the tool: [docs/install-agents.md](docs/install-agents.md).

From the latest `main` (ahead of the most recent release):

```bash
pip install git+https://github.com/Mnemosyne-quest/primecli.git
```

From a local clone (development):

```bash
git clone https://github.com/Mnemosyne-quest/primecli.git
cd primecli
pip install -e .
```

## Quickstart

Read-only commands work with no key configured. Set the key when you want to broadcast.

```bash
# Read live pool state, no key needed.
deltaprime pool-info usdc

# Configure the key (or pass --key on each command for one-off use).
export DELTAPRIME_PRIVATE_KEY=0xabc...

# Preview a deposit. Prints the plan, broadcasts nothing.
deltaprime deposit --pool usdc --amount 100

# Broadcast.
deltaprime deposit --pool usdc --amount 100 --execute
```

Same shape on Base. The same EVM key works on both chains, and `DEGENPRIME_PRIVATE_KEY` falls back to `DELTAPRIME_PRIVATE_KEY` if unset.

```bash
export DEGENPRIME_PRIVATE_KEY=0xabc...   # optional; falls back to DELTAPRIME_PRIVATE_KEY

degenprime pool-info usdc
degenprime my-positions
```

## Commands

State-changing commands preview by default. Add `--execute` to broadcast.

### `deltaprime` (Avalanche C-chain)

| Group | Commands |
|-------|----------|
| Lending core | `pool-info [--json]`, `my-positions`, `deposit`, `withdraw` (24h delayed lender flow, step 1), `withdrawal-requests`, `execute-withdrawal-request --pool X [--index N]`, `cancel-withdrawal-request --pool X --index N`, `borrow`, `repay`, `fund` |
| Prime Account | `create-prime-account` (alias `create-account`), `prime-summary`, `defi --json`, `withdraw-collateral`, `withdrawal-intents`, `execute-withdrawal` |
| Swaps | `swap --from S --to S --amount N [--via yak\|paraswap] [--slippage P]` (`--via yak` default; `--via paraswap` validates the API calldata and patches a non-whitelisted executor to a known-good one before broadcast), `swap-debt --from S --to S --amount N [--slippage P]` (same ParaSwap executor handling) |
| GMX V2 LP (async, keeper-executed) | `gmx-positions`, `gmx-deposit --market M --amount N [--side long\|short]`, `gmx-withdraw --market M --amount N` |
| TraderJoe V2 LB | `lb-positions`, `lb-add --pair P --amount-x N --amount-y N [--shape spot\|curve\|bidask] [--range R]`, `lb-remove --pair P` |
| sJOE staking | `sjoe-position`, `sjoe-stake --amount N`, `sjoe-unstake --amount N`, `sjoe-claim` |
| PRIME leverage tiers | `prime-tier`, `prime-needed --borrow X [--tier premium\|basic]`, `prime-deposit --amount N`, `prime-activate [--amount N]`, `prime-deactivate [--withdraw]`, `prime-unstake --amount N`, `prime-repay --amount N` |
| Zaps (multi-tx macro) | `zap --market M --collateral P --collateral-amount N --borrow-amount N --deposit-amount N [--side long\|short] [--swap]` |

Pools: `usdc`, `wavax`, `weth`, `btc`, `usdt`. GM markets: `avax-usdc`, `btc-usdc`, `eth-usdc` (two-sided GM); `avax+`, `btc+`, `eth+` (single-sided GM+). LB pairs: `avax-usdc`, `avax-usdc-20`, `btc-usdc`, `eth-avax`, `btc-avax`, `avax-btc`, `eurc-usdc`, `usdt-usdc`, `joe-avax`.

`defi --json` emits a full positions snapshot in a DeBank-like shape — `null`, empty-list, and empty-dict fields are stripped, and the decorative `url` key is dropped (numeric `0` and boolean `false` are preserved, since they carry real information). `pool-info --json` emits a single JSON object for a named pool, or a `{name: {...}}` dict for `all`. `zap` composes `fund` → `borrow` → optional `swap` → `gmx-deposit` into one preview-and-execute flow, stopping on the first failure.

Full per-command reference: [docs/deltaprime-reference.md](docs/deltaprime-reference.md). Per-capability build spec: [docs/deltaprime-capabilities.md](docs/deltaprime-capabilities.md).

### `degenprime` (Base)

| Group | Commands |
|-------|----------|
| Lending core | `pool-info [--json]`, `my-positions`, `deposit`, `withdraw` (24h delayed lender flow, step 1), `withdrawal-requests`, `execute-withdrawal-request --pool X [--index N]`, `cancel-withdrawal-request --pool X --index N`, `borrow`, `repay`, `fund` |
| Degen Account | `create-account`, `summary [--json]`, `withdraw-collateral`, `withdrawal-intents`, `execute-withdrawal`, `cancel-withdrawal` |
| Swaps | `swap --from S --to S --amount N [--slippage P]` (ParaSwap v6), `swap-debt --from S --to S --amount N` |
| Aerodrome (read-only in v1) | `aerodrome-positions` |

Pools (v1): `usdc`, `weth`, `cbbtc`, `aero`, `brett`, `kaito`, `cbdoge`, `cbxrp`. Beyond the lendable pools, the on-chain TokenManager exposes 32 collateral symbols (memecoins, LSTs, blue-chips) usable for `swap` / `swap-debt`.

`aerodrome-positions` lists owned and staked Aerodrome NFT tokenIds. Composition decoding and write paths (claim, decrease, add, stake) are deferred to v2.

Full per-command reference: [docs/degenprime-reference.md](docs/degenprime-reference.md). Per-capability build spec: [docs/degenprime-capabilities.md](docs/degenprime-capabilities.md).

### `arbprime` (Arbitrum One)

| Group | Commands |
|-------|----------|
| Lending core | `pool-info [--json]`, `my-positions`, `deposit`, `withdraw` (24h delayed lender flow, step 1), `withdrawal-requests`, `execute-withdrawal-request --pool X [--index N]`, `cancel-withdrawal-request --pool X --index N`, `borrow`, `repay`, `fund` |
| Prime Account | `create-prime-account` (alias `create-account`), `prime-summary`, `defi --json`, `withdraw-collateral`, `withdrawal-intents`, `execute-withdrawal`, `cancel-withdrawal` |
| Swaps | `swap --from S --to S --amount N [--via yak\|paraswap] [--slippage P]`, `swap-debt --from S --to S --amount N [--slippage P]` |
| GMX V2 LP (async, keeper-executed) | `gmx-positions`, `gmx-deposit --market M --amount N [--side auto\|long\|short]`, `gmx-withdraw --market M --amount N` |
| GMX GLV vaults (async, keeper-executed) | `glv-positions`, `glv-deposit --vault V --amount N [--side auto\|long\|short] [--target-market GM]`, `glv-withdraw --vault V --amount N [--target-market GM]` |
| TraderJoe V2 LB | `lb-positions`, `lb-add --pair P --amount-x N --amount-y N [--shape spot\|curve\|bidask] [--range R]`, `lb-remove --pair P` |
| PRIME leverage tiers | `prime-tier`, `prime-needed --borrow X [--tier premium\|basic]`, `prime-deposit --amount N`, `prime-activate [--amount N]`, `prime-deactivate [--withdraw]`, `prime-unstake --amount N`, `prime-repay --amount N` |
| Zaps (multi-tx macro) | `zap --market M --collateral P --collateral-amount N --borrow-amount N [--deposit-amount N] [--side auto\|long\|short] [--swap]` |

Pools: `usdc`, `eth`, `arb`, `btc` (native-wrapped asset is WETH, account symbol `ETH`). GM markets (two-sided, all vs USDC): `eth-usdc`, `btc-usdc`, `arb-usdc`, `link-usdc`, `uni-usdc`, `gmx-usdc`, `near-usdc`, `atom-usdc`, `sui-usdc`, `sei-usdc`; single-sided GM+: `eth+`, `btc+`, `gmx+`. GLV vaults: `weth-usdc`, `btc-usdc`. LB pairs: `eth-usdc`, `eth-usdc-10`, `eth-usdt`, `eth-usdt-10`, `arb-eth`, `arb-eth-v22`, `btc-eth`, `gmx-eth`, `joe-eth`, `wsteth-eth`, `weeth-eth`. Beyond the 4 lendable pools, the live TokenManager registers 29 collateral symbols (GMX, LINK, UNI, weETH, wstETH, JOE, PRIME, the GM/GLV baskets, …) usable as collateral and for `swap` / `swap-debt`.

Full per-command + address reference: [docs/arbprime-reference.md](docs/arbprime-reference.md).

Note: DeltaPrime has TWO deployments on Arbitrum; `arbprime` targets the live one used by app.deltaprime.io (factory `0xFf5e…c20`, TokenManager `0x0a0D…E255`), with every address verified on-chain against the live SmartLoanDiamondBeacon. The stale artifact deployment (factory `0x97f4…E4E`) only carries ETH+USDC pools — don't use addresses from the repo's `deployments/arbitrum/*TUP.json` artifacts.

### `bridge` (cross-chain, Avalanche / Base / Arbitrum)

Moves native or ERC-20 funds between chains for any agent in the wallet table, via the LiFi aggregator (li.quest) — same `--as <agent>` interface as the protocol commands.

```bash
bridge --as <agent> --from <chain> --to <chain> --token <SYM> --amount <decimal> \
       [--to-token <SYM>] [--to-address <addr>] [--slippage <pct>] [--poll] [--execute]
```

Chains: `avalanche` (43114), `base` (8453), `arbitrum` (42161). `--token` is the symbol on the source chain; `--to-token` defaults to the destination chain's native gas token (a gas top-up, e.g. `AVAX` → `ETH` when bridging to Base) and can be overridden for a same-asset bridge. Dry-run by default; `--execute` broadcasts on the source chain and prints the source tx hash, source explorer URL, and the LiFi status URL (`--poll` then waits for the cross-chain leg to settle). Safety rails: **self-bridge only** (a `--to-address` that differs from the signer is refused), and the bridge is refused if the quote's implied slippage exceeds `--slippage` (default 1.0%). Because LiFi picks the cheapest route per quote, a cross-asset bridge may land at or above the cap depending on the route — re-quote, or raise `--slippage` if the price impact is acceptable. RPCs are overridable via `BRIDGE_<CHAIN>_RPC`; `LIFI_API_KEY` is sent on quote/status calls if set.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `DELTAPRIME_PRIVATE_KEY` | — | Your Avalanche signing key. Raw `0x...` hex. |
| `DELTAPRIME_KEY_FILE` | — | Path to a file containing the key. Use this if you don't want the key in process env. |
| `DELTAPRIME_RPC` | `https://api.avax.network/ext/bc/C/rpc` | Avalanche C-chain RPC. Override with a paid Alchemy / QuickNode / Infura endpoint for high-throughput use. |
| `DEGENPRIME_PRIVATE_KEY` | falls back to `DELTAPRIME_PRIVATE_KEY` | Your Base signing key. Same EVM key works on both chains. |
| `DEGENPRIME_KEY_FILE` | falls back to `DELTAPRIME_KEY_FILE` | Path to key file for Base. |
| `DEGENPRIME_RPC` | `https://base.publicnode.com` | Base RPC. |
| `ARBPRIME_PRIVATE_KEY` | falls back to `DELTAPRIME_PRIVATE_KEY` | Your Arbitrum signing key. Same EVM key works on all three chains. |
| `ARBPRIME_KEY_FILE` | falls back to `DELTAPRIME_KEY_FILE` | Path to key file for Arbitrum. |
| `ARBPRIME_AGENT` | falls back to `DELTAPRIME_AGENT` | Named-agent key selection (multi-wallet setups). |
| `ARBPRIME_RPC` | `https://arb1.arbitrum.io/rpc` | Arbitrum One RPC. |

The CLI also accepts a per-command `--key <0xhex>` override that takes precedence over all env vars. Handy for one-off operations from a shell where you don't want to persist the key.

**Key resolution.** `deltaprime` and `arbprime` resolve the signing key in this order (first hit wins): `--key` > `--as <agent>` > `<TOOL>_PRIVATE_KEY` > `<TOOL>_KEY_FILE` > `<TOOL>_ENV_FILE` + `<TOOL>_KEY_VAR` > `<TOOL>_AGENT` env > error. `arbprime`'s `ARBPRIME_*` vars each fall back to the `DELTAPRIME_*` equivalent. `degenprime` is simpler — `--key` > `DEGENPRIME_PRIVATE_KEY` > `DEGENPRIME_KEY_FILE`, with `DELTAPRIME_PRIVATE_KEY` / `DELTAPRIME_KEY_FILE` as fallbacks (it has no `--as` / agent-table mechanism). If none resolve, the command exits 1 with `No signing key found...`.

A copy-paste template is at [examples/env.example](examples/env.example).

## What's covered

### `deltaprime` (Avalanche C-chain)

| Area | Status |
|------|--------|
| Savings pools (USDC, WAVAX, WETH, BTC, USDT) | full read + write |
| Prime Account creation and funding | full |
| Borrow / repay / fund / withdraw-collateral | full |
| Solvency views (health ratio, total value, debt, solvent flag) | full (RedStone-gated reads) |
| Swap (YieldYak + ParaSwap) | full |
| Swap-debt (debt refinancing) | full |
| GMX V2 LP (GM + GM+, 6 markets) | full (async, account freezes until keeper callback) |
| TraderJoe V2 LB (9 whitelisted pairs) | full (max 80 bins per account) |
| sJOE staking | full |
| PRIME leverage tiers (BASIC / PREMIUM) | full |
| Leveraged-long zap macro | full (GM-terminal) |
| Wombat / GLP / Pangolin LP | not yet (specced in docs) |

### `degenprime` (Base)

| Area | Status |
|------|--------|
| Savings pools (8 v1 pools) | full read + write |
| Degen Account creation and funding | full |
| Borrow / repay / fund | full |
| Universal 24h delayed collateral withdrawal | full (3-step create / list / execute, plus cancel) |
| Solvency views | full (RedStone-gated reads, with BaseOracle TWAP fallback for non-RedStone symbols) |
| Swap (ParaSwap v6) | full |
| Swap-debt | full (both legs must have RedStone feeds) |
| Aerodrome positions | read-only (tokenId inventory) |
| Aerodrome write paths (claim, decrease, add, stake) | deferred to v2 |
| `$DgP` staking | not deployed on-chain yet |

### `arbprime` (Arbitrum One)

| Area | Status |
|------|--------|
| Savings pools (USDC, ETH, ARB, BTC) | full read + write |
| Prime Account creation and funding | full |
| Borrow / repay / fund / withdraw-collateral | full (24h+48h intent flow, same as Avalanche) |
| Solvency views (health ratio, total value, debt, solvent flag) | full (RedStone-gated reads, `redstone-primary-prod`) |
| Swap (YieldYak + ParaSwap v6) | full (Arbitrum Yak router) |
| Swap-debt (debt refinancing) | full |
| GMX V2 LP (GM ×10 + GM+ ×3 markets) | full (async, account freezes until keeper callback) |
| GMX GLV vaults (×2, Arbitrum-only) | full (deposit / withdraw / positions, `targetMarket` routing) |
| TraderJoe V2 LB (11 whitelisted pairs) | full (max 300 bins per account — Arbitrum facet override) |
| PRIME leverage tiers (BASIC / PREMIUM) | full (PRIME-WETH LP pair, not PRIME-WAVAX) |
| Leveraged-long zap macro | full (GM-terminal) |
| Penpie / Beefy / Sushi facets | not yet (live on-chain; deferred by scope) |

### PRIME bridge (`deltaprime` and `arbprime`)

Both tools expose a `prime-bridge` subcommand that moves the PRIME token between Avalanche and Arbitrum over LayerZero's OFT:

```bash
deltaprime prime-bridge --from arb  --amount 100 [--execute]   # Arbitrum -> Avalanche
arbprime   prime-bridge --from avax --amount 100 [--execute]   # Avalanche -> Arbitrum
```

`--from` takes `avax` or `arb` (the source chain; destination is the other one). The default `--from` differs per tool — `arb` for `deltaprime`, `avax` for `arbprime` — so always pass it explicitly. Without `--execute` it previews the LayerZero native fee, balance, and the two steps (ERC-20 approve, then `sendFrom()`); with `--execute` it broadcasts both. Gas price and `chainId` are set per the source chain.

The standalone `prime-bridge.py` script that shipped earlier was removed in 0.5.0 — the subcommand is the only supported entry point.

## Documentation

- [DeltaPrime reference](docs/deltaprime-reference.md): protocol model, addresses, facet map, RedStone integration, full command table, GMX / LB / PRIME flows.
- [DeltaPrime capabilities](docs/deltaprime-capabilities.md): per-command build spec with function signatures, parameter encoding, approve targets, slippage / oracle / exec-fee requirements.
- [DegenPrime reference](docs/degenprime-reference.md): Base-side equivalent.
- [DegenPrime capabilities](docs/degenprime-capabilities.md): Base-side per-command build spec.
- [Security model](docs/security.md): key handling, preview-by-default, ParaSwap executor allowlist, RedStone trust model, threat model.

## Using from an AI agent

`primecli` is built for autonomous and semi-autonomous use. Three properties matter:

1. **Preview by default.** Every state-changing command prints a structured preview and stops unless `--execute` is passed. An agent can call any command speculatively, parse the preview, decide whether to broadcast, then re-run with `--execute`.
2. **Predictable, parseable stdout.** Read-only commands (`pool-info` (also `--json`), `my-positions`, `prime-summary`, `summary`, `withdrawal-intents`, `lb-positions`, `gmx-positions`, `aerodrome-positions`, `sjoe-position`, `prime-tier`, `defi --json`) emit fixed-format tables or JSON. `defi --json` is a one-shot full positions snapshot.
3. **Clean failure modes.** Configuration errors do not print stack traces. A missing key prints `deltaprime: No signing key found. Pass --key <0xhex> or --as <agent>, or set DELTAPRIME_PRIVATE_KEY ...` to stderr and exits 1.

Full agent integration guide (Claude Code skill template, MCP notes, recommended guardrails): [docs/agent-integration.md](docs/agent-integration.md).

### Drop-in patterns

**Shell-tooled agents** (Claude Code, Cursor agent mode, Aider, OpenAI Codex CLI, custom bash-using agents). Install with `pip install primecli`, set the env var, and the agent calls `deltaprime` / `degenprime` as normal CLI commands. No further wiring.

**Claude Code skills.** Drop a `SKILL.md` into `.claude/skills/deltaprime/` (or `degenprime/`) describing when to invoke the tool and which commands are read-only vs state-changing. Starter template in [docs/agent-integration.md](docs/agent-integration.md).

**MCP server.** Not shipped in v0.1. If you have a use case, file an issue. The wrapper is a few hundred lines of FastMCP.

### Recommended guardrails

- Never store `--execute` in a model-controlled string. Treat it as a separate authorisation step the operator (or a deliberate policy layer) attaches after seeing the preview.
- Cap daily spend with an external budget check. The tool has no built-in caps; that is the operator's responsibility.
- Log the preview before broadcasting. If the agent decided to swap 100 USDC and the preview says 100,000 USDC, the operator needs to see that.

## Troubleshooting

Common failure modes and their fixes:

- **`No signing key found`.** Set `DELTAPRIME_PRIVATE_KEY` (or `DEGENPRIME_PRIVATE_KEY`), or point `*_KEY_FILE` at a file holding the key, or pass `--key 0x...` for a one-off command.
- **RPC rate-limited (429 or stalls).** The default public RPCs are fine for occasional reads. For sustained or write-heavy use, point `DELTAPRIME_RPC` / `DEGENPRIME_RPC` at a paid endpoint (Alchemy, QuickNode, Infura, your own node).
- **`RedStone gateway unreachable` on a read.** `prime-summary` / `summary` fall back to balances-only when the RedStone gateway is down. On `--execute` of a solvency-gated write, the call cannot proceed without a payload; wait and retry, or try the alternate gateway via the env override.
- **Swap fails on-chain with `InvalidExecutor`.** ParaSwap rotated an executor that is not in the tool's mirror of the on-chain allowlist. The tool patches to a known-good fallback automatically; if reverts persist, the on-chain allowlist itself has likely rotated. Open an issue.
- **GMX deposit reverts `InsufficientNumberOfUniqueSigners(0,3)`.** A required RedStone feed was missing from the appended payload. This was the load-bearing fix on the GMX path (24-05-2026). If you hit it on a current build, capture the tx hash and open an issue.
- **GMX deposit accepted but no GM minted.** The execution fee was below the keeper's threshold and the request expired (refund without mint). Re-run; the tool floors gas at 1 gwei (and pads 2×) in the fee estimator to clear the keeper's bar.
- **`createLoan` succeeded but `getLoansForOwner` returns empty.** The factory's owner→loans map lags a beat behind the receipt. The tool polls for up to 12s; rerun `my-positions` shortly after if it timed out.

If your failure is not on this list and the on-chain revert reason is opaque, capture the tx hash, the exact CLI invocation, and the preview output, and file an issue.

## Versions and releases

`primecli` is on a 0.x line. **Breaking changes are possible** between minor versions until 1.0. Once the on-chain surface stabilises and the v2 work (DegenPrime Aerodrome writes, Wombat / GLP on DeltaPrime) is in, the project will commit to semver.

- Latest version on PyPI: <https://pypi.org/project/primecli/>.
- Per-version release notes: <https://github.com/Mnemosyne-quest/primecli/releases>.
- The `main` branch may be ahead of the latest tagged release; install from git if you need the bleeding edge.

## Health Math (0-100% Scale)

The protocol's health meter runs from 0% (liquidation) to 100% (no debt).
The tool reports `health_ratio` from the SolvencyFacet (1.0 = liquidation line),
but the frontend-friendly 0-100% scale is computed as:

```
equity = total_supplied_usd - total_debt_usd
max_debt = equity * (tier - 1)    # PREMIUM=10, BASIC=5
health_pct = (max_debt - debt) / max_debt * 100
```

Key insight: **LP tokens (GMX, LB, Aerodrome CL) don't count as full-rate
collateral.** Using gross `supplied_usd` as the borrowing base inflates `max_debt`
and overstates health. The formula above uses `equity` only, which matches the
protocol's cross-margin calculation within ~3pp.

### Rebalancing

Auto-rebalance within a configurable range (e.g. 30-70%) by borrowing more
(deploy into existing position) or repaying debt. A 1h cooldown prevents
frequent toggling. Below 20% the cooldown is bypassed and the agent is escalated.

### Stop Loss (Equity Drawdown)

Track a baseline equity (recorded at position open) and trigger a full close
if equity drops X% below that baseline. Withdraw from the position, swap to
USDC, repay all debt. See `examples/health-monitoring/` for a reference
implementation.

## Contributing

PRs welcome. Open an issue first if you are planning anything non-trivial (new facet support, new chain, write paths for Aerodrome). Pinning ABIs and verifying on-chain shapes takes a real probe pass, and it is worth aligning before doing the work.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgements

Built on the work of the [DeltaPrimeLabs team](https://www.deltaprime.io/) (DeltaPrime + DegenPrime) and the broader DeFi tooling ecosystem: web3.py, eth-account, RedStone, ParaSwap (Velora), YieldYak, TraderJoe (LFJ), GMX, Aerodrome.
