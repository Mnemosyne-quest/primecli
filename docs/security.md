# Security model

`primecli` moves real on-chain funds. Read this before using.

This document covers: key handling, the preview-by-default model, the ParaSwap executor allowlist, the RedStone trust model, slippage caps, and what the tool does (and does not) protect against.

## Key handling

The tool reads your signing key from one of three places, in this precedence:

1. `--key <0xhex>` (CLI flag). One-off, for a single command. Best when you don't want to persist the key anywhere.
2. `DELTAPRIME_PRIVATE_KEY` / `DEGENPRIME_PRIVATE_KEY` (env var). The standard path. The key lives in your shell environment.
3. `DELTAPRIME_KEY_FILE` / `DEGENPRIME_KEY_FILE` (env var). Points at a file containing the key. Use this if you don't want the key in process env (so it doesn't show up under `/proc/<pid>/environ` or in `env` dumps).

DegenPrime falls back to the DeltaPrime env vars when its own are not set. The same EVM key works on both chains.

**The tool never writes your key anywhere.** Treat the env var or key file as a hard secret:

- chmod the key file to `600` and store it outside any directory you might ever sync to a cloud service.
- never paste the key into a chat, email, screenshot, or AI assistant.
- never commit it to git. The shipped `.gitignore` blocks the obvious patterns, but cannot stop a determined `git add -f`.
- if you suspect the key is compromised, **send your funds to a fresh key immediately**. There is no rotate.

## Preview by default

Every state-changing command **previews by default** and only broadcasts when you add `--execute`. The full list, across both CLIs: `deposit`, `withdraw`, `fund`, `borrow`, `repay`, `swap`, `swap-debt`, `withdraw-collateral`, `execute-withdrawal`, `cancel-withdrawal`, `gmx-deposit`, `gmx-withdraw`, `lb-add`, `lb-remove`, `sjoe-stake`, `sjoe-unstake`, `sjoe-claim`, `prime-deposit`, `prime-activate`, `prime-deactivate`, `prime-unstake`, `prime-repay`, `zap`, `create-prime-account` / `create-account`.

The preview prints the exact call (function, args, encoded amounts, expected outputs, slippage floors, USD valuations from RedStone, executor address) and any warnings (executor not whitelisted, quoted output below target, projected stake short, bin count over cap).

**Do not pass `--execute` until you have read the preview and understand it.** This is the single most important rule when using the tool.

## ParaSwap executor allowlist

DeltaPrime's `ParaSwapFacet` and `SwapDebtFacet` (and DegenPrime's `ParaSwapFacet`) call the ParaSwap Augustus router through two methods (`swapExactAmountIn` `0xe3ead59e`, `swapExactAmountInOnUniswapV3` `0x876a02f6`) and validate the decoded **executor** against an on-chain allowlist. The tool keeps a local mirror of that allowlist:

```python
PARASWAP_EXECUTORS = {
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57",
    "0x6a000f20005980200259b80c5102003040001068",
    "0x000010036c0190e009a000d0fc3541100a07380a",
    "0x00c600b30fb0400701010f4b080409018b9006e0",
    "0xa0f408a000017007015e0f00320e470d00090a5b",
}
```

**Current status (v0.2.2, 2026-05-29) — ParaSwap path BLOCKED upstream.** The ParaSwap API now routinely emits router methods and executors that the on-chain facet does not decode or whitelist. The previous "executor patch" was always cosmetic and is removed in v0.2.2 — the tool now refuses cleanly at the entry point with a pointer to the tracking issue, rather than emit calldata that would revert on broadcast. Both `swap --via paraswap` and `swap-debt` are dead end-to-end until DeltaPrime governance refreshes `ParaSwapFacet.PARASWAP_SUPPORTED_SELECTORS` / `PARASWAP_EXECUTORS`. **Workarounds:** use `--via yak` for swaps (the default), and compose `borrow → swap --via yak → repay` manually as three txs for refinances. Tracking: https://github.com/Mnemosyne-quest/primecli/issues/2

## RedStone trust model

DeltaPrime and DegenPrime read prices from **RedStone primary-prod** (data service id `redstone-primary-prod`). The price packages are signed by a 3-of-5 set of authorised signers; the tool's `build_redstone_payload` fetches them from the RedStone gateway, filters to RedStone's authorised signer set, and appends the packed payload to the function calldata for solvency-gated writes.

The signer set baked into the tool **must match what is baked into the on-chain `SolvencyFacet`**. If RedStone rotates a signer key, both the on-chain contract and the tool need to update. The current authorised set:

```
0x8bb8f32df04c8b654987daaed53d6b6091e3b774
0xdeb22f54738d54976c4c0fe5ce6d408e40d88499
0x51ce04be4b3e32572c4ec9135221d0691ba7d202
0xdd682daec5a90dd295d14da4b0bec9281017b5be
0x9c5ae89c4af6aa32ce58588dbaf90d18a855b6de
```

If RedStone rotates and the tool starts returning `SignerNotAuthorised` (`0xec459bc0`) errors, the `REDSTONE_AUTHORISED_SIGNERS` constant in `primecli/deltaprime.py` (and the matching one in `primecli/degenprime.py`) needs to be brought up to date.

The on-chain payload encoding is also load-bearing: prices are reconstructed exactly as RedStone signs them (`parseUnits(Number(v).toFixed(8), 8)`). If anyone "tidies" the encoder back to plain `int(round(v * 1e8))`, half-boundary values re-derive a different body, `ecrecover` returns a wrong signer, and the contract reverts intermittently across every RedStone-gated path (lending, swaps, GMX, LB, PRIME, solvency views). `tests/test_redstone_encoding.py` is a regression test pinning this.

## Multicall3 dependency

The tool batches read-only RPC calls (per-pool reads, per-asset balances, RedStone-gated solvency views) through **Multicall3** at `0xcA11bde05977b3631167028862bE2a173976CA11`. This is the canonical mds1 / OpenZeppelin Multicall3 deployment, present at the same address on Avalanche C-chain and Base (and most other EVM chains) via deterministic deployer (`CREATE2`). The contract is immutable, has no admin, no upgrade path, and no token transfer surface — it only forwards `staticcall`s.

The tool calls Multicall3's `aggregate3` with `allowFailure=true` per leg, so a single reverting view does not blow up the batch. Decoded results are checked per-leg.

If your RPC is a custom fork or a chain where Multicall3 has not been deployed, the batched reads will revert. All write paths bypass Multicall3 entirely — broadcasts go directly to the target contract — so trust in Multicall3 is read-only and bounded to "the contract correctly forwards staticcalls".

## Slippage caps

Both protocols enforce **on-chain slippage caps** on top of the user-specified `--slippage`:

- ParaSwap (DeltaPrime + DegenPrime): hard 5% cap, RedStone-priced. Looser slippage reverts on-chain.
- GMX V2 (DeltaPrime): hard ±5% `isWithinBounds` cap on the min-output USD value vs the oracle estimate. Looser reverts `InvalidMinOutputValue`.
- `swap-debt` (both): hard 5% cap on the USD-value difference between the borrow leg and the repay leg.
- TraderJoe V2 LB (DeltaPrime): no slippage cap, but a max 80 bins per Prime Account.

The tool refuses preview when a request would exceed these caps, with a clear message.

## What this tool DOES protect against

- **Malformed ParaSwap calldata.** The tool decodes the API's calldata client-side, validates `src` / `dest` / `from` / `beneficiary` / `partner` / `feeBps` against the on-chain facet's expectations, and refuses on mismatch.
- **Non-whitelisted ParaSwap executors / unsupported router methods.** As of v0.2.2 the tool refuses cleanly at the entry point with a pointer to https://github.com/Mnemosyne-quest/primecli/issues/2 rather than try to patch broken calldata. Use `--via yak` instead.
- **Partial repays.** `repay` auto-caps to `min(requested, current debt, in-account balance)` so an overshoot doesn't revert.
- **Bin-cap violations.** `lb-add` previews the projected total bin count and refuses if it would exceed 80.
- **GMX execution-fee underfunding.** The tool floors the gas price at 25 gwei when estimating the GMX execution fee, so the keeper accepts the deposit.
- **Expired withdrawal intents.** `execute-withdrawal` refuses intents that have not matured or have already expired.

## What this tool does NOT protect against

- **RPC tampering.** Use a trusted RPC. The tool reads price oracles, on-chain state, and broadcasts via the configured RPC; a malicious RPC can return wrong reads, refuse to broadcast, or front-run. Default RPCs are public endpoints. Override with `DELTAPRIME_RPC` / `DEGENPRIME_RPC` to use a paid provider you trust.
- **Key compromise.** If your key leaks, your funds are gone. The tool cannot help.
- **Smart-contract bugs in DeltaPrime or DegenPrime themselves.** The tool calls verified facets; vulnerabilities in those facets are upstream.
- **Oracle manipulation if RedStone is compromised.** The 3-of-5 authorised signer set is the trust root. If 3 keys are compromised, the oracle is.
- **MEV.** Swaps with tight slippage on large amounts are vulnerable to sandwich attacks. The 5% facet cap is the only on-chain guarantee.
- **Network conditions.** Gas spikes, mempool congestion, RPC timeouts. The tool sets reasonable defaults but does not retry or rebroadcast.

## Disclaimer

This is community-maintained tooling. The DeltaPrimeLabs team is not affiliated with this project. Use at your own risk.

The facet ABIs, RedStone payload constants, ParaSwap executor allowlist, and pool addresses are pinned to specific on-chain state verified on the dates noted in the source. If the protocols upgrade their facets, the tool may need updating. Open an issue.
