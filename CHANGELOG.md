# Changelog

All notable changes to `primecli` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may carry breaking changes).

## [0.14.0] - 2026-07-20

### Fixed
- **`aero-increase-liquidity` now converges the two pool-token balances onto the
  position's tick-range ratio instead of stranding the excess on the larger side.**
  The command swept idle NON-pool assets into the bottleneck pool token, then capped the
  two pool-token balances to whichever was smaller (`_aero_cap_to_balance`) and minted —
  so when the two legs were not already near the CL range ratio it silently left the
  excess of the larger leg undeployed. The fresh-mint path (`aero-add-liquidity
  --use-all-available` and `aero-rebuild`'s remint) already ran a "precision balancing"
  pass for exactly this — up to 3 direct token0<->token1 swaps that converge onto the
  k0/k1 tick-range target — but `aero-increase-liquidity` had none. Hit live 2026-07-20:
  rebuilding core1's AERO/cbBTC position left ~$74.56 of loose AERO+cbBTC auto-supplied
  to the lending pool rather than deployed in the LP, and a manual `aero-increase-liquidity`
  sweep-in could not fully deploy it either — it capped to the smaller leg and stranded
  the rest, forcing a hand-computed manual swap before the leftover could go in. The
  balancing loop is now factored into a shared `_aero_precision_balance(...)` helper called
  from BOTH paths: the fresh-mint path passes `width_pct` (the band re-centres on the moving
  tick each pass — behaviour byte-for-byte unchanged), while `aero-increase-liquidity` passes
  the existing NFT's fixed `[tick_lower, tick_upper]` and runs it after the non-pool sweep,
  execute-only. The deliberate anti-dust-grinding limits (3-pass cap; skip a residual swap
  worth < $5 after pass 0) are preserved unchanged.

### Changed
- **The precision-balancing loop now honours `--reserve` on a pool's OWN leg.**
  `_aero_precision_balance` threads the same `reserve` dict the non-pool sweep uses
  (`_aero_apply_reserve`): if a reserve names one of the two pool tokens, that fraction of
  the leg's entry balance is held out of BOTH the k0/k1 target and the swap cap, so
  balancing can never swap away a reserved pool token (the hold is snapshotted once from the
  entry balance so multi-pass convergence can't erode below it). This keeps the "deploy
  everything, no leftovers" goal from overriding the AERO reward-hold carve-out. Dormant
  today — a live reserve only ever names a non-pool reward token, so no current pool config
  exercises this branch — but implemented defensively, since `reserve` is a generic mechanism
  and not core1-specific. Strict no-op (and zero extra balance reads) when no reserve is
  passed, so the mint path stays byte-for-byte.
- 8 new offline tests (`tests/test_aero_precision_balance.py`): convergence of a one-sided
  balance in both fixed-band (increase) and width-recompute (mint) modes; pool-leg reserve
  exclusion (partial and full hold); the no-reserve strict no-op; and the two command wiring
  paths (increase runs balancing after the sweep, execute-only, on the fixed band with
  reserve threaded; add-liquidity delegates with `width_pct` + `reserve`). Full suite 355 green.

## [0.13.1] - 2026-07-20

### Added
- **`npm_version` field on each `aerodrome-positions --json` entry** (degenprime) — the
  on-chain-verified Aerodrome Slipstream deployment (`"v2"`/`"v3"`) a position's NFT actually
  lives on, as already resolved by `_aero_npm_for_token`. `cmd_aerodrome_positions` computed
  this internally (to pick the right pool config for range metrics) but dropped it before
  building the JSON, so JSON consumers could not tell which of the two overlapping NPM
  deployments held a position. Many pairs (e.g. AERO/cbBTC) have a registry entry on BOTH
  deployments at the identical tickSpacing, so pair + tickSpacing alone cannot disambiguate —
  a downstream consumer that had to guess ("prefer V3") displayed the wrong generation for a
  real position still on the V2 NPM (2026-07-20). Exposing the already-resolved value lets
  callers read the fact instead of guessing. Purely additive: the resolution logic (fixed in
  the 2026-07-17 V2/V3 collision pass) is unchanged. 2 new offline tests drive the command
  end-to-end and assert the field carries the resolved deployment for both the v2 and v3 cases.

## [0.13.0] - 2026-07-18

### Added
- **`get_max_pool_utilisation_for_borrowing(proxy, w3=None)`** in all three protocol tools
  (degenprime / deltaprime / arbprime) — reads a pool's on-chain hard borrow ceiling
  `Pool.getMaxPoolUtilisationForBorrowing()` (0.925 = 92.5% on every DegenPrime/DeltaPrime/
  ArbPrime pool today, verified live 2026-07-18), the utilisation above which a borrow
  reverts with `MaxPoolUtilisationBreached()` (selector `0xe5739c7e`). Never raises: returns
  the documented `MAX_POOL_UTIL_FALLBACK` (0.925) when the call reverts or a pool predates the
  getter, so a borrow-sizing caller always has a safe cap to stay below. Also surfaced as a
  `maxPoolUtilisation` field (a FRACTION, not percent) in `pool-info --json` — read in the
  same Multicall3 (no extra round-trip; `allowFailure` means it's simply omitted when the leg
  reverts). This fixes the class of `MaxPoolUtilisationBreached()` reverts where the
  capacity/borrow-sizing layer assumed a hardcoded 0.88/0.90/0.95 cap instead of the real
  on-chain 0.925 (hit live on core1's AERO/cbBTC lever-up, 2026-07-18).

### Fixed
- **`aero-add-liquidity --use-all-available` no longer silently mints a DUPLICATE position.**
  The flag always mints a fresh NFT; when the Degen Account already held an OPEN position on
  the same pool this created a second, duplicate LP (only one of which the auto-rebalancer was
  armed on) — hit live on core1's AERO/cbBTC. The user-facing command now detects an existing
  open position (matching token pair + tickSpacing, liquidity > 0) and refuses, pointing at
  `aero-increase-liquidity --token-id N`; a new `--allow-duplicate` flag overrides for the
  rare intentional second position. The internal rebuild path (which mints only after burning
  the old NFT, so no open position exists at that point) is unaffected — the guard lives only
  in the user-facing entry.

## [0.12.9] - 2026-07-18

### Fixed
- **Broadcast now retries once on a stale-nonce rejection.** `get_w3()` caches a single
  RPC endpoint for the process lifetime, but public multi-node providers (the ones in
  `_BASE_RPC_FALLBACKS`) often load-balance one URL across several backend nodes with no
  read-your-writes guarantee across requests. A tx built with `nonce =
  get_transaction_count(...)` immediately after a PRECEDING tx's receipt confirmed could
  still be rejected with `nonce too low: next nonce N+1, tx nonce N` if the node serving
  the nonce lookup was lagging behind the one that confirmed the earlier tx. Hit live
  2026-07-18 on an `aero-add-liquidity --use-all-available` sequence on Base
  (degenprime): each precision-balancing swap confirmed fine, but the immediately-
  following mint's broadcast rejected on this exact race. New `_send_raw_with_nonce_retry`
  helper (used by `_sign_and_send`, the single broadcast entry point in all three
  protocol CLIs) catches only this specific rejection, waits 3s, re-fetches the nonce,
  and retries the broadcast once — any other error, or a second failure, still propagates
  unchanged. Applied identically to `degenprime.py`, `deltaprime.py`, and `arbprime.py`
  (byte-identical shared code across the three chains). 4 new tests (recovery, bounded
  retry, non-nonce errors pass through untouched, cross-file identity).

## [0.12.8] - 2026-07-18

### Fixed
- Test hygiene only: `test_aero_reserve.py`'s new sweep-wiring test hit a real signing
  key attempt in CI (no credentials there) because it wasn't actually offline despite
  the file's own docstring promising it was — masked locally by a real key being
  configured in this dev environment. v0.12.7's own CI run correctly caught this before
  publishing, so that tag exists but was never released to PyPI; this is the real
  v0.12.7 content, renumbered. See v0.12.7 below for the actual feature/fix.

## [0.12.7] - 2026-07-18

### Fixed
- **`aero-rebalance create`/`update`, `aero-rebuild`, and `aero-increase-liquidity` now
  accept `--reserve SYMBOL:FRACTION`**, threaded through to `_aero_rebuild_sweep` (reusing
  the same `_aero_apply_reserve` helper `aero-add-liquidity --use-all-available` already
  uses). Found live 2026-07-17 rebuilding parakletos-4's ETH/EURC position: a preceding
  `aero-add-liquidity --reserve AERO:0.5` correctly held back half of the position's
  accumulated AERO rewards, but recreating the on-chain rebalancer order right after ran
  its own unconditional "sweep idle assets" pass and swept the held AERO into EURC anyway
  — no value lost (still the same USD amount, just re-denominated), but the reward-hold
  protection was silently undone by a code path that had no way to know about it. Any
  caller with a reward-hold policy (or any other reservation) must now pass the same
  `--reserve` flag to every one of these commands, not just the mint.

## [0.12.6] - 2026-07-17

### Fixed
- **`aero-rebuild` and `aero-rebalance create` pool-key collision** introduced by
  0.12.5's five new V3 registry entries: both resolved a position's pool by a bare
  token0/token1 pair match, which always wins on the V2 entry (first in dict-insertion
  order) for any pair that now has both a V2 and V3 entry — silently rebuilding a real
  V3 position back into the dead V2 pool on the next automated rebuild. `aero-rebalance
  create`'s idle-asset sweep had the same bug (cosmetically mislabeled sweep target,
  no fund-misdirection since the sweep only swaps, never mints). Found live migrating
  core1's AERO/cbBTC position off v2: recreating its rebalance order printed
  "Auto-sweeping idle assets to pool: aero-cbbtc-200" for a tokenId that IS the v3
  position. Fixed by routing both through the existing version-aware
  `_aero_match_pool_cfg(token0, token1, tickSpacing, version)` helper (already used
  correctly elsewhere, e.g. `_aero_position_legs`) instead of the naive inline lookup —
  `_aero_match_pool_cfg`'s own docstring already documented this exact collision class.

## [0.12.5] - 2026-07-17

### Added
- **Five more Aerodrome Slipstream "Gauges-V3" pools in the DegenPrime registry**
  (`AERODROME_POOLS`): `weth-aero-200-v3` (ETH/AERO), `aero-cbbtc-200-v3`
  (AERO/cbBTC), `euroc-usdc-1-v3` (EURC/USDC), `cbxrp-cbbtc-100-v3` (cbXRP/cbBTC),
  and `weth-vvv-100-v3` (ETH/VVV, Venice AI). Found via an exhaustive on-chain scan
  of the whole V2 registry against the Gauges-V3 CLFactory
  (`0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef` `getPool`) plus the Voter
  (`gauges`/`isAlive`): the first four pairs already existed as V2 entries and now
  also have live V3 pools with active gauges; ETH/VVV is a brand-new pair with no
  V2 counterpart. Each entry bakes its on-chain-verified `pool` and `gauge` address
  and `slipstreamVersion: 1`, following the exact pattern of the two original V3
  entries (`virtual-weth-50-v3`, `weth-euroc-100-v3`). Registry-only addition: the
  V2/V3 read+write infrastructure (`_aero_npm_for_token`, `_aero_pool_address`,
  `_aero_mint_params`, `_aero_match_pool_cfg`) is already version-aware, so no other
  code changed. The `-v3` key suffix keeps them from colliding with the untouched V2
  entries, and version-aware matching keeps each V2 sibling resolving to its own pool.

## [0.12.4] - 2026-07-17

### Added
- **`aero-add-liquidity --use-all-available` gains an optional `--reserve
  SYMBOL:FRACTION` flag.** It holds back `FRACTION` (a real number in `[0,1]`) of
  `SYMBOL`'s inventoried loose balance from the sweep, leaving that portion loose and
  untouched in the Degen Account (no swap, no mint); the remaining `(1-FRACTION)`
  deploys exactly as before. Generic over any symbol/fraction (case-insensitive
  match); repeatable for multiple assets. The reserve is applied to the deploy set
  BEFORE the pool/sweep split, so a reserved asset is excluded from both the non-pool
  sweep-and-swap and the pool-token balancing. `FRACTION` is validated to `[0,1]`
  (NaN and out-of-range rejected with a clear error). Motivating use: the defisims
  AERO gauge-reward hold, which reserves a fraction of claimed AERO from the
  autocompound rebuild so it accumulates for manual sale; its capability probe
  inspects the new `reserve` parameter on `cmd_aero_add_liquidity`.
- **Backward compatibility is exact:** omitting `--reserve` is a strict no-op —
  `_aero_apply_reserve` returns the same deploy set unchanged and every new parameter
  defaults to `None`, so the sweep is byte-identical to prior behaviour on every
  position (this path serves all Aerodrome positions, including AERO-leg pools). New
  offline tests (`tests/test_aero_reserve.py`) lock the parse, the subtract math, the
  no-op guarantee, and the presence of the `reserve` parameter.

## [0.12.3] - 2026-07-11

### Fixed
- **`health_monitor.py`'s health-swing detector no longer escalates on a
  DOWN-swing that begins and ends in a safe health range.** It previously
  escalated any confirmed `>10` percentage point drop regardless of where it
  landed — including swings caused by the caller's own multi-tx LP rebuild
  (which routinely moves health 20-40pp over 2-3 minutes as part of normal
  operation). Escalating those spawned a close-and-redeploy agent whose own
  transactions caused further swings, which escalated again: 3 concurrent
  agents running 31 on-chain transactions on one position within 20 minutes.
  A down-swing now only escalates when it lands the account below a new
  `SWING_ESCALATION_DANGER_FLOOR` (20%, above the existing `<10%` hard-critical
  floor) — a genuine approach toward liquidation risk, not rebuild noise.
- **`degenprime.py`'s `summary --json` `healthPct` no longer falsely reads
  `0.0` when a supplied asset has no direct price feed** (e.g. a token only
  priced as part of an LP leg). The health computation now applies the same
  single-unpriced back-solve fallback `gather_defi` already used, so a
  correctly-solvent account no longer reports a false zero health percentage
  right after a fresh LP mint. `totalValueUsd`/`debtUsd`/`solvent` (sourced
  from the on-chain SolvencyFacet) were never affected — this only fixes the
  derived `healthPct` field.

## [0.12.2] - 2026-07-05

### Fixed
- **`health_monitor.py`'s health-swing detector no longer escalates on an
  IMPROVING health reading.** It previously fired on any sustained swing
  `>10` percentage points held for 2 consecutive ticks, in either direction —
  so a health reading that recovered (e.g. a repay swap that failed once and
  succeeded on the very next retry) triggered the exact same "close
  everything, rebalance to target, redeploy" escalation playbook a real
  crash would. Only a drop is a liquidation-risk signal; an increase is
  treated like no swing at all (streak resets, no escalation, no agent
  spawned).

## [0.12.1] - 2026-07-04

### Changed
- **Portability audit follow-up to v0.12.0.** `health_monitor.py`'s
  `NOTIFY_SCRIPT` is now overridable via `PRIMECLI_NOTIFY_SCRIPT` (falls back
  to the previous hardcoded path unchanged — purely additive). Fixed a stray
  personal shebang left in `deltaprime.py`
  (`#!/root/.openclaw/venv/bin/python3` -> `#!/usr/bin/env python3`, matching
  its `arbprime`/`degenprime` siblings).

## [0.12.0] - 2026-07-04

### Changed
- **Named-wallet registry externalised — the package no longer ships personal
  wallet data.** `_wallets.py` is now the single source of truth for the
  `AGENTS` registry; `arbprime.py` and `deltaprime.py` dropped their duplicated
  copies and now import `AGENTS` / `_read_env_var` / `_agent_key` from it
  (`degenprime.py` and `bridge.py` already did). The built-in registry ships
  **empty** — no wallet names, file paths, or env-var names live in the
  published source anymore. Entries are loaded at import time from an external
  JSON config and overlaid on the (empty) built-in (external wins on a name
  collision). This removes the three-places-to-fix duplication that caused the
  2026-07-04 v0.11.8 seed-path bug: a path/wallet change is now a config edit,
  not a version bump + PyPI release. (Consolidation also fixed a latent drift:
  `arbprime`'s copy was missing the `core1` entry that `deltaprime`/`_wallets`
  carried.)

### Added
- **`PRIMECLI_WALLETS_CONFIG` env var + external wallet config.** Wallet
  resolution reads a JSON file resolved from `$PRIMECLI_WALLETS_CONFIG`,
  defaulting to `~/.primecli/wallets.json`. Shape: an object mapping wallet name
  to either `{"env_file": "...", "env_var": "..."}` (raw key) or
  `{"seed_path": "...", "derivation_path": "..."}` (HD-derived). Loading is
  fail-soft: a missing file yields an empty registry (a fresh `pip install`
  with no config never crashes on import), while malformed JSON, a wrong
  top-level type, or a malformed entry warns to stderr and is skipped rather
  than raising. Added `tests/test_wallets_external_config.py`.

## [0.11.8] - 2026-07-04

### Fixed
- **HD wallet seed path stale after relocation.** Parakletos's BIP39 seed file
  moved from `/root/.openclaw/workspace/config/wallet.seed` to
  `/root/.openclaw/wallet.seed`. All three HD-derived `AGENTS` registries
  (`_wallets.py`, `arbprime.py`, `deltaprime.py`) still pointed at the old
  path, so every `parakletos-2`..`parakletos-8` key derivation raised
  `FileNotFoundError` — silently breaking range-monitor, autofarm, and
  account-health-monitor for every position on those HD wallets. Updated all
  three registries to the new path.

## [0.11.7] - 2026-07-04

### Fixed
- **`_aero_decode_minted_token_id` only recognized V3 (Slipstream) mints.** A V2
  (legacy) pool mint emits its Transfer(0x0->account) from `AERODROME_NPM_V2`, a
  different contract than `AERODROME_NPM_V3` — checking only V3 silently missed
  every V2 mint, always falling through to "could not decode tokenId from receipt
  logs" (confirmed live on an AERO/cbBTC V2 rebuild). Now checks both NPM
  deployments. Added `test_decodes_v2_pool_mint`.

### Added
- **`aero-rebuild` now warns when the position being removed has an active
  rebalance order.** Removing a position clears/orphans its order — nothing
  recreates it automatically for the new tokenId (the new id doesn't exist until
  after the mint, and guessing at trigger/mode/fee settings for a real on-chain
  order is the wrong failure mode). The command now prints a clear heads-up
  before Step 1, with the exact `aero-rebalance create` command to run afterward.
  Confirmed live 2026-07-04: a core1 rebuild left the position with no active
  order until manually recreated.

## [0.11.6] - 2026-07-04

### Fixed
- **`aerodrome-positions` crashed the whole listing on one bad RPC read.** A
  transient failure in `get_w3`/`get_prime_account`, or reading a single
  position, took down the entire command instead of surfacing which position
  failed. Now wrapped per-stage: connection/account failures return a
  structured error (JSON mode) or a clear message and exit, and a single
  position's read failure is recorded against that tokenId while the rest of
  the listing still completes.
- `main()` no longer lets an unexpected exception print a raw Python
  traceback to the user; prints a one-line `internal error (...)` message
  and exits 1. Set `DEBUG=1` to get the full traceback back for diagnosis.

### Changed
- Added two more Base RPC fallbacks (`base.gateway.tenderly.co`,
  `base-pokt.nodies.app`) and raised the per-provider connect timeout from
  10s to 15s — more headroom against the RPC flakiness that's shown up
  repeatedly this week.

## [0.11.5] - 2026-07-04

### Fixed
- **`aero-rebuild` stranded funds mid-operation in `--execute` mode.** Step 1
  (`cmd_aero_remove_liquidity`) fully closes the position — unstakes, removes
  liquidity, collects fees, and burns the NFT. The pool lookup that Steps 2/3
  (sweep idle assets + re-mint at the new width) depend on ran *after* that burn,
  reading the now-nonexistent tokenId and aborting with "Could not read position
  #N" — after the funds were already unwound into loose tokens sitting idle in
  the account, undeployed. Confirmed live 2026-07-04 on a core1 aero-cbbtc-200
  rebuild: ~$1,860 sat loose for several minutes before being manually swept and
  re-minted. Fixed by resolving the pool *before* removing the position. Added
  `tests/test_aero_rebuild.py` (mocked, offline) asserting the pool lookup
  precedes removal — nothing previously covered this function at all.

## [0.11.4] - 2026-07-03

### Fixed
- **`degenprime swap` retried a stuck ParaSwap route at a fixed slippage forever
  instead of widening it.** `_paraswap_requote_until_clean` re-quotes on failure, which
  only helps when ParaSwap rotates executors/routes - it does nothing when ParaSwap's
  own off-chain quote is itself rich vs the pool's live price. Confirmed via
  `debug_traceCall` (`base.drpc.org`) on a WETH/EURC Aerodrome Slipstream swap: the pool
  swap succeeded every time, but ParaSwap's Augustus router reverted with
  `InsufficientReturnAmount()` (surfacing through the facet as the generic
  `SwapFailed()`, `0x81ceff30`) because its quote sat ~3.2% above the pool's own
  `slot0()` price. New `_paraswap_swap_with_escalation` wraps the requote loop: starts
  at the caller's requested slippage (no change when that clears), and on a repeated
  `SwapFailed()` steps slippage up by 1.5pp, capped at 4.5% (under the facet's own hard
  5% ceiling). Only escalates on that exact failure signature - a different revert
  returns immediately. `cmd_swap`'s preview now shows the slippage that actually cleared
  and flags when escalation happened, so the broadcast decision isn't made on stale
  display data.

## [0.11.3] - 2026-07-03

### Fixed
- **`degenprime` solvency-gated calls reverted for accounts whose only exposure
  to a RedStone-priced asset was inside a staked Aerodrome LP.**
  `degen_account_price_feeds()` built the RedStone feed list from
  `getAllOwnedAssets()` + `getDebts()` only — staked LP NFTs never appear in
  either (real collateral, but the gauge holds the NFT), so an asset held
  *exclusively* inside a staked LP (no raw balance, no debt in it) never got
  its feed requested. `getTotalValue`/`getHealthRatio`/`isSolvent`/`repay`/
  `shouldRebalance` then reverted for lack of a price, and `summary`/`defi
  --json` silently reported it as "RedStone unavailable" — health 0%, no
  `totalValueUsd`. Surfaced on a live WETH/EURC position: RedStone does have a
  EURC feed on Base (tracked as `EUROC`), the account was never actually
  underwater (verified on-chain: `getTotalValue` ≈ $1780, `getHealthRatio` ≈
  1.23, `isSolvent` = true), the revert was purely a feed-scan gap.
  `degen_account_price_feeds(account, w3=None)` now also enumerates staked
  Aerodrome LP legs (via `_aero_position_legs`) when `w3` is passed; all three
  call sites (summary/defi solvency path, `repay`, `aero-rebalance
  shouldRebalance`) now pass it.
- **Related alias miss:** `_aero_position_legs` read LP leg symbols straight
  off the ERC20 (`_resolve_token_symbol` → `"EURC"`), while DegenPrime's
  TokenManager and RedStone's feed use the aliased name (`"EUROC"`) that
  `_account_asset_symbol` already applies to raw holdings. Without aliasing at
  the source, LP-leg USD lookups against `REDSTONE_AVAILABLE_FEEDS` /
  `solvency["prices"]` silently missed, leaving the local `health_pct` at 0%
  even once the on-chain values were fixed. `_aero_position_legs` now aliases
  `sym0`/`sym1` at construction, matching every other consumer of account
  symbols.

### Changed
- **Batched several sequential-`eth_call` read loops into single Multicall3
  round-trips to cut RPC call volume** (the DegenPrime Aerodrome inventory scan
  is the documented cause of Base-converge RPC 429s). Pure read-path change: the
  values fetched and every downstream decision are unchanged; only the fetch is
  batched, via the existing `multicall()` (`aggregate3` allowFailure) helper so
  one reverting leg never kills a batch, and each function's per-item tolerance
  is preserved. Affected: `degenprime._aero_use_all_available` /
  `_aero_rebuild_sweep` (shared new `_aero_inventory_available`, ~2N→1),
  `deltaprime.cmd_withdrawal_requests` (3·11→1), `cmd_withdrawal_intents` on
  delta/degen/arb (3N→1 each), `cmd_lb_remove` on delta/arb (1+3N→1, the write
  path still aborts on a failed read), and the Aerodrome NFT-decode reads in
  `degenprime._aero_position_legs` / `_aero_unclaimed_usd` (shared new
  `_aero_resolve_positions_batched` batches the V2/V3 `positions()` probes;
  both-live ownership disambiguation stays delegated to `_aero_npm_for_token`).

## [0.11.2] - 2026-07-02

### Changed
- **`degenprime` ParaSwap quote requests now allowlist route types instead of
  blocklisting them.** `_paraswap_price_route` sent `excludeContractMethods` naming the
  specific router methods `AerodromeFacet`/`SwapDebtFacet` can't decode. Switched to
  `includeContractMethods=swapExactAmountIn,swapExactAmountInOnUniswapV3` (matching
  `PARASWAP_SUPPORTED_SELECTORS`) so a ParaSwap route type added in the future is
  excluded by construction rather than needing a new blocklist entry to keep up. No
  behavior change today (the local `_paraswap_decode_and_check` decode-and-refuse
  gate already caught anything unsupported before broadcast) — this only tightens the
  quote request itself, matching the DegenPrime team's own integration guidance.

## [0.11.1] - 2026-07-02

### Fixed
- **`aero-add-liquidity --use-all-available` swept a pool token away under its
  account-symbol alias (EURC/EUROC).** `_aero_use_all_available`'s sweep-separation
  loop compared raw account symbols against the pool config's `symbol0`/`symbol1`
  directly. When a pool's `symbol1` is `EURC` but the account (and its RedStone feed)
  stores the same underlying as `EUROC`, both keys carry the identical on-chain
  balance, and `"EUROC" == "EURC"` is False — so the `EUROC` entry fell into the
  non-pool "sweep" bucket even though `EURC` was simultaneously counted as the pool
  leg. On `--execute` the tool would then swap the account's entire real EURC holding
  into the pool's bottleneck token before minting (a ~$820 EURC->ETH swap on the live
  `weth-euroc-100-v3` position, caught in preview before broadcast). Fixed by
  normalizing both sides through `_account_asset_symbol` before the comparison,
  factored into a small `_aero_separate_pool_and_sweeps` helper with a regression test.
  Same alias-dedup class as ba9ae34 / 4572343; the downstream precision-balance sweep
  was already safe (it reads via `_aero_in_account_balance`, which normalizes).
- **Restored cross-file byte-identity for `_resolve_debt_coverages` (the anti-drift
  guard was red on `main`).** `4572343` wrapped the symbol in `_account_asset_symbol`
  inside `degenprime.py`'s batched debtCoverage resolver only, leaving
  `deltaprime.py`/`arbprime.py` with the un-normalized `asset_b32(s)` call and no
  `_account_asset_symbol` helper at all — so `test_cross_file_identity`'s
  supply-chain-drift guard failed on `_resolve_debt_coverages` (241/242 since that
  commit; nobody hit it because no release ran in between). Mirrored the same wrap into
  both siblings and added a chain-appropriate `_account_asset_symbol` to each. The
  helper is the identity today on Avalanche and Arbitrum: neither passes a display-vs-
  account symbol mismatch to the resolver (Avalanche configures the euro coin as
  `EUROC` directly; Arbitrum has no euro asset), so no real alias table was warranted —
  but the wrap keeps all three resolvers identical, preserving the guard rather than
  carving the function out of it.

### Changed
- **`degenprime swap-debt` now warns when `--amount` exceeds the outstanding debt.**
  `--amount` is denominated in the `--from` token's native units (not USD) and was
  silently capped to `min(requested, debt)`, so requesting more than the debt quietly
  refinanced the entire position with no notice. It now prints an explicit one-line
  warning that names the unit denomination and the cap. `cmd_repay` already surfaces
  its cap in both preview and execute paths and is unchanged.

## [0.11.0] - 2026-06-27

### Added
- **DegenPrime Aerodrome V3 (Gauges-V3 / Slipstream-3) pool support.** Added the two
  DegenPrime-whitelisted V3 pools — `virtual-weth-50-v3` (VIRTUAL/ETH, tickSpacing 50,
  V3-only) and `weth-euroc-100-v3` (WETH/EURC, tickSpacing 100, live gauge; the older V2
  weth-euroc gauge is dead). New `slipstreamVersion` registry field (default `0` = V2) plus
  `AERODROME_CL_FACTORY_V2/V3` constants. Mint sets the facet's `slipstreamVersion` param
  (byte-identical static ABI encode — only the version word changes); `_aero_pool_address`
  resolves via the registry's baked pool address / factory-by-version; per-tokenId reads
  route through a new version-aware `_aero_npm_for_token` resolver (the V2 and V3
  NonfungiblePositionManagers are independent ERC-721s). increase/remove/collect dispatch by
  the facet's stored on-chain version, so no client write-arg change. Verified: on-chain
  mint `eth_call` simulation accepted by the DegenPrime facet; existing V2 pools unaffected.

### Fixed
- **V3 position display correctness.** `_aero_npm_for_token` is now ownership-aware: when the
  same numeric tokenId is live on both the V2 and V3 NPMs, it returns the deployment the
  prime account actually owns (`ownerOf == pa`, or `gauge.stakedContains(pa, tid)` when
  staked) instead of a v2-first guess that could surface a stranger's position. And
  `_aero_match_pool_cfg` now matches on (pair, tickSpacing, version), so a V3 position
  resolves its own pool rather than a same-pair V2 entry (this also fixes a latent
  multi-tickSpacing V2 mis-resolution). Display-only — no fund or broadcast path was affected.

## [0.10.4] - 2026-06-24

### Fixed
- **degenprime swap / swap-debt now re-quote ParaSwap on transient `SwapFailed()` reverts.**
  Velora's `/prices` is non-deterministic per call and sometimes returns a route whose executor
  is whitelisted-but-dead through the DegenPrime ParaSwapFacet, or an RFQ/maker leg that won't
  fill for a contract caller — both revert with `SwapFailed()` (selector `0x81ceff30`), tripping
  `degenprime swap`, `swap-debt`, and the autofarm converges that drive them. A fresh quote
  usually returns a clean route. The old path gave up after one bad route and patched in a dead
  legacy fallback executor, which only ever produced `InvalidExecutor()`. The fix, additive and
  happy-path-neutral: `PARASWAP_EXECUTORS` is pruned to the single on-chain-verified-good v6.2
  executor (`0x8faa0000…`; the other six all hard-revert — `SwapFailed()` or `InvalidExecutor()`)
  and the useless `_PARASWAP_FALLBACK_EXECUTOR` is removed; a new `_paraswap_requote_until_clean`
  re-quotes up to 5× and takes the first route that simulates clean for the caller's facet method
  (`cmd_swap` and `cmd_swap_debt` both use it — a clean first quote behaves exactly as before);
  and `_paraswap_price_route` now passes `excludeDEXS` to drop the RFQ/maker sources up front.
  degenprime-only — the cross-file identity guard does not pin the ParaSwap path.

## [0.10.3] - 2026-06-23

### Fixed
- **create-and-fund never logged a live deposit flow.** `cmd_create_account` with funding
  broadcasts `createAndFundLoan(asset, amount)` — the path that seeds a fresh position — but
  only the standalone `fund` command appended to the flow ledger. A position opened via
  create-and-fund therefore recorded no going-forward contribution until someone ran a manual
  `pnl_backfill`, defeating the "no rescan needed" point of the live ledger. All three protocol
  CLIs now append the seed deposit post-receipt. The factory pulls the full amount via
  `transferFrom` (or reverts — it can't be partial), so the requested amount is exact here; the
  backfill still dedupes on `(tx, asset, type)`, so there's no double-count. Found by the
  post-fix audit of the 0.10.2 flow-logging change.

## [0.10.2] - 2026-06-23

### Fixed
- **Flow ledger logged the requested fund amount, not what actually moved on-chain.**
  `_log_fund_flow` recorded the `amount` argument passed to `fund(asset, amount)` as the
  external contribution. But an ERC20 `fund()` pulls only what the wallet holds: when a
  leveraged position is opened the EOA holds dust and the rest is borrowed, so the contract
  transferred far less than requested while the ledger booked the full request. This inflated
  every downstream PnL basis — e.g. a Base position showed "since open −$349" when the real
  figure was ≈ −$44 — and produced absurd effective-APR readings (the phantom flow gets netted
  out of each trailing window). Both the fund and withdrawal-execute paths across all three
  protocol CLIs (`degenprime`/`deltaprime`/`arbprime`) now log the **actual** ERC20
  `Transfer(EOA↔account)` amount parsed from the tx receipt via the new
  `_flowledger.transferred_amount` helper. Native funding (exact `msg.value`, can't be partial)
  is unchanged. Going-forward fix only — existing ledgers need a one-time reconcile.

## [0.10.1] - 2026-06-21

### Fixed
- **degenprime `swap-debt` case-insensitive asset resolution.** `cmd_swap_debt` uppercased
  `--from`/`--to` before the `SWAP_ASSETS` / `REDSTONE_AVAILABLE_FEEDS` membership checks, so
  mixed-case pool symbols (`cbBTC`, `cbDOGE`, `cbXRP`) were rejected as "Unknown asset" even
  though they are valid pool assets with RedStone feeds. Symbols now resolve case-insensitively
  to the canonical `SWAP_ASSETS` key. Enables swap-debt **into cbBTC** (the core1 3-asset debt
  strategy).

### Added
- **deltaprime: `core1` wallet in `AGENTS`.** `--as core1` (BRUNO_CORE1_PRIVATE_KEY) now resolves
  on Avalanche, matching degenprime/bridge.

## [0.10.0] - 2026-06-17

### Added
- **Cross-chain `bridge` command.** Move native or ERC-20 funds between Avalanche, Base,
  and Arbitrum for any wallet primecli knows (`parakletos`, `paraklaudios`, `core1`) via the
  same `--as <agent>` interface the protocol commands use. Routes through the LiFi aggregator
  (`li.quest`), mirroring the proven same-chain swap tx shape with `toChain != fromChain`.
  - `bridge --as <agent> --from <chain> --to <chain> --token <SYM> --amount <N> [--to-token <SYM>]
    [--to-address <addr>] [--slippage <pct>] [--poll] [--execute]`
  - **Safety:** dry-run by default (`--execute` to broadcast); self-bridge only — the
    destination is the signer's own EOA and a differing `--to-address` is refused; a slippage
    cap (default 1%) refuses any quote whose `toAmountMin` implies worse; the destination token
    defaults to the destination chain's native gas token (gas top-up).
  - Validated live: bridged 1 AVAX (Avalanche) → ETH (Base) self-bridge, settled via the
    `near` route at 2% in under a minute.

### Changed
- **Shared wallet key table (`primecli/_wallets.py`).** The agent→key resolution
  (`AGENTS`, `_agent_key`) is now a single importable source of truth; `degenprime`
  re-exports from it instead of carrying its own copy, and `bridge` consumes the same map.

## [0.9.1] - 2026-06-17

### Fixed
- **`aero-rebalance status --history` performance.** The event scan issued one `getLogs`
  per event type across a ~180k-block window (16 round-trips), which could time out a
  per-tick caller (the defi-sims on-chain range-monitor) on a throttled public RPC. Now
  fetches all four event types in a single `getLogs` per chunk (topic0 OR-list) over a
  ~90k-block window (covers the 48h KO window), cutting round-trips ~8x (~2s in practice).

## [0.9.0] - 2026-06-17

### Added
- **DegenPrime Aerodrome on-chain auto-rebalancer (`aero-rebalance`).** New command group to
  manage the protocol's native rebalance orders on Aerodrome CL (Slipstream) positions:
  - `aero-rebalance status [--token-id N] [--check] [--history] [--json]` — read active orders
    (`getAllRebalanceOrders`), resolve the underlying Aerodrome NFP (v2/v3), optionally check
    `shouldRebalance` (RedStone-gated) and decode lifecycle events from the shared
    `RebalanceEventEmitter` (`0x74a1b3715DD3dcB565c7483551b4C67F8FF3E3dc`).
  - `aero-rebalance create|update --token-id N --width-pct W [--mode outside|inside]
    [--trigger-bps T] [--max-fee-weth F] [--mint-slip-bps] [--swap-slip-bps] [--execute]` —
    create/update an order (symmetric range band + drift trigger in bps; the sign of the
    trigger selects OUTSIDE vs INSIDE mode; `executionFeeWeth` is a max-fee ceiling, not a
    deposit).
  - `aero-rebalance cancel --token-id N [--execute]` — remove an order.
  - create/update/cancel are owner-only and NOT RedStone-gated (settled empirically);
    `shouldRebalance` IS gated. Every `--execute` is preceded by an `eth_call` pre-flight.
    Validated end-to-end on a live Base position (create → status → cancel).

### Fixed
- **Health computation:** the synthetic-LP gap fallback is now gated on a real staked
  Aerodrome NFT existing, so a price gap can't invent phantom collateral.
- **ParaSwap executor whitelist:** added Velora v1 (`0x6f05…0900`) to `PARASWAP_EXECUTORS`,
  removing a spurious not-whitelisted warning on Base swaps.

## [0.8.1] - 2026-06-14

### Fixed
- **Aerodrome close/remove-liquidity path (DegenPrime, Base).** `aero-remove-liquidity`
  now calls `batchRemoveStakedLiquidityAerodrome(uint256[])` (selector `0x27bed82e`)
  with a RedStone payload — the proven full-close (unstake + remove + collect + burn)
  for a staked position. The previous `decreaseAerodromeLiquidity` (`0xcb16b6c6`)
  reverted `Diamond: Function does not exist`. Verified by an `eth_call` close sim on a
  live staked position. Partial (`--percentage < 100`) is refused with a clear message —
  this facet method is full-close only.
- **`aero-collect-fees` display** showed `liquidity=0` for staked positions; now reads
  `NPM.positions(tokenId)` and reports real liquidity.
- **`aerodrome-positions` price range** showed `[0.0000, 0.0000]`; now computes the human
  price from the position ticks with token-decimal scaling.

### Removed
- Dead `decreaseAerodromeLiquidity` selector / helper / ABI (a wrong-guess selector that
  never matched the live diamond).

## [0.8.0] - 2026-06-14

### Fixed
- **Aerodrome concentrated-liquidity mint encoding (DegenPrime, Base).** The
  `mintAndStakeLiquidityAerodrome` calldata (selector `0xf32f1e56`) is now encoded
  to match the live facet byte-for-byte: 14 flat args including `tickSpacing`,
  native-wei amounts, the live/center pool tick (not `sqrtPriceX96`), and no
  recipient field. Previously the encoder dropped `tickSpacing` (shifting every
  later arg) and sent the stable side in human units, so every CLI mint reverted
  with no data. Verified against a live mint (Base tx `0x1a99a420…`).
- **Staked Aerodrome position reads.** `aerodrome-positions` (and the remove-liquidity
  read) now read `NPM.positions(tokenId)` directly. Staked NFTs are owned by the
  gauge, so the simplified-composition view returned liquidity 0 and garbage ticks;
  staked positions now report correct liquidity and tick range.
- **Solvency display.** A no-debt account (where `isSolvent()` can return null) no
  longer renders a false `Solvent: NO - liquidatable`.
- Removed a dead/misleading inline `mintAndStakeLiquidityAerodrome` ABI that hashed
  to the wrong selector and was never used.

### Added
- **Simulate-before-broadcast for Aerodrome add/remove-liquidity.** Every `--execute`
  runs an `eth_call` on the exact final calldata first and aborts with the revert
  reason instead of broadcasting (and burning gas) on a call that would fail.
- **Auto-cap LP amounts to on-chain balance.** Requested amounts are capped to the
  Degen Account's actual in-account balance (minus 1 wei), preventing the
  "requested the rounded display value, over-requested the real balance" revert.
- **Dust-balance display precision.** Balances that 6-dp rounding would show as zero
  or misleadingly round up now render in scientific notation plus raw wei.

## [0.7.5] - 2026-06-14

### Added
- **`--owner <address>` keyless read-only mode for `deltaprime` and `arbprime`.**
  Lets monitoring / simulation jobs inspect a wallet's Prime Account and positions
  (`defi`, `lb-positions`) without resolving or loading a private key — a public EOA
  address is enough. `get_account()` returns a read-only account (address only,
  cannot sign) when `--owner` is set; write commands are refused while `--owner` is
  active (read-only commands only). Upstreamed from the defi-sims engines' vendored
  fork so those engines can run against canonical primecli (single source).

## [0.7.4] - 2026-06-13

### Added
- **Aerodrome pool registry expanded from 6 to 31 on-chain-verified pools (Base, degenprime).**
  `AERODROME_POOLS` now lists the authoritative set of DegenPrime-supported
  SlipStream CL pools. Every entry was verified on Base (2026-06-13):
  token0/token1/tickSpacing read from the pool, decimals/symbol from each token,
  and each pool address cross-checked against the SlipStream factory's
  `getPool(token0, token1, tickSpacing)` (all 31 matched). tickSpacing tiers in
  use: 1, 50, 100, 200, 2000.
- New optional `gauge_alive` field flags the 6 pools with a dead Aerodrome gauge
  (no AERO emissions; still tradeable/LP-able): `weth-aero-200`,
  `cbbtc-cbdoge-100`, `weth-euroc-100`, `weth-cbxrp-2000`, `euroc-usdc-1`,
  `cbxrp-cbbtc-100`. The field is additive — consumers read entries by explicit
  key, none assume a fixed schema.

### Fixed
- Corrections to the original 6 entries: `aero-usdc-100` (no DegenPrime pool at
  ts=100) replaced by `aero-usdc-2000` with on-chain token0=USDC/token1=AERO
  ordering; `weth-aero-200` token0/token1 un-reversed to match on-chain. Dropped
  `weth-usdc-5` and `weth-cbbtc-30` (not DegenPrime-supported; only the ts=100
  variants are). GIZA skipped (live pool not on the factory at its tickSpacing).

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

## [0.7.3] - 2026-06-07

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
