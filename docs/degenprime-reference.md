# DegenPrime Reference

Canonical reference for the DegenPrime protocol on Base and the `degenprime` CLI command that drives it. All addresses and behaviours verified on-chain on 29-05-2026 (Base, chainId 8453).

**Audience:** anyone (human or agent) who needs to understand the protocol surface on Base, the pool / facet addresses, and what each `degenprime` subcommand does. Pair with [`degenprime-capabilities.md`](degenprime-capabilities.md) when you need the exact function signatures, calldata encoding, approve targets, and per-call RedStone requirements.

**What the tool covers today (v1):** lending core (deposit / withdraw / borrow / repay / fund), Degen Account create+fund, swaps (ParaSwap v6 / Velora), swap-debt, the universal 24h delayed collateral withdrawal (create / list / execute / cancel), and a read-only Aerodrome position inventory. The RedStone payload wrap is shipped. Aerodrome write paths and position-composition decoding are deferred to v2.

---

## 1. What DegenPrime is

DegenPrime is the Base-chain sister protocol to DeltaPrime on Avalanche, built by the same team (DeltaPrimeLabs) on the same EIP-2535 Diamond + per-user smart loan architecture. Two layers:

1. **Savings pools** — deposit an asset to earn lending yield. Regular wallets (EOAs) deposit and withdraw directly against the pool contract.
2. **Degen Accounts** — per-user smart-contract accounts (DegenPrime's name for what DeltaPrime calls a Prime Account) for leveraged borrowing. You create one, fund it with collateral, then borrow against that collateral. The Degen Account is what talks to the pools on the borrow side.

You don't need a Degen Account to earn yield. You do need one to borrow.

The DegenPrime brand leans into Base's memecoin culture: 32 collateral assets including a long tail of base-native memes (TOSHI, BRETT, BASEDPEPE, KEYCAT, MOG, BNKR, DRB, CLANKER, DINO, ZORA, etc.) alongside the usual blue-chip basket. The architecture is identical to DeltaPrime; the asset universe is the differentiator.

---

## 2. Architecture

### Core addresses (verified on Base 29-05-2026)

| Component | Address | Notes |
|-----------|---------|-------|
| SmartLoansFactory (TUP) | `0x5A6a0e2702cF4603a098C3Df01f3F0DF56115456` | Creates and tracks Degen Accounts |
| SmartLoanDiamondBeacon | `0x85c2BAA28C1d7A07bFC5C5c9903FFf4c39ae5151` | Beacon every Degen Account proxies to |
| TokenManager | `0x97e74e0A3D2713D87E3fBf6d18F869042F0d0116` | Source of truth: 8 pools + 32 collateral tokens |
| BaseOracle (TUP) | `0x7E7Ca97A0AC811e76Efb4AD8f7AaAfeFdB0d46F5` | TWAP oracle for symbols without RedStone feeds |
| Owner Multisig | `0xd6Ef2C4DeEcCD77E154b99bC2F039E5f82DCc7c9` | Protocol owner |
| Admin Multisig | `0xCD053EeA1B82867c491dECe0A8833941849771D0` | Protocol admin |
| ParaSwap Augustus | `0x6A000F20005980200259B80c5102003040001068` | v6 router, shared with Avalanche |
| Base wrapped ETH (WETH) | `0x4200000000000000000000000000000000000006` | Native ETH wrapper, used by the weth pool |

### Pools (savings layer)

- Each pool is a Transparent Upgradeable Proxy (TUP) sharing the same DegenPrime Pool implementation (same code lineage as DeltaPrime's pool — `deposit`, `withdraw`, `borrow`, `getBorrowed`, `getDepositRate`, `getBorrowingRate`, etc.).
- The on-chain registry of **active** pools is the **TokenManager**:
  - `getPoolAddress(bytes32 asset) -> address` — resolve symbol to active pool proxy.
- **The TokenManager is the source of truth.** Same rule as on DeltaPrime: never trust gitbook or random repos for the pool addresses.

### Degen Accounts (borrow / leverage layer)

- Degen Accounts are EIP-2535 Diamonds, one per owner.
- Created via the **SmartLoansFactory**:
  - `createLoan() -> address` — creates an EMPTY Degen Account.
  - `createAndFundLoan(bytes32 asset, uint256 amount) -> address` — create + fund in one tx (ERC20 only).
  - `getLoansForOwner(address) -> address[]` — owner to account lookup. **Note the plural/array shape**: this differs from DeltaPrime's singular `getLoanForOwner` that returns one address. The one-loan-per-owner invariant still holds in practice (`createLoan` reverts if the caller already has one); the tool reads `loans[0]` when non-empty.
  - The diamond beacon is `SmartLoanDiamondBeacon` (`0x85c2BAA28C1d7A07bFC5C5c9903FFf4c39ae5151`). Every Degen Account is a per-user proxy delegating to this beacon, so the facet logic (borrow / repay / fund + view functions) is reachable at any deployed account address.

### Diamond pattern: beacon vs direct

DeltaPrime's diamond is the address directly. DegenPrime uses a **beacon proxy** pattern — `SmartLoanDiamondBeacon.implementation()` returns the active diamond implementation, and the beacon address is what the factory wires into each new Degen Account. Functionally equivalent, just one extra hop for facet upgrades.

---

## 3. Active pools (USE THESE)

Resolved from `TokenManager.getPoolAddress()` and verified live (totalSupply > 0, wired in TokenManager, 29-05-2026).

| Pool key | bytes32 symbol | Active pool proxy | Underlying token | Decimals | Native |
|----------|----------------|-------------------|------------------|----------|--------|
| `usdc` | `USDC` | `0x2Fc7641F6A569d0e678C473B95C2Fc56A88aDF75` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | 6 | no |
| `weth` | `ETH` | `0x81b0b59C7967479EC5Ce55cF6588bf314C3E4852` | WETH `0x4200…0006` | 18 | **yes** |
| `cbbtc` | `cbBTC` | `0xCA8C954073054551B99EDee4e1F20c3d08778329` | `0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf` | 8 | no |
| `aero` | `AERO` | `0x4524D39Ca5b32527E7AF6c288Ad3E2871B9f343B` | `0x940181a94A35A4569E4529A3CDfB74e38FD98631` | 18 | no |
| `brett` | `BRETT` | `0x6c307F792FfDA3f63D467416C9AEdfeE2DD27ECF` | `0x532f27101965dd16442E59d40670FaF5eBB142E4` | 18 | no |
| `kaito` | `KAITO` | `0x293E41F1405Dde427B41c0074dee0aC55D064825` | `0x98d0baa52b2D063E780DE12F615f963Fe8537553` | 18 | no |
| `cbdoge` | `cbDOGE` | `0xAf61B10BDB78e31fdbC5Da4e57d60e32aFe468B9` | `0xcbD06E5A2B0C65597161de254AA074E489dEb510` | 8 | no |
| `cbxrp` | `cbXRP` | `0x056076e717332403Bc23B2D4F6D87683ceF582B9` | `0xcb585250f852C6c6bf90434AB21A00f02833a4af` | 6 | no |

The `weth` pool is the native-ETH path: `deposit` accepts `value` and the pool wraps ETH → WETH internally (same pattern as DeltaPrime's `wavax` pool). All other pools take an explicit ERC20 approve + deposit.

**TVL note (29-05-2026):** total deposits across all 8 pools are ~$29k. Some pools are basically empty — cbDOGE ~$4, cbXRP ~$9. A meaningful borrow on those would push utilization into the kink and skew rates. The blue-chip pools (USDC, ETH, cbBTC) carry most of the TVL.

### Collateral assets (32 total, beyond the pool set)

A Degen Account can hold any of the 32 TokenManager-registered collateral tokens, enumerated via `TokenManager.getSupportedTokensAddresses()` and resolved to symbols via `tokenAddressToSymbol(address)`. Known symbols (29-05-2026):

`USDC, ETH, cbBTC, AERO, BRETT, AIXBT, TOSHI, VIRTUAL, MOG, SKI, DEGEN, KEYCAT, BASEDPEPE, KAITO, VVV, CLANKER, BNKR, DRB, COOKIE, ZORA, DINO, EUROC, weETH, ezETH, cbDOGE, cbXRP, SPX, LBTC, USDT, cbLTC, AVNT, GIZA.`

Only the 8 in the pool table above are lendable / borrowable; the rest are collateral-only (you can `fund` them in via direct ERC20 transfer + the Degen Account's accounting view, but you cannot `borrow` against them through a pool because no pool exists for them).

---

## 4. RedStone oracle config

Verified **identical** to DeltaPrime's config — same data service, same authorised signer set, same threshold, same gateways, same marker bytes. The Solvency math reads RedStone-signed prices appended to the tx calldata; the wrapping is the same payload format the DeltaPrime tool builds.

| Field | Value |
|-------|-------|
| Data service | `redstone-primary-prod` |
| Signers threshold | 3 of 5 |
| Authorised signers | `0x8bb8…b774`, `0xdeb2…8499`, `0x51ce…d202`, `0xdd68…b5be`, `0x9c5a…b6de` |
| Gateways | `oracle-gateway-1.a.redstone.finance`, `oracle-gateway-2.a.redstone.finance` |
| Value decimals | 8 |
| Marker bytes | `000002ed57011e0000` (9 bytes) |

**Feed coverage on Base is partial.** Of the ~32 supported collateral symbols, only 13 have `redstone-primary-prod` feeds:

> **In RedStone:** `USDC, ETH, cbBTC, AERO, BRETT, KAITO, DEGEN, MOG, weETH, EUROC, USDT, LBTC, ezETH`.
>
> **NOT in RedStone (priced on-chain by SolvencyFacet via BaseOracle TWAP):** `cbXRP, cbDOGE, TOSHI, KEYCAT, VIRTUAL, SKI, BASEDPEPE, AIXBT, VVV, SPX, CLANKER, BNKR, DRB, COOKIE, ZORA, DINO, AVNT, GIZA, cbLTC`.

The tool filters its RedStone payload to feed-available symbols only. The SolvencyFacet sources the rest from BaseOracle internally, so `summary`'s total value / debt / health figures cover every asset — but per-symbol USD lines in the output only show for the 13 feed-available symbols (the BaseOracle prices are not exposed through `getPrices`).

**Consequence for `swap-debt`:** both legs must have RedStone feeds, because the facet's value-match step calls `getPrices` to enforce its 5% USD-diff cap. The tool refuses if either symbol isn't in the feed set.

---

## 5. Aerodrome integration (read-only in v1)

Aerodrome is the canonical Base DEX (a Solidly fork) and DegenPrime's only native DeFi integration beyond the lending / swap core. The diamond exposes a small surface; only the read view ships in tool v1.

**Confirmed wired view selectors:**

- `getOwnedStakedAerodromeTokenIds() -> uint256[]` — list of Aerodrome NFT tokenIds the Degen Account owns or has staked. Oracle-free, always works. This is the v1 `aerodrome-positions` command.
- `getPositionCompositionSimplified(uint256 tokenId)` — exists, but the return shape needs decoding work (positions are concentrated-liquidity NFTs with bin-style composition). Decoding is deferred to v2.

**Confirmed wired write selectors (NOT in v1):**

- `claimRewardsAerodrome(uint256)` — claim accrued AERO / pool fees for a position.
- `decreaseLiquidityAerodrome(uint256, uint128, uint256, uint256, uint256)` — partial / full liquidity removal.

**Deferred to v2** (exist on Aerodrome itself, exact diamond signatures still need probing): `depositLiquidityAerodrome`, `stakePositionAerodrome`, `unstakePositionAerodrome`, `increaseLiquidityAerodrome`.

v1 lists tokenIds and points the user at the Aerodrome UI for manage / claim. Once the composition return shape is decoded and the write signatures probed against a live position, v2 can expose `aerodrome-claim` / `aerodrome-decrease` and eventually full add/stake.

---

## 6. Differences from DeltaPrime

DegenPrime is architecturally the same protocol as DeltaPrime, but the on-chain surface diverges in a handful of load-bearing places. The list below is what actually trips up a port.

### 6.1 Universal 24h withdrawal time-lock

Both protocols now lock **every** withdrawal that leaves the protocol behind a `WithdrawalIntent` flow — this covers both the lender-side savings pools and the Degen/Prime Account collateral. The time-lock is always 24h; the execute window differs by path. Nothing exits instantly on either protocol. On DegenPrime, **every** collateral withdrawal from a Degen Account is locked, regardless of asset. The Degen Account flow is:

1. `createWithdrawalIntent(bytes32 asset, uint256 amount)` — **RedStone-gated** on the Degen Account (on-chain solvency check at create), registers the intent.
2. 24h time-lock, then a **48h** execute window (72h total; `expiresAt = actionableAt + 48h`).
3. `executeWithdrawalIntent(bytes32 asset, uint256[] indices)` — RedStone-gated, pulls the funds to the EOA.

`cancelWithdrawalIntent(bytes32, uint256)` aborts a pending intent (oracle-free). `getAvailableBalance(bytes32)` is the oracle-free view of in-account balance minus pending intents.

Lender-side pool ("diamond hands") withdrawals are ALSO time-locked, but with the pool's own executor and a shorter window: the single-arg `withdraw(uint256)` reverts (bare `0x`, never resolves a named intent), so the flow is `createWithdrawalIntent(uint256)` (oracle-free) → wait 24h → `withdraw(uint256 amount, uint256[] intentIndices)` (selector 0x5915d806, oracle-free); `cancelWithdrawalIntent(uint256 index)` is oracle-free. The DegenPrime pool re-anchors `expiresAt` to `block.timestamp + 48h`, so its execute window is **24h** (48h total), NOT 48h. This is the savings-pool lender side; the bytes32-asset calls above are the Degen Account collateral side. The `withdraw` / `withdrawal-requests` / `execute-withdrawal-request` / `cancel-withdrawal-request` commands drive the pool side; the `*-collateral` / `withdrawal-intents` commands drive the collateral side. Plan around the lock: surprise 24h delays are how DeFi positions end up wedged. (Verified on-chain 2026-06-02.)

### 6.2 No premium / leverage-tier system

DeltaPrime has `PrimeLeverageFacet` — stake PRIME tokens to unlock a 10x leverage tier (vs the ~5x default), with the required stake proportional to USD borrow and a PRIME-denominated rent-debt accruing over time. The DegenPrime gitbook mentions a `$DgP` token, but it is **not deployed**, and the corresponding facet selectors (`getLeverageTier()`, `getRequiredPrimeStake()`, `stakeAndActivatePremium()`, etc.) are **not wired** on the live diamond. Max leverage is ~5x flat. No premium tier, no rent-debt track, no `prime-*` commands.

### 6.3 Swap routing: ParaSwap only

DeltaPrime ships two swap routes (`YieldYakSwapFacet` with on-chain `findBestPath` discovery + `ParaSwapFacet` with off-chain API calldata). On Base, YieldYak doesn't exist as a chain-native aggregator, and the diamond has no equivalent on-chain router facet wired. The only swap route is **ParaSwap v6 / Velora**, called via `ParaSwapFacet.paraSwapV6(bytes4 selector, bytes data)`.

The facet decodes exactly two router methods:

- `swapExactAmountIn` (selector `0xe3ead59e`) — generic executor route.
- `swapExactAmountInOnUniswapV3` (selector `0x876a02f6`) — Uniswap V3 direct route.

If the ParaSwap API returns any other method (`multiSwap`, `megaSwap`, `protectedSimpleSwap`, etc.), the build refuses. The tool passes `excludeContractMethods` to the API to keep it on a decodable route. The facet enforces a hard 5% slippage cap (RedStone-priced) on top of the `--slippage` flag. Executor whitelist applies as on DeltaPrime; the tool patches to a known-good fallback executor if ParaSwap returns one that isn't on the list.

### 6.4 Factory function shape

```
DeltaPrime:   getLoanForOwner(address) -> address       // singular, scalar
DegenPrime:   getLoansForOwner(address) -> address[]    // plural, array
```

The factory still enforces one loan per owner (`createLoan` reverts if the caller already has one), so the array is always length 0 or 1. The tool collapses an empty array to `None` and reads `loans[0]` otherwise. Cosmetic but a real footgun if you copy DeltaPrime's lookup code verbatim.

### 6.5 Diamond as a beacon proxy

DeltaPrime's `smartLoanDiamond()` returns the diamond address directly. DegenPrime fronts the diamond with `SmartLoanDiamondBeacon`; the active implementation is `SmartLoanDiamondBeacon.implementation()`. For routine calls from an account address this is transparent (the per-account proxy resolves the beacon and forwards), but anything that introspects the diamond directly (DiamondLoupe walks, selector probes) needs to read through the beacon.

### 6.6 RedStone feed coverage is partial

DeltaPrime's RedStone payload covers every supported pool asset on Avalanche. On Base, only 13 of the ~32 collateral symbols are in `redstone-primary-prod` (see §4). The SolvencyFacet sources the rest from a custom **BaseOracle TWAP** on-chain. The tool filters the payload to feed-available symbols — sending a payload that lists a symbol RedStone doesn't have crashes the gateway lookup; sending a payload that omits an asset the SolvencyFacet handles via BaseOracle is fine.

The practical effect: `summary`'s per-asset USD column only shows for the 13 feed-available symbols, but the total / debt / health figures cover every asset (BaseOracle prices flow through SolvencyFacet internally). `swap-debt` requires both legs to be feed-available (the facet's value-match step calls `getPrices`).

### 6.7 No POA middleware on Base

Avalanche needs the geth POA middleware injected into web3.py for block header parsing. Base is a standard EVM chain — injecting POA middleware errors on the block headers. `get_w3()` returns a vanilla provider.

### 6.8 RPC and explorer surface

| | DeltaPrime (Avalanche) | DegenPrime (Base) |
|--|--|--|
| Default RPC | `api.avax.network/ext/bc/C/rpc` | `base.publicnode.com` |
| RPC notes | Generous limits | `mainnet.base.org` rate-limits hard (429 within ~5 req/sec); publicnode is fronted by a load balancer with much higher anonymous limits |
| Explorer API | Snowtrace (works anonymously for verified ABIs) | Basescan v1 deprecated; v2 needs an API key (no anonymous reads) |
| ABI source | Snowtrace pull at runtime | Hand-curated (Pool, Factory, TokenManager) + EIP-1967 proxy slot reads |

The tool sidesteps Basescan entirely. Proxy implementations come from the EIP-1967 storage slot (`0x360894…2bbc`); the facet ABIs are hand-picked from probed selectors. This keeps the tool dependency-free of any keyed explorer API.

### 6.9 Gas-price floor

Avalanche's *transaction* gas-price floor in the tool is **25 gwei** (legacy gas pricing on the C-chain); its GMX exec-fee estimator floors at 1 gwei separately. Base has no GMX integration and no such requirement; the tool uses **`max(network_price * 2, 1 gwei)`** to keep txs from stranding when Base's ~0.001 gwei base fee ticks up after submission. Cost is negligible on Base.

### 6.10 No GMX, TraderJoe V2 LB, sJOE, Wombat, GLP, Pangolin

None of DeltaPrime's extended-DeFi facets are deployed on DegenPrime. The only DeFi integration beyond lending / swap is Aerodrome (§5), and only the read view ships in v1. If someone asks about LP, staking, or GM tokens on DegenPrime, the answer is "not on this protocol."

---

## 7. Pool contract functions (savings layer)

Same shape as DeltaPrime's Pool implementation. Selectors that matter:

`deposit(uint256)`, `depositNativeToken()` (payable, for the `weth` pool only), `withdraw(uint256,uint256[])` (the intent-gated step-2 executor, selector 0x5915d806), `createWithdrawalIntent(uint256)`, `cancelWithdrawalIntent(uint256)`, `totalSupply()`, `totalBorrowed()`, `balanceOf(address)`, `getBorrowed(address)`, `getDepositRate()`, `getBorrowingRate()`, `tokenAddress()`. (The single-arg `withdraw(uint256)` exists in bytecode but reverts bare `0x` — it does not resolve a named intent. `instantWithdraw` is absent.)

`getDepositRate()` and `getBorrowingRate()` return 1e18-scaled annualised rates. Multiply by 100 for the percentage display.

---

## 8. Critical gotchas

These are the non-obvious bits. They are the reason naïve approaches fail.

1. **Universal 24h time-lock on every withdrawal.** See §6.1. Both the Degen Account collateral path AND the lender-side savings pools are 24h-locked — nothing exits instantly. The old "stable assets withdraw instantly" mental model is wrong on both protocols now.

2. **bytes32 asset symbols.** Same scheme as DeltaPrime: right-pad the ASCII symbol with zero bytes to 32. Use the bytes32 symbol, not the wrapped-token name. Symbols of note: `ETH` (not `WETH`), `cbBTC` (case matters), `cbDOGE`, `cbXRP`.

3. **RedStone gating + partial feed coverage.** Functions that compute USD value or check solvency revert `0xe7764c9e` on a bare `eth_call`; the signed-price payload must be appended. The payload covers only the 13 feed-available symbols; the SolvencyFacet sources the rest from BaseOracle internally. The tool's `degen_account_price_feeds()` filters owned + debt assets to feed-available symbols.

4. **ParaSwap router method whitelist.** Only `swapExactAmountIn` (`0xe3ead59e`) and `swapExactAmountInOnUniswapV3` (`0x876a02f6`) decode. The tool passes `excludeContractMethods` to keep ParaSwap on a decodable route; if a different method comes back, the build refuses with a clear error rather than letting the on-chain call revert.

5. **ParaSwap executor whitelist.** The facet validates the executor address embedded in the calldata. The tool maintains a starting whitelist (lower-cased) and patches to a known-good fallback executor (`0x000010036C0190E009a000d0fc3541100A07380A`) if ParaSwap returns one that isn't on the list. Real reverts surface missing executors with `InvalidExecutor`; add them as they show up.

6. **Borrow needs setup.** `createLoan()` makes an EMPTY Degen Account. Fund it with collateral before `borrow()` will succeed. The EOA needs ETH for gas. `createAndFundLoan(bytes32, uint256)` does create + fund in one tx (ERC20 only — native ETH is blocked; use the two-step flow with `fund --pool weth`).

7. **Repay caps to in-account balance.** `repay()` reverts if `amount > debt` OR `amount > in-account balance`. The tool caps to `min(requested, debt, in-account)` and prints the cap reason. If in-account balance is zero, swap into the debt asset first (`swap --to <sym> --amount N --execute`).

8. **`getLoansForOwner` lag after createLoan.** The factory's owner→loans map can lag a beat behind the create-tx receipt. The tool polls every 2s for up to 12s after `--execute` to print the new account address; if it still hasn't propagated, it prints a "run `my-positions` shortly" hint rather than `None`.

9. **TVL is tiny.** ~$29k total across 8 pools (29-05-2026). Small borrows can skew utilization rates; thin pools like cbDOGE / cbXRP wouldn't absorb meaningful borrow size. Quoted swap routes for non-stable, non-blue-chip assets can be thin too — preview the quote before executing.

10. **Decimals matter:** USDC 6, USDT 6, cbXRP 6, cbBTC 8, cbDOGE 8, WETH 18, AERO 18, BRETT 18, KAITO 18. The tool handles scaling internally; this matters if you ever compute amounts by hand.

---

## 9. The tool: `degenprime`

- Installed by `pip install primecli`; entry point is the `degenprime` console script.
- Default RPC: `https://base.publicnode.com` (Base, chainId 8453). Override with `DEGENPRIME_RPC` (paid Alchemy/QuickNode/Infura recommended for heavy use). Public fallback: `mainnet.base.org`.
- Signing: only under `--execute`, with the key resolved per the precedence below. Real wallet, real funds.

### Signing key resolution

The Degen Account is derived on-chain from the wallet owner (`getLoansForOwner`), so each user automatically operates on their own Degen Account — no per-user addresses are hardcoded. The same EVM keypair works on both Avalanche and Base, so the resolution falls back to the DeltaPrime env vars if the DegenPrime ones are not set.

Key resolution order (first hit wins):

1. `--key <0xhex>` CLI flag → one-off raw key.
2. `DEGENPRIME_PRIVATE_KEY` env var → raw `0x…` key.
3. `DEGENPRIME_KEY_FILE` env var → path to a file containing the `0x…` key.
4. `DELTAPRIME_PRIVATE_KEY` / `DELTAPRIME_KEY_FILE` — fallback (same key, both chains).

### Commands

The tool ships **17 commands**. State-changing commands default to a PREVIEW; add `--execute` to broadcast. Solvency-gated writes append a RedStone signed-price payload on `--execute` (noted "gated" below).

| Command | Type | What it does |
|---------|------|--------------|
| `pool-info [usdc\|weth\|cbbtc\|aero\|brett\|kaito\|cbdoge\|cbxrp\|all] [--json]` | read-only | Pool supply / borrow / utilization / deposit APR / borrow APR / TVL. Defaults to `all`. With `--json`: emits a single JSON object for a named pool, or a `{name: {...}}` dict for `all` (same shape as `deltaprime pool-info --json`). |
| `my-positions` | read-only | Wallet ETH balance, per-pool wallet + deposit + borrow, Degen Account address. |
| `deposit --pool X --amount Y [--execute]` | state-changing | Deposit into a savings pool. ERC20 approve handled automatically (approves the **pool**); native ETH (`weth`) sends `value` and skips the approve. |
| `withdraw --pool X --amount Y [--execute]` | state-changing | **Step 1 of delayed lender withdraw (24h flow).** Registers a withdrawal intent on the pool via `createWithdrawalIntent(uint256)`. The single-arg `withdraw(uint256)` does not resolve a named intent (reverts bare `0x`) — the savings-pool side has the same time-locked intent flow as the Degen Account collateral side. 24h time-lock, then a **24h** execute window (48h total). Oracle-free; no RedStone payload. |
| `withdrawal-requests` | read-only | Lists pending **lender / pool-side** withdrawal intents (per pool, with ready/expired state) + current pool deposit. Oracle-free. Distinct from `withdrawal-intents` (Degen Account collateral side). |
| `execute-withdrawal-request --pool X [--index N] [--execute]` | state-changing | Step 2 of lender pool withdraw: consumes a matured intent via the two-arg `withdraw(uint256 amount, uint256[] intentIndices)` (selector 0x5915d806, same as the DegenPrime pool — not `instantWithdraw` or the single-arg form). An eth_call simulation runs first and refuses to broadcast on revert. Oracle-free. |
| `cancel-withdrawal-request --pool X --index N [--execute]` | state-changing | Cancel a pending lender withdrawal intent via `cancelWithdrawalIntent(uint256)`. |
| `create-account [--execute]` | state-changing | `factory.createLoan()` — empty Degen Account. |
| `create-account --fund-pool X --fund-amount Y [--execute]` | state-changing | `factory.createAndFundLoan()` — create + fund in one tx (ERC20 only; approves the **factory**). |
| `fund --pool X --amount Y [--execute]` | state-changing | Move collateral from the wallet into the Degen Account. ERC20: approves the **Degen Account** then calls `fund()`. Native ETH (`weth`): payable `depositNativeToken()` (wraps ETH→WETH inside the account, no approve). |
| `borrow --pool X --amount Y [--execute]` | state-changing (gated) | Calls `borrow()` on the Degen Account. |
| `repay --pool X --amount Y [--execute]` | state-changing | Calls `repay()` on the Degen Account. NOT solvency-gated; no payload. Auto-caps to `min(requested, debt, in-account balance)`. |
| `summary [--json]` | read-only | Degen Account assets / debts + **live solvency** (health ratio, total value, debt, solvent flag) via RedStone-gated SolvencyFacet reads (falls back to balances-only if the gateway is down). With `--json`: emits a single trimmed JSON object (drops null, empty list, empty dict; preserves 0 and false) for one-shot agent ingestion. |
| `swap --from S --to S --amount N [--slippage P] [--execute]` | state-changing (gated) | Swap one in-account asset for another via ParaSwap v6 / Velora. Hard 5% facet slippage cap on top of `--slippage`. RedStone-gated on execute. |
| `swap-debt --from S --to S --amount N [--slippage P] [--execute]` | state-changing (gated) | Refinance debt: borrow `--to`, ParaSwap into `--from`, repay the old `--from` debt. Both symbols must have RedStone feeds. 5% USD-diff cap. RedStone-gated on execute. |
| `withdraw-collateral --pool X --amount Y [--execute]` | state-changing (gated) | Step 1 of the Degen Account collateral withdrawal: registers a `WithdrawalIntent` — **RedStone-gated** on the Account. 24h time-lock, then a **48h** execute window (72h total). |
| `withdrawal-intents` | read-only | Lists pending intents per owned asset (READY / maturing / EXPIRED) + per-asset available balance. Oracle-free. |
| `execute-withdrawal --pool X [--index N] [--execute]` | state-changing (gated) | Step 2: pulls matured intent(s) to the wallet (`executeWithdrawalIntent`). Default executes all currently-actionable intents for the asset. |
| `cancel-withdrawal --pool X --index N [--execute]` | state-changing | Cancel a pending intent before maturity. Oracle-free, no payload. |
| `aerodrome-positions` | read-only | Lists the Aerodrome NFT tokenIds the Degen Account owns/has staked via `getOwnedStakedAerodromeTokenIds`. v1 lists IDs only — composition + write paths deferred to v2. |

**Approve targets differ by command** (easy to get wrong, all handled correctly by the tool): `deposit` approves the **pool**; `fund` approves the **Degen Account**; `create-account --fund-*` approves the **factory**; `swap` / `swap-debt` operate on balances already inside the Degen Account, so the EOA approves nothing for them (the facet itself approves the Augustus router mid-tx).

### Preview vs broadcast

Every state-changing command **defaults to a PREVIEW** that prints what it would do and does nothing on-chain. It only signs and broadcasts when you add `--execute`. On `--execute`, solvency-gated writes (`borrow`, `swap`, `swap-debt`, `withdraw-collateral`, `execute-withdrawal`) append a RedStone signed-price payload to the calldata; the rest (`deposit`, `withdraw`, `fund`, `repay`, `create-account`, `cancel-withdrawal`) need no payload. (The Degen Account `createWithdrawalIntent` is RedStone-gated on-chain; the exact create-time feed set is still being reconciled.) Read-only commands ignore `--execute`.

---

## 10. Typical flows

**Earn yield:**
```
deposit --pool usdc --amount 100 --execute
```
The ERC20 approve is sent automatically before the deposit. Native ETH (`weth`) deposits pass `value` instead of approving.

**Leverage:**
```
create-account --execute                       # creates an empty account
fund --pool X --amount Y --execute             # move collateral in
borrow --pool X --amount Y --execute           # borrow against collateral
# ... later ...
repay --pool X --amount Y --execute
withdraw-collateral --pool X --amount Z --execute   # step 1: register intent
# wait 24h ...
execute-withdrawal --pool X --execute               # step 2: pull funds out

# Or collapse the first two steps (ERC20 collateral only):
create-account --fund-pool usdc --fund-amount 100 --execute
```

**Refinance debt:**
```
swap-debt --from ETH --to USDC --amount 0.05 --execute   # repays 0.05 ETH of debt, borrows USDC equivalent
```
Both legs must have RedStone feeds. The tool value-matches the borrow to the repay against the facet's own 5% cap and refuses if it can't fit.

---

## 11. Safety

- State-changing commands default to preview; `--execute` is required to broadcast.
- **Never broadcast a real transaction (`--execute`) without understanding what the preview is about to do.** Real wallet, real funds.
- The private key (env var or file) is never written anywhere by the tool. Treat its storage as a hard secret — never echo, log, or commit it.
- Confirm the `Wallet:` line shown on write paths matches the wallet you intend before any `--execute`.
- TVL on long-tail pools is small; start small. A modest test deposit may be a non-trivial share of a long-tail pool's total TVL — size accordingly.
