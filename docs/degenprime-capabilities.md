# DegenPrime Capabilities — Build Spec

Per-capability build spec for the DegenPrime surface on Base (chainId 8453), precise enough to (re)wire each one into the `degenprime` CLI. Verified on-chain 29-05-2026 against the live diamond beacon.

**Audience:** contributors (human or agent) who need to extend, debug, or audit the tool's implementation. Pair with [`degenprime-reference.md`](degenprime-reference.md) for the protocol model, pool list, facet map, and the differences from DeltaPrime.

**Build status (v1, 29-05-2026).** The RedStone payload wrap is shipped and all 17 commands in §1–§9 below are tooled, including the universal 24h delayed collateral withdrawal (§7) and read-only Aerodrome (§9). Deferred to v2: Aerodrome write paths (claim, decrease, add, stake) and Aerodrome position composition decoding.

**Where the calls go.** Everything from §3 onwards runs on the Degen Account (the per-user EIP-2535 diamond). Functions are reached by calling the diamond at the Degen Account's own address; the facet logic is shared via the `SmartLoanDiamondBeacon` at `0x85c2BAA28C1d7A07bFC5C5c9903FFf4c39ae5151`. Calls originate from the EOA owner; the diamond enforces `onlyOwner` (the EOA that created the account).

---

## Conventions (read first)

- **bytes32 asset symbols**: right-pad the ASCII symbol with zero bytes to 32. Symbols: `USDC`, `ETH`, `cbBTC`, `AERO`, `BRETT`, `KAITO`, `cbDOGE`, `cbXRP`, plus any of the 32 TokenManager-listed collateral symbols (`AIXBT`, `TOSHI`, `VIRTUAL`, `MOG`, `SKI`, `DEGEN`, `KEYCAT`, `BASEDPEPE`, `VVV`, `CLANKER`, `BNKR`, `DRB`, `COOKIE`, `ZORA`, `DINO`, `EUROC`, `weETH`, `ezETH`, `SPX`, `LBTC`, `USDT`, `cbLTC`, `AVNT`, `GIZA`). Case matters (`cbBTC` not `cbbtc`). Use `symbol.encode().ljust(32, b"\x00")`.
- **Decimals**: USDC 6, USDT 6, cbXRP 6, cbBTC 8, cbDOGE 8, WETH 18, AERO 18, BRETT 18, KAITO 18, most other collateral 18. Resolve unknowns via `TokenManager.getAssetAddress(bytes32) -> address` then `ERC20.decimals()`. Pool decimals are baked into `POOLS` in the tool.
- **Solvency gating**: state-changing facet functions that carry `remainsSolvent` (so: `borrow`, `swap` via `paraSwapV6`, `swap-debt` via `swapDebtParaSwap`, the Degen Account's `createWithdrawalIntent` and `executeWithdrawalIntent`) run the RedStone-gated solvency math inside the transaction. A real broadcast needs RedStone signed price calldata appended (the tool's `build_redstone_payload` does this). Plain `eth_call` previews of these revert `0xe7764c9e` ("missing oracle payload"). The remaining writes (`deposit`, `withdraw`, `fund`, `repay`, `createLoan`, `createAndFundLoan`, `cancelWithdrawalIntent`, and the pool-side `createWithdrawalIntent`) need no payload.
- **Approve targets** (easy to get wrong):
  - `deposit` (pool savings) approves the **pool proxy**.
  - `fund` (collateral into Degen Account) approves the **Degen Account** itself.
  - `create-account --fund-*` approves the **factory** (`SmartLoansFactory`).
  - `swap` / `swap-debt` operate on balances already inside the Degen Account, so the EOA approves nothing for them — the facet `safeApprove`s the Augustus router mid-tx.
- **Funds live inside the Degen Account.** Swaps and refinance ops operate on balances already inside the account. The flow is always: `fund` collateral in (or `borrow` to create leverage) → then swap / refinance from in-account balances. You never pass tokens from the EOA into a swap call.
- **Gas-price floor.** The tool uses `max(network_price * 2, 1 gwei)` so Base's ~0.001 gwei base fee can't strand a tx if it ticks up after submission. Cost is negligible on Base.

---

## 1. Deposit (savings pool) — ✅ SHIPPED as `deposit --pool X --amount Y`

```
Pool.deposit(uint256 amount)                       // ERC20 path
Pool.deposit(uint256 amount) payable               // native path (weth pool; value=amount)
```
- ERC20 path: `ERC20.approve(poolProxy, amount)` then `Pool.deposit(amount)`.
- Native path (`weth` pool only): single `Pool.deposit(amount)` with `value=amount`; the pool wraps ETH→WETH internally (same pattern as DeltaPrime's `wavax` pool).
- **Approve target:** the **pool proxy** (not the underlying token, not the diamond).
- **RedStone:** none — pool deposits don't run solvency math on the depositor.
- **Gotchas:**
  - The `weth` pool's `native: True` flag in `POOLS` is what triggers the value-bearing path; ERC20 path for other pools.
  - Two-tx approve+deposit issues the approve and deposit back-to-back with incrementing nonces; tool uses 300k gas for deposit, 100k for approve.

---

## 2. Withdraw (savings pool, lender side) — ✅ SHIPPED as `withdraw` / `withdrawal-requests` / `execute-withdrawal-request` / `cancel-withdrawal-request`

**There is no instant lender withdraw on DegenPrime today.** The pool's single-arg `withdraw(uint256)` reverts (bare `0x`; it never resolves a named intent). The lender side runs through the SAME delayed-intent flow as the Degen Account collateral side (§7), but with the pool's own two-arg executor. Verified on-chain 2026-06-02: `withdraw(amount,[index])` reaches the intent lookup; `instantWithdraw` / `executeWithdrawalIntent(uint256[])` are ABSENT on the pool. Same `IntentInfo` struct shape as the collateral facet.

```
Pool.createWithdrawalIntent(uint256 amount)                // step 1: register intent (oracle-free)
Pool.withdraw(uint256 amount, uint256[] intentIndices)     // step 2: after 24h, consume the matured intent (selector 0x5915d806)
Pool.cancelWithdrawalIntent(uint256 index)                 // abort a pending intent (oracle-free)
Pool.getUserIntents(address user) -> IntentInfo[]          // list per-EOA intents
Pool.getTotalIntentAmount(address user) -> uint256
```
- Timing: 24h time-lock, then a **24h** execute window (48h total). The DegenPrime pool re-anchors `expiresAt` to `block.timestamp + 48h` (not `actionableAt + 48h`), so its window is 24h, not 48h. (DeltaPrime's savings pool, by contrast, is `actionableAt + 48h` = 72h total.)
- Storage is **per-EOA on the pool** (the wallet that deposited), NOT per-Degen-Account.
- All five functions are **oracle-free** — no RedStone payload needed.
- **Approve target:** none.
- **Tool flow:** `withdraw --pool X --amount Y` registers an intent. `withdrawal-requests` lists pending intents across all pools. After maturity, `execute-withdrawal-request --pool X` pulls all currently-actionable intents (or one via `--index`). `cancel-withdrawal-request --pool X --index N` cancels a pending one.
- **Gotchas:** distinct from §7 (collateral withdraw). The lender and collateral sides have the SAME 24h pattern but live on different contracts. Use the `*-request` commands for the pool side and the `*-intent` / `withdrawal-intents` commands for the Degen Account side.

---

## 3. Fund collateral (wallet → Degen Account) — ✅ SHIPPED as `fund --pool X --amount Y`

```
DegenAccount.fund(bytes32 fundedAsset, uint256 amount)            // ERC20
DegenAccount.depositNativeToken() payable                          // native ETH (weth pool)
```
- ERC20: `ERC20.approve(degenAccount, amount)` then `DegenAccount.fund(bytes32 symbol, amount)`.
- Native: `DegenAccount.depositNativeToken()` with `value=amount`; the account wraps ETH→WETH internally (no approve).
- **Approve target:** the **Degen Account** (not the token, not the factory). This is the #1 thing you'll get wrong if you skim the DeltaPrime code.
- **RedStone:** none.
- **Gotchas:** if no Degen Account exists, the tool exits with a "create one first" message; `getLoansForOwner(EOA)` is the lookup.

---

## 4. Create Degen Account — ✅ SHIPPED as `create-account [--fund-pool X --fund-amount Y]`

```
SmartLoansFactory.createLoan() -> address                                  // empty
SmartLoansFactory.createAndFundLoan(bytes32 asset, uint256 amount) -> address   // create + fund (ERC20 only)
```
- Empty path: `factory.createLoan()`, 4M gas (the diamond construction in the factory is gas-heavy).
- Create+fund path: `ERC20.approve(factoryProxy, amount)` then `factory.createAndFundLoan(bytes32 symbol, amount)`. **ERC20 only** — the factory has no payable variant, so native ETH funding must go through the two-step flow (`create-account --execute`, then `fund --pool weth --amount N --execute`).
- **Approve target:** the **factory** (`SmartLoansFactory` proxy).
- **RedStone:** none.
- **Gotchas:**
  - One loan per owner: `createLoan` reverts if the EOA already has one. The tool checks `getLoansForOwner` first and short-circuits with the existing account address.
  - `getLoansForOwner` returns an **array** (different from DeltaPrime's singular `getLoanForOwner`). The factory still enforces one loan per owner; the array is always length 0 or 1.
  - After a successful `createLoan` the factory's map can lag a beat behind the tx receipt. The tool polls every 2s for up to 12s to print the new account address; otherwise it prints a "run `my-positions` shortly" hint rather than `None`.

---

## 5. Borrow / Repay — ✅ SHIPPED as `borrow --pool X --amount Y` and `repay --pool X --amount Y`

```
DegenAccount.borrow(bytes32 asset, uint256 amount)                 // remainsSolvent → RedStone-gated
DegenAccount.repay(bytes32 asset, uint256 amount)  payable         // NOT solvency-gated
```
- `borrow` carries `remainsSolvent`; the tool appends a RedStone payload on `--execute`. Feeds = `degen_account_price_feeds(account)` + the borrow symbol if it has a RedStone feed (the SolvencyFacet handles BaseOracle-priced symbols internally).
- `repay` is NOT solvency-gated (the facet checks `debt + in-account balance` instead), so no payload. The facet reverts if `amount > debt` OR `amount > in-account balance`. The tool caps to `min(requested, debt, in-account)` and prints the cap reason.
- **Approve target:** none for either (the account already holds the funds; for repay, the funds come from the in-account balance — fund them in first via `fund` or swap into the debt asset via `swap`).
- **RedStone:** `borrow` yes, `repay` no.
- **Gotchas:**
  - **Borrow on an empty account fails.** Fund collateral first; the tool exits with a clear message if no Degen Account exists.
  - **Repay with zero in-account balance.** The tool refuses with a hint to `swap --to <debt asset> --amount N --execute` first. Common case after `borrow` if the borrowed funds weren't moved into a productive position and just sat in-account.
  - The borrow tx is sent as raw calldata (`account.encode_abi("borrow", args=[...]) + payload.hex()`) rather than through `build_transaction()` because the RedStone payload appends bytes the ABI codec doesn't know about.

---

## 6. Swap (ParaSwap v6 only) — ✅ SHIPPED as `swap --from S --to S --amount N [--slippage P]`

```
DegenAccount.paraSwapV6(bytes4 selector, bytes data)               // remainsSolvent → RedStone-gated
```
- `selector` + `data` are the **ParaSwap API swap calldata**, split into the 4-byte method selector and the remaining ABI-encoded args.
- Build flow:
  1. `_paraswap_price_route(srcToken, srcDec, destToken, destDec, amountIn, userAddress=DegenAccount)` → `GET /prices` on `apiv5.paraswap.io` with `network=8453`, `version=6.2`, `side=SELL`, `excludeContractMethods=multiSwap,megaSwap,protectedMultiSwap,protectedMegaSwap,protectedSimpleSwap,simpleSwap` (keeps the API on a facet-decodable route).
  2. `_paraswap_build_tx(priceRoute, …, slippage)` → `POST /transactions/8453?ignoreChecks=true&ignoreGasEstimate=true` with `userAddress=receiver=DegenAccount`, `partner="paraswap"` (resolves to partner=0/fee=0, which the facet requires). `ignoreChecks=true` is mandatory: the swapper is the Degen Account (a contract that hasn't approved Augustus yet — the facet does that mid-tx), so the API's balance/allowance pre-checks would reject an otherwise valid build.
  3. `_paraswap_decode_and_check(selector, data, …)` mirrors the facet's `decodeParaSwapData` + `validateSwapParameters` so a preview fails loud here rather than reverting on-chain. Validates: supported selector, executor in whitelist (otherwise patches to a known-good fallback), `partner=0`/`feeBps=0`, src/dest token match, beneficiary in `{0x0, DegenAccount}`, `fromAmount == amountIn`.
- **Selector whitelist** (facet decodes exactly two router methods):
  - `swapExactAmountIn` (`0xe3ead59e`) — generic executor route.
  - `swapExactAmountInOnUniswapV3` (`0x876a02f6`) — Uniswap V3 direct route.
  Anything else → refuse.
- **Executor whitelist** (lower-cased):
  - `0xdef171fe48cf0115b1d80b88dc8eab59176fee57`, `0x6a000f20005980200259b80c5102003040001068`, `0x000010036c0190e009a000d0fc3541100a07380a`, `0x00c600b30fb0400701010f4b080409018b9006e0`, `0xa0f408a000017007015e0f00320e470d00090a5b`.
  Patches to `0x000010036C0190E009a000d0fc3541100A07380A` if the API returns one that isn't on the list (mirrors DeltaPrime's swap-debt path). New executors surface on-chain with `InvalidExecutor`; add as they appear.
- **Slippage:** `--slippage` is passed as bps to the API; the facet enforces a hard 5% cap on top, RedStone-priced. The tool prints both numbers.
- **Approve target:** none (facet does it).
- **RedStone:** yes on `--execute`. Feeds = `degen_account_price_feeds(account)` + `from_sym` + `to_sym` if either has a RedStone feed.
- **Gotchas:**
  - The Augustus router address `0x6A000F20005980200259B80c5102003040001068` is shared with Avalanche (v6 is unified).
  - The non-pool collateral path: `swap --from MOG --to USDC` works (MOG is a TokenManager-listed collateral); `_swap_asset_meta` falls back to TokenManager resolution if the symbol isn't in `POOLS`.
  - Memecoin routes can be thin. Preview the quote before executing; the printed "Expected out" vs "Min out" gap shows the API's confidence.

---

## 7. Swap debt (refinance) — ✅ SHIPPED as `swap-debt --from S --to S --amount N [--slippage P]`

```
DegenAccount.swapDebtParaSwap(bytes32 fromAsset, bytes32 toAsset,
                              uint256 repayAmount, uint256 borrowAmount,
                              bytes4 selector, bytes data)         // remainsSolvent → RedStone-gated
```
- Refinances debt from `--from` (existing debt asset) into `--to` (new debt asset). Mechanics: borrow `borrowAmount` of `toAsset` → ParaSwap into `fromAsset` (`selector`+`data` is the ParaSwap calldata for `to → from`) → repay `repayAmount` of `fromAsset` debt.
- Build flow:
  1. Read current `fromAsset` debt from the from-pool (`pool.getBorrowed(degenAccount)`); cap `repayAmount` to `min(requested, debt)`.
  2. Read RedStone-priced `getPrices([from, to])` (1e8-scaled USD); compute `borrowAmount` so its USD value ≈ repay USD value: `borrowAmount = price_from * repay_amount * 10**to_dec // (price_to * 10**from_dec)`.
  3. Refuse if `borrowAmount == 0` (repay amount too small).
  4. Build ParaSwap calldata for `to → from` (sells `borrowAmount` of `to`, receives ≥ `repayAmount` of `from`). Same `_paraswap_*` helpers as §6.
  5. Refuse if the USD-diff between repay and borrow legs exceeds 5% / 500 bps (the facet's own cap). Warn (don't refuse) if quoted swap output is below `repayAmount` — the facet repays `min(swap output, repayAmount, debt)`, so any shortfall leaves residual old debt.
- **Both symbols must have RedStone feeds.** The facet's value-match step calls `getPrices`, which only works for feed-available symbols. The tool refuses upfront if either leg isn't in `REDSTONE_AVAILABLE_FEEDS`.
- **Both symbols must be DegenPrime pool assets.** Only pool assets have a `getBorrowed` view (you can only have debt in something there's a pool for).
- **Approve target:** none.
- **RedStone:** yes on `--execute`. Payload covers `degen_account_price_feeds(account) ∪ {from_sym, to_sym}`.
- **Gotchas:**
  - `paraSwapDecodedData.fromAmount` must equal `borrowAmount` **exactly** (facet checks this). The tool's preview catches mismatches before broadcast.
  - The 5% USD-diff cap is RedStone-priced, not API-priced. A trade that fits the API's slippage but blows the RedStone-priced cap will revert on-chain; the preview catches it because it uses the same RedStone read the facet does.

---

## 8. Universal 24h delayed collateral withdrawal — ✅ SHIPPED as `withdraw-collateral` / `withdrawal-intents` / `execute-withdrawal` / `cancel-withdrawal`

**No instant withdrawal of any kind.** DegenPrime locks **every** collateral withdrawal from a Degen Account behind a 24h time-lock, regardless of asset. The lender-side savings pools (`Pool.withdraw`, §2) are ALSO 24h-locked behind the same delayed-intent flow — the single-arg `withdraw(uint256)` reverts. Nothing leaves the protocol without the 24h wait (same model as DeltaPrime now). The two differ: the Degen Account uses a **48h** execute window and `createWithdrawalIntent` is RedStone-gated; the savings pool uses a **24h** window and is oracle-free (§2).

```
DegenAccount.createWithdrawalIntent(bytes32 asset, uint256 amount)               // step 1, RedStone-gated
DegenAccount.executeWithdrawalIntent(bytes32 asset, uint256[] intentIndices)     // step 2, RedStone-gated
DegenAccount.cancelWithdrawalIntent(bytes32 asset, uint256 intentIndex)          // oracle-free
DegenAccount.getUserIntents(bytes32 asset) -> IntentInfo[]                       // view, oracle-free
DegenAccount.getAvailableBalance(bytes32 asset) -> uint256                       // view, oracle-free
DegenAccount.getTotalIntentAmount(bytes32 asset) -> uint256                      // view, oracle-free
```

**Timing (from `IntentInfo` flags on-chain):**
- `actionableAt = createdAt + 24h`
- `expiresAt = actionableAt + 48h`
- Executable in a **24h–72h window**, then expires.

**`IntentInfo` struct:**
```
(uint256 amount, uint256 actionableAt, uint256 expiresAt,
 bool isPending, bool isActionable, bool isExpired)
```

**Step 1 — `withdraw-collateral`:**
- Calls `createWithdrawalIntent(symbol, amountWei)`. Oracle-free, no payload.
- Pre-flight read: `getAvailableBalance(symbol)` (in-account balance minus existing pending intents). Refuses if `amountWei > available`.

**Step 2 — `execute-withdrawal`:**
- Reads `getUserIntents(symbol)`; selects either the explicit `--index N` or all currently-actionable indices.
- Sorts indices strictly increasing (`executeWithdrawalIntent` requires it).
- Refuses if any selected index is `isExpired` (cancel/clear instead) or not yet `isActionable`.
- Appends a fresh RedStone payload (`degen_account_price_feeds` + the asset symbol if feed-available) and broadcasts.

**Step 3 — `cancel-withdrawal`:**
- Calls `cancelWithdrawalIntent(symbol, index)`. Oracle-free.
- Useful when changing your mind about a queued withdrawal, or freeing up the locked amount for `swap-debt` / `repay` first.

**`withdrawal-intents` (read-only):**
- Walks every owned asset (`getAllOwnedAssets()`), reads `getAvailableBalance` + `getTotalIntentAmount` + `getUserIntents`, prints with READY / maturing / EXPIRED state plus a chain-time-anchored "actionable in 23h45m, expires in 71h45m" window.

**Gotchas:**
- **There is no instant collateral withdrawal.** Period. Plan around the 24h lock or use `cancel-withdrawal` to recover.
- The `getAvailableBalance` view subtracts pending intents — useful when sizing a second intent, but also means stacking many small intents can lock up the balance even before any matures.
- `executeWithdrawalIntent` is RedStone-gated and calls `canRepayDebtFully` + `remainsSolvent` internally — if the Degen Account's health changed adversely between create and execute (e.g. collateral price dropped), execute can revert and the funds stay locked until the window expires (then cancel + retry).

---

## 9. Aerodrome (read-only inventory in v1) — ✅ SHIPPED as `aerodrome-positions`

```
DegenAccount.getOwnedStakedAerodromeTokenIds() -> uint256[]        // wired, oracle-free
DegenAccount.getPositionCompositionSimplified(uint256 tokenId)     // wired but return shape undecoded
```

**v1 ships only the inventory view** — list of Aerodrome NFT tokenIds the Degen Account owns or has staked. Composition decoding (per-token amounts inside each position) needs the return shape worked out and is deferred.

**Wired write selectors NOT exposed in v1** (exist on the diamond, kept for v2 reference):
- `claimRewardsAerodrome(uint256)` — claim AERO / pool fees for a position.
- `decreaseLiquidityAerodrome(uint256, uint128, uint256, uint256, uint256)` — partial / full liquidity removal.

**Deferred to v2** (exist on Aerodrome itself, exact diamond signatures still need probing): `depositLiquidityAerodrome`, `stakePositionAerodrome`, `unstakePositionAerodrome`, `increaseLiquidityAerodrome`.

The v1 command lists tokenIds and points the user at the Aerodrome UI for manage / claim. Once the composition return shape is decoded and the write signatures probed against a live position, v2 can expose `aerodrome-claim` / `aerodrome-decrease` and eventually full add/stake.

---

## RedStone wrapping — SHIPPED (`build_redstone_payload` in the `degenprime` module)

RedStone config on Base is **identical** to DeltaPrime's Avalanche config (same data service `redstone-primary-prod`, same 5 authorised signers, same 3-of-5 threshold, same marker bytes, same gateways). The wrap implementation is a direct port:

1. Fetch signed packages from the gateway (`/data-packages/latest/redstone-primary-prod`). Per-run cache so one `summary` hits the gateway once.
2. For each requested feed symbol, recover the signer (`ecrecover` over `keccak256(body)` with no EIP-191 prefix), filter to the authorised set, take the first 3.
3. Serialise each package: `for each dataPoint: feedId(bytes32) ++ value(uint256, 1e8-scaled, big-endian)`, then trailer `timestamp_ms(6) ++ dataPointValueByteSize(4)=32 ++ dataPointsCount(3)`, then `signature(65) = r ++ s ++ v`.
4. Concatenate all packages, append `dataPackagesCount(2) ++ unsignedMetadataSize(3)=0 ++ marker(9 bytes)`.
5. Append the payload bytes to the function calldata (after the normal ABI-encoded args). Sign + send (or `eth_call` for RedStone-gated reads).

The value scaling MUST reproduce RedStone's `parseUnits(Number(value).toFixed(8), 8)` exactly. The tool uses `Decimal(float(value)).quantize(Decimal(1).scaleb(-8), ROUND_HALF_UP)` to match that byte-for-byte — the naïve `int(round(value * 1e8))` double-rounds and produces a body the contract ecrecovers wrong, yielding `SignerNotAuthorised`. This was the load-bearing fix on DeltaPrime; kept verbatim here.

**Feed coverage on Base is partial** (see reference §4). The tool's `degen_account_price_feeds(account)` filters owned + debt symbols to the 13 RedStone-available ones; the SolvencyFacet sources BaseOracle prices for the rest internally. Sending a payload that lists a symbol the gateway doesn't have crashes the lookup with a clear error; sending a payload that omits a BaseOracle-priced symbol is fine.

**Read-only RedStone-gated views** (used by `summary`): `getTotalValue`, `getDebt`, `getHealthRatio`, `isSolvent`, `getPrices`. All four revert `0xe7764c9e` on a bare `eth_call`. `redstone_view_call(w3, account, fn_name, payload, args)` builds the calldata + appends the payload + `eth_call`s, then decodes the result against the function's output types. Same payload feeds all of them in a single `summary` pass.

**Writes that need NO payload:** `deposit`, `withdraw`, `fund`, `repay`, `createLoan`, `createAndFundLoan`, `createWithdrawalIntent`, `cancelWithdrawalIntent`. These don't carry `remainsSolvent`; the facet handles their safety via direct balance / debt checks.

**Writes that DO need the payload:** `borrow`, `paraSwapV6` (swap), `swapDebtParaSwap` (swap-debt), `executeWithdrawalIntent` (the matured step of collateral withdrawal).
