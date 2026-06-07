# Changelog

All notable changes to `primecli` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may carry breaking changes).

## [0.7.2] - 2026-06-07

### Fixed
- **Broader feed selection for all `remainsSolvent`-gated Degen Account paths (Base).**
  `cmd_swap`, `cmd_swap_debt`, `cmd_aero_add_liquidity`, and
  `cmd_aero_collect_fees` were using the account-scoped
  `degen_account_price_feeds()` (ETH + owned + debt assets) for the RedStone
  payload. The on-chain solvency check iterates ALL 13 registered collateral
  types regardless of the operation type. Switched all to
  `sorted(REDSTONE_AVAILABLE_FEEDS)`, the same set used for `borrow` and
  `executeWithdrawalIntent`.
  + Minor: `bridge-lifi.py` nonce-stale bug fixed (approve + bridge in the same
    run no longer reuses the same nonce).

## [0.7.1] - 2026-06-07

### Fixed
- **DegenAccount `executeWithdrawalIntent` RedStone feed selection (Base).**
  `cmd_execute_withdrawal` was using `degen_account_price_feeds(account)` which
  returns only ETH + USDC (the two feeds the Degen Account directly holds), but
  the on-chain `remainsSolvent` check needs prices for ALL 13 registered
  collateral types that could be liquidated together. Changed to
  `sorted(REDSTONE_AVAILABLE_FEEDS)` to match the creation path
  (`cmd_withdraw_collateral`, `cmd_borrow`).
  
- **RedStone payload unsigned-metadata byte-size mismatch (Base).**
  `build_redstone_payload` wrote the unsigned-metadata length as 2 bytes
  (`len(signed_metadata).to_bytes(2, "big")`), but the RedStone on-chain
  contract reads it as `uint24` (3 bytes). This shift caused
  `CalldataOverOrUnderFlow()` on every tx that carries a fresh RedStone payload.
  Fixed to store 3 bytes with the correct total: `len(signed_metadata) + 4`
  (because fields between the data-package count and the size field — padding,
  timestamp digit, and the metadata string itself — all count toward the
  unsigned-metadata region the contract skips).

## [Unreleased]

### Added
- **`_sign_and_send()` centralized tx send helper** across all three tools:
  - Always estimates gas from final calldata (incl. RedStone payload) with configurable buffer.
  - Detects out-of-gas (gasUsed == gasLimit) on failure and retries once with 50% more buffer.
  - Prints `✓ label confirmed` + tx link on success, `✗ label failed` + gasUsed/gasLimit on failure.
  - Replaces ~50 inline sign+send+wait+print blocks across all tools with a single helper call.
  - Tests: `test_gas_limit.py` covers estimation, fallback, out-of-gas detection logic.

### Changed
- **All tx broadcast paths now use `_sign_and_send()`**:
  - Every borrow, deposit, withdraw, fund, repay, swap, swap-debt, GMX, LB,
    sJOE, Aerodrome, PRIME, and bridge operation across deltaprime, arbprime,
    and degenprime now estimates gas dynamically and has OOG retry.
  - Net reduction of ~140 lines of boilerplate.

### Fixed
- Borrow, swap, and swap-debt broadcasts now estimate gas from the final calldata
  (including the RedStone payload) and add a 25% buffer before signing. This fixes routes
  that simulated cleanly at an 8M gas allowance but reverted on broadcast under the old
  fixed 3M swap cap (seen live on Base DegenPrime USDC→AERO, which needed ~4.24M gas).
  If RPC estimation fails, the tools keep the previous fixed cap rather than crashing.

## [0.7.0] - 2026-06-06

### Changed
- **Contract-exact 0-100% account health across all three tools.** `_compute_health_pct`
  no longer approximates the meter with `max_debt = equity·(max_mult − 1)` and a fixed,
  uniform multiplier (off-by-one vs the protocol, and wrong for mixed-asset accounts). It
  now mirrors the on-chain `HealthMeterFacetProd.getHealthMeter` exactly:

      net_i = supplied_usd_i − borrowed_usd_i
      weightedCollateral = Σ dc_i·net_i (net-long legs) − Σ dc_i·(−net_i) (net-short legs)
      weightedBorrowed   = Σ dc_i·borrowed_usd_i
      borrowed           = Σ borrowed_usd_i                 (UNWEIGHTED)
      borrowed == 0                                          → 100
      wc > 0 and wc + weightedBorrowed > borrowed
          → (wc + weightedBorrowed − borrowed) / wc · 100   (clamped 0..100)
      else                                                  → 0

  Per-asset `debtCoverage` is read LIVE on-chain from each chain's TokenManager: the symbol
  is resolved via `getAssetAddress(bytes32,true)`, then `tieredDebtCoverage(tier, token)` at
  the account's PRIME leverage tier on Avalanche/Arbitrum (exactly what the contract's
  `getPrimeLeverageTier()` selects), falling back to the un-tiered `debtCoverage(token)` —
  which is the only coverage getter on Base (DegenPrime has no tier system). Lookups are
  batched through multicall (~2 eth_calls for N assets) and cached per run. `gather_lending`
  / `_gather_account_state` now stamp each row's `dc` so the meter math has its inputs. The
  shared `_health_meter_pct` core and the `_resolve_debt_coverages` resolver are byte-/code-
  identical across the three siblings and pinned by `test_cross_file_identity`.
- Removed the now-unused `DEGEN_MAX_MULT` constant.

### Fixed
- **arbprime `TJ_LB_PAIRS` eth-usdt pair `tokenY`** was the USDC constant; the real on-chain
  `tokenY` for pair `0xd387c40a72703B38A5181573724bcaF2Ce6038a5` is USDT (verified via
  `getTokenY` → `0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9`). Fixed.
- **degenprime `summary` crashed with a TypeError** when a solvency view came back `None`
  while `solvency["error"]` was still `None` (a silently-empty multicall leg): the
  currency format `${None:,.2f}` raised. `Total value` / `Debt` now print `n/a` in that
  case instead of crashing.

### Notes
- Supersedes the partial 0.6.1, which shipped the deltaprime/arbprime health change without
  the matching degenprime update, the eth-usdt fix, the summary crash guard, or the test
  refresh. 0.7.0 makes all three tools consistent and brings the suite back to green.

## [0.6.1] - 2026-06-06

### Fixed
- `cmd_repay` (all three tools): intent-aware repay cap — reads `getTotalIntentAmount` to
  compute `available = balance − pending intents`, caps repay to
  `min(requested, debt, available)`, warns when intents lock part of the balance, and
  decodes the revert reason (known selectors + `Error(string)`) on a failed broadcast.
- _(Backfilled entry.)_

## [0.6.0] - 2026-06-06

### Changed
- Cross-margin health formula, LB auto-deploy, RedStone v0.9 payload format + corrected
  Base DegenPrime feed set. _(Backfilled entry — released without a changelog entry.)_

## [0.5.6] - 2026-06-04

### Fixed
- **Velora/ParaSwap executor handling is now simulate-first** (all three tools, both
  the `swap --via paraswap` leg and `swap-debt`). DeltaPrime fixed the protocol-level
  facet bug that rejected rotating Velora executors (`InvalidExecutor`); since the fix,
  API-built calldata passes with its own executor while the old hard-patch to the
  legacy executor *reverts* (executor-specific calldata mismatch). The tools now
  `eth_call`-simulate the exact tx (calldata + RedStone payload) and keep the API
  executor when the simulation passes, fall back to the legacy executor only if the
  unpatched calldata reverts, and refuse to broadcast when both variants revert.
  Verified live on Avalanche: swap-debt USDC→AVAX ($31) through Velora executor
  `0x8faa…e820`, tx `22390b83…4013`. The static `PARASWAP_EXECUTORS` set is now
  label-only (known vs new executor in output), no longer a gate.
- **Completed the EIP-1559 gas refactor across siblings.** `_set_gas_price` /
  `_set_gas_price_for` had been modernized in deltaprime only (try EIP-1559 with a
  legacy-gasPrice fallback, honour pre-set fee fields), leaving arbprime/degenprime
  on the old chain-id-keyed logic and breaking the cross-file identity guard.
  arbprime + degenprime now match deltaprime byte-for-byte; `test_gas_pricing.py`
  rewritten for the new behaviour (EIP-1559 on Avalanche post-Etna, legacy only as
  fallback, pre-set fields preserved).
- Tests still asserting the pre-0.5.4 `bruno_pct` key updated to `health_pct`;
  removed the `bruno_pct` backward-compat read in `health_monitor.py`. Full suite
  green again (101 passed).

## [0.5.5] - 2026-06-04

### Added
- Startup version check for outdated installs (silent on network failure).
- _(Backfilled entry — released without a changelog entry.)_

## [0.5.4] - 2026-06-04

### Changed
- Depersonalized health metric names (`bruno_pct` → `health_pct`, "Health (Bruno
  0-100%)" → "Health (0-100%)"); added the metric to degenprime.
- _(Backfilled entry — released without a changelog entry.)_

## [0.5.3] - 2026-06-04

### Added
- Equity-based 0-100% health metric in `prime-summary` and `defi --json`.

### Fixed
- Pin `setuptools<73` for twine compatibility.
- _(Backfilled entry — released without a changelog entry.)_

## [0.5.2] - 2026-06-04

### Added
- `degenprime defi` command emitting the shared cross-tool JSON shape; fixes the
  Base health-monitor arms which called a nonexistent command. Previously
  `health_monitor.py` invoked `<tool> defi --json` for every chain, but only
  `deltaprime` and `arbprime` had `cmd_defi` — the DegenPrime arms returned
  "Unknown command: defi" and never produced data. `gather_defi` reuses the
  existing `summary` solvency machinery (now factored into `_gather_pool_deposits`
  + `_gather_account_state`, shared by both commands) and assembles the same
  `protocol/chain/wallet/prime_account/total_usd/health_ratio/solvent/groups/status`
  shape as `deltaprime`, with a `Lending / Leverage` group and a `Savings` group
  for Diamond-Hands pool deposits. Output is trimmed by a ported `_trim_defi_json`
  (drops null/empty fields, preserves numeric 0 and boolean false). On error it
  emits `{"status": "error", ...}` rather than raising.

## [0.5.1] - 2026-06-04

### Fixed
- Avalanche legacy gas-price floor lowered from 25 gwei to 1 gwei. The 25 gwei
  figure was the pre-Etna C-chain minimum; ACP-125 (Dec 2024) reduced the network
  minimum base fee to 1 nAVAX (live base is ~0.01 nAVAX), so the old floor
  overpaid ~2500x and inflated the node's upfront `gas x price + value` balance
  check beyond small EOAs — observed blocking a GMX deposit whose actual cost was
  well under the wallet balance.

## [0.5.0] - 2026-06-04

### Changed (BREAKING)
- **Fail-closed key resolution.** Removed the silent fallback to a baked-in default agent.
  With no signing key configured, every tool now exits 1 with `No signing key found...`
  instead of signing with a default key. Operators must select a key explicitly via
  `--key`, `--as`, `<TOOL>_PRIVATE_KEY`, `<TOOL>_KEY_FILE`, `<TOOL>_ENV_FILE` + `<TOOL>_KEY_VAR`,
  or `<TOOL>_AGENT`.

### Added
- Unified key/RPC interface across all three tools: `--key <0xhex>` CLI flag,
  `<TOOL>_PRIVATE_KEY`, `<TOOL>_KEY_FILE`, and RPC override `<TOOL>_RPC`
  (`DELTAPRIME_RPC` / `ARBPRIME_RPC` / `DEGENPRIME_RPC`). `deltaprime` and `arbprime`
  additionally support `--as <agent>`, `<TOOL>_ENV_FILE` + `<TOOL>_KEY_VAR`, and
  `<TOOL>_AGENT`. `arbprime`'s `ARBPRIME_*` vars fall back to the `DELTAPRIME_*`
  equivalents; `degenprime` falls back to `DELTAPRIME_PRIVATE_KEY` / `DELTAPRIME_KEY_FILE`.
- Test suite (pytest) and a CI test job.

### Changed
- PRIME bridge gas pricing is now set per source chain, with an explicit `chainId` on
  each bridge transaction.
- `eth-abi` dependency capped at `<7` to avoid an incompatible major release.

### Fixed
- `to_wei_units` uses `Decimal` for human-amount conversion, eliminating float drift in
  base-unit amounts.
- `health_monitor` now gates auto-actions behind a valuation-completeness check, so it
  never auto-levers or de-levers on incomplete or untrustworthy valuation data.

### Removed
- The bundled standalone `prime-bridge.py` script. The `prime-bridge` subcommand on
  `deltaprime` and `arbprime` is the only supported entry point.

## [0.4.0] - 2026-06-04

### Added
- PRIME token bridge between Avalanche and Arbitrum over LayerZero (`prime-bridge`
  subcommand on `deltaprime` and `arbprime`).

### Changed
- Arbitrum gas-pricing fixes.
- GMX market-list pruning.

## [0.3.0] - 2026-06-03

### Added
- `arbprime` tool: DeltaPrime on Arbitrum One, full Avalanche parity plus GMX GLV vaults.
- TraderJoe V2 Liquidity Book enabled on Arbitrum (11 on-chain-verified pairs).

## [0.2.7] - 2026-06-02

### Added
- Health-monitor module, per-agent configs, and a Python `health` command.

## [0.2.4] - 2026-06-02

### Fixed
- Corrected withdrawal mechanics on DeltaPrime and DegenPrime (matured-intent executors,
  24h/48h windows, RedStone gating).

[0.5.0]: https://github.com/Mnemosyne-quest/primecli/releases
[0.4.0]: https://github.com/Mnemosyne-quest/primecli/releases/tag/v0.4.0
[0.3.0]: https://github.com/Mnemosyne-quest/primecli/releases/tag/v0.3.0
[0.2.7]: https://github.com/Mnemosyne-quest/primecli/releases
[0.2.4]: https://github.com/Mnemosyne-quest/primecli/releases/tag/v0.2.4
