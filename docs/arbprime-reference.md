# arbprime — DeltaPrime on Arbitrum One: full reference

_Verified on-chain 03-06-2026 against `https://arb1.arbitrum.io/rpc` (chain id 42161). Every address below was either read from the live contracts (factory/TokenManager/beacon/pools/facet-selector mapping) or taken from verified facet source and confirmed to have code on-chain._

## 0. The two-deployments trap (read first)

DeltaPrime has **two** deployments on Arbitrum. `arbprime` targets the **live** one — the one app.deltaprime.io uses and the deployed facet bytecode references (via `contracts/lib/arbitrum/DeploymentConstants.sol`):

| Contract | LIVE address (use this) | Stale artifact deployment (do NOT use) |
|---|---|---|
| SmartLoansFactory | `0xFf5e3dDaefF411a1dC6CcE00014e4Bca39265c20` | `0x97f4C81Be9edD44953Da7A1F289D30d3a47F6E4E` |
| SmartLoanDiamondBeacon | `0x62Cf82FB0484aF382714cD09296260edc1DC0c6c` | `0x968f944e9c43FC8AD80F6C1629F10570a46e2651` |
| TokenManager | `0x0a0D954d4b0F0b47a5990C0abd179A90fF74E255` | `0x4f032CC36B72D934551bc0395Df17162eF92D8D9` |

The repo's `deployments/arbitrum/*TUP.json` artifacts point at the stale deployment (2 pools, ~no liquidity). The live one carries 5 registered pools and the full 29-asset collateral set. `factory.smartLoanDiamond()` == the live beacon (verified). AddressProvider (shared): `0x6Aa0Fe94731aDD419897f5783712eBc13E8F3982`.

## 1. Architecture

Identical to DeltaPrime Avalanche: EIP-2535 Diamond. Each user gets a **Prime Account** (SmartLoan beacon proxy) created via `SmartLoansFactory.createLoan()` / `createAndFundLoan(bytes32,uint256)`; `getLoanForOwner(address)` resolves it. All borrow/repay/fund/swap/LP calls are sent **to the Prime Account address** and delegate through the beacon to chain-specific facets (27 on the live beacon). Savings-pool deposits/withdrawals go **directly to the pool proxies** from the EOA.

- No POA middleware (standard rollup; unlike Avalanche C-chain).
- Gas: base fee ~0.01 gwei; arbprime uses `max(2× network, 0.01 gwei)`.
- Multicall3 at the canonical `0xcA11bde05977b3631167028862bE2a173976CA11`.
- Native-wrapped asset is **WETH** `0x82aF49447D8a07e3bd95BD0d56f35241523fBab1`, account symbol `ETH`. The eth pool's native path is the payable `depositNativeToken()`.

## 2. Pools (lending / borrowable)

`TokenManager.getAllPoolAssets()` (live) = `[USDC, DAI, BTC, ARB, ETH]`. The tool exposes 4 (DAI deliberately excluded):

| key | Pool proxy | Underlying | symbol | dec | native |
|---|---|---|---|---|---|
| `eth` | `0x788A8324943beb1a7A47B76959E6C1e6B87eD360` | `0x82aF49447D8a07e3bd95BD0d56f35241523fBab1` (WETH) | ETH | 18 | yes |
| `usdc` | `0x8Ac9Dc27a6174a1CC30873B367A60AcdFAb965cc` | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` (native Circle) | USDC | 6 | no |
| `arb` | `0xC629E8889350F1BBBf6eD1955095C2198dDC41c2` | `0x912CE59144191C1204E64559FE8253a0e49E6548` | ARB | 18 | no |
| `btc` | `0x0ed7B42B74F039eda928E1AE6F44Eed5EF195Fb5` | `0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f` (WBTC) | BTC | 8 | no |

(DAI pool, excluded: `0xFA354E4289db87bEB81034A3ABD6D465328378f1` / `0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1`.)

Pool mechanics are identical to Avalanche/Base: hand-curated `POOL_ABI`, lender withdrawals via the 24h `createWithdrawalIntent(uint256)` → two-arg intent-gated `withdraw(uint256,uint256[])` (selector `0x5915d806`) flow, 48h execute window after the 24h lock.

## 3. Collateral assets (TokenManager, 29 registered)

Resolver: `getAssetAddress(bytes32,bool)` (**`getTokenAddress` does not exist on this TokenManager**). Hold any of these in the Prime Account; borrow only pool assets. Key non-pool assets:

| symbol | address | dec |
|---|---|---|
| USDT | `0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9` | 6 |
| GMX | `0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a` | 18 |
| DAI | `0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1` | 18 |
| LINK | `0xf97f4df75117a78c1A5a0DBb814Af92458539FB4` | 18 |
| UNI | `0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0` | 18 |
| weETH | `0x35751007a407ca6FEFfE80b3cB397736D2cf4dbe` | 18 |
| wstETH | `0x5979D7b546E38E414F7E9822514be443A4800529` | 18 |
| JOE | `0x371c7ec6D8039ff7933a2AA28EB827Ffe1F52f07` | 18 |
| PRIME | `0x3De81CE90f5A27C5E6A5aDb04b54ABA488a6d14E` | 18 |
| MOO_GMX | `0x5B904f19fb9ccf493b623e5c8cE91603665788b0` | 18 |

Plus the GM_*/GLV* basket tokens (§5) — all registered assets in their own right.

## 4. Facet map (live beacon, derived by selector — authoritative)

| Facet | Address |
|---|---|
| AssetsOperations (borrow/repay/fund) | `0x53C1F700211BBBdcb3077BaaED5C76b2Bb64A567` |
| DepositNativeToken | `0x8D784A9bEab8eE3517b2B686616F9889e6994D95` |
| SolvencyFacetProdArbitrum | `0x2a43C8Db8DAc47fA5B62E5343005458ac7Bf2a8F` |
| Balances / StakedPositions views | `0xf33ca4515d75DDC22765dB156264b69530cCfa51` |
| WithdrawalIntentFacet | `0xa8DF1C6Aa5E04e8Aa473EaAE56B1216717e9c52A` |
| ParaSwapFacet (paraSwapV6) | `0x641493cB5143980E9e71f45442144D65CB19f90A` |
| YieldYakSwapArbitrumFacet (yakSwap) | `0xa60cD8eBbB1C612177aE1098C80c6c30da8ec6B3` |
| SwapDebtFacet (swapDebtParaSwap) | `0xdc168a1F130F6416a8D77b1F8A49D232520Bc576` |
| GmxV2FacetArbitrum (two-sided GM) | `0x3b84303BE9adB0e09d1657534704c9CbbE9d81A3` |
| GmxV2PlusFacetArbitrum (single-sided) | `0x736D70bAbBA06FC54E42BBc329Ee82EB62241A11` |
| GlvFacetArbitrum | `0xCA9676425540D51BD3247c61bb9FC05eC10Ce1AB` |
| GmxV2CallbacksFacetArbitrum | `0x1D74FC4776848FE0D5da3F0d5Fd4DBE1056a636F` |
| TraderJoeV2ArbitrumFacet | `0x9DB8016429f61a0562f20D2C1aC7FA01dFe0aFe4` |
| PrimeLeverageFacet | `0x5D3301e8ab82826B7A6761867961B308a7938dcc` |

Note: several facet addresses published in the repo artifacts belong to the stale deployment (e.g. its ParaSwap facet `0xEd01F33f…`). The table above was re-derived from the **live** beacon's `facetAddresses()` + `facetFunctionSelectors()`.

## 5. GMX V2 — GM, GM+, GLV

GMX core (Arbitrum, shared by both deployments, verified): ExchangeRouter `0x1C3fa76e6E1088bCE750f23a5BFcffa1efEF6A41`, Router `0x7452c558d45f8afC8c83dAe62C3f8A5BE19c71f6`, DataStore `0xFD70de6b91282D8017aA4E741e9Ae325CAb992d8`, Reader `0x470fbC46bcC0f16532691Df360A07d8Bf5ee0789`, DepositVault `0xF89e77e8Dc11691C9e8757e84aaFbCD8A67d7A55`, WithdrawalVault `0x0628D46b5D145f183AdB6Ef1f2c97eD1C4701C55`, GLV Reader `0x2C670A23f1E798184647288072e84054938B5497`.

Same async mechanics as Avalanche: payable, keeper-executed, account frozen per-market until the callback; execution fee estimated off DataStore gas params (callbackGasLimit 600000) × gas price × fee-buffer; min-out slippage hard-capped at 5% by `isWithinBounds`. RedStone-gated. GM feed id = the bytes32 TokenManager symbol itself.

**Two-sided GM markets** (`deposit<X>UsdcGmxV2(bool isLongToken, uint256 tokenAmount, uint256 minGmAmount, uint256 executionFee)` / `withdraw<X>UsdcGmxV2`):

| key | TM symbol | GM token | long leg |
|---|---|---|---|
| `eth-usdc` | GM_ETH_WETH_USDC | `0x70d95587d40A2caf56bd97485aB3Eec10Bee6336` | ETH |
| `btc-usdc` | GM_BTC_WBTC_USDC | `0x47c031236e19d024b42f8AE6780E44A573170703` | BTC |
| `arb-usdc` | GM_ARB_ARB_USDC | `0xC25cEf6061Cf5dE5eb761b50E4743c1F5D7E5407` | ARB |
| `link-usdc` | GM_LINK_LINK_USDC | `0x7f1fa204bb700853D36994DA19F830b6Ad18455C` | LINK |
| `uni-usdc` | GM_UNI_UNI_USDC | `0xc7Abb2C5f3BF3CEB389dF0Eecd6120D451170B50` | UNI |
| `gmx-usdc` | GM_GMX_GMX_USDC | `0x55391D178Ce46e7AC8eaAEa50A72D1A5a8A622Da` | GMX |
| `near-usdc` | GM_NEAR_WETH_USDC | `0x63Dc80EE90F26363B3FCD609007CC9e14c8991BE` | WETH (synthetic) |
| `atom-usdc` | GM_ATOM_WETH_USDC | `0x248C35760068cE009a13076D573ed3497A47bCD4` | WETH (synthetic) |
| `sui-usdc` | GM_SUI_WETH_USDC | `0x6Ecf2133E2C9751cAAdCb6958b9654baE198a797` | WETH (synthetic) |
| `sei-usdc` | GM_SEI_WETH_USDC | `0xB489711B1cB86afDA48924730084e23310EB4883` | WETH (synthetic) |

(The facet also ships a Sol market, but SOL is not in the live registered asset set — omitted.) Synthetic markets (NEAR/ATOM/SUI/SEI): the index is synthetic; the long deposit token is WETH.

**GM+ single-sided** (`deposit<X>GmxV2Plus(uint256 tokenAmount, uint256 minGmAmount, uint256 executionFee)`):

| key | TM symbol | GM token |
|---|---|---|
| `eth+` | GM_ETH_WETH | `0x450bb6774Dd8a756274E0ab4107953259d2ac541` |
| `btc+` | GM_BTC_WBTC | `0x7C11F78Ce78768518D743E81Fdfa2F860C6b9A77` |
| `gmx+` | GM_GMX_GMX | `0xbD48149673724f9cAeE647bb4e9D9dDaF896Efeb` |

**GLV vaults (Arbitrum-only — no Avalanche equivalent).** Deposit: `deposit{Weth,Btc}UsdcGlv(bool isLongToken, uint256 tokenAmount, uint256 minGlvAmount, address targetMarket, uint256 executionFee)` — note the extra `targetMarket` (the GM market inside the GLV that receives the liquidity). Withdraw: `withdraw{Weth,Btc}UsdcGlv(uint256 glvAmount, address targetMarket, uint256 minLongTokenAmount, uint256 minShortTokenAmount, uint256 executionFee)`.

| key | TM symbol | GLV token | default targetMarket |
|---|---|---|---|
| `weth-usdc` | GLVWETHUSDC | `0x528A5bac7E746C9A509A1f4F6dF58A03d44279F9` | GM_ETH_WETH_USDC |
| `btc-usdc` | GLVBTCUSDC | `0xdF03EEd325b82bC1d4Db8b49c30ecc9E05104b96` | GM_BTC_WBTC_USDC |

## 6. Swaps

- **ParaSwap v6.2** — same Augustus router as Avalanche/Base (`0x6A000F20005980200259B80c5102003040001068`), API network 42161, same two decoded selectors (`swapExactAmountIn` `0xe3ead59e`, `swapExactAmountInOnUniswapV3` `0x876a02f6`), same executor whitelist semantics, hard 5% facet slippage cap.
- **YieldYak** — exists on Arbitrum with a **different router**: `0xb32C79a25291265eF240Eb32E9faBbc6DcEE3cE3` (Avalanche's is `0xC472…488c`). `findBestPath` off-chain, whitelisted adapters, same `yakSwap` facet call.
- **swap-debt** — `swapDebtParaSwap(_fromAsset,_toAsset,_repayAmount,_borrowAmount,selector,data)`, identical semantics and 5% USD-diff cap.

## 7. TraderJoe (LFJ) V2 Liquidity Book — 11 whitelisted pairs

Whitelist source: the facet's own `getWhitelistedTraderJoeV2Pairs()` (20 entries), filtered to pairs whose **both tokens are registered assets** (the DAI/USDC.e/WOO/GRAIL/MAGIC/ezETH pairs would fail the facet's `_getAvailableBalance` symbol lookup). Each pair verified on-chain (code, canonical tokenX/tokenY, binStep). Routers are LFJ's deterministic cross-chain deployments: v2.1 `0xb4315e873dBcf96Ffd0acd8EA43f689D8c20fB30`, v2.2 `0x18556DA13313f3532c54711497A8FedAC273220E`.

| key | pair | ver | binStep | X / Y |
|---|---|---|---|---|
| `eth-usdc` | `0x69f1216cB2905bf0852f74624D5Fa7b5FC4dA710` | v2.1 | 15 | ETH/USDC |
| `eth-usdc-10` | `0xb7236B927e03542AC3bE0A054F2bEa8868AF9508` | v2.2 | 10 | ETH/USDC |
| `eth-usdt` | `0xd387c40a72703B38A5181573724bcaF2Ce6038a5` | v2.1 | 15 | ETH/USDT |
| `eth-usdt-10` | `0x055f2cF6da90F14598D35C1184ED535C908dE737` | v2.2 | 10 | ETH/USDT |
| `arb-eth` | `0x0Be4aC7dA6cd4bAD60d96FbC6d091e1098aFA358` | v2.1 | 10 | ARB/ETH |
| `arb-eth-v22` | `0xC09F4ad33a164e29DF3c94719ffD5F7B5B057781` | v2.2 | 10 | ARB/ETH |
| `btc-eth` | `0xcfA09B20c85933B197e8901226ad0D6dACa7f114` | v2.1 | 10 | BTC/ETH |
| `gmx-eth` | `0x60563686ca7b668e4a2d7D31448e5F10456ecaF8` | v2.1 | 20 | GMX/ETH |
| `joe-eth` | `0x4b9bfeD1dD4E6780454b2B02213788f31FfBA74a` | v2.1 | 20 | JOE/ETH |
| `wsteth-eth` | `0x71bc33F539f83b99674D71AcFeb2ce0373376512` | v2.2 | 5 | wstETH/ETH |
| `weeth-eth` | `0x2088eB5E23F24458e241430eF155d4EC05BBc9e8` | v2.2 | 5 | weETH/ETH |

**Max 300 bins per Prime Account on Arbitrum** (the facet's `maxBinsPerPrimeAccount()` override; Avalanche's is 80). `addLiquidityTraderJoeV2` is RedStone-gated; `removeLiquidityTraderJoeV2` is not and closes the account's entire position for the pair.

## 8. PRIME leverage tiers

Same `PrimeLeverageFacet` mechanism as Avalanche (BASIC ~5x / PREMIUM 10x; stake `getRequiredPrimeStake` of PRIME, rent-debt accrues; ratios live in the TokenManager — never hardcode). PRIME token (`Prime_L2`): `0x3De81CE90f5A27C5E6A5aDb04b54ABA488a6d14E`. **The PRIME DEX LP pair on Arbitrum is PRIME-WETH** (Avalanche's is PRIME-WAVAX). sPrime `0x8B1e2420e0d453a718d4b70e3a043263Eab77851`, vPrimeControllerArbitrum `0x2323dAC85C6Ab9bd6a8B5Fb75B0581E31232d12b`.

## 9. RedStone & withdrawal mechanics

**Identical to Avalanche** — `SolvencyFacetProdArbitrum` is literally `contract SolvencyFacetProdArbitrum is SolvencyFacetProd {}`; both chains consume `redstone-primary-prod`, 3-of-5 signers, same gateways and 9-byte marker. The WithdrawalIntentFacet (shared, `0xa8DF1C6Aa5E04e8Aa473EaAE56B1216717e9c52A`) gives the 24h lock + 48h execute window (72h total) on Prime-Account collateral; pool lender withdrawals run the same intent pattern on the pool contract.

## 10. Key resolution / env

`ARBPRIME_PRIVATE_KEY` → `DELTAPRIME_PRIVATE_KEY` → `ARBPRIME_ENV_FILE`+`ARBPRIME_KEY_VAR` (→ `DELTAPRIME_*`) → `ARBPRIME_AGENT` → `DELTAPRIME_AGENT` → `DEFAULT_AGENT` (= `parakletos`, the original back-compat default). `--as <agent>` CLI flag beats everything. `ARBPRIME_RPC` overrides the RPC. Same EVM key works on all three chains.

## 11. Not yet tooled (live on-chain, deferred by scope)

PenpieFacet (Pendle LP) `0xFf1138Cb2C4653eb9B775ed53ca3bFcfA6ae2b83`, BeefyFinanceArbitrumFacet `0xbc6fF4657e94DfE30704F398f462d6FFf90D2edD`, SushiSwapDEXFacet `0xE34c4eAE900579a6DdB0f8203823Fd3F8dBf60B3`, YieldYakFacetArbi (Wombex autocompound) `0xDB53236D355Aa62ce7b1E349f45cFB4c23c62C7D`, legacy GLPFacetArbi `0x1B8c6Ece5588D21369935A91D3f2459F66F0cbD0`. (Note: these were sourced from repo artifacts; re-verify against the live beacon before tooling them.)
