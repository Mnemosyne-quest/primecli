# DeltaPrime Reference

Canonical reference for the DeltaPrime protocol on Avalanche C-chain and the `deltaprime` CLI command that drives it. All addresses and behaviours verified on-chain on 23-05-2026 (chainId 43114).

**Audience:** anyone (human or agent) who needs to understand the protocol surface, the pool / facet addresses, and what each `deltaprime` subcommand does. Pair with [`deltaprime-capabilities.md`](deltaprime-capabilities.md) when you need the exact function signatures, calldata encoding, approve targets, and oracle / exec-fee requirements.

**What the tool covers today:** lending core (deposit / withdraw / borrow / repay / fund), Prime Account create+fund, swaps (YieldYak and ParaSwap / Velora), swap-debt, delayed collateral withdrawal, GMX V2 GM and GM+ LP, TraderJoe V2 LB, sJOE staking, PRIME leverage tiers, and a leveraged-long `zap` macro. The RedStone payload wrap is shipped, so every solvency-gated write can `--execute`. Wombat liquid-staking LP, legacy GLP, and Pangolin LP are documented in the capabilities spec but not yet tooled.

---

## 1. What DeltaPrime is

A lending and leverage protocol on Avalanche C-chain. Two layers:

1. **Savings pools** — deposit an asset to earn lending yield. Regular wallets (EOAs) deposit and withdraw directly against the pool contract.
2. **Prime Accounts** — per-user smart-contract accounts for leveraged borrowing. You create one, fund it with collateral, then borrow against that collateral. The Prime Account is what talks to the pools on the borrow side.

You don't need a Prime Account to earn yield. You do need one to borrow.

---

## 2. Architecture

### Pools (savings layer)

- Each pool is a Transparent Upgradeable Proxy (TUP). All active pools delegate to one shared implementation: `0xBbfE1DE572B1EA81d208dF6C490327242e3accC3`.
- The on-chain registry of **active** pools is the **TokenManager** at `0xF3978209B7cfF2b90100C6F87CEC77dE928Ed58e`:
  - `getAllPoolAssets() -> bytes32[]` — list of active asset symbols.
  - `getPoolAddress(bytes32 asset) -> address` — resolve symbol to active pool proxy.
- **The TokenManager is the source of truth.** DeltaPrime's GitHub repo per-pool deployment artifacts are STALE and list frozen pools. Never trust the repo addresses; resolve via TokenManager.

### Prime Accounts (borrow / leverage layer)

- Prime Accounts are EIP-2535 Diamonds, one per owner.
- Created via the **SmartLoansFactory**:
  - Proxy: `0x3Ea9D480295A73fd2aF95b4D96c2afF88b21B03D`
  - Implementation: `0xDc6410b13A81Ab16543E29Cf16985803806218D1`
  - `createLoan() -> address` — creates an EMPTY Prime Account.
  - `createAndFundLoan(bytes32 asset, uint256 amount) -> address` — create + fund in one tx.
  - `getLoanForOwner(address) -> address` — owner to account lookup; zero address means none exists.
  - `smartLoanDiamond() -> 0x2916B3bf7C35bd21e63D01C93C62FB0d4994e56D` — the diamond beacon every Prime Account delegates to. Because every account is a per-user proxy delegating to this beacon, the facet logic (borrow/repay/fund + view functions) is reachable at any deployed account address.

---

## 3. Active pools (USE THESE)

Resolved from `TokenManager.getPoolAddress()` and verified by matching `totalSupply()` to the live app's displayed pool sizes.

| Asset | bytes32 symbol | Active pool proxy | Token | Decimals |
|-------|----------------|-------------------|-------|----------|
| AVAX (WAVAX) | `AVAX` | `0xaa39f39802F8C44e48d4cc42E088C09EDF4daad4` | WAVAX `0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7` | 18 |
| USDC | `USDC` | `0x8027e004d80274FB320e9b8f882C92196d779CE8` | `0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E` | 6 |
| ETH (WETH) | `ETH` | `0x2A84c101F3d45610595050a622684d5412bdf510` | `0x49D5c2BdFfac6CE2BFdB6640F4F80f226bc10bAB` | 18 |
| BTC (BTC.b) | `BTC` | `0x70e80001bDbeC5b9e932cEe2FEcC8F123c98F738` | `0x152b9d0FdC40C096757F570A51E494bd4b943E50` | 8 |
| USDT | `USDT` | `0x1b6D7A6044fB68163D8E249Bce86F3eFbb12368e` | `0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7` | 6 |
| EUROC | (listed in TokenManager) | `0x3144975FC0458eE0BF9BcF4B8226AFfE253E991F` | — | — |

The `deltaprime` command wires up the first five (USDC, WAVAX, WETH, BTC, USDT). EUROC is registered in the TokenManager but not exposed as a tool pool key.

### LP / market reference addresses (for the extended capabilities)

**GMX V2 GM markets** (two-sided, `GmxV2FacetAvalanche`): GM_AVAX `0x913C1F46b48b3eD35E7dc3Cf754d4ae8499F31CF` (WAVAX/USDC), GM_BTC `0xFb02132333A79C8B5Bd0b64E3AbccA5f7fAf2937` (BTC.b/USDC), GM_ETH `0xB7e69749E3d2EDd90ea59A4932EFEa2D41E245d7` (WETH.e/USDC).
**GMX V2 GM+ markets** (single-sided, `GmxV2PlusFacetAvalanche`): GM_AVAX `0x08b25A2a89036d298D6dB8A74ace9d1ce6Db15E5`, GM_BTC `0x3ce7BCDB37Bf587d1C17B930Fa0A7000A0648D12`, GM_ETH `0x2A3Cf4ad7db715DF994393e4482D6f1e58a1b533`.
**GMX infra**: Router `0x820F5FfC5b525cD4d88Cd91aCf2c28F16530Cc68`, ExchangeRouter `0xF0864BE1C39C0AB28a8f1918BC8321beF8F7C317`, DepositVault `0x90c670825d0C62ede1c5ee9571d6d9a17A722DFF`, WithdrawalVault `0xf5F30B10141E1F63FC11eD772931A8294a591996`.

**TraderJoe V2 LB whitelisted pairs** (only these can be LP'd; max 80 bins/account). Routers: TJ v2.1 `0xb4315e873dBcf96Ffd0acd8EA43f689D8c20fB30`, TJ v2.2 `0x18556DA13313f3532c54711497A8FedAC273220E`.
| Pair | LBPair address |
|------|----------------|
| WAVAX/USDC (v2.1) | `0xD446eb1660F766d533BeCeEf890Df7A69d26f7d1` |
| WETH.e/WAVAX (v2.1) | `0x1901011a39B11271578a1283D620373aBeD66faA` |
| BTC.b/WAVAX (v2.1) | `0xD9fa522F5BC6cfa40211944F2C8DA785773Ad99D` |
| USDt/USDC (v2.1) | `0x2823299af89285fF1a1abF58DB37cE57006FEf5D` |
| JOE/WAVAX (v2.1) | `0xEA7309636E7025Fda0Ee2282733Ea248c3898495` |
| WAVAX/BTC.b (v2.2) | `0x856b38Bf1e2E367F747DD4d3951DDA8a35F1bF60` |
| WAVAX/USDC (v2.2) | `0x864d4e5Ee7318e97483DB7EB0912E09F161516EA` |
| BTC.b/USDC (v2.2) | `0x4224f6F4C9280509724Db2DbAc314621e4465C29` |
| aUSD/WAVAX (v2.2) | `0xe92C7661E51121F167D7b36Ed07D297E3792A95f` |
| EURC/USDC (v2.2) | `0xcD4f57d6B160B4ef2DFb78Ad1c76Cc4242EDB4CE` |
| EURC/WAVAX (v2.2) | `0x7b7D06668d4B9b353747B47a22CCd2400F200314` |
| aUSD/USDt (v2.2) | `0xcEC377285AbF370FDf872625D2742252656d631a` |
| aUSD/USDC (v2.2) | `0x8573F98175D816d520248B5fACF40D309B1c9ceE` |

**Aggregator endpoints**: YieldYak router `0xC4729E56b831d74bBc18797e0e17A295fA77488c`; ggAVAX token `0xA25EaF2906FA1a3a13EdAc9B9657108Af7B703e3`.

---

## 4. Frozen pools (DO NOT USE — reference only)

These are the stale addresses from the old docs/repo. Their write paths revert; see the PoolFrozen gotcha below. Listed here so they can be recognised and avoided.

| Asset | Frozen proxy |
|-------|--------------|
| USDC | `0x2323dAC85C6Ab9bd6a8B5Fb75B0581E31232d12b` |
| WAVAX | `0xD26E504fc642B96751fD55D3E68AF295806542f5` |
| WETH | `0xD7fEB276ba254cD9b34804A986CE9a8C3E359148` |
| BTC | `0x475589b0Ed87591A893Df42EC6076d2499bB63d0` |
| USDT | `0xd222e10D7Fe6B7f9608F14A8B5Cf703c74eFBcA1` |

---

## 5. Prime Account facets

Facet logic is reachable at any Prime Account address (the account delegates to the diamond beacon `0x2916B3bf7C35bd21e63D01C93C62FB0d4994e56D`). The diamond exposes **26 facets / ~170 selectors** — the lending core that the `deltaprime` command drives today uses only a handful. The full per-capability build spec is in `deltaprime-capabilities.md`; this section gives the facet map and the core lending facets the tool currently drives.

### 5.1 Complete facet map (all 26, verified on-chain 23-05-2026 via DiamondLoupe `facets()`)

| Facet | Address | What it does | Key functions |
|-------|---------|--------------|---------------|
| **AssetsOperationsAvalancheFacet** | `0x5a501B5698eAdE321B3553eA633046c6a91E3763` | Borrow / repay / fund collateral (lending core) | `borrow` `repay` `fund` `fundGLP` `unfreezeAccount` `withdrawUnsupportedToken` `addOwnedAsset` |
| **SmartLoanViewFacet** | `0x2B2C18F21A50c4DcbdFA54fb8cdC009F36AF27d9` | Oracle-free + oracle-gated account views | `getAllOwnedAssets` `getBalance` `getDebts` `getStakedPositions` `getSupportedTokensAddresses` `getAllAssetsBalances`* `getAllAssetsPrices`* `getGmTokenBalanceAfterFees` |
| **SmartLoanWrappedNativeTokenFacet** | `0x81252DF686542B1F353671458561DF8E9151c8C1` | Native AVAX in/wrap | `depositNativeToken` (payable) `wrapNativeToken` |
| **HealthMeterFacetProd** | `0x519AeEfC6558aD1f138E3892A09eBFC327eb67E2` | Health meter | `getHealthMeter`* |
| **SolvencyFacetProdAvalanche** | `0x968f944e9c43FC8AD80F6C1629F10570a46e2651` | Solvency / pricing math (21 fns) | `isSolvent`* `getHealthRatio`* `getTotalValue`* `getDebt`* `getPrices`* `getFullLoanStatus`* `getOwnedAssetsWithNativePrices`* |
| **SolvencyFacetProdAvalanche** (config) | `0x636557Cf41D39092739f53A8fad50C333C3884C6` | RedStone signer/feed config getters | (3 RedStone config selectors) |
| **WithdrawalIntentFacet** | `0xf88f82e8982de4f7831B0A8BA55Ce23536872FD9` | **Withdraw collateral** (24h-delayed intent) | `createWithdrawalIntent` `executeWithdrawalIntent` `cancelWithdrawalIntent` `getUserIntents` `getAvailableBalance` |
| **ParaSwapFacet** | `0x3732ba82d54568609b2E63cB64487af0D7f3FBcc` | **Swap** via ParaSwap/Velora | `paraSwapV6` |
| **YieldYakSwapFacet** | `0x7b90769acaFb6540D00C06c406ba01Ab58B3028C` | **Swap** via YieldYak aggregator | `yakSwap` `isWhitelistedAdapterOptimized` |
| **SwapDebtFacet** | `0x1e36f07aCaB2Ed9989f2364e27FeD7af92C0ff49` | **Swap debt** (refinance asset→asset) | `swapDebtParaSwap` |
| **GmxV2FacetAvalanche** | `0x759902b8D105cBB20D1b2C7b76b355a175E32286` | **GMX V2 GM LP** (two-sided long+short) | `deposit{Avax,Btc,Eth}UsdcGmxV2` `withdraw{...}UsdcGmxV2` `getGmPerformance` |
| **GmxV2PlusFacetAvalanche** | `0xe9C87e730f3a5972C9EA78995d32eb2Fd936D7Bf` | **GMX V2 GM+ LP** (single-sided) | `deposit{Avax,Btc,Eth}GmxV2Plus` `withdraw{...}GmxV2Plus` `getGmPlusPerformance` |
| **GmxV2FacetAvalanche** (fee const) | `0x5C5478df593Dfd2c82aff4DfABc488d95224d7d5` | GMX fee constant | `FEE_PERCENTAGE` |
| **GmxV2CallbacksFacetAvalanche** | `0xfcCf6CDf19AAD9D5cA8771370B3ba8d973fA97ee` | GMX keeper callbacks (after deposit/withdrawal) | (keeper-only, 5 selectors) |
| **TraderJoeV2AvalancheFacet** | `0x1899F6D524637808f2d53125b6CCFe6D2dF1Fa91` | **TraderJoe V2 LB** concentrated liquidity | `addLiquidityTraderJoeV2` `removeLiquidityTraderJoeV2` `fundLiquidityTraderJoeV2` `claimReward` `getOwnedTraderJoeV2Bins` |
| **WombatFacet** | `0x94aAa81E3Efc79a485D7Ef78A9df9a9aE9437Bae` | **Liquid staking LP** (AVAX/sAVAX, AVAX/ggAVAX) + staking (25 fns) | `depositAndStakeAvaxSavaxLpAvax` `depositAvaxToAvaxGgavax` `withdraw…` `claimAllWombatRewards` |
| **SJoeFacet** | `0x8aD9028f60Cf0F823271FE689EbDD0A58492cC75` | **sJOE staking** (JOE → USDC rewards) | `stakeJoe` `unstakeJoe` `claimSJoeRewards` |
| **GLPFacet** | `0x419404442A77F9bb718f48856f6D2c09f7959fc5` | **GLP** mint/redeem (legacy GMX V1) | `mintAndStakeGlp` `unstakeAndRedeemGlp` `claimGLpFees` |
| **YieldYakFacet** | `0xF62b626324d65183933d697CDb45be96E3C7da92` | YieldYak GLP auto-compound stake | `stakeGLPYak` `unstakeGLPYak` |
| **PangolinDEXFacet** | `0xf907Fdd5B20dD074bf0D18b8a8d0cacE71170DDa` | **Pangolin V2 LP** | `addLiquidityPangolin` `removeLiquidityPangolin` |
| **PrimeLeverageFacet** | `0x912609401D93779bEd71C9027c5f11f518397Bdd` | PRIME staking / leverage tiers (12 fns) | `stakePrimeAndActivatePremium` `depositPrime` `unstakePrime` `getLeverageTier` |
| **SmartLoanLiquidationFacet** | `0xBc8bBFD5ae45D7E7619347DFB5da51ee5F980D85` | Liquidation (liquidators only) | `liquidate` `whitelistLiquidators` `getLiquidationFeePercent` |
| **WithdrawUnsupportedPositionsFacet** | `0xaa9CEa1c69870F82e957f72C552a0b12d751Ba78` | Recover delisted assets | `withdrawUnsupportedPositions` `hasUnsupportedAssets` |
| **OwnershipFacet** | `0x4c3Ee50716C5f6ef4C64aB74EC549fa450c8F22f` | Account ownership transfer | `owner` `proposeOwnershipTransfer` `acceptOwnership` |
| **DiamondCutFacet** | `0x4d0884222d09259893fEA6F1569a4150803Ef6C4` | Diamond upgrade + pause (governance) | `diamondCut` `pause` `unpause` |
| **DiamondLoupeFacet** | `0xb2c4b9206988E160B55Eb9C9e29b7A9ab6A22CFC` | Diamond introspection | `facets` `facetAddresses` `facetFunctionSelectors` `facetAddress` `supportsInterface` |

`*` = RedStone-gated (reverts `0xe7764c9e` on a plain `eth_call`; needs signed price calldata appended — see gotcha 3 and the capabilities doc).

### 5.2 Core lending facets (what the `deltaprime` command drives today)

**AssetsOperationsAvalancheFacet** `0x5a501B5698eAdE321B3553eA633046c6a91E3763` (state-changing):
- `borrow(bytes32 asset, uint256 amount)`
- `repay(bytes32 asset, uint256 amount)` — payable
- `fund(bytes32 asset, uint256 amount)`

**SmartLoanViewFacet** `0x2B2C18F21A50c4DcbdFA54fb8cdC009F36AF27d9` (oracle-free views, always work):
- `getAllOwnedAssets() -> bytes32[]`
- `getBalance(bytes32) -> uint256`
- `getDebts() -> (bytes32 name, uint256 debt)[]`

**SmartLoanWrappedNativeTokenFacet** `0x81252DF686542B1F353671458561DF8E9151c8C1`:
- `depositNativeToken()` — payable, wraps AVAX→WAVAX inside the account (used by `fund --pool wavax`)

**HealthMeterFacetProd** `0x519AeEfC6558aD1f138E3892A09eBFC327eb67E2`:
- `getHealthMeter() -> uint256` — RedStone-gated (see gotcha 3).

### 5.3 Extended capabilities (swap, LP, staking, etc.)

The other 22 facets cover swap, swap-debt, withdraw-collateral, GMX V2 LP, TraderJoe LB, Wombat/sJOE/GLP staking, Pangolin LP, and PRIME tiers. **Most are now tooled** (the RedStone payload wrap is shipped, so solvency-gated writes can `--execute`): swap (`swap --via yak|paraswap`), swap-debt (`swap-debt`), withdraw-collateral (`withdraw-collateral` / `withdrawal-intents` / `execute-withdrawal`), GMX V2 GM/GM+ LP (`gmx-deposit` / `gmx-withdraw` / `gmx-positions`), TraderJoe V2 LB (`lb-add` / `lb-remove` / `lb-positions`), sJOE staking (`sjoe-stake` / `sjoe-unstake` / `sjoe-claim` / `sjoe-position`), PRIME leverage tiers (`prime-tier` / `prime-needed` / `prime-deposit` / `prime-activate` / `prime-deactivate` / `prime-unstake` / `prime-repay`), and a leveraged-long `zap` macro. **Still untooled:** Wombat/GLP staking and Pangolin LP. Each capability is documented with exact signatures, parameter encoding, approve targets, and the RedStone requirement in **`deltaprime-capabilities.md`**. Summary of how each works:

- **Swap** (`ParaSwapFacet.paraSwapV6` / `YieldYakSwapFacet.yakSwap`): two aggregator routes — ParaSwap (branded **Velora** on Avalanche; pass API-built calldata) and YieldYak (pass router-derived `path`+`adapters`, every adapter whitelisted). Swaps in-account balances. Velora needs an off-chain API call; YakSwap is fully on-chain-derivable (router `findBestPath`).
- **Swap debt** (`SwapDebtFacet.swapDebtParaSwap`): borrow new asset → ParaSwap into old asset → repay old debt, in one tx. Hard 5% USD-value-difference cap.
- **Withdraw collateral** (`WithdrawalIntentFacet`): **no instant withdraw** — `createWithdrawalIntent` then `executeWithdrawalIntent` in a **24h–72h window** (`actionableAt = now+24h`, `expiresAt = +48h`). `getAvailableBalance(bytes32)` is an oracle-free view of free balance.
- **GMX V2 GM LP** (`GmxV2FacetAvalanche` two-sided / `GmxV2PlusFacetAvalanche` single-sided): mint/redeem GM market LP tokens. **All deposit/withdraw fns are payable and need a GMX execution fee as `msg.value`** (keeper gas). Async — the account is **frozen** until the GMX keeper callback fires. `minGmAmount` / `minLong/ShortTokenAmount` are slippage floors.
- **TraderJoe V2 LB** (`TraderJoeV2AvalancheFacet`): concentrated liquidity across price **bins**. `addLiquidityTraderJoeV2(router, LiquidityParameters)` where the struct's `deltaIds` + `distributionX/Y` encode the shape (Spot/Curve/Bid-Ask) and range. Whitelisted routers (TJ v2.1/v2.2) and 13 whitelisted pairs only. **Max 80 bins per account.** Most complex to tool — needs the LB SDK for bin math.
- **Liquid staking & farms** (`WombatFacet`): on this deployment, ggAVAX/sAVAX yield is reached via **Wombat** AVAX/sAVAX & AVAX/ggAVAX pools + LP staking — there is **no standalone GogoPool facet deployed** (its `swapToGgAvax` selector is absent from the live diamond). Plain liquid-staking exposure is also obtainable by `yakSwap` AVAX→ggAVAX (GGAvaxAdapter). Plus `SJoeFacet` (JOE→USDC fee rewards), `GLPFacet`+`YieldYakFacet` (legacy GLP), `PangolinDEXFacet` (UniV2-style LP).
- **Zaps** (one-click leveraged entry) — ✅ tooled as `zap`: **not a separate facet** — a front-end macro that bundles fund → borrow → swap → LP into one flow. The tool replicates this as a sequence of the existing leg commands (`cmd_fund`/`cmd_borrow`/`cmd_swap`/`cmd_gmx_deposit`), each its own tx, stopping on the first failure. One bounded leveraged-long flow is shipped (GM-market terminal); the canonical first zap.
- **PRIME tiers** (`PrimeLeverageFacet`) — ✅ tooled as `prime-tier`/`prime-needed`/`prime-activate`/`prime-deactivate`/`prime-unstake`/`prime-repay`: stake the protocol's PRIME token to unlock PREMIUM (10x) leverage. The required stake is **proportional to USD borrow** (`tieredPrimeStakingRatio`, ~1.2 PRIME/$100), read live from the TokenManager (governance-mutable, never hard-coded). PREMIUM does **not** change the USD borrow APR; its cost is a separate **PRIME-denominated rent-debt** that accrues over time (`tieredPrimeDebtRatio`, ~0.5 PRIME/$100/yr) and is policed by its own `shouldLiquidatePrimeDebt` track. All PrimeLeverageFacet writes (`depositPrime`, `stakePrimeAndActivatePremium`, `unstakePrime`, `deactivatePremiumTier`, `repayPrimeDebt`) require an appended RedStone payload (reverts `CalldataMustHaveValidPayload` otherwise — fixed 24-05-2026). PRIME (18-dec, `0x33c8…7f298`) is a separate token from sPRIME, acquired on a DEX (LFJ/TraderJoe PRIME-WAVAX).

---

## 6. Critical gotchas

These are the non-obvious bits. They are the reason naïve approaches fail.

1. **PoolFrozen (`0xfd4851e9`).** The old docs pools are frozen. Their WRITE paths (deposit/withdraw) revert with this custom error. But their VIEW functions (`totalSupply`, etc.) still return STALE non-zero values. So "does a read revert?" is NOT how you tell a pool is dead. The reliable test: call `totalSupply()` and match it against the live app's displayed pool size. That is how the active pools in section 3 were found.

2. **bytes32 asset symbols.** Prime Account functions identify assets by their symbol string encoded as bytes32, right-padded with zero bytes. Use the symbol, not the wrapped-token name:
   - `AVAX` (not `WAVAX`), `USDC`, `ETH` (not `WETH`), `BTC`, `USDT`.

3. **RedStone oracle gating (`0xe7764c9e`, "missing oracle payload").** Functions that compute USD value or check solvency (`getHealthMeter()`, `getAllAssetsBalances()`, and **every state-changing facet function carrying `remainsSolvent`**, which covers all of swap, swap-debt, GMX LP, TraderJoe LP, staking writes, and the execute step of withdraw-collateral) revert on a plain `eth_call` because they need RedStone signed price calldata appended. A real call wraps the tx calldata with the RedStone EVM connector (append the signed price payload bytes after the normal ABI-encoded args). The oracle-free views (`getBalance`, `getDebts`, `getAllOwnedAssets`, `getAvailableBalance`, `getOwnedTraderJoeV2Bins`, Wombat / sJOE balance views) work without it. The tool implements the RedStone wrapping (`build_redstone_payload` appends the signed price packages to the calldata tail), so `prime-summary` reports a real health ratio / total value / debt / solvent flag, and every solvency-gated write (swap, swap-debt, GMX LP, TraderJoe LP, sJOE stake / claim, execute-withdrawal) can `--execute`. It falls back to balances-only if the gateway is unreachable. See the "RedStone wrapping" section in `deltaprime-capabilities.md`.

4. **Borrow needs setup.** `createLoan()` makes an EMPTY Prime Account. You must `fund()` it with collateral before `borrow()` will succeed. The EOA needs AVAX for gas. `createAndFundLoan()` does create + fund in one tx.

5. **maxPoolUtilisation = 92.5%.** Pools above this utilization reject new borrows.

6. **Decimals for amount scaling:** USDC 6, USDT 6, BTC 8, AVAX 18, ETH 18. The tool handles scaling internally; this matters if you ever compute amounts by hand.

7. **GMX deposit needs the FULL solvency RedStone payload (real bug, fixed 24-05-2026).** Before minting GM tokens, the deposit facet runs an inline solvency check that prices **every** debt-registry asset — the whole pool set `AVAX, USDC, BTC, ETH, USDT, EUROC`, even ones with zero balance/debt — each needing 3 unique RedStone signers in the appended payload. The tool builds the write payload from `prime_account_price_feeds(account)` + the GM feed; a missing feed reverts the deposit with `InsufficientNumberOfUniqueSigners(0,3)` (wrapped in `ProxyCalldataFailedWithCustomError`). The read path (`gmx-positions`) does NOT hit this — GM view calls skip the full solvency simulation, so a read can succeed while a naive write reverts. See §12.

8. **GMX execution fee needs a gas-price floor (real bug, fixed 24-05-2026).** GMX keepers require a real execution fee (~0.08–0.19 AVAX), but Avalanche's live base fee can be ~0.01–0.02 gwei, which estimates a uselessly tiny fee the keeper rejects (the request expires and refunds without minting). The tool floors the gas price at **25 gwei** in the fee estimator so the `msg.value` clears GMX's requirement; GMX refunds the unused part to the account. The EOA must hold the execution fee up front (on top of the deposit amount and gas). See §12.

---

## 7. Pool contract functions (savings layer)

`deposit(uint256)`, `withdraw(uint256)`, `totalSupply()`, `totalBorrowed()`, `balanceOf(address)`, `getBorrowed(address)`, `getFullPoolStatus()`, `lockDeposit(uint256, uint256)`.

---


## 8. Pool borrow/supply rates (read on-chain)

The pool contracts expose `getBorrowingRate()` (selector `0xfdb74fff`) and `getDepositRate()` (selector `0x6e029037`) on the implementation via EIP-1967 proxy. These return per-second rates in 1e18 wad. Multiply by `SECONDS_PER_YEAR = 365.25 * 24 * 3600` to get the APR.

**Example at 63.17% utilization on USDC:**
- Borrow ≈ 5.5% APR
- Supply ≈ 3.5% APR
- Spread ≈ 2% (protocol revenue)

At utilization above ~80% the rates accelerate (kink model). The `ratesCalculator` contract holds the interest rate curve parameters.

## 9. Corrected costs for leveraged farming (2026-05-28)

Early models overestimated two key costs. The corrected figures:

| Cost | Early estimate | Corrected | Source |
|------|---------------|-----------|--------|
| USDC borrow APR | 7% | **~5.5%** | On-chain `getBorrowingRate()` at 63% util |
| PRIME rent debt | 4.5% of debt | **0.5% of debt** | Protocol docs: `tieredPrimeDebtRatio = 0.5e18` (0.5 PRIME/$100/yr), NO leverage multiplier |
| Combined carry | 11.5% | **~6%** | 5.5% borrow + 0.5% PRIME |

The PRIME debt formula (from protocol docs): `accruedPrimeDebt = totalBorrowedValueUSD * 0.5 * timeElapsed / (100 * 365 days)`. That is 0.5 PRIME per $100 of total borrow, at any leverage tier.

## 10. The tool: `deltaprime`

- Installed by `pip install primecli`; entry point is the `deltaprime` console script.
- Default RPC: `https://api.avax.network/ext/bc/C/rpc`. Override with `DELTAPRIME_RPC` (paid Alchemy/QuickNode/Infura recommended for heavy use).
- Signing: only under `--execute`, with the key resolved per the precedence below. Real wallet, real funds.

### Signing key resolution

The Prime Account is derived on-chain from the wallet owner (`getLoanForOwner`), so each user automatically operates on their own Prime Account. No per-user addresses are hardcoded.

Key resolution order (first hit wins):

1. `--key <0xhex>` CLI flag → one-off raw key.
2. `DELTAPRIME_PRIVATE_KEY` env var → raw `0x…` key (the primary path).
3. `DELTAPRIME_KEY_FILE` env var → path to a file containing the `0x…` key.

The CLI key (#1) is read at startup; the env vars (#2/#3) are read lazily so read-only commands (`pool-info`, `my-positions`, `prime-summary`, `defi --json`, ...) work without a key configured. Every command prints the resolved `Wallet:` line on write paths, so the active wallet is always visible.

### Commands

The tool ships **32 commands**. State-changing commands default to a PREVIEW; add `--execute` to broadcast. Solvency-gated writes append a RedStone signed-price payload on `--execute` (noted "gated" below).

**Lending (savings + borrow core)**

| Command | Type | What it does |
|---------|------|--------------|
| `pool-info [usdc\|wavax\|weth\|btc\|usdt\|all] [--json]` | read-only | Pool supply / borrow / utilization / deposit APR / borrow APR / TVL. Defaults to `all`. With `--json`: emits a single JSON object for a named pool, or a `{name: {...}}` dict for `all` (same shape as `degenprime pool-info --json`). |
| `my-positions` | read-only | Wallet balances + pool positions + Prime Account address. |
| `deposit --pool X --amount Y [--execute]` | state-changing | Deposit into a savings pool. ERC20 approve handled automatically (approves the **pool**). |
| `withdraw --pool X --amount Y [--execute]` | state-changing | Withdraw from a savings pool. |
| `borrow --pool X --amount Y [--execute]` | state-changing | Calls `borrow()` on the Prime Account. |
| `repay --pool X --amount Y [--execute]` | state-changing | Calls `repay()` on the Prime Account. |
| `fund --pool X --amount Y [--execute]` | state-changing | Move collateral from the wallet into the Prime Account. ERC20: approves the **Prime Account** then calls `fund()`. Native AVAX (`wavax`): payable `depositNativeToken()` (wraps AVAX→WAVAX inside the account, no approve, spends raw AVAX). |

**Prime Account**

| Command | Type | What it does |
|---------|------|--------------|
| `create-prime-account [--execute]` (alias `create-account`) | state-changing | `factory.createLoan()` — creates an empty Prime Account. |
| `create-prime-account --fund-pool X --fund-amount Y [--execute]` | state-changing | `factory.createAndFundLoan()` — create + fund in one tx. **ERC20 only** (approves the **factory**); native AVAX is blocked, use the two-step flow. |
| `prime-summary` | read-only | Prime Account assets / debts + **live solvency** (health ratio, total value, debt, solvent flag) via RedStone-gated `SolvencyFacetProdAvalanche` reads (falls back to balances-only if the gateway is down). |
| `withdraw-collateral --pool X --amount Y [--execute]` | state-changing | Step 1 of delayed collateral withdrawal: registers a `WithdrawalIntent` (no RedStone). Executable ~24h later for a 48h window. |
| `withdrawal-intents` | read-only | Lists pending intents (with ready/expired state) + per-asset available balance. Oracle-free. |
| `execute-withdrawal --pool X [--index N] [--execute]` | state-changing (gated) | Step 2: pulls a matured intent to the wallet (`executeWithdrawalIntent`, RedStone-gated). |

**Swaps**

| Command | Type | What it does |
|---------|------|--------------|
| `swap --from S --to S --amount N [--via yak\|paraswap] [--slippage P] [--execute]` | state-changing (gated) | Swaps one in-account asset for another. `--via yak` (default, YieldYak `findBestPath`) or `paraswap` (ParaSwap/Velora v6.2 API calldata; hard 5% facet slippage cap). RedStone-gated. |
| `swap-debt --from S --to S --amount N [--slippage P] [--execute]` | state-changing (gated) | Refinances debt: borrows `--amount` of the NEW asset (`--to`), ParaSwaps it into the OLD debt asset (`--from`), repays the old debt. Hard 5% USD-value-diff cap. RedStone-gated. |

**GMX V2 LP** (async — keeper-executed; account frozen for the market until callback)

| Command | Type | What it does |
|---------|------|--------------|
| `gmx-positions` | read-only | Per owned market: GM balance after fees + annualised performance (RedStone-gated `eth_call` views). |
| `gmx-deposit --market M --amount N [--side long\|short] [--slippage P] [--fee-buffer X] [--execute]` | state-changing (gated, payable) | Open a GM (two-sided) / GM+ (single-sided) LP position. Pays a GMX execution fee as `msg.value`. Markets: `avax-usdc`, `btc-usdc`, `eth-usdc` (GM); `avax+`, `btc+`, `eth+` (GM+). RedStone-gated. |
| `gmx-withdraw --market M --amount N [--slippage P] [--fee-buffer X] [--execute]` | state-changing (gated, payable) | Burn GM tokens to close a position. Pays the execution fee as `msg.value`. RedStone-gated. |

**TraderJoe V2 Liquidity Book**

| Command | Type | What it does |
|---------|------|--------------|
| `lb-positions` | read-only | Lists owned (pair, bin) pairs + the account's per-token share of each bin. Oracle-free. |
| `lb-add --pair P --amount-x N --amount-y N [--shape spot\|curve\|bidask] [--range R] [--slippage P] [--id-slippage S] [--execute]` | state-changing (gated) | Open a concentrated-liquidity position across price bins (max 80 bins/account). RedStone-gated. |
| `lb-remove --pair P [--slippage P] [--execute]` | state-changing (gated) | Close the account's **entire** position for the pair. **Requires the RedStone payload** (facet reverts `CalldataMustHaveValidPayload` / `0xe7764c9e` without it — fixed 24-05-2026). |

**sJOE staking**

| Command | Type | What it does |
|---------|------|--------------|
| `sjoe-position` | read-only | Staked JOE + pending USDC rewards. Oracle-free. |
| `sjoe-stake --amount N [--execute]` | state-changing (gated) | Stake in-account JOE into sJOE for USDC fee rewards. RedStone-gated. |
| `sjoe-unstake --amount N [--execute]` | state-changing | Unstake JOE back into the account. NOT solvency-gated → no RedStone payload. |
| `sjoe-claim [--execute]` | state-changing (gated) | Claim accrued USDC rewards (~90% net; 10% protocol fee). RedStone-gated. |

**PRIME leverage tiers** (stake PRIME to unlock 10x; `PrimeLeverageFacet`)

| Command | Type | What it does |
|---------|------|--------------|
| `prime-tier` | read-only | Current tier (BASIC ~5x / PREMIUM 10x), staked PRIME, recorded PRIME debt, the EOA + in-account PRIME balances, and `shouldLiquidatePrimeDebt()` (state-mutating, so `eth_call`'d as a read-only sim — needs a RedStone payload since it reads debt). Graceful if no Prime Account. |
| `prime-needed --borrow X [--tier premium\|basic]` | read-only | PRIME needed to back $X of borrow, via `getRequiredPrimeStake` (live `tieredPrimeStakingRatio` — proportional to USD borrow, never hard-coded). Default tier `premium`. |
| `prime-deposit --amount N [--execute]` | state-changing (gated) | Deposit PRIME from the wallet INTO the Prime Account without activating PREMIUM (ERC20 approve → `depositPrime`, RedStone-gated). The PRIME then sits in-account, ready for `prime-activate`. Caps to wallet PRIME. |
| `prime-activate [--amount N] [--execute]` | state-changing (gated if `--amount`) | Activate PREMIUM. `--amount N` first `depositPrime(N)`s PRIME from the EOA into the account (ERC20 approve → `depositPrime`, RedStone-gated), then `stakePrimeAndActivatePremium()` stakes the required amount (against 10x your free collateral) and flips to PREMIUM. Omit `--amount` to stake from PRIME already in the account. Fails closed if the in-account PRIME would be short. |
| `prime-deactivate [--withdraw] [--execute]` | state-changing | `deactivatePremiumTier(withdrawStake)` — repays ALL PRIME debt first (reverts if PRIME can't cover it; 50% burn / 50% treasury), drops to BASIC. `--withdraw` also releases the freed stake into the account. Requires the RedStone payload (PrimeLeverageFacet — stake & repay probe-confirmed, fixed 24-05-2026). |
| `prime-unstake --amount N [--execute]` | state-changing | `unstakePrime(N)` — release staked PRIME. In PREMIUM the remaining stake must still cover the USD ratio + accrued PRIME debt or the facet reverts. Requires the RedStone payload (PrimeLeverageFacet — stake & repay probe-confirmed, fixed 24-05-2026). |
| `prime-repay --amount N [--execute]` | state-changing | `repayPrimeDebt(N)` — repay accrued PRIME rent-debt from in-account PRIME (capped to current debt; 50% burn / 50% treasury). Requires the RedStone payload (PrimeLeverageFacet — stake & repay probe-confirmed, fixed 24-05-2026). |

**Zaps** (tool-level macro — not a separate facet)

| Command | Type | What it does |
|---------|------|--------------|
| `zap --market M --collateral P --collateral-amount N --borrow-amount N --deposit-amount N [--side long\|short] [--swap] [--slippage P] [--fee-buffer X] [--execute]` | macro (multi-tx) | **Leveraged-long** one-click entry composing existing legs: `fund` collateral → `borrow` USDC → optional `--swap` USDC→long → `gmx-deposit` into a two-sided GM market (`avax-usdc`, `btc-usdc`, `eth-usdc`). Each leg is its own tx with an explicit amount. PREVIEW prints the full ordered plan; `--execute` runs the legs sequentially and **stops on the first failure**, reporting which legs completed (partial-state safe). The terminal GMX leg is async — `--execute` only fires the deposit request (account freezes until the keeper settles). |

`--pool` values are the lending tool keys: `usdc`, `wavax`, `weth`, `btc`, `usdt`. Swap/swap-debt asset symbols (`--from`/`--to`) are the bytes32 symbols: `AVAX`, `ETH`, `BTC`, `USDC`, `USDT`.

**Approve targets differ by command** (easy to get wrong, all handled correctly by the tool): `deposit` approves the **pool**; `fund` approves the **Prime Account**; `create-prime-account --fund-*` approves the **factory**. Swaps/LP/staking operate on balances already inside the Prime Account, so the EOA approves nothing for them.

### Preview vs broadcast

Every state-changing command **defaults to a PREVIEW** that prints what it would do and does nothing on-chain. It only signs and broadcasts when you add `--execute`. On `--execute`, solvency-gated writes (swap, swap-debt, GMX LP, TraderJoe `lb-add`, `sjoe-stake`/`sjoe-claim`, `execute-withdrawal`) append a RedStone signed-price payload to the calldata; `sjoe-unstake` and `withdraw-collateral` (intent creation) need no payload (`lb-remove` ALSO appends the payload — the facet rejects it otherwise, fixed 24-05-2026). Read-only commands ignore `--execute`.

### GMX deposits/withdrawals are async

`gmx-deposit` / `gmx-withdraw` are payable and **asynchronous**: they pay a GMX execution fee as `msg.value`, queue the request on the GMX ExchangeRouter, and a GMX **keeper** executes it some blocks later via a callback. The position does not appear or disappear instantly, and the Prime Account is **frozen until the keeper callback fires**. The freeze is global per-account, not per-market (`DiamondStorageLib.freezeAccount` sets a single `SmartLoanStorage.frozenSince` timestamp; the keeper callback clears it).

**On the freeze (not surfaced in the tool).** The freeze clears automatically when the GMX keeper callback fires, normally within minutes. The tool deliberately does not read or display the freeze flag: there is no external getter (`isAccountFrozen()` is `internal`, and reading the raw storage slot proved unreliable), and the manual `unfreezeAccount()` (`AssetsOperationsAvalancheFacet`, selector `0x7c5fc3fb`) is `onlyWhitelistedLiquidators`, so an owner EOA cannot call it and there is no self-recovery. Practical rule: after a `gmx-deposit` / `gmx-withdraw`, wait and re-check `gmx-positions`. The EOA also needs AVAX for its own tx gas on top of the execution fee.

---

## 11. Typical flows

**Earn yield:**
```
deposit --pool usdc --amount 100 --execute
```
The ERC20 approve is sent automatically before the deposit. Native AVAX deposits pass `value` instead of approving.

**Leverage:**
```
create-prime-account --execute           # creates an empty account
fund --pool X --amount Y --execute       # move collateral in
borrow --pool X --amount Y --execute     # borrow against collateral
# ... later ...
repay  --pool X --amount Y --execute
withdraw --pool X --amount Z --execute

# Or collapse the first two steps (ERC20 collateral only):
create-prime-account --fund-pool usdc --fund-amount 100 --execute
```

---

## 12. Safety

- State-changing commands default to preview; `--execute` is required to broadcast.
- **Never broadcast a real transaction (`--execute`) without understanding what the preview is about to do.** This is a real wallet with real funds.
- The private key (env var or file) is never written anywhere by the tool. Treat its storage as a hard secret — never echo, log, or commit it.
- Confirm the `Wallet:` line shown on write paths matches the wallet you intend before any `--execute`.

---

## 13. GMX V2 position lifecycle (open / change / close)

The worked, verified path for a GMX V2 LP position. Markets:

- **Two-sided GM** — `avax-usdc`, `btc-usdc`, `eth-usdc`. Take `--side long|short`: `long` = the volatile leg (WAVAX/BTC.b/WETH.e), `short` = USDC. Implemented on `GmxV2FacetAvalanche`.
- **Single-sided GM+** — `avax+`, `btc+`, `eth+`. `--side` is ignored (one asset only). Implemented on `GmxV2PlusFacetAvalanche`.

All GMX deposit/withdraw transactions are **payable and asynchronous**: they pay a GMX execution fee as `msg.value`, queue the request, and a GMX keeper mints/burns the GM tokens a few blocks later via a callback. The Prime Account is **frozen until the callback fires** (normally within minutes). Always re-check with `gmx-positions` after a deposit or withdraw — the position does not appear/disappear in the same block.

### Open

```
fund --pool wavax --amount 0.5 --execute               # moves 0.5 AVAX collateral into the Prime Account as WAVAX
gmx-deposit --market avax-usdc --amount 0.5 --side long --execute
```

1. `fund --pool wavax` wraps native AVAX → WAVAX inside the Prime Account (payable `depositNativeToken()`, no approve). This is the collateral the GM deposit will consume.
2. `gmx-deposit` queues the GMX deposit request and pays the execution fee. A keeper mints the GM LP tokens shortly after.
3. Re-run `gmx-positions` once it settles to confirm the minted GM balance.

The EOA must hold, up front: the deposit amount **plus** the execution fee (~0.08–0.19 AVAX) **plus** its own tx gas. The execution fee is paid as `msg.value`; GMX refunds any unused portion to the account.

### Change (add to the position)

```
gmx-deposit --market avax-usdc --amount N --side long --execute
```

Another `gmx-deposit` into the **same market** adds to the existing position (mints more GM). Same async + execution-fee mechanics as opening.

### Close (partial or full)

```
gmx-positions                                          # read the current GM balance to size the withdraw
gmx-withdraw --market avax-usdc --amount <GM-amount> --execute
```

`gmx-withdraw` burns `<GM-amount>` GM tokens back into the underlying assets in the account (for a two-sided GM market, both the volatile leg and USDC; for GM+, the single asset). Also async + keeper + execution fee. Size the withdraw against the live GM balance from `gmx-positions` — partial closes are fine; pass the full balance to close out entirely.

### Worked example (verified end-to-end, 24-05-2026)

A live walkthrough: funded 0.5 AVAX into a Prime Account, then `deltaprime gmx-deposit --market avax-usdc --amount 0.5 --side long --execute`; a GMX keeper minted **~2.76 GM (~$4.65)** a few blocks later, confirmed via `gmx-positions`. The first attempt reverted on gotcha (a) below before the fix; the retry succeeded once both fixes landed.

### Two gotchas (were real bugs, fixed 24-05-2026 — here so the behaviour is understood)

(a) **The deposit must carry the FULL solvency RedStone payload.** Before minting, the facet runs an inline solvency check that prices **every** debt-registry asset — the whole pool set `AVAX, USDC, BTC, ETH, USDT, EUROC` — even ones with zero balance/debt, each needing 3 unique RedStone signers in the appended payload. The tool builds the write payload from `prime_account_price_feeds(account)` + the GM feed. If any required feed is missing, the deposit reverts with `InsufficientNumberOfUniqueSigners(0,3)` (wrapped in DeltaPrime's `ProxyCalldataFailedWithCustomError`). The read path (`gmx-positions`) does **not** hit this, because GM view calls skip the full solvency simulation — only the write triggers it.

---

(b) **The execution fee needs a gas-price floor.** GMX keepers require a real execution fee (~0.08–0.19 AVAX), but Avalanche's live base fee can be ~0.01–0.02 gwei, which estimates a uselessly tiny fee the keeper would reject (the request would expire and refund without ever minting). The tool floors the gas price at **25 gwei** in the fee estimator so the `msg.value` clears GMX's requirement; GMX refunds the unused part to the account. The **EOA must hold the execution fee up front** (on top of the deposit amount and gas).

## TraderJoe V2 Liquidity Book — strategy

Compiled 24-05-2026 from LFJ docs + LB whitepaper, the MagicSea LB-fork docs, Eli5DeFi, and the LFJ Discord (incl. LFJ team member 0xlevi_). Decision-grade backing for `lb-add`/`lb-remove`. **Caveats:** exact shape *curvature* is unverified (LFJ renders the shapes as images); the rebalance dwell-time / skew thresholds below are practitioner & auto-manager conventions to calibrate against live behaviour, not protocol constants; `avax-usdc` = bin step 10 / base fee 0.05% is confirmed in the tool's pair config.

### Bin model
A LB pool is a set of discrete price **bins**. **Bin step** = spacing in bps (`avax-usdc` 10 = 0.10%/bin; `price(i+1) = price(i)·(1+binStep/1e4)`). The **active bin** holds the current price and is the only bin holding *both* tokens. Token **Y** (quote, USDC) sits in the active bin + all bins **below**; token **X** (volatile, WAVAX) in the active bin + all bins **above** — bins below are bids (USDC waiting to buy X as price falls), bins above are asks (X waiting to sell as price rises); LB is an order book made of liquidity. Swaps inside one bin have **zero slippage** (single fixed price); slippage only appears when a swap exhausts the active bin and crosses into the next. **Crossing bins converts your holdings** — price up sells your X for Y, price down buys X with your Y; this *is* the IL mechanism. **Out of range** (price past all your bins) = 100% one-sided and **zero fee accrual** — only the active bin earns fees, so you want it to sweep back and forth through your bins.

### The three shapes (`--shape`)
- **SPOT (uniform):** equal liquidity per bin. Balanced, the most resilient (no over-weighted bin), moderate symmetric IL. Best for ranging/stable markets and as the default for a volatile pair you can't babysit. The only safe hands-off shape.
- **CURVE (bell, peaked at active):** most liquidity near the active bin, tapering to the edges. Highest fee density per dollar while price stays centered, but least resilient and highest, **front-loaded** IL (a move off-center converts the bulk fast). For low-vol, mean-reverting markets with active re-centering only — never on a trending/high-vol pair left unmanaged.
- **BID-ASK (inverse, edge-weighted):** least at the active bin, most at the outer edges. Harvests volatility — deep edge liquidity catches big excursions and sells back *if price returns to center*; the natural DCA-in/out / layered-limit-order shape. **Back-loaded** IL: great if price mean-reverts within the range, punishing if it trends past an edge and never returns. For expected volatility spikes that mean-revert, or deliberate accumulation/distribution — never if you expect a clean one-way trend.

### Range / bin-count sizing
`--range R` → 2R+1 bins; with bin step 10, R bins each side ≈ ±(R×0.10%). R=10≈±1%, R=20≈±2%, R=39≈±3.9% (near the per-add max; account-wide cap 80 bins). Narrow = higher fee density but frequent out-of-range → frequent realized IL + gas. Wide = resilient (rare rebalancing) but diluted fees per bin. Size to realized volatility over your no-touch horizon: `R ≈ (expected % move)/0.10`, padded ~1.3×. Practitioner default for semi-active AVAX/USDC: SPOT, R=30–39. Width is downstream of management cadence + goal, not a fixed number. Avoid very small R unless actively managing — IL hits much faster when a tight range is breached.

### Rebalance vs wait vs widen
Rebalance here = `lb-remove` (full close, realizing the current one-sided conversion) + `lb-add` re-centered (2× gas, fresh slippage/id-slippage). No cheap partial nudge exists, so each rebalance costs the *full* realized IL + 2× gas — a high bar. Worth it IF:

```
fee_APR_at_center × position_value × expected_days_in_range / 365
   >  gas(2 tx) + realized_conversion_now + fees_lost_if_it_mean_reverts
```

Keyed heuristics: **distance out** (just past edge → WAIT; far past / clearly new regime → REBALANCE or WIDEN, IL is mostly realized either way); **time out** (brief → WAIT; sustained many hours → REBALANCE); **vol regime** (choppy/mean-reverting → WAIT, reversion bails you out; trending → REBALANCE/WIDEN, sitting out is dead capital); **≥80/20 inventory skew held for a dwell time** = a clean codifiable rebalance trigger (standard auto-manager pattern); **gas vs size** (small position → raise thresholds, only act on big sustained moves); **fee APR** (high → rebalance sooner; dead/low-volume pool → widen or EXIT). **WIDEN** (bigger R, SPOT) instead of re-centering when knocked out repeatedly (>~2× in a short window) — trades fee density for far fewer realized-IL events.

### Impermanent loss & fees
IL is the usual divergence loss (you sell the winner / buy the loser through your bins), but its **severity is set by shape + width**: narrow → fast, abrupt IL (price exits quickly, you're stuck one-sided); wide → slow, gentle. CURVE front-loads conversion at the center; SPOT converts ~linearly with price; BID-ASK back-loads to the edges (recoverable on reversion, punishing on a trend). **Hard truth:** a sustained trend in the volatile asset is *not* out-earnable by fees (a ~5%/day drop would need ~1825% APR to offset) — IL management = exiting/avoiding trends, not farming through them. Per-swap fee = **base fee** (`baseFactor×binStep`; AVAX/USDC = 0.05%) + **variable fee** (`∝ (volatilityAccumulator×binStep)²`, scales quadratically with volatility and decays in quiet periods). Fees are highest exactly during volatile, high-throughput periods — the engine behind **volatility farming** (bid-ask in choppy-but-reverting markets), which only nets positive if price reverts.

> Condensed IF/THEN decision ruleset lives in a downstream skill ("TraderJoe V2 LB — strategy & autonomous decisions"). The CLI itself only ships the mechanical primitives; strategy lives with the operator.

## GMX V2 GM pools — strategy

Compiled 24-05-2026 from GMX docs, the Dune GMX V2 LP dashboard, Compass Labs / Exponential / DeltaPrime guides, and the GMX Discord (13 sources). This is the *why/when*; the *how* (open/change/close, async keeper, exec fee, min-out, RedStone payload) is in §12 and capabilities §4. **Verify live numbers (Fee APR, Pool PnL APR, utilization, skew, funding) against app.gmx.io and dune.com/gmx-io/v2-lp-dashboard before acting** — GMX V2 evolves (funding was cut ~65% in May 2026; OI-balance calc changed Dec 2025).

### The GM model — what you hold
A GM token is an LP share in one GMX V2 market (index token + long token + short token; e.g. AVAX/USDC holds WAVAX + USDC). Pool value ≈ USD(long tokens) + USD(short tokens) + a slice of pending borrow fees − net pending trader PnL − impact pool; GM price = pool value / supply. **GM LPs are the counterparty to perp traders**: fees are unconditional (you earn them whether traders win or lose), but the PnL leg is a bet — net-winning traders are a pool liability (GM price down), net-losing traders enrich the pool (GM price up). `MAX_PNL_FACTOR` + Auto-Deleveraging (ADL force-closes profitable positions past a PnL/pool threshold) bound the downside. AVAX/BTC/ETH are fully-backed (the pool holds the asset backing long profits), not synthetic.

### Two-sided vs GM+ single-sided
Two-sided X/USDC: ~50% volatile + ~50% USDC backing, ~0.5× asset delta, earns swap fees, subject to swap price impact on deposit/withdraw. GM+ single-sided: 100% volatile asset, ~1× delta, no swap fees, **zero swap price impact**. GM+ holds a fixed asset *quantity* that grows/shrinks with fees-minus-trader-PnL; its USD value rides the asset 1:1. Two-sided AVAX/USDC is NOT delta-neutral on its own (still ~50% AVAX-exposed) — the asset price is the single biggest driver of GM value.

### Fees / yield
63% of all market fees → GM LPs (auto-compound into GM price; GM count fixed, price rises — no claim). Sources: trading (open/close, lower bps on the side that improves OI balance), swap (two-sided only), borrow (utilization kink; larger-OI side pays more), funding (dominant OI side pays minority; ~65% smaller since May 2026), liquidation, price-impact. **Fee APR ≠ realized return**: Fee APR excludes asset price, trader PnL, and funding. The Dune "Pool PnL APR" (realized trader-flow P/L) can be negative at high Fee APR — a GM ETH pool showed −8% over 1Y while ETH rose 50-70% (winning longs cost the pool). "Performance APR" benchmarks vs a 50/50 rebalance. Always read Fee APR AND Pool PnL APR.

### Risks to reason about
1. Skew / net-trader-PnL: heavily one-sided OI that's positioned to win drains pool value even while fees accrue; balanced OI is the LP's friend (ADL only caps the extreme). 2. Underlying price exposure: two-sided ~50%, GM+ ~100% — the dominant GM-price driver, outside GMX's control. 3. Price impact on large single-sided deposit/withdraw (use balanced/pair deposits; GM+ has none). 4. Available-liquidity/reserve limits: sellable = pool×reserveFactor − reserved; high utilization can block full withdrawal until trader positions close. 5. Async keeper/freeze/exec-fee (capabilities §4): account frozen until callback; cancelled request refunds fee, mints nothing. 6. Bridge-depeg tail risk on BTC.b/WETH.e.

### Regime logic & comparisons
PROVIDE when Fee APR is high, utilization healthy (<~90%), OI skew balanced — or one-sided in a direction you expect to lose (counterparty edge) — and the price exposure suits your view. EXIT/AVOID on adverse skew (winning side against your view, Pool PnL APR negative), a strong move against the pool's net position, collapsing volume/Fee APR, turning bearish (esp. GM+), or maxed utilization. Pick the market on best live (Fee APR + non-negative Pool PnL APR), liquidity depth, and aligned/neutral skew; BTC/ETH are deeper and less idiosyncratic than AVAX. **GM vs holding**: GM adds fee yield + a trader-PnL bet on top of the asset; it beats HODL when traders net-lose and fees are strong, underperforms in strong trends where leveraged longs win. **GM vs TraderJoe LB**: GM has no swap-IL and is passive but takes a counterparty bet; LB is active bin management with divergence-loss IL. **DeltaPrime leverage**: borrowing USDC to scale a GM deposit = leveraged-long AVAX (a documented ~3x play), NOT neutral; for delta-neutral, borrow the volatile leg and `swap-debt` so debt composition matches the pool — keep account health well above liquidation.

> Condensed IF/THEN ruleset lives with the operator. The CLI ships the mechanical primitives only; deciding *when* to use them is downstream.
