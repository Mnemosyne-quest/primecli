# DeltaPrime Capabilities — Build Spec

Per-capability build spec for the full DeltaPrime capability surface on Avalanche C-chain (chainId 43114), precise enough to wire each one into the `deltaprime` CLI command. Verified on-chain 23-05-2026 against the live diamond beacon and `DeltaPrimeLabs/deltaprime-contracts-v2` source. Sibling to `deltaprime-reference.md` (which has the high-level model, pools, and the full command table).

**Build status (24-05-2026):** the RedStone payload wrap is shipped, and most of this spec is now tooled — including zaps as tool-level macros (§7) and PRIME leverage tiers (§8). Section headers marked ✅ SHIPPED name the command(s) that implement them; the build detail below each is kept as the verified implementation record. Still untooled: Wombat liquid-staking LP (§6a), GLP (§6c), and PangolinDEX LP (§6d).

**Everything below runs on the Prime Account** (the per-user EIP-2535 diamond). All functions are reached by calling the diamond at the Prime Account's own address — the facet logic is shared via the beacon `0x2916B3bf7C35bd21e63D01C93C62FB0d4994e56D`. Call from the EOA owner; the diamond enforces `onlyOwner` (= `DiamondStorageLib.contractOwner()` = the EOA that created the account).

---

## Conventions (read first)

- **bytes32 asset symbols**: same scheme as the lending core. Right-pad the ASCII symbol with zero bytes to 32. Symbols: `AVAX`, `USDC`, `ETH`, `BTC`, `USDT`, `EUROC`, plus staking/LP symbols like `ggAVAX`, `sAVAX`, `PRIME`, `GM_*` market symbols. Use `symbol.encode().ljust(32, b"\x00")`.
- **Decimals**: USDC 6, USDT 6, BTC.b 8, WAVAX 18, WETH.e 18, GM tokens 18, most LP/stake tokens 18. The TokenManager maps token address → symbol; resolve unknowns via `TokenManager.tokenAddressToSymbol(address)`.
- **Solvency gating**: nearly every state-changing facet function carries `remainsSolvent` / `noBorrowInTheSameBlock` / `notInLiquidation` modifiers. These run the RedStone-gated solvency math **inside the transaction**, so a real broadcast needs RedStone signed price calldata appended to the call (the RedStone EVM connector / SDK wraps the tx). Plain `eth_call` previews of these functions revert with `0xe7764c9e` ("missing oracle payload") — same gotcha as `getHealthMeter()`. **This is the single biggest implementation hurdle for every write capability below.** See "RedStone wrapping" at the end.
- **Approve targets**: swaps and LP adds approve the *router/aggregator* (handled internally by the facet via `safeApprove` — the Prime Account already holds the funds, so the EOA does NOT approve anything for these; funds are already inside the account). Funding the account from the EOA still uses the lending-core `fund()` flow (approve the Prime Account). 
- **Funds live inside the Prime Account.** Swaps, LP, staking all operate on balances *already inside* the account. The flow is always: `fund` collateral in (or `borrow` to create leverage) → then swap/LP/stake using in-account balances. You never pass tokens from the EOA into a swap call.

---

## 1. Swap assets (Assets tab → Swap) — ✅ SHIPPED as `swap --from S --to S --amount N [--via yak|paraswap] [--slippage P]`

Two aggregator routes (`--via yak` default, or `--via paraswap`). App lets the user pick whichever gives the better quote.

### 1a. ParaSwap (Velora on Avalanche) — `ParaSwapFacet` `0x3732ba82d54568609b2E63cB64487af0D7f3FBcc`

```
paraSwapV6(bytes4 selector, bytes data)
```
- `selector` + `data` are the **ParaSwap/Velora API swap calldata** (the 4-byte method selector of the ParaSwap Augustus V6 router call, and the ABI-encoded args) obtained from the ParaSwap API `/swap` (or `/transactions`) endpoint for this chain. The facet decodes it (`decodeParaSwapData`), validates srcToken/destToken are supported assets, executes the swap from the account's balance, and re-syncs exposure.
- Build: query ParaSwap API for a route srcToken→destToken on Avalanche, with the **Prime Account address as the sender/receiver**, extract the call's selector and the remaining calldata, pass them in. Slippage is encoded in the ParaSwap quote (`destAmount`/`minDestAmount`).
- `paraSwapBeforeLiquidation(bytes4,bytes)` — same but `onlyWhitelistedLiquidators`; liquidation-only, not for us.

### 1b. YieldYak Swap — `YieldYakSwapFacet` `0x7b90769acaFb6540D00C06c406ba01Ab58B3028C`

```
yakSwap(uint256 amountIn, uint256 amountOut, address[] path, address[] adapters)
```
- `amountIn` — sell amount (capped to available balance internally). `amountOut` — minimum bought (slippage floor). `path` — token-address hops `[srcToken, ..., destToken]`. `adapters` — YieldYak adapter address per hop; **every adapter must be in the facet whitelist** (revert otherwise).
- Build: call YieldYak router `0xC4729E56b831d74bBc18797e0e17A295fA77488c` `findBestPath(amountIn, tokenIn, tokenOut, maxSteps)` off-chain to get `path` + `adapters` + expected out, set `amountOut = expected * (1 - slippage)`, pass through.
- Whitelisted adapters (subset, from source): UnilikeAdapter (`0xDB66686Ac8bEA67400CF9E5DD6c8849575B90148`, `0x3614657EDc3cb90BA420E5f4F61679777e4974E3`, `0x3f314530a4964acCA1f20dad2D35275C23Ed7F5d`), CurvePlainAdapter, UniswapV3Adapter, LB2Adapter (TraderJoe LB), WoofiV2Adapter, SAvaxAdapter, WAvaxAdapter, WombatAdapter, GGAvaxAdapter (`0x79632b8194a1Ce048e5d9b0e282E9eE2d4579c20`), and others. `isWhitelistedAdapterOptimized(address)` is a view to check.
- Note: the facet rewrites the sAVAX/wsAVAX address pair (`0xaE64d5...` ↔ `0x9e295B...`) internally.

**Recommendation:** YakSwap is fully on-chain-derivable (router findBestPath → path+adapters), so it's the easier route to tool. ParaSwap needs an off-chain API call to build calldata.

---

## 2. Swap debt / refinance (Assets tab → Swap Debt) — ✅ SHIPPED as `swap-debt --from S --to S --amount N` — `SwapDebtFacet` `0x1e36f07aCaB2Ed9989f2364e27FeD7af92C0ff49`

```
swapDebtParaSwap(bytes32 _fromAsset, bytes32 _toAsset, uint256 _repayAmount, uint256 _borrowAmount, bytes4 selector, bytes data)
```
- Refinances debt from one asset to another of (roughly) equal USD value. Mechanics: borrow `_borrowAmount` of `_toAsset` → ParaSwap-swap it into `_fromAsset` (`selector`+`data` is the ParaSwap calldata, same format as §1a) → repay `_repayAmount` of `_fromAsset` debt.
- `_repayAmount` is capped to current borrowed amount of `_fromAsset`. `_borrowAmount` must be > 0 and `_fromAsset != _toAsset`.
- **Hard guard: max 5% USD-value difference** between repay value and borrow value (`maxDiff <= 500` bps), priced via RedStone. `paraSwapDecodedData.fromAmount` must equal `_borrowAmount` exactly.
- Build: pick from/to symbols, set `_borrowAmount` so its USD value ≈ current debt USD value (within 5%), get ParaSwap calldata for `_toAsset`→`_fromAsset` swapping `_borrowAmount`, pass through.

---

## 3. Withdraw collateral (Assets tab → Withdraw) — ✅ SHIPPED as `withdraw-collateral` / `withdrawal-intents` / `execute-withdrawal` — `WithdrawalIntentFacet` `0xf88f82e8982de4f7831B0A8BA55Ce23536872FD9`

**There is no instant withdraw on the Prime Account.** Pulling collateral OUT of the account to the EOA is a **two-step, time-delayed intent** flow:

```
createWithdrawalIntent(bytes32 _asset, uint256 _amount)      // step 1: register intent
executeWithdrawalIntent(bytes32 _asset, uint256[] intentIndices)  // step 2: after 24h, before 72h
cancelWithdrawalIntent(bytes32 _asset, uint256 intentIndex)  // abort a pending intent
clearExpiredIntents(bytes32 _asset)                          // housekeeping (anyone can call)
```
- Timing (from source): `actionableAt = now + 24h`, `expiresAt = actionableAt + 48h`. So an intent is executable in a **24h–72h window**, then expires.
- `executeWithdrawalIntent` checks `canRepayDebtFully` + `remainsSolvent`, transfers the asset to `msg.sender` (the owner EOA). `intentIndices` must be strictly increasing.
- Views: `getUserIntents(bytes32) -> IntentInfo[]` (amount, actionableAt, expiresAt, isPending/isActionable/isExpired), `getTotalIntentAmount(bytes32)`, `getAvailableBalance(bytes32)` (= in-account balance minus staked minus pending intents — this is the oracle-free "how much can I still act on" view).
- Build: a `withdraw-collateral` command needs two phases (create now, execute ≥24h later). Track intent indices off-chain (or read them via `getUserIntents`). `getAvailableBalance` is a clean read-only view (no RedStone) — good for `prime-summary`.

---

## 4. GMX V2 LP — GM tokens (LP tab) — ✅ SHIPPED as `gmx-deposit` / `gmx-withdraw` / `gmx-positions`

GM tokens are GMX V2 market LP tokens (long+short composite). DeltaPrime mints/redeems them through the GMX V2 ExchangeRouter via a two-leg async flow (deposit request → GMX keeper executes → callback). **All deposit/withdraw functions are `payable` and require a GMX execution fee as `msg.value`.** The deposit/withdraw are **asynchronous and keeper-executed**: the request is queued, then a GMX keeper executes it some blocks later via a callback. The position does not appear/disappear instantly, and the **Prime Account is frozen until the callback fires**.

> **For the full open/change/close workflow + worked example + the two gotchas (a)/(b), see `deltaprime-reference.md` §12.** This section is the on-chain build spec; §12 is the operational path.

**Freeze behaviour — NOT tooled (deliberately).** A GMX deposit/withdraw calls `DiamondStorageLib.freezeAccount(gmToken)` (`GmxV2Facet`/`GmxV2PlusFacet`), setting a single global `SmartLoanStorage.frozenSince = block.timestamp` (NOT per-market). The GMX keeper callbacks (`GmxV2CallbacksFacet`, afterDeposit/Withdrawal Execution/Cancellation) call `unfreezeAccount` → `frozenSince = 0`. We do not surface freeze state in the tool: there is **no external getter** (`isAccountFrozen()` is `internal`, and reading the raw storage slot proved unreliable — the offset that looked like `frozenSince` actually held an unrelated init field that false-positived a brand-new account), and the manual `unfreezeAccount()` (`AssetsOperationsAvalancheFacet`, selector `0x7c5fc3fb`, no args) is **`onlyWhitelistedLiquidators`** — an owner EOA cannot call it, so there is no self-recovery regardless. The keeper callback (normally within minutes) is the real unfreeze path; the practical rule is to re-check `gmx-positions` after a deposit/withdraw.

GMX infra (both facets): Router `0x820F5FfC5b525cD4d88Cd91aCf2c28F16530Cc68`, ExchangeRouter `0xF0864BE1C39C0AB28a8f1918BC8321beF8F7C317`, DepositVault `0x90c670825d0C62ede1c5ee9571d6d9a17A722DFF`, WithdrawalVault `0xf5F30B10141E1F63FC11eD772931A8294a591996`. Callback gas limit 600000 (`callbackGasLimit`, hard-coded in both facets). The execution fee is estimated from the GMX DataStore gas params times the gas price, padded by `--fee-buffer` (default 2x); GMX refunds any excess to the account.

**Execution-fee gas-price floor (real bug, fixed 24-05-2026).** Avalanche's live base fee can be ~0.01–0.02 gwei, which estimates a uselessly tiny fee the keeper rejects (the request expires and refunds without minting). The tool floors the gas price at **25 gwei** in the fee estimator so the `msg.value` clears GMX's requirement (~0.08–0.19 AVAX). The EOA must hold the execution fee up front, on top of the deposit amount and its own tx gas.

### 4a. GM (two-sided, long+short token) — `GmxV2FacetAvalanche` `0x759902b8D105cBB20D1b2C7b76b355a175E32286`

Markets: GM_AVAX (`0x913C1F46b48b3eD35E7dc3Cf754d4ae8499F31CF`, WAVAX/USDC), GM_BTC (`0xFb02132333A79C8B5Bd0b64E3AbccA5f7fAf2937`, BTC.b/USDC), GM_ETH (`0xB7e69749E3d2EDd90ea59A4932EFEa2D41E245d7`, WETH.e/USDC).
```
depositAvaxUsdcGmxV2(bool isLongToken, uint256 tokenAmount, uint256 minGmAmount, uint256 executionFee)  payable
depositBtcUsdcGmxV2(bool isLongToken, ...)   payable
depositEthUsdcGmxV2(bool isLongToken, ...)   payable
withdrawAvaxUsdcGmxV2(uint256 gmAmount, uint256 minLongTokenAmount, uint256 minShortTokenAmount, uint256 executionFee)  payable
withdrawBtcUsdcGmxV2(...)   payable
withdrawEthUsdcGmxV2(...)   payable
```
- `isLongToken=true` deposits the volatile leg (WAVAX/BTC.b/WETH.e); `false` deposits USDC (short leg). `tokenAmount` from in-account balance. `minGmAmount` = slippage floor on GM minted. `executionFee == msg.value` (GMX keeper gas, in AVAX).
- Withdraw burns `gmAmount` GM, returns both legs with `minLongTokenAmount`/`minShortTokenAmount` floors.
- **Account is frozen** after a deposit/withdraw request until the GMX keeper callback fires (async). `getGmTokenBalanceAfterFees(address)` / `getGmxPositionBenchmark(address)` views on `SmartLoanViewFacet`. `getGmPerformance(address)` view here.

### 4b. GM Plus (single-sided, long-token-only markets) — `GmxV2PlusFacetAvalanche` `0xe9C87e730f3a5972C9EA78995d32eb2Fd936D7Bf`

Markets: GM_AVAX (`0x08b25A2a89036d298D6dB8A74ace9d1ce6Db15E5`, WAVAX-only), GM_BTC (`0x3ce7BCDB37Bf587d1C17B930Fa0A7000A0648D12`, BTC.b-only), GM_ETH (`0x2A3Cf4ad7db715DF994393e4482D6f1e58a1b533`, WETH.e-only).
```
depositAvaxGmxV2Plus(uint256 tokenAmount, uint256 minGmAmount, uint256 executionFee)  payable
depositBtcGmxV2Plus(...)   payable
depositEthGmxV2Plus(...)   payable
withdrawAvaxGmxV2Plus(uint256 gmAmount, uint256 minLongTokenAmount, uint256 minShortTokenAmount, uint256 executionFee)  payable
withdrawBtcGmxV2Plus(...)  payable
withdrawEthGmxV2Plus(...)  payable
```
- Single-asset GM markets (no USDC short leg). `getGmPlusPerformance(address)` view.

**FULL-solvency-payload gotcha (real bug, fixed 24-05-2026).** Before minting, the deposit facet runs an inline solvency check that prices **every** debt-registry asset — the whole pool set `AVAX, USDC, BTC, ETH, USDT, EUROC`, even ones with zero balance/debt — each needing 3 unique RedStone signers in the appended payload. The tool builds the write payload from `prime_account_price_feeds(account)` + the GM feed. If any required feed is missing, the deposit reverts with `InsufficientNumberOfUniqueSigners(0,3)` (wrapped in DeltaPrime's `ProxyCalldataFailedWithCustomError`). The **read** path (`gmx-positions`) does NOT hit this — GM view calls skip the full solvency simulation, so only the write triggers it. (This is why a read can succeed while a naive write reverts.)

**Callbacks** are on `GmxV2CallbacksFacetAvalanche` `0xfcCf6CDf19AAD9D5cA8771370B3ba8d973fA97ee` (afterDeposit/Withdrawal Execution/Cancellation) — called by the GMX keeper, never by us. Listed for completeness.

---

## 5. TraderJoe V2 Liquidity Book — concentrated liquidity (LP tab) — ✅ SHIPPED as `lb-add` / `lb-remove` / `lb-positions` — `TraderJoeV2AvalancheFacet` `0x1899F6D524637808f2d53125b6CCFe6D2dF1Fa91`

LB liquidity is distributed across discrete price **bins** (each bin = a fixed price; `binStep` sets spacing). "Shapes" (Spot/Curve/Bid-Ask) are just different `distributionX`/`distributionY` weightings across `deltaIds` relative to the active bin.

```
addLiquidityTraderJoeV2(address traderJoeV2Router, LiquidityParameters liquidityParameters)
removeLiquidityTraderJoeV2(address traderJoeV2Router, RemoveLiquidityParameters parameters)
fundLiquidityTraderJoeV2(address pair, uint256[] ids, uint256[] amounts)   // deposit existing LB tokens into the account
claimReward(address pair, uint256[] ids)                                   // claim LB hook rewards
getOwnedTraderJoeV2Bins() -> TraderJoeV2Bin[]                              // view: owned (pair,id) bins
getJoeV2RouterAddress() -> address                                         // view
```
- **`LiquidityParameters`** (ILBRouter struct, the `addLiquidity` arg): `(IERC20 tokenX, IERC20 tokenY, uint256 binStep, uint256 amountX, uint256 amountY, uint256 amountXMin, uint256 amountYMin, uint256 activeIdDesired, uint256 idSlippage, int256[] deltaIds, uint256[] distributionX, uint256[] distributionY, address to, address refundTo, uint256 deadline)`. The facet overrides `to`/`refundTo` to the account. `deltaIds` are bin offsets from the active bin; `distributionX`/`distributionY` are the per-bin weightings (sum to 1e18) that define the shape.
- **`RemoveLiquidityParameters`**: `(IERC20 tokenX, IERC20 tokenY, uint16 binStep, uint256 amountXMin, uint256 amountYMin, uint256[] ids, uint256[] amounts, uint256 deadline)`. Note `binStep` here is **uint16** (the `LiquidityParameters.binStep` on the add side is uint256).
- Whitelisted **routers**: TJ V2.1 `0xb4315e873dBcf96Ffd0acd8EA43f689D8c20fB30`, TJ V2.2 `0x18556DA13313f3532c54711497A8FedAC273220E`. The router resolves the pair via `getFactory().getLBPairInformation(tokenX, tokenY, binStep)`.
- Whitelisted **pairs** (13, from source) — only these can be LP'd: WAVAX/USDC, WETH.e/WAVAX, BTC.b/WAVAX, USDt/USDC, JOE/WAVAX (v2.1); WAVAX/BTC.b, WAVAX/USDC, BTC.b/USDC, aUSD/WAVAX, EURC/USDC, EURC/WAVAX, aUSD/USDt, aUSD/USDC (v2.2). Pair addresses listed in reference doc.
- **Max 80 bins per Prime Account** (`maxBinsPerPrimeAccount() = 80`). Owned bins tracked in account storage.
- Build: this is the most complex capability — needs the LB SDK / router math to compute `deltaIds` + distributions for a chosen shape and price range. Defer until simpler caps are done.

---

## 6. Farms / staking / liquid staking (Farms tab)

### 6a. Wombat — sAVAX & ggAVAX LP + staking — `WombatFacet` `0x94aAa81E3Efc79a485D7Ef78A9df9a9aE9437Bae`

This is **the liquid-staking-yield path on this deployment** (there is NO standalone GogoPool facet deployed — `swapToGgAvax` selector is absent from the live diamond). Wombat AVAX/sAVAX and AVAX/ggAVAX stable pools, with auto-staking for rewards.
```
depositAvaxToAvaxSavax(uint256 amount, uint256 minLpOut)         // AVAX leg into AVAX/sAVAX pool
depositSavaxToAvaxSavax(uint256 amount, uint256 minLpOut)        // sAVAX leg
depositAndStakeAvaxSavaxLpAvax(uint256 amount)                   // deposit + stake (AVAX leg)
depositAndStakeAvaxSavaxLpSavax(uint256 amount)
depositAvaxToAvaxGgavax(uint256 amount, uint256 minLpOut)        // AVAX/ggAVAX pool
depositGgavaxToAvaxGgavax(uint256 amount, uint256 minLpOut)
depositAvaxGgavaxLpGgavax(uint256 amount)
depositAndStakeAvaxGgavaxLpAvax(uint256 amount)
withdrawAvaxFromAvaxSavax(uint256 amount, uint256 minOut)        // + ...InOtherToken variants
withdrawSavaxFromAvaxSavax(uint256 amount, uint256 minOut)
withdrawAvaxFromAvaxGgavax(uint256 amount, uint256 minOut)
withdrawGgavaxFromAvaxGgavax(uint256 amount, uint256 minOut)
// each withdraw has an ...InOtherToken(uint256,uint256) variant (exit to the opposite asset)
claimAllWombatRewards()
// views: avaxBalanceAvaxSavax(), sAvaxBalanceAvaxSavax(), avaxBalanceAvaxGgavax(),
//        ggAvaxBalanceAvaxGgavax(), pendingRewardsForAvax{Savax,Ggavax}Lp{Avax,Savax,Ggavax}()
```
- To get exposure to ggAVAX/sAVAX yield: deposit AVAX into the Wombat AVAX/ggAVAX (or AVAX/sAVAX) pool and stake the LP. Rewards via `claimAllWombatRewards()`. Alternatively, plain liquid-staking exposure can be obtained by **swapping** AVAX→ggAVAX via `yakSwap` using the GGAvaxAdapter (§1b).

### 6b. sJOE staking — ✅ SHIPPED as `sjoe-stake` / `sjoe-unstake` / `sjoe-claim` / `sjoe-position` — `SJoeFacet` `0x8aD9028f60Cf0F823271FE689EbDD0A58492cC75`
```
stakeJoe(uint256 amount)       // onlyOwner + remainsSolvent → RedStone-gated on --execute
unstakeJoe(uint256 amount)     // onlyOwnerOrInsolvent → NOT solvency-gated, no payload
claimSJoeRewards()             // onlyOwner + remainsSolvent → RedStone-gated on --execute
// views: joeBalanceInSJoe(), rewardsInSJoe()  (oracle-free)
```
Stake in-account JOE into sJOE for USDC fee rewards. Each reward-bearing call skims a 10% protocol fee off the USDC claimed in that tx, so the account nets ~90% of realised rewards.

### 6c. GLP (legacy GMX V1 LP) — `GLPFacet` `0x419404442A77F9bb718f48856f6D2c09f7959fc5` + `YieldYakFacet` `0xF62b626324d65183933d697CDb45be96E3C7da92`
```
mintAndStakeGlp(address token, uint256 amount, uint256 minUsdg, uint256 minGlp)   // GLPFacet
unstakeAndRedeemGlp(address token, uint256 glpAmount, uint256 minOut)
claimGLpFees()
stakeGLPYak(uint256 amount)  / unstakeGLPYak(uint256 amount)                       // YieldYakFacet (auto-compound via YieldYak)
```
Also `fundGLP(uint256)` on `AssetsOperationsAvalancheFacet`. Legacy; lower priority.

### 6d. PangolinDEX LP (v2 AMM) — `PangolinDEXFacet` `0xf907Fdd5B20dD074bf0D18b8a8d0cacE71170DDa`
```
addLiquidityPangolin(bytes32 assetA, bytes32 assetB, uint256 amountA, uint256 amountB, uint256 minA, uint256 minB)
removeLiquidityPangolin(bytes32 assetA, bytes32 assetB, uint256 liquidity, uint256 minA, uint256 minB)
```
Standard UniV2-style LP. Lower priority.

---

## 7. Zaps (one-click leveraged entry) — ✅ SHIPPED as `zap`

Zaps are **not a separate on-chain facet** — they are a **front-end orchestration** that bundles the primitives above into one UX click (e.g. "leveraged long AVAX/USDC GM": fund collateral → borrow USDC → swap → GMX deposit, all in one signed flow). On-chain it decomposes into the §1–§6 calls. No new ABI needed — built as a tool-level macro over the existing leg commands.

**Implementation (`cmd_zap`).** One bounded **leveraged-long** flow, the canonical DeltaPrime first-zap, terminating in a two-sided GM market deposit. Legs, each its own transaction, composing the existing commands verbatim (no re-encoding): (1) `cmd_fund` collateral in → (2) `cmd_borrow` USDC (the leverage) → (3) OPTIONAL `cmd_swap` USDC→long token (`--swap`, YieldYak route) → (4) `cmd_gmx_deposit` into `--market`. Each leg takes an **explicit amount** (`--collateral-amount` / `--borrow-amount` / `--deposit-amount` / `--side`) — no fragile auto-sizing across the oracle-priced/async boundary. To support programmatic composition the five leg commands now `return ok` (bool) at their broadcast tail; the zap's execute loop treats any non-`True` return as a stop (fail-closed). **Preview** prints the full ordered plan and runs each leg in preview (nothing broadcast), flagging solvency-gated legs. **`--execute`** runs the legs sequentially and **stops immediately on the first failure**, reporting which legs completed and which failed (partial-state safety — it warns that completed legs are live on-chain and to review with `prime-summary` before retrying rather than blindly re-running). The terminal GMX leg is **async** — `--execute` only fires the deposit request; a keeper mints the GM later and the account freezes until then (see §4; re-check `gmx-positions` once it settles). Only the GM-terminal long is built; an LB-terminal long is reachable by running fund → borrow → [swap] then `lb-add` manually (surface kept small deliberately).

---

## 8. Prime token leverage tiers — ✅ SHIPPED as `prime-tier` / `prime-needed` / `prime-activate` / `prime-deactivate` / `prime-unstake` / `prime-repay` — `PrimeLeverageFacet` `0x912609401D93779bEd71C9027c5f11f518397Bdd`

Staking the protocol's **PRIME** token unlocks PREMIUM tier (10x max leverage vs the BASIC ~5x default). Verified on Snowtrace 24-05-2026 against the verified `PrimeLeverageFacet` source (Solidity 0.8.17, BUSL-1.1) + `LeverageTierLib` + `IPrimeLeverageFacet`.

**Tiers** — `LeverageTierLib.LeverageTier` enum, **`uint8` on the wire**: `BASIC=0` (~5x), `PREMIUM=1` (10x), `_NON_EXISTENT=2`.

**PRIME token** — `0x33c8036e99082b0c395374832fecf70c42c7f298`, symbol `PRIME`, 18 decimals. Resolved on-chain via `TokenManager.getAssetAddress("PRIME", true)` (registered only as a stakeable asset; `false` reverts "Asset inactive"). **Not** sPRIME (a separate PRIME-AVAX LP receipt token). Acquired on a DEX (LFJ/TraderJoe PRIME-WAVAX).

**Required-PRIME formula (verified `getRequiredPrimeStake`):** `requiredPrimeStake = borrowedValue * tieredPrimeStakingRatio(tier) / (100 * 1e18)`, where `borrowedValue` is 18-dec USD and the result is PRIME wei. `tieredPrimeStakingRatio` lives in the **TokenManager** and is **governance-mutable** (1.2e18 for PREMIUM as of 24-05-2026 = 1.2 PRIME / $100; 0 for BASIC). The tool **never hard-codes the ratio** — it calls the view. Live: $1,000 borrow at PREMIUM → **12 PRIME** (sanity-checked 24-05-2026).

**PRIME rent-debt.** PREMIUM does **not** change the USD borrow APR. Its cost is a PRIME-denominated debt that accrues over time (`tieredPrimeDebtRatio`, 0.5e18 for PREMIUM = 0.5 PRIME / $100 / yr; 0 for BASIC): `accruedPrimeDebt = totalBorrowedValueUSD * tieredPrimeDebtRatio * timeElapsed / (100 * 365 days * 1e18)`. `getLeverageTierFullInfo().recordedDebt` is the last on-chain snapshot; unsnapshotted accrual is added on the next write or `updatePrimeDebt()`. `getCurrentPrimeDebt` is `internal` (not callable), so the tool reports the snapshot and flags that accrual since it is not included.

**Writes (verified modifiers):**
```
depositPrime(uint256 _amount)          onlyOwner + noBorrowInTheSameBlock + nonReentrant + remainsSolvent
                                       -> RedStone-gated. Pulls PRIME from the EOA (safeTransferFrom; ERC20
                                          approve to the Prime Account first), caps to the EOA balance, adds it
                                          as an IN-ACCOUNT balance (syncExposureOfPrime — deliberately NOT a
                                          solvency-counted owned asset).
stakePrimeAndActivatePremium()         onlyOwner + nonReentrant  (NOT remainsSolvent -> no payload)
                                       -> stakes getRequiredPrimeStake(PREMIUM, (totalValue - debt) * 10) from
                                          the IN-ACCOUNT PRIME balance (provisions the 10x-max-debt stake up
                                          front), sets tier=PREMIUM, snapshots debt. Reverts if already PREMIUM
                                          or if in-account PRIME < required ("Insufficient PRIME balance").
deactivatePremiumTier(bool withdraw)   onlyOwner + nonReentrant
                                       -> repays the FULL current PRIME debt first (reverts if in-account PRIME
                                          can't cover it; 50% burn to 0x…dEaD / 50% treasury), drops to BASIC.
                                          withdraw=true releases stake above the new BASIC requirement (=0).
unstakePrime(uint256 amount)           onlyOwner + nonReentrant
                                       -> guards (when still PREMIUM): remaining stake >= borrowedValue*ratio/
                                          (100*1e18) AND >= current PRIME debt (snapshots debt first — L-02 fix).
repayPrimeDebt(uint256 amount)         onlyOwner
                                       -> snapshots debt, caps amount to current debt (no overpayment), splits
                                          50% burn / 50% treasury, decrements recordedPrimeDebt.
updatePrimeDebt()                      public — snapshots accrued debt into recordedPrimeDebt.
```

**Views (oracle-free unless noted):**
```
getLeverageTier() -> uint8
getLeverageTierFullInfo() -> (uint8 currentTier, uint256 stakedPrime, uint256 recordedDebt)
getPrimeStakedAmount() -> uint256 (PRIME wei)
getRequiredPrimeStake(uint8 tier, uint256 borrowedValue1e18) -> uint256 (PRIME wei)
shouldLiquidatePrimeDebt() -> bool   // NON-view: MUTATES (snapshots debt). The tool only eth_calls it
                                     // (read-only sim), never broadcasts. It reads _getDebt() internally,
                                     // so despite being a PRIME-side check it hits the solvency oracle path
                                     // and reverts 0xe7764c9e on a BARE call — a RedStone payload must be
                                     // appended (confirmed live 24-05-2026). The other four views are truly
                                     // oracle-free and return on a bare call.
```

**Liquidation track.** `shouldLiquidatePrimeDebt()` returns true when `primeDebt > weeklyAccrualBuffer + stakedPrime` (accrued PRIME rent has eaten through the staked PRIME plus a one-week buffer). `liquidatePrimeDebt()` (`onlyWhitelistedLiquidators`) then seizes staked PRIME (50% burn / 50% treasury) and force-downgrades to BASIC if stake can't cover debt. This is a **separate track** from normal USD-solvency liquidation — it polices the PRIME rent, not the loan health.

**Tooling.** `prime-needed` and the four read fields in `prime-tier` are pure reads. `prime-activate --amount N` runs the approve → `depositPrime` (RedStone-wrapped, reusing `build_redstone_payload` exactly as the other gated writes) → `stakePrimeAndActivatePremium` sequence, fail-closing if the projected in-account PRIME is below the required stake. The other writes are `onlyOwner`-only, so no payload. Preview by default; `--execute` broadcasts.

---

## 9. Liquidation & housekeeping (not for us, reference only)

- `SmartLoanLiquidationFacet` `0xBc8bBFD5ae45D7E7619347DFB5da51ee5F980D85`: `liquidate(bool)`, `whitelistLiquidators`, `getLiquidationFeePercent`, `getHealthRatioSnapshot`, etc. Liquidators only.
- `WithdrawUnsupportedPositionsFacet` `0xaa9CEa1c69870F82e957f72C552a0b12d751Ba78`: `withdrawUnsupportedPositions()`, `hasUnsupportedAssets()`. Recovery path for delisted assets.
- `AssetsOperationsAvalancheFacet` extras: `unfreezeAccount()` (un-freeze after a GMX async op resolves — `onlyWhitelistedLiquidators`, NOT owner-callable, so not tooled; the GMX keeper callback is the normal unfreeze path), `withdrawUnsupportedToken(address)`, `addOwnedAsset`, `removeUnsupportedOwnedAsset`, `removeUnsupportedStakedPosition`.
- `DiamondCutFacet` `0x4d0884222d09259893fEA6F1569a4150803Ef6C4`: `diamondCut(...)`, `pause()`, `unpause()` — protocol governance, not user.
- `OwnershipFacet` `0x4c3Ee50716C5f6ef4C64aB74EC549fa450c8F22f`: `owner()`, `proposeOwnershipTransfer`, `acceptOwnership` — transfer a Prime Account.

---

## RedStone wrapping — SHIPPED (`build_redstone_payload` in the `deltaprime` module)

This was the gating hurdle; it is now implemented in the tool. Most solvency-gated writes in §1–§6 carry `remainsSolvent`, which calls `SolvencyFacetProdAvalanche` price math that reads RedStone signed prices from **calldata appended after the normal function args**. To broadcast these, the tx calldata must be `<normal abi-encoded call> ++ <RedStone payload>`. The tool replicates the RedStone EVM connector (`@redstone-finance/evm-connector`) wrapping in Python (`build_redstone_payload`): it fetches signed packages from the `redstone-avalanche-prod` gateway, picks 3 unique signers per feed, serialises them to the on-chain byte layout, and appends the payload (terminated by the 9-byte marker). The same wrapped payload also drives the RedStone-gated *read* views (`prime-summary` solvency, `gmx-positions`) via `redstone_view_call`. Reproduced here for reference:
1. Fetch a signed price package from the RedStone data service used by DeltaPrime (data feed ids = the asset symbols; data service id and unique-signers-threshold are read from `SolvencyFacetProdAvalanche` — selectors `0x360398a3`/`0x7a70bcce`/`0xc3abc376`, the RedStone config getters).
2. Append the packed RedStone payload bytes to the function calldata.
3. Sign + send.

Read-only views that do NOT need this: `getBalance`, `getDebts`, `getAllOwnedAssets`, `getAvailableBalance` (WithdrawalIntent), `getOwnedTraderJoeV2Bins`, `getUserIntents`, the sJOE balance views. (Note: `getGmPerformance`/`getGmTokenBalanceAfterFees` and the SolvencyFacet views DO need the wrap — they revert `0xe7764c9e` on a bare `eth_call`, so the tool appends the payload for those reads too.) Anything that quotes USD value or checks solvency needs the RedStone wrap.

With the wrap shipped, every LP/swap/stake/solvency-gated write can `--execute` (not just build + preview). **`lb-remove` (`removeLiquidityTraderJoeV2`) DOES require the payload** — the facet reverts `CalldataMustHaveValidPayload` / `0xe7764c9e` without it (verified + fixed 24-05-2026). The writes that genuinely need no payload are `unstakeJoe` (`sjoe-unstake`) and intent *creation* (`createWithdrawalIntent`).

## TraderJoe V2 LB — capabilities & limits

**CAN do via `lb-add`:** open a Liquidity Book position on any whitelisted pair (`avax-usdc` binStep 10, `avax-usdc-20` binStep 20, `btc-usdc`, `eth-avax`, `btc-avax`, `avax-btc`, `eurc-usdc`, `usdt-usdc`, `joe-avax`); choose the **shape** (`spot`/`curve`/`bidask`) and the **range** (`--range R` = 2R+1 bins, R≤~39, account-wide 80-bin cap); deposit one-sided or two-sided (`--amount-x`/`--amount-y`); bound execution with `--slippage`/`--id-slippage`. Token X is the volatile leg, Y the quote.

**CAN do via `lb-remove`:** close a pair's LB position — reads all owned bins for the pair and removes them.

**CANNOT do (tool gaps — design around these):**
- **No partial reduce.** `lb-remove` is full-close-only; there is no `--percent`/`--bins` partial. (The contract supports per-bin partial removal, so a partial-reduce primitive is buildable but not exposed — the "move 5% back into range" nudge real LPs use is unavailable today.)
- **No one-shot rebalance.** Re-centering = `lb-remove` then `lb-add` as two separate txs; mind the live active bin id and slippage between them. Every rebalance therefore realizes the *full* conversion + 2× gas — budget them as expensive, discrete events; prefer WAIT/WIDEN over frequent re-centers.

**RedStone payload:** `lb-add` is solvency-gated and appends the payload; **`lb-remove` ALSO requires it** — the DeltaPrime LB facet reverts `CalldataMustHaveValidPayload` (`0xe7764c9e`) without an appended payload (verified on a live remove and fixed 24-05-2026; the earlier "lb-remove needs no payload" assumption was wrong, in both tool copies).

**When to use which shape/range:** see `deltaprime-reference.md` → "TraderJoe V2 Liquidity Book — strategy" and the SKILL decision ruleset. Default for hands-off AVAX/USDC = SPOT, R=30–39.

## GMX V2 GM pools — capabilities & strategy pointer

**CAN do via `gmx-deposit`/`gmx-withdraw`:** provide/redeem GM liquidity on the two-sided markets `avax-usdc`, `btc-usdc`, `eth-usdc` (`GmxV2Facet`) and the GM+ single-sided variants (`GmxV2PlusFacet`); choose `--side long|short`; partial withdraw by GM amount. Two-sided pools earn swap+trading+borrow fees and hold ~50% volatile + USDC; GM+ holds 100% of the asset with zero swap price impact.

**Execution model (async — design around it):** deposit/withdraw queue a request that a GMX keeper executes a few blocks later via callback. The Prime Account is **frozen for that market until the callback fires** — never queue a second op in between; re-check `gmx-positions`. Execution fee ~0.08-0.19 AVAX paid as msg.value (the tool floors gas at 25 gwei so the keeper accepts it); the EOA must hold it on top of the deposit + gas. A cancelled/expired request refunds the fee and mints nothing — verify, don't assume success. Withdrawals derive min-out from the GMX Reader and append the RedStone solvency payload (tool handles both).

**The bet you're taking:** GM LPs are the counterparty to perp traders — you earn 63% of fees unconditionally but absorb traders' net PnL, plus the backing asset's price exposure (~50% two-sided, ~100% GM+). **Fee APR ≠ realized return** — always check the Dune Pool PnL APR too.

**When to use which / provide vs exit:** see `deltaprime-reference.md` → "GMX V2 GM pools — strategy" and the SKILL decision ruleset.
