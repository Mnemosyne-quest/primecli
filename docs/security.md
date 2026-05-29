# Security model

`primecli` moves real on-chain funds. Read this before using.

## Key handling

The tool reads your signing key from one of three places, in this precedence:

1. `--key <0xhex>` — passed on the CLI for a single command. Best for one-off operations from a shell where you don't want to persist the key.
2. `DELTAPRIME_PRIVATE_KEY` / `DEGENPRIME_PRIVATE_KEY` env var — the standard path. The key lives in your shell environment.
3. `DELTAPRIME_KEY_FILE` / `DEGENPRIME_KEY_FILE` env var — points at a file that contains the key. Use this if you don't want the key in process env (so it doesn't show up under `/proc/<pid>/environ` or in `env` dumps).

DegenPrime falls back to the DeltaPrime env vars if its own are not set, because the same EVM key works on both chains.

**The tool never writes your key anywhere.** Treat the env var or key file as a hard secret:

- chmod the key file to `600` and store it outside any directory you might ever sync to a cloud service.
- never paste the key into a chat, email, screenshot, or AI assistant.
- never commit it to git (the shipped `.gitignore` blocks the obvious patterns, but it cannot stop a determined `git add -f`).
- if you suspect the key is compromised, **send your funds to a fresh key immediately** — there is no rotate.

## Preview by default

Every state-changing command (`deposit`, `withdraw`, `fund`, `borrow`, `repay`, `swap`, `swap-debt`, `withdraw-collateral`, `execute-withdrawal`, `gmx-deposit`, `gmx-withdraw`, `lb-add`, `lb-remove`, `sjoe-stake`, `sjoe-unstake`, `sjoe-claim`, `prime-deposit`, `prime-activate`, `prime-deactivate`, `prime-unstake`, `prime-repay`, `zap`, `create-prime-account`, `create-account`) **previews by default** and only broadcasts when you add `--execute`.

The preview prints the exact call (function, args, encoded amounts, expected outputs, slippage floors, USD valuations from RedStone, executor address, etc.) and any warnings (executor not whitelisted, quoted output below target, projected stake short, bin count over cap, ...).

**Do not pass `--execute` until you have read the preview and understand it.** This is the single most important rule when using the tool.

## ParaSwap executor allowlist

DeltaPrime's `ParaSwapFacet` (and `SwapDebtFacet`) call the ParaSwap Augustus router via two router methods (`swapExactAmountIn` 0xe3ead59e, `swapExactAmountInOnUniswapV3` 0x876a02f6) and validate the decoded **executor** against an on-chain allowlist. The tool keeps a local mirror of this allowlist:

```python
PARASWAP_EXECUTORS = {
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57",
    "0x6a000f20005980200259b80c5102003040001068",
    "0x000010036c0190e009a000d0fc3541100a07380a",
    "0x00c600b30fb0400701010f4b080409018b9006e0",
    "0xa0f408a000017007015e0f00320e470d00090a5b",
}
```

If the ParaSwap API returns an executor not in this set, the tool emits a warning and patches the calldata to the known fallback executor (`0x000010036C0190E009a000d0fc3541100A07380A` — the canonical legacy executor whose calldata format is compatible with the current API's output). This stops on-chain `InvalidExecutor()` reverts when ParaSwap rotates its executor set.

The fallback is best-effort. If the on-chain allowlist itself rotates (e.g. DeltaPrime governance adds a new executor), the local mirror needs updating to match. Open an issue if you see persistent `InvalidExecutor` reverts.

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

If RedStone rotates and the tool returns `SignerNotAuthorised` (0xec459bc0) errors, the constant in `primecli/deltaprime.py` (`REDSTONE_AUTHORISED_SIGNERS`) — and the matching one in `primecli/degenprime.py` — needs to be brought up to date.

The on-chain payload encoding is also load-bearing: prices are reconstructed exactly as RedStone signs them (`parseUnits(Number(v).toFixed(8), 8)`). If anyone "tidies" the encoder back to plain `int(round(v * 1e8))`, half-boundary values re-derive a different body, `ecrecover` returns a wrong signer, and the contract reverts intermittently across every RedStone-gated path (lending, swaps, GMX, LB, PRIME, solvency views). `tests/test_redstone_encoding.py` is a regression test for this.

## Slippage caps

Both protocols enforce **on-chain slippage caps** on top of the user-specified `--slippage`:

- ParaSwap (DeltaPrime + DegenPrime): hard 5% cap, RedStone-priced. Looser slippage reverts on-chain.
- GMX V2 (DeltaPrime): hard ±5% `isWithinBounds` cap on the min-output USD value vs the oracle estimate. Looser reverts `InvalidMinOutputValue`.
- `swap-debt` (both): hard 5% cap on the USD-value difference between the borrow leg and the repay leg.
- TraderJoe V2 LB (DeltaPrime): no slippage cap, but a max 80 bins per account.

The tool refuses preview when a request would exceed these caps, with a clear message.

## What this tool DOES protect against

- **Malformed ParaSwap calldata** — the tool decodes the API's calldata client-side, validates `src/dest/from/beneficiary/partner/feeBps` against the on-chain facet's expectations, and refuses on mismatch.
- **Non-whitelisted ParaSwap executors** — the tool warns and patches to the known fallback executor before broadcasting.
- **Partial repays** — `repay` auto-caps to `min(requested, current debt, in-account balance)` so an overshoot doesn't revert.
- **Bin-cap violations** — `lb-add` previews the projected total bin count and refuses if it would exceed 80.
- **GMX execution-fee underfunding** — the tool floors the gas price at 25 gwei when estimating the GMX execution fee, so the keeper accepts the deposit.
- **Expired withdrawal intents** — `execute-withdrawal` refuses intents that have not matured or have already expired.

## What this tool does NOT protect against

- **RPC tampering.** Use a trusted RPC. The tool reads price oracles, on-chain state, and broadcasts via the configured RPC; a malicious RPC can return wrong reads, refuse to broadcast, or front-run. Default RPCs are public endpoints; override with `DELTAPRIME_RPC` / `DEGENPRIME_RPC` to use a paid provider you trust.
- **Key compromise.** If your key leaks, your funds are gone. The tool can't help.
- **Smart-contract bugs in DeltaPrime / DegenPrime themselves.** The tool calls verified facets; vulnerabilities in those facets are upstream.
- **Oracle manipulation if RedStone is compromised.** The 3-of-5 authorised signer set is the trust root; if 3 keys are compromised, the oracle is.
- **MEV.** Swaps with tight slippage on large amounts are vulnerable to sandwich attacks. The 5% facet cap is the only guarantee.
- **Network conditions.** Gas spikes, mempool congestion, RPC timeouts — the tool sets reasonable defaults but does not retry or rebroadcast.

## Disclaimer

This is community-maintained tooling. The DeltaPrime team (DeltaPrimeLabs) is not affiliated with this project. Use at your own risk.

The facet ABIs, RedStone payload constants, ParaSwap executor allowlist, and pool addresses are pinned to specific on-chain state verified on the dates noted in the source comments. If the protocols upgrade their facets, the tool may need updating — open an issue.
