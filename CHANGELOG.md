# Changelog

All notable changes to `primecli` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may carry breaking changes).

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
