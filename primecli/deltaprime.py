#!/usr/bin/env python3
"""DeltaPrime Protocol interaction module (Avalanche C-chain).

Lending pools take direct EOA deposits/withdrawals. Borrowing and leverage go
through a Prime Account: a per-user SmartLoan (EIP-2535 diamond) created via the
SmartLoansFactory. The EOA owns it; borrow/repay/fund run on the Prime Account,
which itself talks to the pools.

Usage:
  deltaprime pool-info [usdc|wavax|weth|btc|usdt|all]
  deltaprime my-positions
  deltaprime deposit --pool usdc --amount 100 [--execute]
  deltaprime withdraw --pool usdc --amount 100 [--execute]
  deltaprime create-prime-account [--execute]   (alias: create-account)
  deltaprime create-prime-account --fund-pool usdc --fund-amount 100 [--execute]
  deltaprime prime-summary
  deltaprime defi --json          (aggregate ALL positions as DeBank-style JSON; read-only)
  deltaprime fund --pool usdc --amount 100 [--execute]
  deltaprime borrow --pool usdc --amount 100 [--execute]
  deltaprime repay --pool usdc --amount 100 [--execute]
  deltaprime swap --from USDC --to AVAX --amount 10 [--via yak|paraswap] [--slippage 0.5] [--execute]
  deltaprime swap-debt --from AVAX --to USDC --amount 100 [--slippage 0.5] [--execute]
  deltaprime withdraw-collateral --pool usdc --amount 100 [--execute]
  deltaprime withdrawal-intents
  deltaprime execute-withdrawal --pool usdc [--index N] [--execute]
  deltaprime gmx-positions
  deltaprime gmx-deposit --market avax-usdc --amount 10 [--side long|short] [--slippage 1] [--fee-buffer 2] [--execute]
  deltaprime gmx-withdraw --market avax+ --amount 5 [--slippage 1] [--fee-buffer 2] [--execute]
  deltaprime lb-positions
  deltaprime lb-add --pair avax-usdc --amount-x 1 --amount-y 30 [--shape spot|curve|bidask] [--range 5] [--slippage 1] [--id-slippage 5] [--execute]
  deltaprime lb-remove --pair avax-usdc [--slippage 1] [--execute]
  deltaprime sjoe-position
  deltaprime sjoe-stake --amount 100 [--execute]
  deltaprime sjoe-unstake --amount 100 [--execute]
  deltaprime sjoe-claim [--execute]
  deltaprime prime-tier
  deltaprime prime-needed --borrow 1000 [--tier premium|basic]
  deltaprime prime-deposit --amount 200 [--execute]
  deltaprime prime-activate [--amount N] [--execute]
  deltaprime prime-deactivate [--withdraw] [--execute]
  deltaprime prime-unstake --amount N [--execute]
  deltaprime prime-repay --amount N [--execute]
  deltaprime zap --market avax-usdc --collateral wavax --collateral-amount 1 --borrow-amount 30 --deposit-amount 30 [--side long|short] [--swap] [--slippage 1] [--fee-buffer 2] [--execute]

Configuration (env vars):
  DELTAPRIME_PRIVATE_KEY  Raw 0x... private key for the signer.
  DELTAPRIME_KEY_FILE     Path to a file containing the 0x key (alternative to the env var).
  DELTAPRIME_RPC          Avalanche C-chain RPC URL (defaults to api.avax.network).
  --key <0xhex>           One-off CLI override (takes precedence over both env vars).

prime-summary reports live solvency (health ratio, total value, debt, solvent flag) from
SolvencyFacetProdAvalanche, read via eth_call with a RedStone price payload appended (falls
back to balances-only if the gateway is unreachable).

Collateral withdrawal is a two-step, time-delayed flow on the Prime Account (there is NO
instant withdraw of in-account collateral; the savings-pool `withdraw` above is separate).
withdraw-collateral registers a WithdrawalIntent (createWithdrawalIntent, no RedStone). The
intent becomes executable ~24h later for a 48h window (24h-72h total); execute-withdrawal
then pulls it to the wallet (executeWithdrawalIntent, RedStone-gated). withdrawal-intents
lists pending intents + per-asset available balance (oracle-free reads). The maturity window
and ready/expired state come straight off-chain from the IntentInfo struct.

Leverage flow: create-prime-account -> fund (collateral) -> borrow -> repay -> withdraw.
fund moves collateral from the wallet into the Prime Account; borrow needs a funded
account. ERC20 assets approve the account then call fund(); native AVAX (wavax pool)
uses the payable depositNativeToken(). create-prime-account --fund-* does both in one
tx via createAndFundLoan() (ERC20 only).

swap trades one in-account asset for another on the Prime Account, via either aggregator
route (--via, default yak):
  - yak (YieldYakSwapFacet.yakSwap): the YieldYak router's findBestPath derives the
    path+adapters off-chain; the swap runs against the account's in-account balance of
    the --from asset. Every adapter must be whitelisted on the facet.
  - paraswap (ParaSwapFacet.paraSwapV6): the ParaSwap/Velora v6.2 API for Avalanche
    builds the swap calldata (/prices price route -> /transactions tx data). The facet
    takes paraSwapV6(bytes4 selector, bytes data) — we split the API calldata into its
    4-byte selector + remaining bytes and pass them through. Only the two router methods
    the facet decodes are accepted: swapExactAmountIn (0xe3ead59e) and
    swapExactAmountInOnUniswapV3 (0x876a02f6). The facet enforces a hard 5% slippage cap
    (RedStone-priced) regardless of --slippage.
Both routes carry remainsSolvent, so --execute appends a RedStone signed-price payload to
the calldata (see the RedStone wrapping helpers below). Asset names are the bytes32
symbols (AVAX/ETH/BTC/USDC/USDT), not the wrapped-token names.

swap-debt refinances debt from one asset into another in a single tx via
SwapDebtFacet.swapDebtParaSwap(_fromAsset, _toAsset, _repayAmount, _borrowAmount, selector,
data): it borrows --amount of the NEW debt asset (--to), ParaSwaps it into the OLD debt
asset (--from), and repays the old debt. --from is the existing debt being refinanced;
--to is the new debt taken on. The facet enforces a hard 5% cap on the USD-value
difference between the repaid and borrowed amounts (RedStone-priced), and requires the
ParaSwap quote's fromAmount to equal the borrow amount exactly. RedStone-gated on execute.

gmx-deposit / gmx-withdraw open/close GMX V2 GM (two-sided) and GM+ (single-sided) LP
positions on the Prime Account, via GmxV2FacetAvalanche (deposit{Avax,Btc,Eth}UsdcGmxV2 /
withdraw{...}UsdcGmxV2) and GmxV2PlusFacetAvalanche (deposit{Avax,Btc,Eth}GmxV2Plus /
withdraw{...}GmxV2Plus). Markets (--market): avax-usdc, btc-usdc, eth-usdc (GM); avax+,
btc+, eth+ (GM+). gmx-deposit takes an in-account underlying (two-sided: --side long|short,
long = volatile leg, short = USDC; GM+ ignores --side); gmx-withdraw burns GM tokens.
  - These functions are PAYABLE + ASYNC. They pay a GMX execution fee as msg.value (== the
    executionFee arg; the facet reverts InvalidExecutionFee if they differ), queue the
    request on the GMX ExchangeRouter, and a GMX KEEPER executes it some blocks later via a
    callback. The position does NOT appear/disappear instantly, and the Prime Account is
    FROZEN for that market until the keeper callback fires. The fee is estimated from the GMX
    DataStore gas params (callbackGasLimit 600000) times the gas price, padded by --fee-buffer
    (default 2x) to survive a gas-price rise before keeper execution; GMX refunds any excess
    to the account. The EOA also needs AVAX for its own tx gas on top of the execution fee.
  - minGmAmount (deposit) / min long+short token amounts (withdraw) are slippage floors set
    from the RedStone oracle prices minus --slippage. The facet's isWithinBounds check
    HARD-CAPS slippage at 5% (±5% of the oracle estimate) — looser reverts InvalidMinOutput.
  - RedStone-gated: --execute appends a signed price payload (GM feed + underlyings). The GM
    token price has no SolvencyFacet feed, so it is read from the RedStone gateway median (the
    same on-demand value the facet aggregates from calldata).
gmx-positions is read-only: per owned market it shows the GM balance after the accrued
performance fee (SmartLoanViewFacet.getGmTokenBalanceAfterFees) and the annualised
performance (getGm[Plus]Performance) — both RedStone-gated views, eth_call'd with a payload.

lb-add / lb-remove open/close TraderJoe V2 Liquidity Book (concentrated liquidity) positions
on the Prime Account via TraderJoeV2AvalancheFacet (addLiquidityTraderJoeV2 /
removeLiquidityTraderJoeV2). --pair is a whitelisted LB pair key (avax-usdc, avax-usdc-20,
btc-usdc, eth-avax, btc-avax, avax-btc, eurc-usdc, usdt-usdc, joe-avax). LB liquidity sits in
discrete price BINS; a position is encoded as deltaIds[] (bin offsets from the active bin) +
distributionX[]/distributionY[] (per-bin token weightings, each populated side summing to
1e18). --shape sets that weighting: spot = uniform across the range (default, the common
case), curve = concentrated near the active price, bidask = concentrated at the range edges.
--range R spreads liquidity over R bins each side of the active bin (2R+1 total). Token X (the
pair's base) fills bins at/above the active bin; token Y (the quote) fills bins at/below it.
  - lb-add takes per-token amounts (--amount-x / --amount-y, in token units; one-sided is fine)
    from in-account balances. amountXMin/amountYMin are slippage floors; --id-slippage guards
    the active-bin id shifting before inclusion. The facet overrides to/refundTo to the account.
  - Max 80 bins per Prime Account (cumulative across pairs); both the preview and the on-chain
    facet enforce it (TooManyBins). The preview projects the post-add bin count and refuses if
    it would exceed 80.
  - addLiquidity carries remainsSolvent, so --execute appends a RedStone signed-price payload.
    removeLiquidity is NOT solvency-gated, so lb-remove needs no payload. lb-remove closes the
    account's ENTIRE position for the pair (all owned bins).
lb-positions is read-only: getOwnedTraderJoeV2Bins (oracle-free) lists owned (pair, bin) pairs;
per pair it shows the active bin and the account's share of each bin's reserves (balanceOf /
totalSupply * getBin) as per-token totals. No RedStone, no tx.

sjoe-stake / sjoe-unstake / sjoe-claim drive TraderJoe's sJOE staking on the Prime Account via
SJoeFacet (0x8aD9028f60Cf0F823271FE689EbDD0A58492cC75): stake in-account JOE to earn USDC fee
rewards, unstake JOE back into the account, claim accrued USDC. Verified on Snowtrace 23-05-2026
against the verified SJoeFacet source:
  - stakeJoe(uint256): onlyOwner + remainsSolvent + noBorrowInTheSameBlock + notInLiquidation, so
    --execute appends a RedStone signed-price payload. Caps to the account's in-account JOE.
  - unstakeJoe(uint256): onlyOwnerOrInsolvent + noBorrowInTheSameBlock — NOT remainsSolvent, so it
    needs no payload (same as lb-remove). Caps to the staked JOE.
  - claimSJoeRewards(): onlyOwner + remainsSolvent + noBorrowInTheSameBlock, so --execute appends a
    payload. Drives the sJOE withdraw(0) reward-claim path.
Every reward-bearing call (stake/unstake/claim) skims a 10% protocol fee off the USDC claimed in that
tx (CLAIMING_FEE = 0.1e18, split stability-pool/treasury), so the account nets ~90% of the rewards
realised. sjoe-position is read-only: joeBalanceInSJoe (staked JOE, 18-dec) + rewardsInSJoe (pending
USDC, 6-dec), both oracle-free views — no RedStone, no tx.

zap is a tool-level MACRO (zaps are NOT a separate on-chain facet — they are front-end orchestration
that chains the existing primitives, capabilities §7). One bounded "leveraged long" flow, composing
the existing leg commands (no new ABI), terminating in a GMX V2 GM market deposit:
  1. fund --collateral collateral into the Prime Account,
  2. borrow --borrow-amount USDC against it (the leverage),
  3. OPTIONAL (--swap): YieldYak-swap the borrowed USDC into the market's long token,
  4. gmx-deposit --deposit-amount of the chosen leg (--side long|short) into --market.
Each leg is its OWN transaction with an EXPLICIT amount (no fragile auto-sizing across the oracle/async
boundary). PREVIEW prints the full ordered plan and runs each leg in preview (nothing broadcast),
flagging which legs are RedStone-gated. --EXECUTE runs the legs sequentially and STOPS immediately on
the first failure, reporting exactly which legs completed and which failed (partial-state safety — a
halted zap leaves the completed legs live on-chain, so it warns to review with prime-summary before
retrying rather than blindly re-running). The terminal GMX leg is ASYNC: --execute only FIRES the
deposit request — a GMX keeper mints the GM tokens later and the account is FROZEN until then
(re-check gmx-positions once the keeper settles). Only the GM-terminal leveraged long is built; an LB-terminal
long is reachable by running fund -> borrow -> [swap] then lb-add manually.

prime-* drive DeltaPrime's PRIME-token leverage tiers (PrimeLeverageFacet on the Prime Account). Two tiers:
BASIC (~5x, the default) and PREMIUM (10x). PREMIUM is gated by STAKING the protocol's PRIME token in an
amount PROPORTIONAL to USD borrow (tieredPrimeStakingRatio, ~1.2 PRIME/$100), and it accrues a PRIME-
denominated rent-debt over time (tieredPrimeDebtRatio, ~0.5 PRIME/$100/yr). Both ratios live in the
TokenManager and are governance-mutable, so the tool reads them on-chain (getRequiredPrimeStake), never
hard-codes them. PRIME (18-dec) is a separate token from sPRIME and must be acquired on a DEX (LFJ/TraderJoe
PRIME-WAVAX). Verified 24-05-2026 against the verified PrimeLeverageFacet source.
  - prime-tier: read-only status — current tier, staked PRIME, recorded PRIME debt (last snapshot), the EOA
    + in-account PRIME balances, and shouldLiquidatePrimeDebt() (state-mutating, so eth_call'd as a read-only sim).
  - prime-needed --borrow X [--tier premium|basic]: read-only quote of PRIME needed to back $X of borrow, via
    getRequiredPrimeStake (live ratio). Default tier premium.
  - prime-activate [--amount N]: --amount first depositPrime(N)s PRIME from the EOA into the account (ERC20
    approve -> depositPrime, RedStone-gated), then stakePrimeAndActivatePremium() stakes the required amount
    (against 10x your free collateral) and flips to PREMIUM. Omit --amount to stake from PRIME already in the
    account. Preview shows the plan + projected required stake and fails closed if the in-account PRIME is short.
  - prime-deactivate [--withdraw]: deactivatePremiumTier — repays ALL PRIME debt first (reverts if PRIME can't
    cover it; 50% burn / 50% treasury), drops to BASIC. --withdraw also releases the freed stake into the account.
  - prime-unstake --amount N: unstakePrime — release staked PRIME; in PREMIUM the remaining stake must still cover
    the USD ratio + accrued PRIME debt or the facet reverts.
  - prime-repay --amount N: repayPrimeDebt — repay accrued PRIME rent-debt from in-account PRIME (capped to the
    current debt, 50% burn / 50% treasury).
Only depositPrime (inside prime-activate --amount) is solvency-gated -> RedStone payload on --execute; every other
prime-* write is onlyOwner and needs no payload. All prime-* views are oracle-free. Preview by default; --execute broadcasts.
"""

import json, os, sys, time, re, base64
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import requests
from eth_account import Account
from eth_keys import keys as eth_keys
from eth_abi import encode as abi_encode, decode as abi_decode
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# Default Avalanche C-chain RPC. Override with the DELTAPRIME_RPC env var (paid
# Alchemy/QuickNode/Infura endpoints are recommended for higher throughput; the
# public endpoint rate-limits hard on busy `defi --json` reads).
AVALANCHE_RPC = os.environ.get("DELTAPRIME_RPC", "https://api.avax.network/ext/bc/C/rpc")
EXPLORER = "https://snowtrace.io"
CHAIN_ID = 43114
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
# ── Signing key resolution ──────────────────────────────────────────────────
# The Prime Account is derived on-chain from the wallet owner (getLoanForOwner),
# so each user automatically operates on their own Prime Account — no per-user
# addresses are hardcoded.
#
# Key resolution order (first hit wins; see resolve_private_key):
#   1. --key <0xhex> CLI flag         -> raw 0x... key (one-off escape hatch)
#   2. DELTAPRIME_PRIVATE_KEY env var -> raw 0x... key (primary path)
#   3. DELTAPRIME_KEY_FILE env var    -> path to a file containing the 0x key
#
# The CLI key (#1) is set by main() before the command runs; the env vars (#2/#3)
# are read lazily so read-only commands that don't sign never need a key at all.
_CLI_KEY = None  # set by the --key CLI flag in main()
FACTORY_PROXY = "0x3Ea9D480295A73fd2aF95b4D96c2afF88b21B03D"
# On-chain registry of active pools. getPoolAddress(bytes32 asset) is the source
# of truth — the docs/repo per-pool artifacts are stale and point at frozen pools.
TOKEN_MANAGER = "0xF3978209B7cfF2b90100C6F87CEC77dE928Ed58e"
# SmartLoan diamond beacon. Every Prime Account is a per-user proxy that delegates
# here, so the facet ABIs (borrow/repay/fund + view fns) are reachable at any
# deployed account address. Sourced from SmartLoansFactory.smartLoanDiamond().
SMART_LOAN_DIAMOND = "0x2916B3bf7C35bd21e63D01C93C62FB0d4994e56D"

# YieldYak aggregator router. findBestPath() is read-only and returns the optimal
# multi-hop route (path + per-hop adapter addresses). The Prime Account's
# YieldYakSwapFacet executes yakSwap() over those, requiring every adapter to be
# whitelisted (isWhitelistedAdapterOptimized).
YAK_ROUTER = "0xC4729E56b831d74bBc18797e0e17A295fA77488c"

# ParaSwap / Velora v6.2 aggregator. The Prime Account's ParaSwapFacet.paraSwapV6 and
# SwapDebtFacet.swapDebtParaSwap both call this Augustus router with API-built calldata
# (verified hard-coded as PARA_ROUTER in the deployed ParaSwapHelper). The facet only
# decodes two router methods, so the API route must resolve to one of these selectors:
#   swapExactAmountIn          0xe3ead59e  (generic executor route)
#   swapExactAmountInOnUniV3   0x876a02f6  (Uniswap-V3 direct route)
# It validates the decoded executor against a fixed allowlist, the partner against the
# treasury (we force partner=0), the beneficiary against the account (0 is allowed), and
# applies a 5% hard slippage cap priced via RedStone.
PARASWAP_API = "https://apiv5.paraswap.io"
PARASWAP_AUGUSTUS = "0x6A000F20005980200259B80c5102003040001068"
PARASWAP_SUPPORTED_SELECTORS = {"0xe3ead59e", "0x876a02f6"}
# Executors the facet whitelists (ParaSwapHelper._checkExecutorAddress). Lowercased.
PARASWAP_EXECUTORS = {
    # Must match the ParaSwap executor whitelist on DeltaPrime's ParaSwapFacet and
    # SwapDebtFacet. The ParaSwap API can return new executors that aren't whitelisted
    # yet — those cause on-chain InvalidExecutor() reverts. Only add executors verified
    # to be whitelisted on-chain.
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57",
    "0x6a000f20005980200259b80c5102003040001068",
    "0x000010036c0190e009a000d0fc3541100a07380a",
    "0x00c600b30fb0400701010f4b080409018b9006e0",
    "0xa0f408a000017007015e0f00320e470d00090a5b",
}

# RedStone on-demand oracle config for DeltaPrime on Avalanche. The Prime Account's
# solvency math (every remainsSolvent-gated facet call, plus oracle views like
# getHealthRatio/isSolvent/getTotalValue) reads signed prices appended to the tx
# calldata. Values are from AvalancheDataServiceConsumerBase in the deployed source:
# data service "redstone-avalanche-prod", 3-of-5 unique authorised signers, default
# 3-minute staleness window. The 9-byte marker terminates a RedStone payload.
# DeltaPrime uses RedStone PRIMARY production (PrimaryProdDataServiceConsumerBase), not Classic.
# The authorised signer set and gateway endpoint MUST match.
REDSTONE_DATA_SERVICE = "redstone-primary-prod"
REDSTONE_SIGNERS_THRESHOLD = 3
REDSTONE_MARKER = bytes.fromhex("000002ed57011e0000")
REDSTONE_GATEWAYS = [
    "https://oracle-gateway-1.a.redstone.finance",
    "https://oracle-gateway-2.a.redstone.finance",
]

# Active pool proxies resolved from TokenManager.getPoolAddress() and verified
# on-chain (2026-05-23) by matching totalSupply() to the live app sizes. The old
# docs addresses are frozen: totalSupply() still returns stale values, but the
# deposit/withdraw write paths revert with PoolFrozen() (0xfd4851e9).
POOLS = {
    "usdc": {
        "proxy": "0x8027e004d80274FB320e9b8f882C92196d779CE8",
        "token": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        "symbol": "USDC", "decimals": 6, "native": False,
    },
    "wavax": {
        "proxy": "0xaa39f39802F8C44e48d4cc42E088C09EDF4daad4",
        "token": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
        "symbol": "AVAX", "decimals": 18, "native": True,
    },
    "weth": {
        "proxy": "0x2A84c101F3d45610595050a622684d5412bdf510",
        "token": "0x49D5c2BdFfac6CE2BFdB6640F4F80f226bc10bAB",
        "symbol": "ETH", "decimals": 18, "native": False,
    },
    "btc": {
        "proxy": "0x70e80001bDbeC5b9e932cEe2FEcC8F123c98F738",
        "token": "0x152b9d0FdC40C096757F570A51E494bd4b943E50",
        "symbol": "BTC", "decimals": 8, "native": False,
    },
    "usdt": {
        "proxy": "0x1b6D7A6044fB68163D8E249Bce86F3eFbb12368e",
        "token": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
        "symbol": "USDT", "decimals": 6, "native": False,
    },
}

# ─── GMX V2 GM / GM+ markets (LP) ────────────────────────────────────────────
# DeltaPrime mints/redeems GMX V2 market LP tokens (GM = two-sided long+short, GM+ =
# single-sided) through two diamond facets, reachable at any Prime Account address.
# Deposit/withdraw are PAYABLE + ASYNC: a GMX execution fee is paid as msg.value, the
# request is queued on the GMX ExchangeRouter, and a GMX keeper executes it some blocks
# later via the callbacks facet. The Prime Account is FROZEN for that market until the
# keeper callback fires. Facet/function signatures + the executionFee==msg.value rule are
# verified against the deployed verified source on Snowtrace (23-05-2026).
#   GmxV2FacetAvalanche      0x759902b8D105cBB20D1b2C7b76b355a175E32286 (two-sided)
#   GmxV2PlusFacetAvalanche  0xe9C87e730f3a5972C9EA78995d32eb2Fd936D7Bf (single-sided)
# Each market: the GM token, its long/short underlying lending symbols, the RedStone GM
# feed id (priced off the gateway median — SolvencyFacet.getPrices has no feed for the GM
# symbol and reverts 0xec459bc0, so GM prices come straight from the gateway, the same
# source the contract reads from calldata), and the facet deposit/withdraw fn stems.
GMX_MARKETS = {
    # Two-sided GM markets (volatile leg + USDC). depositXUsdcGmxV2(bool isLongToken, ...).
    "avax-usdc": {
        "plus": False, "gm_token": "0x913C1F46b48b3eD35E7dc3Cf754d4ae8499F31CF",
        "long": "AVAX", "short": "USDC", "gm_feed": "GM_AVAX_WAVAX_USDC",
        "deposit_fn": "depositAvaxUsdcGmxV2", "withdraw_fn": "withdrawAvaxUsdcGmxV2",
    },
    "btc-usdc": {
        "plus": False, "gm_token": "0xFb02132333A79C8B5Bd0b64E3AbccA5f7fAf2937",
        "long": "BTC", "short": "USDC", "gm_feed": "GM_BTC_BTCb_USDC",
        "deposit_fn": "depositBtcUsdcGmxV2", "withdraw_fn": "withdrawBtcUsdcGmxV2",
    },
    "eth-usdc": {
        "plus": False, "gm_token": "0xB7e69749E3d2EDd90ea59A4932EFEa2D41E245d7",
        "long": "ETH", "short": "USDC", "gm_feed": "GM_ETH_WETHe_USDC",
        "deposit_fn": "depositEthUsdcGmxV2", "withdraw_fn": "withdrawEthUsdcGmxV2",
    },
    # Single-sided GM+ markets (one asset, no USDC short leg). depositXGmxV2Plus(...).
    # long == short underlying; the facet splits a deposit 50/50 across both legs.
    "avax+": {
        "plus": True, "gm_token": "0x08b25A2a89036d298D6dB8A74ace9d1ce6Db15E5",
        "long": "AVAX", "short": "AVAX", "gm_feed": "GM_AVAX_WAVAX",
        "deposit_fn": "depositAvaxGmxV2Plus", "withdraw_fn": "withdrawAvaxGmxV2Plus",
    },
    "btc+": {
        "plus": True, "gm_token": "0x3ce7BCDB37Bf587d1C17B930Fa0A7000A0648D12",
        "long": "BTC", "short": "BTC", "gm_feed": "GM_BTC_BTCb",
        "deposit_fn": "depositBtcGmxV2Plus", "withdraw_fn": "withdrawBtcGmxV2Plus",
    },
    "eth+": {
        "plus": True, "gm_token": "0x2A3Cf4ad7db715DF994393e4482D6f1e58a1b533",
        "long": "ETH", "short": "ETH", "gm_feed": "GM_ETH_WETHe",
        "deposit_fn": "depositEthGmxV2Plus", "withdraw_fn": "withdrawEthGmxV2Plus",
    },
}

# GMX V2 infra used for execution-fee estimation. The DataStore holds the gas-limit
# params; the keeper requires executionFee >= adjustedGasLimit * tx.gasprice at execution
# time, so the fee is estimated as (base + perOracle*count + estimate*multiplier/1e30) *
# gasPrice, then padded (see _estimate_gmx_execution_fee). callbackGasLimit is hard-coded
# to 600000 in both facets. Addresses are the verified facet constants (Snowtrace).
GMX_DATASTORE = "0x2F0b22339414ADeD7D5F06f9D604c7fF5b2fe3f6"
GMX_READER = "0x62Cb8740E6986B29dC671B2EB596676f60590A5B"
GMX_CALLBACK_GAS_LIMIT = 600000
# GMX market token decimals are 18; the underlyings reuse the lending-pool decimals.
GM_TOKEN_DECIMALS = 18
# isWithinBounds (DiamondMethodsAccess) requires the USD value of the user's min-output to
# be within ±5% of the contract's own oracle estimate. So slippage on minGmAmount /
# min-token-outs is hard-capped at 5% — anything looser reverts InvalidMinOutputValue.
GMX_MAX_SLIPPAGE_PCT = 5.0

# ─── TraderJoe V2 Liquidity Book (concentrated liquidity) ────────────────────
# DeltaPrime LPs into TraderJoe V2 LB pairs through TraderJoeV2AvalancheFacet, reachable
# at any Prime Account. Liquidity is spread across discrete price BINS; each bin is a
# fixed price and binStep sets the spacing (in basis points). A position is encoded as
# deltaIds[] (bin offsets from the active bin), distributionX[]/distributionY[] (per-bin
# weightings of each token, each side summing to 1e18), an activeIdDesired+idSlippage
# guard, and amountX/Y + mins. "Shape" (Spot/Curve/Bid-Ask) is just the distribution
# arrays over the chosen range. Facet/struct/router details verified on Snowtrace
# 23-05-2026 against the verified TraderJoeV2AvalancheFacet + ILBRouter/ILBPair source:
#   - addLiquidityTraderJoeV2(router, LiquidityParameters): remainsSolvent (RedStone-gated),
#     validates router + pair whitelist, overrides to/refundTo to the account, enforces
#     maxBinsPerPrimeAccount()==80 AFTER the add (cumulative across the account's bins).
#   - removeLiquidityTraderJoeV2(router, RemoveLiquidityParameters): NOT remainsSolvent
#     (onlyOwnerOrLiquidation only) so it needs no RedStone payload; binStep is uint16.
#   - The router resolves the pair via getFactory().getLBPairInformation(tokenX,tokenY,binStep);
#     tokenX/tokenY MUST match the pair's canonical getTokenX()/getTokenY() order.
#   - The facet checks _getAvailableBalance(symbol) for each token, where symbol is the
#     TokenManager.tokenAddressToSymbol() value (NB: EURC's account symbol is "EUROC").
TJ_LB_FACET = "0x1899F6D524637808f2d53125b6CCFe6D2dF1Fa91"
TJ_ROUTER_V21 = "0xb4315e873dBcf96Ffd0acd8EA43f689D8c20fB30"
TJ_ROUTER_V22 = "0x18556DA13313f3532c54711497A8FedAC273220E"
TJ_MAX_BINS = 80

# Whitelisted LB pairs exposed as tool keys, matching the DeltaPrime frontend (bin step in
# the key suffix where a pair exists at two steps). For each: the LBPair address, the
# router version the pair belongs to, the canonical (tokenX, tokenY) order read on-chain,
# and the binStep. tokenX/tokenY carry the ERC20 address, the account bytes32 symbol (for
# the in-account balance read + RedStone feed), and decimals. The 13 source-whitelisted
# pairs include 4 aUSD pairs not on the frontend; those are omitted (no clean
# symbol/decimals + out of scope).
_WAVAX = {"addr": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7", "symbol": "AVAX", "decimals": 18}
_USDC = {"addr": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", "symbol": "USDC", "decimals": 6}
_USDT = {"addr": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7", "symbol": "USDT", "decimals": 6}
_WETH = {"addr": "0x49D5c2BdFfac6CE2BFdB6640F4F80f226bc10bAB", "symbol": "ETH", "decimals": 18}
_BTCB = {"addr": "0x152b9d0FdC40C096757F570A51E494bd4b943E50", "symbol": "BTC", "decimals": 8}
_EURC = {"addr": "0xC891EB4cbdEFf6e073e859e987815Ed1505c2ACD", "symbol": "EUROC", "decimals": 6}
_JOE = {"addr": "0x6e84a6216eA6dACC71eE8E6b0a5B7322EEbC0fDd", "symbol": "JOE", "decimals": 18}
TJ_LB_PAIRS = {
    "avax-usdc":    {"pair": "0x864d4e5Ee7318e97483DB7EB0912E09F161516EA", "router": TJ_ROUTER_V22, "binStep": 10, "tokenX": _WAVAX, "tokenY": _USDC},
    "avax-usdc-20": {"pair": "0xD446eb1660F766d533BeCeEf890Df7A69d26f7d1", "router": TJ_ROUTER_V21, "binStep": 20, "tokenX": _WAVAX, "tokenY": _USDC},
    "btc-usdc":     {"pair": "0x4224f6F4C9280509724Db2DbAc314621e4465C29", "router": TJ_ROUTER_V22, "binStep": 10, "tokenX": _BTCB, "tokenY": _USDC},
    "eth-avax":     {"pair": "0x1901011a39B11271578a1283D620373aBeD66faA", "router": TJ_ROUTER_V21, "binStep": 10, "tokenX": _WETH, "tokenY": _WAVAX},
    "btc-avax":     {"pair": "0xD9fa522F5BC6cfa40211944F2C8DA785773Ad99D", "router": TJ_ROUTER_V21, "binStep": 10, "tokenX": _BTCB, "tokenY": _WAVAX},
    "avax-btc":     {"pair": "0x856b38Bf1e2E367F747DD4d3951DDA8a35F1bF60", "router": TJ_ROUTER_V22, "binStep": 5,  "tokenX": _WAVAX, "tokenY": _BTCB},
    "eurc-usdc":    {"pair": "0xcD4f57d6B160B4ef2DFb78Ad1c76Cc4242EDB4CE", "router": TJ_ROUTER_V22, "binStep": 2,  "tokenX": _EURC, "tokenY": _USDC},
    "usdt-usdc":    {"pair": "0x2823299af89285fF1a1abF58DB37cE57006FEf5D", "router": TJ_ROUTER_V21, "binStep": 1,  "tokenX": _USDT, "tokenY": _USDC},
    "joe-avax":     {"pair": "0xEA7309636E7025Fda0Ee2282733Ea248c3898495", "router": TJ_ROUTER_V21, "binStep": 25, "tokenX": _JOE,  "tokenY": _WAVAX},
}

# LB pair (ILBPair) reads used for previews + position views. getActiveId is the current
# price bin; getTokenX/Y the canonical order; getBin(id) the bin's reserves; balanceOf /
# totalSupply give the account's share of a bin (LB liquidity is an ERC1155-style token).
LB_PAIR_ABI = [
    {"inputs": [], "name": "getActiveId", "outputs": [{"type": "uint24"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getTokenX", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getTokenY", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getBinStep", "outputs": [{"type": "uint16"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "id", "type": "uint24"}], "name": "getBin",
     "outputs": [{"name": "binReserveX", "type": "uint128"}, {"name": "binReserveY", "type": "uint128"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "id", "type": "uint256"}], "name": "totalSupply",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# ─── sJOE staking (SJoeFacet) ────────────────────────────────────────────────
# DeltaPrime stakes in-account JOE into TraderJoe's StableJoeStaking (sJOE) to earn USDC fee
# rewards, via SJoeFacet reachable at any Prime Account. The account holds JOE under bytes32
# symbol "JOE" (18-dec); rewards accrue in USDC (6-dec). Verified on Snowtrace 23-05-2026 against
# the verified SJoeFacet source — function names, the SJOE/reward-token constants, the 10%
# claiming fee, and the per-function modifiers:
#   stakeJoe(uint256)       onlyOwner + remainsSolvent + noBorrowInTheSameBlock + notInLiquidation
#   unstakeJoe(uint256)     onlyOwnerOrInsolvent + noBorrowInTheSameBlock      (NOT remainsSolvent)
#   claimSJoeRewards()      onlyOwner + remainsSolvent + noBorrowInTheSameBlock
#   joeBalanceInSJoe()/rewardsInSJoe()  oracle-free views (getUserInfo / pendingReward on sJOE)
# So stake + claim are RedStone-gated (payload appended on --execute); unstake is not. Each
# reward-bearing call skims CLAIMING_FEE (10%) off the USDC claimed in that tx, split between the
# stability pool and treasury, so the account nets ~90% of realised rewards.
SJOE_STAKING = "0x1a731B2299E22FbAC282E7094EdA41046343Cb51"
SJOE_JOE = {"addr": "0x6e84a6216eA6dACC71eE8E6b0a5B7322EEbC0fDd", "symbol": "JOE", "decimals": 18}
SJOE_REWARD = {"addr": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", "symbol": "USDC", "decimals": 6}
SJOE_CLAIMING_FEE_PCT = 10.0

# ─── PRIME-token leverage tiers (PrimeLeverageFacet) ─────────────────────────
# DeltaPrime gates higher max-leverage behind staking the protocol's own PRIME token,
# via PrimeLeverageFacet reachable at any Prime Account. Two tiers (LeverageTierLib.
# LeverageTier enum, uint8 on the wire): BASIC=0 (~5x default) and PREMIUM=1 (10x).
# PREMIUM requires PRIME staked PROPORTIONAL to USD borrow (tieredPrimeStakingRatio,
# 1.2 PRIME / $100 as of 24-05-2026) and accrues a PRIME-denominated rent-debt over time
# (tieredPrimeDebtRatio, 0.5 PRIME / $100 / yr). BOTH ratios live in the TokenManager and
# are governance-mutable, so the tool NEVER hard-codes them — it calls getRequiredPrimeStake
# on-chain. Facet/signatures/flow verified on Snowtrace 24-05-2026 against the verified
# PrimeLeverageFacet source (0.8.17, BUSL-1.1):
#   depositPrime(uint256)             onlyOwner + noBorrowInTheSameBlock + nonReentrant + remainsSolvent
#                                     -> RedStone-gated; pulls PRIME from the EOA (ERC20 approve first),
#                                        adds it as an in-account balance (NOT a solvency asset).
#   stakePrimeAndActivatePremium()    onlyOwner + nonReentrant (NOT remainsSolvent -> no payload).
#                                     Stakes getRequiredPrimeStake(PREMIUM, (totalValue-debt)*10) from the
#                                        IN-ACCOUNT PRIME balance (provisions the 10x-max-debt stake up
#                                        front), sets tier=PREMIUM. Reverts if already PREMIUM or short PRIME.
#   deactivatePremiumTier(bool)       onlyOwner + nonReentrant. Repays ALL PRIME debt first (reverts if it
#                                        can't), drops to BASIC; bool=true also releases excess stake.
#   unstakePrime(uint256)             onlyOwner + nonReentrant. Guards: remaining stake must cover the PREMIUM
#                                        USD ratio against current debt AND the accrued PRIME debt.
#   repayPrimeDebt(uint256)           onlyOwner. Caps to current debt; 50% burn / 50% treasury.
#   getLeverageTier/getLeverageTierFullInfo/getPrimeStakedAmount/getRequiredPrimeStake  oracle-free views.
#   shouldLiquidatePrimeDebt()        NON-view (mutates: snapshots debt) — we only eth_call it (read-only sim).
# PRIME token (18-dec) is resolved on-chain via TokenManager.getAssetAddress("PRIME", true). Do NOT confuse
# with sPRIME (a separate PRIME-AVAX LP receipt token); the facet stakes plain PRIME.
PRIME_LEVERAGE_FACET = "0x912609401D93779bEd71C9027c5f11f518397Bdd"
PRIME_TOKEN = {"addr": "0x33c8036e99082b0c395374832fecf70c42c7f298", "symbol": "PRIME", "decimals": 18}
PRIME_TIERS = {"basic": 0, "premium": 1}
PRIME_TIER_NAMES = {0: "BASIC", 1: "PREMIUM", 2: "_NON_EXISTENT"}

# Minimal ERC20 ABI: balanceOf is the only function we read off arbitrary tokens (wallet
# balances for cmd_my_positions). The approve selector is hot-loaded inline at write sites.
ERC20_BALANCE_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",'
    '"outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]'
)

# Pool ABI — hand-curated subset (totalSupply, totalBorrowed, balanceOf, getBorrowed,
# deposit, withdraw). DeltaPrime's Pool implementation is the parent of DegenPrime's,
# and every pool function this tool calls is in this subset. Previously deltaprime
# fetched per-pool ABIs from Snowtrace (5 API hits per `pool-info all` on cold cache,
# rate-limited); the hand-curated ABI removes that dependency entirely. Verified against
# the DeltaPrime Pool contract on Snowtrace (2026-05-23).
POOL_ABI = json.loads(
    '['
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[],"name":"totalSupply","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"totalBorrowed","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"getBorrowed","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}'
    ']'
)

# Process-local Web3 singleton. Each get_w3() call previously constructed a fresh
# HTTPProvider — wasteful on multi-pool reads (cmd_pool_info("all"), gather_defi).
_W3 = None

def get_w3():
    """Process-local Web3 client. Avalanche C-chain needs the POA middleware injected
    once; subsequent callers share the same provider + middleware stack."""
    global _W3
    if _W3 is None:
        w3 = Web3(Web3.HTTPProvider(AVALANCHE_RPC))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        _W3 = w3
    return _W3

def _tx_gas_price(w3) -> int:
    """Gas price for broadcasts: 2x the current network price with a 1 gwei floor.
    Avalanche's base fee is ~0.02 gwei, so a bare w3.eth.gas_price tx can strand
    (sit unmined / get dropped) if the base fee ticks up after submission. The bump
    guarantees timely inclusion and gives headroom to REPLACE a stranded same-nonce
    tx. Cost is negligible (~3M gas at ~1 gwei = ~0.003 AVAX). NOTE: this is the tx
    gasPrice; the GMX keeper execution-fee floor (25 gwei) is a separate calc, kept as-is."""
    return max(int(w3.eth.gas_price * 2), 10**9)

def resolve_private_key():
    """Resolve the signing key per the documented precedence:
       1. --key <0xhex> CLI flag
       2. DELTAPRIME_PRIVATE_KEY env var
       3. DELTAPRIME_KEY_FILE env var (path to a file containing the 0x key)
    Raises with a clear message if none of the three are set."""
    if _CLI_KEY:
        return _CLI_KEY.strip()
    raw = os.environ.get("DELTAPRIME_PRIVATE_KEY")
    if raw:
        return raw.strip()
    key_file = os.environ.get("DELTAPRIME_KEY_FILE")
    if key_file:
        try:
            return Path(key_file).read_text().strip()
        except FileNotFoundError:
            raise RuntimeError(f"DELTAPRIME_KEY_FILE points at {key_file} but the file does not exist.")
    raise RuntimeError(
        "No signing key found. Set DELTAPRIME_PRIVATE_KEY (raw 0x... key), or "
        "DELTAPRIME_KEY_FILE (path to a file containing the key), or pass --key <0xhex>."
    )

def get_account() -> Account:
    return Account.from_key(resolve_private_key())

def get_pool_contract(pool_name: str):
    """Pool proxy contract bound to the hand-curated POOL_ABI. Previously this fetched
    the implementation ABI from Snowtrace per pool (1 API hit on cold cache, often
    rate-limited); the hand-curated subset covers every function the tool calls."""
    cfg = POOLS[pool_name]
    proxy = Web3.to_checksum_address(cfg["proxy"])
    w3 = get_w3()
    return w3.eth.contract(address=proxy, abi=POOL_ABI), cfg, w3

# Minimal Prime Account ABI: only the facet functions this tool calls. The diamond
# beacon's own ABI exposes beacon-management only, so the borrow/repay/fund and
# view selectors live in facets — we hand-pick the verified signatures here rather
# than enumerate 26 facet contracts at runtime.
#   borrow/repay/fund: AssetsOperationsAvalancheFacet 0x5a501B5698eAdE321B3553eA633046c6a91E3763
#   depositNativeToken: SmartLoanWrappedNativeTokenFacet 0x81252DF686542B1F353671458561DF8E9151c8C1
#   getDebts/getBalance/getAllOwnedAssets: SmartLoanViewFacet 0x2B2C18F21A50c4DcbdFA54fb8cdC009F36AF27d9
#   getHealthMeter: HealthMeterFacetProd 0x519AeEfC6558aD1f138E3892A09eBFC327eb67E2 (RedStone-gated)
#   yakSwap/isWhitelistedAdapterOptimized: YieldYakSwapFacet 0x7b90769acaFb6540D00C06c406ba01Ab58B3028C (yakSwap is RedStone-gated)
#   getHealthRatio/isSolvent/getTotalValue/getDebt: SolvencyFacetProdAvalanche 0x968f944e9c43FC8AD80F6C1629F10570a46e2651 (RedStone-gated)
#   createWithdrawalIntent/executeWithdrawalIntent/getUserIntents/getAvailableBalance/getTotalIntentAmount:
#     WithdrawalIntentFacet 0xf88f82e8982de4f7831B0A8BA55Ce23536872FD9 (executeWithdrawalIntent is RedStone-gated;
#     the others are oracle-free; signatures verified on Snowtrace 23-05-2026)
PRIME_ACCOUNT_ABI = [
    {"inputs": [{"name": "_asset", "type": "bytes32"}, {"name": "_amount", "type": "uint256"}],
     "name": "borrow", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}, {"name": "_amount", "type": "uint256"}],
     "name": "repay", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "_fundedAsset", "type": "bytes32"}, {"name": "_amount", "type": "uint256"}],
     "name": "fund", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "depositNativeToken", "outputs": [],
     "stateMutability": "payable", "type": "function"},
    {"inputs": [], "name": "getAllOwnedAssets", "outputs": [{"type": "bytes32[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}], "name": "getBalance",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getDebts",
     "outputs": [{"components": [{"name": "name", "type": "bytes32"}, {"name": "debt", "type": "uint256"}], "type": "tuple[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getHealthMeter", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_adapter", "type": "address"}], "name": "isWhitelistedAdapterOptimized",
     "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_amountIn", "type": "uint256"}, {"name": "_amountOut", "type": "uint256"},
                {"name": "_path", "type": "address[]"}, {"name": "_adapters", "type": "address[]"}],
     "name": "yakSwap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    # ParaSwapFacet.paraSwapV6 / SwapDebtFacet.swapDebtParaSwap — both RedStone-gated
    # (remainsSolvent). selector+data are the ParaSwap Augustus calldata, split into its
    # 4-byte method selector and the remaining ABI-encoded args. Signatures verified on
    # Snowtrace 23-05-2026.
    {"inputs": [{"name": "selector", "type": "bytes4"}, {"name": "data", "type": "bytes"}],
     "name": "paraSwapV6", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_fromAsset", "type": "bytes32"}, {"name": "_toAsset", "type": "bytes32"},
                {"name": "_repayAmount", "type": "uint256"}, {"name": "_borrowAmount", "type": "uint256"},
                {"name": "selector", "type": "bytes4"}, {"name": "data", "type": "bytes"}],
     "name": "swapDebtParaSwap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "owner", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    # SolvencyFacetProdAvalanche views — RedStone-gated. getTotalValue/getDebt are
    # 1e18-scaled USD; getHealthRatio is 1e18-scaled (1e18 == liquidation line, so the
    # human ratio is the raw value / 1e18). All revert with 0xe7764c9e on a bare
    # eth_call — a signed RedStone price payload must be appended to the calldata.
    {"inputs": [], "name": "getHealthRatio", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getTotalValue", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getDebt", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "isSolvent", "outputs": [{"type": "bool"}],
     "stateMutability": "view", "type": "function"},
    # getPrices: 1e8-scaled USD prices for the given symbols. RedStone-gated, so a payload
    # is appended for the read. swap-debt uses it to value-match the borrow vs repay leg
    # against the facet's own 5% cap (the facet calls the same view internally).
    {"inputs": [{"name": "symbols", "type": "bytes32[]"}], "name": "getPrices",
     "outputs": [{"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
    # WithdrawalIntentFacet — delayed collateral withdrawal. createWithdrawalIntent
    # registers an intent (no RedStone); executeWithdrawalIntent pulls it to the EOA
    # after maturity (RedStone-gated, also runs canRepayDebtFully). getUserIntents /
    # getAvailableBalance / getTotalIntentAmount are oracle-free reads. IntentInfo's
    # isActionable/isExpired flags make the 24h-72h window readable on-chain.
    {"inputs": [{"name": "_asset", "type": "bytes32"}, {"name": "_amount", "type": "uint256"}],
     "name": "createWithdrawalIntent", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}, {"name": "intentIndices", "type": "uint256[]"}],
     "name": "executeWithdrawalIntent", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}, {"name": "intentIndex", "type": "uint256"}],
     "name": "cancelWithdrawalIntent", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}], "name": "getUserIntents",
     "outputs": [{"components": [{"name": "amount", "type": "uint256"},
                                 {"name": "actionableAt", "type": "uint256"},
                                 {"name": "expiresAt", "type": "uint256"},
                                 {"name": "isPending", "type": "bool"},
                                 {"name": "isActionable", "type": "bool"},
                                 {"name": "isExpired", "type": "bool"}], "type": "tuple[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}], "name": "getAvailableBalance",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}], "name": "getTotalIntentAmount",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    # ─── GMX V2 GM / GM+ LP (GmxV2FacetAvalanche / GmxV2PlusFacetAvalanche) ───
    # All deposit/withdraw fns are PAYABLE and require executionFee == msg.value (the facet
    # reverts InvalidExecutionFee otherwise). Two-sided deposits take a leading bool
    # isLongToken (true = volatile leg, false = USDC); GM+ deposits omit it. Withdraws take
    # gmAmount + min long/short token floors. Gated by an inline RedStone-priced solvency
    # simulation (_getThresholdWeightedValuePayable/_getDebtPayable) + isWithinBounds, so
    # --execute appends a signed price payload. getGmPerformance / getGmPlusPerformance and
    # SmartLoanViewFacet.getGmTokenBalanceAfterFees read RedStone prices too (they revert
    # 0xe7764c9e on a bare eth_call). Signatures verified on Snowtrace 23-05-2026.
    {"inputs": [{"name": "isLongToken", "type": "bool"}, {"name": "tokenAmount", "type": "uint256"},
                {"name": "minGmAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "depositAvaxUsdcGmxV2", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "isLongToken", "type": "bool"}, {"name": "tokenAmount", "type": "uint256"},
                {"name": "minGmAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "depositBtcUsdcGmxV2", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "isLongToken", "type": "bool"}, {"name": "tokenAmount", "type": "uint256"},
                {"name": "minGmAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "depositEthUsdcGmxV2", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "gmAmount", "type": "uint256"}, {"name": "minLongTokenAmount", "type": "uint256"},
                {"name": "minShortTokenAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "withdrawAvaxUsdcGmxV2", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "gmAmount", "type": "uint256"}, {"name": "minLongTokenAmount", "type": "uint256"},
                {"name": "minShortTokenAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "withdrawBtcUsdcGmxV2", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "gmAmount", "type": "uint256"}, {"name": "minLongTokenAmount", "type": "uint256"},
                {"name": "minShortTokenAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "withdrawEthUsdcGmxV2", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "tokenAmount", "type": "uint256"}, {"name": "minGmAmount", "type": "uint256"},
                {"name": "executionFee", "type": "uint256"}],
     "name": "depositAvaxGmxV2Plus", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "tokenAmount", "type": "uint256"}, {"name": "minGmAmount", "type": "uint256"},
                {"name": "executionFee", "type": "uint256"}],
     "name": "depositBtcGmxV2Plus", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "tokenAmount", "type": "uint256"}, {"name": "minGmAmount", "type": "uint256"},
                {"name": "executionFee", "type": "uint256"}],
     "name": "depositEthGmxV2Plus", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "gmAmount", "type": "uint256"}, {"name": "minLongTokenAmount", "type": "uint256"},
                {"name": "minShortTokenAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "withdrawAvaxGmxV2Plus", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "gmAmount", "type": "uint256"}, {"name": "minLongTokenAmount", "type": "uint256"},
                {"name": "minShortTokenAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "withdrawBtcGmxV2Plus", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "gmAmount", "type": "uint256"}, {"name": "minLongTokenAmount", "type": "uint256"},
                {"name": "minShortTokenAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
     "name": "withdrawEthGmxV2Plus", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "gmToken", "type": "address"}], "name": "getGmPerformance",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "gmToken", "type": "address"}], "name": "getGmPlusPerformance",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "gmToken", "type": "address"}], "name": "getGmTokenBalanceAfterFees",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    # ─── TraderJoe V2 Liquidity Book (TraderJoeV2AvalancheFacet) ──────────────
    # Concentrated liquidity across discrete price bins. addLiquidityTraderJoeV2 carries
    # remainsSolvent (RedStone-gated on --execute); removeLiquidityTraderJoeV2 does NOT
    # (only onlyOwnerOrLiquidation/noBorrowInTheSameBlock) so it needs no payload.
    # getOwnedTraderJoeV2Bins / getJoeV2RouterAddress are oracle-free views. The facet
    # overrides LiquidityParameters.to/refundTo to the account itself and enforces
    # maxBinsPerPrimeAccount()==80. Signatures verified on Snowtrace 23-05-2026 against the
    # verified TraderJoeV2AvalancheFacet + ILBRouter source.
    {"inputs": [{"name": "traderJoeV2Router", "type": "address"},
                {"name": "liquidityParameters", "type": "tuple", "components": [
                    {"name": "tokenX", "type": "address"}, {"name": "tokenY", "type": "address"},
                    {"name": "binStep", "type": "uint256"}, {"name": "amountX", "type": "uint256"},
                    {"name": "amountY", "type": "uint256"}, {"name": "amountXMin", "type": "uint256"},
                    {"name": "amountYMin", "type": "uint256"}, {"name": "activeIdDesired", "type": "uint256"},
                    {"name": "idSlippage", "type": "uint256"}, {"name": "deltaIds", "type": "int256[]"},
                    {"name": "distributionX", "type": "uint256[]"}, {"name": "distributionY", "type": "uint256[]"},
                    {"name": "to", "type": "address"}, {"name": "refundTo", "type": "address"},
                    {"name": "deadline", "type": "uint256"}]}],
     "name": "addLiquidityTraderJoeV2", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "traderJoeV2Router", "type": "address"},
                {"name": "parameters", "type": "tuple", "components": [
                    {"name": "tokenX", "type": "address"}, {"name": "tokenY", "type": "address"},
                    {"name": "binStep", "type": "uint16"}, {"name": "amountXMin", "type": "uint256"},
                    {"name": "amountYMin", "type": "uint256"}, {"name": "ids", "type": "uint256[]"},
                    {"name": "amounts", "type": "uint256[]"}, {"name": "deadline", "type": "uint256"}]}],
     "name": "removeLiquidityTraderJoeV2", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "getOwnedTraderJoeV2Bins",
     "outputs": [{"components": [{"name": "pair", "type": "address"}, {"name": "id", "type": "uint24"}], "type": "tuple[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getJoeV2RouterAddress", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    # ─── sJOE staking (SJoeFacet) ─────────────────────────────────────────────
    # stakeJoe / claimSJoeRewards carry remainsSolvent (RedStone-gated on --execute);
    # unstakeJoe is onlyOwnerOrInsolvent (NOT remainsSolvent) so it needs no payload.
    # joeBalanceInSJoe (staked JOE) and rewardsInSJoe (pending USDC) are oracle-free views.
    # Signatures verified on Snowtrace 23-05-2026 against the verified SJoeFacet source.
    {"inputs": [{"name": "amount", "type": "uint256"}], "name": "stakeJoe", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "amount", "type": "uint256"}], "name": "unstakeJoe", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "claimSJoeRewards", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "joeBalanceInSJoe", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "rewardsInSJoe", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    # ─── PRIME-token leverage tiers (PrimeLeverageFacet) ──────────────────────
    # depositPrime carries remainsSolvent (RedStone-gated on --execute); the other writes
    # (stake/activate, deactivate, unstake, repay) are onlyOwner only, so they need no
    # payload. The four getters are oracle-free views. shouldLiquidatePrimeDebt is declared
    # nonpayable because it MUTATES (snapshots debt) — we only eth_call it (read-only sim),
    # never broadcast it. The LeverageTier enum is a uint8 on the wire (BASIC=0, PREMIUM=1).
    # Signatures verified on Snowtrace 24-05-2026 against the verified PrimeLeverageFacet source.
    {"inputs": [{"name": "_amount", "type": "uint256"}], "name": "depositPrime", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "stakePrimeAndActivatePremium", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "withdrawStake", "type": "bool"}], "name": "deactivatePremiumTier",
     "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "amount", "type": "uint256"}], "name": "unstakePrime", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "amount", "type": "uint256"}], "name": "repayPrimeDebt", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "getLeverageTier", "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getLeverageTierFullInfo",
     "outputs": [{"name": "currentTier", "type": "uint8"}, {"name": "stakedPrime", "type": "uint256"},
                 {"name": "recordedDebt", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getPrimeStakedAmount", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tier", "type": "uint8"}, {"name": "borrowedValue", "type": "uint256"}],
     "name": "getRequiredPrimeStake", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "shouldLiquidatePrimeDebt", "outputs": [{"type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
]

# GMX DataStore: getUint(bytes32 key) holds the gas-limit params for fee estimation.
GMX_DATASTORE_ABI = [
    {"inputs": [{"name": "key", "type": "bytes32"}], "name": "getUint",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# YieldYak router findBestPath. Returns a FormattedOffer struct
# (uint256[] amounts, address[] adapters, address[] path, uint256 gasEstimate).
YAK_ROUTER_ABI = [
    {"inputs": [{"name": "_amountIn", "type": "uint256"}, {"name": "_tokenIn", "type": "address"},
                {"name": "_tokenOut", "type": "address"}, {"name": "_maxSteps", "type": "uint256"}],
     "name": "findBestPath",
     "outputs": [{"components": [{"name": "amounts", "type": "uint256[]"},
                                 {"name": "adapters", "type": "address[]"},
                                 {"name": "path", "type": "address[]"},
                                 {"name": "gasEstimate", "type": "uint256"}], "type": "tuple"}],
     "stateMutability": "view", "type": "function"},
]

# Tool asset symbol -> (token address, decimals). Swap operates on these. Same set as
# the lending pools; the symbol is the bytes32 the Prime Account uses, the address is
# the underlying ERC20 the YieldYak router routes on.
SWAP_ASSETS = {cfg["symbol"]: {"token": cfg["token"], "decimals": cfg["decimals"]}
               for cfg in POOLS.values()}

# SmartLoansFactory ABI — hand-curated minimum surface. createLoan / createAndFundLoan
# for writes; getLoanForOwner for the per-EOA Prime Account lookup. Previously fetched
# from Snowtrace per session; the hand-curated subset removes that dependency. Verified
# against the factory on Snowtrace (2026-05-23).
FACTORY_ABI = json.loads(
    '['
    '{"inputs":[],"name":"createLoan","outputs":[{"type":"address"}],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_fundedAsset","type":"bytes32"},{"name":"_amount","type":"uint256"}],"name":"createAndFundLoan","outputs":[{"type":"address"}],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"getLoanForOwner","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"}'
    ']'
)

def get_factory_contract(w3):
    return w3.eth.contract(address=Web3.to_checksum_address(FACTORY_PROXY), abi=FACTORY_ABI)

def get_prime_account(w3, owner: str) -> str:
    """Owner -> Prime Account address. Zero address means none exists yet."""
    pa = get_factory_contract(w3).functions.getLoanForOwner(Web3.to_checksum_address(owner)).call()
    return None if int(pa, 16) == 0 else pa

def asset_b32(symbol: str) -> bytes:
    return symbol.encode().ljust(32, b"\x00")

def pool_to_asset_symbol(pool_name: str) -> str:
    """Pool key -> on-chain bytes32 asset symbol (the contracts use 'AVAX', not 'WAVAX')."""
    return POOLS[pool_name]["symbol"]

def token_price(symbol: str) -> float:
    """Price from KuCoin."""
    try:
        r = requests.get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}-USDT", timeout=3)
        if r.status_code == 200 and r.json().get("code") == "200000":
            return float(r.json()["data"]["price"])
    except: pass
    return 0.0

# ─── RedStone on-demand price wrapping ───────────────────────────────────────
# DeltaPrime's Prime Account uses RedStone's on-demand model: signed price packages
# are fetched off-chain and APPENDED to the function calldata (after the normal
# ABI-encoded args). The solvency math (remainsSolvent modifier, and oracle views)
# parses them from the calldata tail, verifies the signatures, and aggregates by
# median. Without the payload these calls revert with 0xe7764c9e.
#
# Payload layout (matches @redstone-finance/evm-connector, verified against the
# deployed SolvencyFacetProdAvalanche source). Each signed data package:
#     for each data point: symbol(bytes32) ++ value(uint256, scaled 1e8, big-endian)
#     trailer: timestamp_ms(6) ++ dataPointValueByteSize(4)=32 ++ dataPointsCount(3)
#     signature(65): r ++ s ++ v
# After all packages: dataPackagesCount(2) ++ unsignedMetadataSize(3)=0 ++ marker(9).
# The signed message a signer signs is exactly (dataPoints ++ trailer); keccak256 of
# that (no EIP-191 prefix) recovers the signer address.
#
# The value MUST be reconstructed exactly as RedStone signed it: parseUnits(toFixed(8), 8).
# Python float is the same IEEE-754 double as JS Number, so Decimal(float).quantize(1e-8,
# ROUND_HALF_UP) reproduces toFixed(8) byte-for-byte. The old int(round(value*1e8)) added a
# second float + banker's rounding, so half-boundary/high-precision values re-derived a WRONG
# body; the contract then ecrecovered a GARBAGE address and reverted SignerNotAuthorised
# (0xec459bc0, wrapped in 0xfd36fde3) with a different bogus signer each call (24-05-2026).
# Verified 2340/2340 signer matches with Decimal vs intermittent misses with round(). This
# affected EVERY RedStone-gated path (lending, swaps, GMX, LB, PRIME, solvency views), not
# just PRIME — it just surfaced on PRIME activation.

# Authorised redstone-primary-prod signers (3-of-5). Verified by recovering every gateway
# package + a read-only eth_call clearing the signer revert. Re-verify if SignerNotAuthorised
# resurfaces — RedStone can rotate node keys.
REDSTONE_VALUE_DECIMALS = 8
# The 5 authorised signers from PrimaryProdDataServiceConsumerBase.getAuthorisedSignerIndex().
# Must match what's baked into the on-chain contract EXACTLY — any mismatch causes
# SignerNotAuthorised reverts on every solvency-gated operation.
# Stored lower-case because _redstone_package_signer returns checksummed addresses and the
# filter compares signer.lower() in this set.
REDSTONE_AUTHORISED_SIGNERS = {
    "0x8bb8f32df04c8b654987daaed53d6b6091e3b774",
    "0xdeb22f54738d54976c4c0fe5ce6d408e40d88499",
    "0x51ce04be4b3e32572c4ec9135221d0691ba7d202",
    "0xdd682daec5a90dd295d14da4b0bec9281017b5be",
    "0x9c5ae89c4af6aa32ce58588dbaf90d18a855b6de",
}

# Per-run cache for the RedStone gateway response — a single command (prime-summary, defi
# --json, gather_gmx looping markets, swap/lb-add write paths) hits the gateway once
# instead of per-feed-symbol. Cleared implicitly when the process exits; safe within the
# RedStone ~3-minute staleness window. Mirrors degenprime's pattern.
_redstone_gateway_cache = None

def _redstone_fetch_packages(use_cache: bool = True) -> dict:
    """Fetch the latest signed price packages from the RedStone gateway. Returns the
    per-feed map: {feedSymbol: [package, ...]} with one package per signer. The per-run
    cache lets repeat callers reuse the same snapshot — important for any command that
    builds multiple payloads (defi --json, gmx multi-market sweeps, prime-activate's
    two-payload deposit + activate flow)."""
    global _redstone_gateway_cache
    if use_cache and _redstone_gateway_cache is not None:
        return _redstone_gateway_cache
    last_err = None
    for gw in REDSTONE_GATEWAYS:
        try:
            r = requests.get(f"{gw}/data-packages/latest/{REDSTONE_DATA_SERVICE}", timeout=20)
            if r.status_code == 200:
                _redstone_gateway_cache = r.json()
                return _redstone_gateway_cache
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = e
    raise RuntimeError(f"RedStone gateway fetch failed: {last_err}")

def _redstone_scaled_value(value) -> int:
    """Reconstruct the signed uint256 exactly as RedStone does: parseUnits(Number(value)
    .toFixed(8), 8). Decimal(float(value)) is the exact IEEE-754 double (same as JS
    Number); quantizing to 1e-8 with ROUND_HALF_UP reproduces toFixed(8). Using plain
    int(round(value*1e8)) double-rounds and re-derives a wrong body -> garbage ecrecover
    -> SignerNotAuthorised on the stricter facets."""
    d = Decimal(float(value)).quantize(Decimal(1).scaleb(-REDSTONE_VALUE_DECIMALS),
                                       rounding=ROUND_HALF_UP)
    return int((d * (10 ** REDSTONE_VALUE_DECIMALS)).to_integral_value())

def _redstone_encode_package(pkg: dict) -> bytes:
    """Serialize one signed data package to the on-chain byte layout."""
    data_points = pkg["dataPoints"]
    ts = int(pkg["timestampMilliseconds"])
    body = b""
    for dp in data_points:
        value_scaled = _redstone_scaled_value(dp["value"])
        body += dp["dataFeedId"].encode().ljust(32, b"\x00") + value_scaled.to_bytes(32, "big")
    body += ts.to_bytes(6, "big") + (32).to_bytes(4, "big") + len(data_points).to_bytes(3, "big")
    return body + base64.b64decode(pkg["signature"])

def _redstone_package_signer(pkg: dict) -> str:
    """Recover a package's signer: ecrecover over keccak256(body) (no EIP-191 prefix),
    where body is the encoded package minus its trailing 65-byte signature."""
    body = _redstone_encode_package(pkg)[:-65]
    sig = base64.b64decode(pkg["signature"])
    r = int.from_bytes(sig[0:32], "big")
    s = int.from_bytes(sig[32:64], "big")
    rec_id = sig[64] - 27 if sig[64] >= 27 else sig[64]
    return eth_keys.Signature(vrs=(rec_id, r, s)).recover_public_key_from_msg_hash(
        Web3.keccak(body)).to_checksum_address()

def build_redstone_payload(symbols: list) -> bytes:
    """Build a RedStone calldata payload covering the given feed symbols. Recovers each
    package's signer, keeps only RedStone's authorised set, then takes the first
    REDSTONE_SIGNERS_THRESHOLD per feed (the contract needs that many unique authorised
    signers per feed to aggregate a median). Filtering guards against the gateway ever
    returning extra/standby signers and surfaces a clear error rather than an on-chain revert."""
    gateway = _redstone_fetch_packages()
    packages = []
    for sym in symbols:
        feed_packages = gateway.get(sym)
        if not feed_packages:
            raise RuntimeError(f"RedStone gateway has no feed for '{sym}'")
        authorised = [p for p in feed_packages
                      if _redstone_package_signer(p).lower() in REDSTONE_AUTHORISED_SIGNERS]
        if len(authorised) < REDSTONE_SIGNERS_THRESHOLD:
            raise RuntimeError(
                f"RedStone feed '{sym}' has only {len(authorised)} authorised signers "
                f"(of {len(feed_packages)} returned), need {REDSTONE_SIGNERS_THRESHOLD}")
        for pkg in authorised[:REDSTONE_SIGNERS_THRESHOLD]:
            packages.append(_redstone_encode_package(pkg))
    payload = b"".join(packages)
    payload += len(packages).to_bytes(2, "big")   # data packages count
    payload += (0).to_bytes(3, "big")             # unsigned metadata byte size = 0
    payload += REDSTONE_MARKER
    return payload

def prime_account_price_feeds(account) -> list:
    """The set of RedStone feed symbols a solvency check on this account needs: the
    native AVAX symbol, every owned asset, and every debt-registry asset. The solvency
    math prices ALL debt-registry assets (getDebts() returns the full pool set, not
    just non-zero balances), so every symbol it returns must be in the payload even at
    zero debt — otherwise that feed shows 0 signers and the call reverts with
    InsufficientNumberOfUniqueSigners. Deduped, AVAX first (priced as element 0)."""
    feeds = ["AVAX"]
    for a in account.functions.getAllOwnedAssets().call():
        sym = a.rstrip(b"\x00").decode(errors="replace")
        if sym and sym not in feeds:
            feeds.append(sym)
    for name, _debt in account.functions.getDebts().call():
        sym = name.rstrip(b"\x00").decode(errors="replace")
        if sym and sym not in feeds:
            feeds.append(sym)
    return feeds

def redstone_view_call(w3, account, fn_name: str, payload: bytes, args: list = None):
    """Read-only call of a RedStone-gated view on the Prime Account. The signed price
    payload is appended to the ABI-encoded calldata (same wrapping as a write tx), then
    eth_call'd and the result decoded against the function's ABI. Used for the solvency
    views (getHealthRatio/getTotalValue/getDebt/isSolvent, no args) and the GMX views
    (getGm[Plus]Performance/getGmTokenBalanceAfterFees, one address arg), which revert with
    0xe7764c9e on a bare call. `payload` is reused across calls so the gateway is hit once."""
    data = account.encode_abi(fn_name, args=args or []) + payload.hex()
    raw = w3.eth.call({"to": account.address, "data": data})
    fn_abi = next(f for f in PRIME_ACCOUNT_ABI if f.get("name") == fn_name)
    out_types = [o["type"] for o in fn_abi["outputs"]]
    return w3.codec.decode(out_types, bytes(raw))

# ─── Commands ──────────────────────────────────────────────────────────────

def cmd_pool_info(pool_name: str):
    if pool_name == "all":
        for name in POOLS:
            cmd_pool_info(name)
            print()
        return

    contract, cfg, w3 = get_pool_contract(pool_name)
    p = cfg["proxy"][:12]
    d = cfg["decimals"]

    ts = contract.functions.totalSupply().call()
    tb = contract.functions.totalBorrowed().call()
    print(f"=== {cfg['symbol']} Pool ({p}...) ===")
    print(f"  Total Supply:   {ts / 10**d:>14,.2f} {cfg['symbol']}")
    print(f"  Total Borrowed: {tb / 10**d:>14,.2f} {cfg['symbol']}")
    util = tb / ts * 100 if ts > 0 else 0
    print(f"  Utilization:    {util:>14.2f}%")
    price = token_price(cfg["symbol"])
    if price:
        print(f"  Token Price:    ${price:>13,.2f}")
        print(f"  TVL:            ${ts / 10**d * price:>13,.2f}")

    # Show the signer's pool deposit when a key is configured; pool-info should
    # also work as a pure read-only command without one.
    try:
        acct = get_account()
    except RuntimeError:
        return
    my_bal = contract.functions.balanceOf(acct.address).call()
    if my_bal > 0:
        print(f"  My Deposit:     {my_bal / 10**d:.4f} {cfg['symbol']}")

def cmd_my_positions():
    acct = get_account()
    w3 = get_w3()
    print(f"Wallet: {acct.address}")

    # Wallet AVAX
    avax = w3.eth.get_balance(acct.address) / 1e18
    print(f"AVAX: {avax:.6f}")

    # Wallet PRIME (not a pool token; shown so it's detected/displayed in the wallet view)
    try:
        prime_bal = _prime_token_contract(w3).functions.balanceOf(acct.address).call()
        if prime_bal > 0:
            print(f"  Wallet PRIME: {prime_bal / 10**PRIME_TOKEN['decimals']:.6f}")
    except Exception:
        pass

    # Check each pool
    for name, cfg in POOLS.items():
        try:
            contract, _, _ = get_pool_contract(name)
            token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]),
                                    abi=ERC20_BALANCE_ABI)
            bal = token.functions.balanceOf(acct.address).call()
            if bal > 0:
                print(f"  Wallet {cfg['symbol']}: {bal / 10**cfg['decimals']:.4f}")

            # Check pool deposit
            pool_bal = contract.functions.balanceOf(acct.address).call()
            if pool_bal > 0:
                print(f"  Pool Deposit {cfg['symbol']}: {pool_bal / 10**cfg['decimals']:.4f}")

            # Check borrow
            borrowed = contract.functions.getBorrowed(acct.address).call()
            if borrowed > 0:
                print(f"  Borrowed {cfg['symbol']}: {borrowed / 10**cfg['decimals']:.4f}")

        except Exception as e:
            print(f"  {name}: {e}")

    # Prime Account (via getLoanForOwner — the factory has no getAccount())
    try:
        pa = get_prime_account(w3, acct.address)
        if pa:
            print(f"\nPrime Account: {pa}")
            pa_avax = w3.eth.get_balance(Web3.to_checksum_address(pa)) / 1e18
            print(f"  AVAX balance: {pa_avax:.6f}")
        else:
            print("\nNo Prime Account yet. Create with: deltaprime create-prime-account --execute")
    except Exception as e:
        print(f"\nPrime Account lookup failed: {e}")

def cmd_deposit(pool_name: str, amount: float, execute: bool = False):
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    amount_wei = int(amount * 10**cfg["decimals"])

    if not execute:
        print(f"Preview: Deposit {amount} {cfg['symbol']} into {pool_name.upper()} pool")
        print("Run with --execute to broadcast")
        return

    if cfg["native"]:
        tx = contract.functions.deposit(amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 200000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID, "value": amount_wei,
        })
        signed = acct.sign_transaction(tx)
    else:
        # Approve
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]),
                                abi=json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]'))
        app_tx = token.functions.approve(Web3.to_checksum_address(cfg["proxy"]), amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
        signed_app = acct.sign_transaction(app_tx)
        w3.eth.send_raw_transaction(signed_app.raw_transaction)

        # Deposit
        dep_tx = contract.functions.deposit(amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 200000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
        signed = acct.sign_transaction(dep_tx)

    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Deposit {amount} {cfg['symbol']} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_withdraw(pool_name: str, amount: float, execute: bool = False):
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    amount_wei = int(amount * 10**cfg["decimals"])

    if not execute:
        print(f"Preview: Withdraw {amount} {cfg['symbol']} from {pool_name.upper()} pool")
        print("Run with --execute to broadcast")
        return

    tx = contract.functions.withdraw(amount_wei).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 200000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Withdraw {amount} {cfg['symbol']} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

# ─── Prime Account commands ──────────────────────────────────────────────────

# bytes32 asset symbol -> decimals. The Prime Account can hold assets beyond the
# five lending pools; fall back to 18 (the EVM default) for anything unmapped.
_ASSET_DECIMALS = {cfg["symbol"]: cfg["decimals"] for cfg in POOLS.values()}

def _asset_decimals(symbol: str) -> int:
    return _ASSET_DECIMALS.get(symbol, 18)

def cmd_create_prime_account(execute: bool = False, fund_pool: str = None, fund_amount: float = None):
    """Create a Prime Account. With fund_pool/fund_amount, create and fund in one
    tx via SmartLoansFactory.createAndFundLoan(bytes32 asset, amount) — ERC20 only,
    and the factory pulls the asset via transferFrom so it needs a prior approve to
    the factory. Without fund args, plain createLoan() makes an empty account."""
    w3 = get_w3()
    acct = get_account()
    existing = get_prime_account(w3, acct.address)
    if existing:
        print(f"Prime Account already exists: {existing}")
        print("Nothing to create. Fund it with: deltaprime fund --pool <p> --amount <n> --execute")
        return

    funding = fund_pool is not None and fund_amount is not None
    cfg = POOLS[fund_pool] if funding else None
    if funding and cfg["native"]:
        print("createAndFundLoan is ERC20-only — it cannot wrap native AVAX.")
        print("For an AVAX-funded account: create-prime-account --execute, then")
        print("  fund --pool wavax --amount <n> --execute  (uses depositNativeToken()).")
        return

    factory = get_factory_contract(w3)
    factory_cs = Web3.to_checksum_address(FACTORY_PROXY)

    if not execute:
        print(f"Preview: Create a new Prime Account for {acct.address}")
        if funding:
            symbol = cfg["symbol"]
            amount_wei = int(fund_amount * 10**cfg["decimals"])
            print(f"  Factory: {FACTORY_PROXY} (SmartLoansFactory.createAndFundLoan())")
            print(f"  Approves the factory to spend {fund_amount} {symbol}, then")
            print(f"  calls createAndFundLoan(bytes32 '{symbol}', {amount_wei}) — creates + funds in one go.")
            print("  Wallet must hold enough of the asset.")
        else:
            print(f"  Factory: {FACTORY_PROXY} (SmartLoansFactory.createLoan())")
            print("  Creates an empty account; fund it afterwards before borrowing.")
        print("Run with --execute to broadcast")
        return

    if funding:
        symbol = cfg["symbol"]
        amount_wei = int(fund_amount * 10**cfg["decimals"])
        # createAndFundLoan does token.transferFrom(msg.sender, factory, amount),
        # so approve the factory first.
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]),
                                abi=json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]'))
        app_tx = token.functions.approve(factory_cs, amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
        signed_app = acct.sign_transaction(app_tx)
        w3.eth.send_raw_transaction(signed_app.raw_transaction)

        tx = factory.functions.createAndFundLoan(asset_b32(symbol), amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 4000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
    else:
        tx = factory.functions.createLoan().build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 4000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    label = "Create+fund Prime Account" if funding else "Create Prime Account"
    print(f"{'✓' if ok else '✗'} {label} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    if ok:
        # getLoanForOwner can lag a beat behind the receipt; poll briefly so we
        # print the new account address instead of None right after creation.
        pa = None
        for _ in range(6):
            pa = get_prime_account(w3, acct.address)
            if pa:
                break
            time.sleep(2)
        if pa:
            print(f"  Prime Account: {pa}")
        else:
            print("  Prime Account: created — getLoanForOwner not propagated yet, run 'my-positions' shortly.")

def cmd_fund(pool_name: str, amount: float, execute: bool = False):
    """Fund collateral from the EOA wallet into its Prime Account.

    ERC20 assets: approve the Prime Account to spend the token, then call
    fund(bytes32 asset, amount) on it. Native AVAX (wavax pool): call the
    payable depositNativeToken() and send AVAX as msg.value — the account
    wraps AVAX->WAVAX internally, so no token approve is needed.
    """
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account. Create one first: deltaprime create-prime-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    amount_wei = int(amount * 10**cfg["decimals"])
    pa_cs = Web3.to_checksum_address(pa)

    if not execute:
        print(f"Preview: Fund {amount} {symbol} into Prime Account {pa}")
        if cfg["native"]:
            print(f"  Native AVAX: calls depositNativeToken() with value={amount_wei} wei")
            print("  Wraps AVAX->WAVAX inside the account; no token approval needed.")
        else:
            print(f"  Approves {pa} to spend {amount} {symbol}, then calls fund(bytes32 '{symbol}', {amount_wei})")
        print("  Wallet must hold enough of the asset.")
        print("Run with --execute to broadcast")
        return

    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    if cfg["native"]:
        tx = account.functions.depositNativeToken().build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID, "value": amount_wei,
        })
        signed = acct.sign_transaction(tx)
    else:
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]),
                                abi=json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]'))
        app_tx = token.functions.approve(pa_cs, amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
        signed_app = acct.sign_transaction(app_tx)
        w3.eth.send_raw_transaction(signed_app.raw_transaction)

        fund_tx = account.functions.fund(asset_b32(symbol), amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
        signed = acct.sign_transaction(fund_tx)

    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Fund {amount} {symbol} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    return ok

def _prices_usd(w3, account, symbols: list, payload: bytes) -> dict:
    """Best-effort per-symbol USD price map via the RedStone-gated getPrices view (1e8-scaled).
    Reuses an already-built `payload`; returns {symbol: float}. Any symbol whose feed is
    missing from the payload is omitted (callers treat a missing entry as usd=None) rather
    than failing the whole readout."""
    syms = [s for s in dict.fromkeys(symbols) if s]
    if not syms:
        return {}
    try:
        raw = redstone_view_call(w3, account, "getPrices", payload,
                                 args=[[asset_b32(s) for s in syms]])[0]
        return {s: raw[i] / 1e8 for i, s in enumerate(syms)}
    except Exception:
        return {}

def gather_lending(w3, account):
    """Read-only lending/leverage data for a Prime Account: in-account collateral
    (getAllOwnedAssets/getBalance), debts (getDebts), and RedStone-gated solvency
    (getTotalValue/getDebt/getHealthRatio/isSolvent) plus best-effort per-asset USD via
    getPrices. Shared by cmd_prime_summary (print) and cmd_defi (--json). Solvency fields
    fall back to None if the RedStone gateway is unreachable or a view reverts."""
    # One read of getAllOwnedAssets + getDebts feeds both the supplied/borrowed lists
    # AND the RedStone price feed set, so we inline the feeds derivation here rather
    # than calling prime_account_price_feeds() (which would repeat both reads).
    owned_raw = account.functions.getAllOwnedAssets().call()
    debts_raw = account.functions.getDebts().call()
    owned = [a.rstrip(b"\x00").decode(errors="replace") for a in owned_raw]
    supplied = []
    for sym in owned:
        bal = account.functions.getBalance(asset_b32(sym)).call()
        supplied.append({"symbol": sym, "raw": bal, "decimals": _asset_decimals(sym),
                         "balance": f"{bal / 10**_asset_decimals(sym):.6f}"})
    borrowed = []
    for n, v in debts_raw:
        sym = n.rstrip(b"\x00").decode(errors="replace")
        if v > 0:
            borrowed.append({"symbol": sym, "raw": v, "decimals": _asset_decimals(sym),
                             "balance": f"{v / 10**_asset_decimals(sym):.6f}"})
    # Derive the price feeds inline from the already-read assets/debts (mirrors
    # prime_account_price_feeds but skips its two extra eth_calls). AVAX first.
    feeds = ["AVAX"]
    for sym in owned:
        if sym and sym not in feeds:
            feeds.append(sym)
    for n, _v in debts_raw:
        sym = n.rstrip(b"\x00").decode(errors="replace")
        if sym and sym not in feeds:
            feeds.append(sym)
    out = {"supplied": supplied, "borrowed": borrowed,
           "total_value_usd": None, "debt_usd": None, "health_ratio": None, "solvent": None}
    try:
        payload = build_redstone_payload(feeds)
        out["total_value_usd"] = redstone_view_call(w3, account, "getTotalValue", payload)[0] / 1e18
        out["debt_usd"] = redstone_view_call(w3, account, "getDebt", payload)[0] / 1e18
        ratio = redstone_view_call(w3, account, "getHealthRatio", payload)[0] / 1e18
        # With no/negligible debt the ratio is astronomically large (e.g. 1e59) — meaningless
        # to render. Surface it as None so consumers show "no debt" instead of a junk number.
        out["health_ratio"] = None if ratio > 1000 else ratio
        out["solvent"] = bool(redstone_view_call(w3, account, "isSolvent", payload)[0])
        prices = _prices_usd(w3, account, [r["symbol"] for r in supplied + borrowed], payload)
        for r in supplied + borrowed:
            p = prices.get(r["symbol"])
            r["usd"] = (r["raw"] / 10**r["decimals"] * p) if p is not None else None
    except Exception as e:
        out["solvency_error"] = type(e).__name__
        for r in supplied + borrowed:
            r["usd"] = None
    return out

def cmd_prime_summary():
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Owner wallet: {acct.address}")
    if not pa:
        print("No Prime Account yet. Create one with: deltaprime create-prime-account --execute")
        return

    print(f"Prime Account: {pa}")
    pa_avax = w3.eth.get_balance(pa) / 1e18
    print(f"  Native AVAX (gas):  {pa_avax:.6f}")

    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    data = gather_lending(w3, account)

    print("  Assets:")
    if data["supplied"]:
        for r in data["supplied"]:
            print(f"    {r['symbol']:<8} {float(r['balance']):,.6f}")
    else:
        print("    (none)")

    print("  Debts:")
    if data["borrowed"]:
        for r in data["borrowed"]:
            print(f"    {r['symbol']:<8} {float(r['balance']):,.6f}")
    else:
        print("    (none)")

    # Solvency views (SolvencyFacetProdAvalanche) are RedStone-gated: they revert
    # (0xe7764c9e) without signed price calldata appended. gather_lending fetches a fresh
    # RedStone payload covering every feed the solvency math touches and eth_calls the views
    # with it appended — no tx, read-only. getTotalValue/getDebt are 1e18-scaled USD;
    # getHealthRatio is 1e18-scaled where 1.0 is the liquidation line. Falls back to the old
    # note if the gateway is unreachable or a view reverts.
    if "solvency_error" not in data:
        print(f"  Total value:        ${data['total_value_usd']:,.2f}")
        print(f"  Debt:               ${data['debt_usd']:,.2f}")
        ratio = data["health_ratio"]
        # gather_lending nulls the ratio when debt is negligible (the raw value is
        # astronomically large there); render that as ">1000" rather than a junk number.
        ratio_str = ">1000.00 (negligible debt)" if ratio is None else f"{ratio:.4f}"
        print(f"  Health ratio:       {ratio_str}  (>1.0 = solvent)")
        print(f"  Solvent:            {'yes' if data['solvent'] else 'NO — liquidatable'}")
    else:
        print(f"  Health/solvency:    RedStone fetch/call failed ({data.get('solvency_error', 'error')}); "
              "showing balances only")

def cmd_borrow(pool_name: str, amount: float, execute: bool = False):
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account. Create one first: deltaprime create-prime-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    amount_wei = int(amount * 10**cfg["decimals"])
    if not execute:
        print(f"Preview: Borrow {amount} {symbol} into Prime Account {pa}")
        print(f"  Calls borrow(bytes32 '{symbol}', {amount_wei}) on the Prime Account")
        print("  Requires sufficient collateral funded into the account.")
        print("Run with --execute to broadcast")
        return

    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    # borrow has remainsSolvent → needs RedStone price payload appended to calldata
    feeds = prime_account_price_feeds(account)
    if symbol not in feeds:
        feeds.append(symbol)
    payload = build_redstone_payload(feeds)
    base_calldata = account.encode_abi("borrow", args=[asset_b32(symbol), amount_wei])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 4000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Borrow {amount} {symbol} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    return ok

def cmd_repay(pool_name: str, amount: float, execute: bool = False):
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account. Create one first: deltaprime create-prime-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    pool, _, _ = get_pool_contract(pool_name)
    # The facet's repay reverts if amount > debt OR amount > in-account balance.
    # Cap to min(requested, debt, in_account) so callers don't need to know either
    # exact figure — pass an overshoot like 9999 and it clips cleanly.
    requested_wei = int(amount * 10**cfg["decimals"])
    debt_wei = pool.functions.getBorrowed(pa_cs).call()
    in_acct_wei = account.functions.getBalance(asset_b32(symbol)).call()
    if debt_wei == 0:
        print(f"No {symbol} debt to repay on Prime Account {pa}.")
        return
    amount_wei = min(requested_wei, debt_wei, in_acct_wei)
    if amount_wei == 0:
        print(f"Repay {amount} {symbol}: in-account {symbol} balance is 0 — "
              f"swap into {symbol} first (e.g. deltaprime swap --to {symbol} --amount N --execute).")
        return
    cap_notes = []
    if amount_wei < requested_wei:
        if in_acct_wei < min(requested_wei, debt_wei):
            cap_notes.append(f"in-account {symbol} only {in_acct_wei / 10**cfg['decimals']:.6f}")
        if debt_wei < requested_wei:
            cap_notes.append(f"debt only {debt_wei / 10**cfg['decimals']:.6f} {symbol}")

    if not execute:
        print(f"Preview: Repay {amount_wei / 10**cfg['decimals']:.6f} {symbol} from Prime Account {pa}")
        if cap_notes:
            print(f"  Capped from requested {amount}: {'; '.join(cap_notes)}")
        print(f"  Calls repay(bytes32 '{symbol}', {amount_wei}) on the Prime Account")
        print(f"  Current debt: {debt_wei / 10**cfg['decimals']:.6f} {symbol} | "
              f"in-account: {in_acct_wei / 10**cfg['decimals']:.6f} {symbol}")
        if in_acct_wei < debt_wei:
            shortfall = (debt_wei - in_acct_wei) / 10**cfg['decimals']
            print(f"  Note: in-account < debt by {shortfall:.6f} {symbol} — "
                  f"swap into {symbol} first to close the position fully.")
        print("Run with --execute to broadcast")
        return

    if cap_notes:
        print(f"  Capped requested {amount} {symbol} to {amount_wei / 10**cfg['decimals']:.6f} "
              f"({'; '.join(cap_notes)}).")
    # repay calls _isSolvent() which uses proxyDelegateCalldata → needs RedStone payload
    feeds = prime_account_price_feeds(account)
    if symbol not in feeds:
        feeds.append(symbol)
    payload = build_redstone_payload(feeds)
    base_calldata = account.encode_abi("repay", args=[asset_b32(symbol), amount_wei])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 4000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    repaid = amount_wei / 10**cfg['decimals']
    print(f"{'✓' if ok else '✗'} Repay {repaid:.6f} {symbol} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def _decode_formatted_offer(raw: bytes):
    """Manually decode YieldYak's FormattedOffer struct
    (uint256[] amounts, address[] adapters, address[] path, uint256 gasEstimate).
    Hand-rolled because eth-abi 6.0.0b1's validate_pointers rejects this otherwise
    valid encoding (the trailing inline uint256 confuses its pointer validator)."""
    base = 32  # offsets inside the struct are relative to the start of its body
    amounts_off = base + int.from_bytes(raw[32:64], "big")
    adapters_off = base + int.from_bytes(raw[64:96], "big")
    path_off = base + int.from_bytes(raw[96:128], "big")

    def read_array(off, is_address):
        n = int.from_bytes(raw[off:off + 32], "big")
        out = []
        for i in range(n):
            word = raw[off + 32 + i * 32: off + 64 + i * 32]
            out.append(Web3.to_checksum_address(word[12:]) if is_address
                       else int.from_bytes(word, "big"))
        return out

    return read_array(amounts_off, False), read_array(adapters_off, True), read_array(path_off, True)

def _yak_find_best_path(w3, amount_in_wei: int, token_in: str, token_out: str, max_steps: int = 3):
    """Off-chain route lookup via the YieldYak router. Returns (amounts, adapters, path)."""
    router = w3.eth.contract(address=Web3.to_checksum_address(YAK_ROUTER), abi=YAK_ROUTER_ABI)
    data = router.encode_abi("findBestPath", args=[
        amount_in_wei, Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out), max_steps])
    raw = w3.eth.call({"to": Web3.to_checksum_address(YAK_ROUTER), "data": data})
    return _decode_formatted_offer(bytes(raw))

# ─── ParaSwap / Velora route ─────────────────────────────────────────────────
# The Prime Account already holds the funds, so the facet (not the EOA) approves the
# Augustus router and executes. We only build the API calldata with the Prime Account as
# the swapper + receiver, then hand its (selector, data) to paraSwapV6 / swapDebtParaSwap.

def _paraswap_price_route(src_token, src_dec, dest_token, dest_dec, amount_in_wei, user_addr):
    """ParaSwap /prices: returns the priceRoute dict for a SELL of amount_in_wei src->dest
    on Avalanche v6.2. The priceRoute is passed verbatim to /transactions."""
    params = {
        "srcToken": src_token, "srcDecimals": src_dec,
        "destToken": dest_token, "destDecimals": dest_dec,
        "amount": str(amount_in_wei), "side": "SELL",
        "network": CHAIN_ID, "version": "6.2", "userAddress": user_addr,
    }
    r = requests.get(f"{PARASWAP_API}/prices", params=params,
                     headers={"Accept": "application/json"}, timeout=20)
    d = r.json()
    pr = d.get("priceRoute")
    if not pr:
        raise RuntimeError(f"ParaSwap /prices returned no route: {d.get('error', d)}")
    return pr

def _paraswap_build_tx(price_route, src_token, src_dec, dest_token, dest_dec,
                       amount_in_wei, slippage_pct, user_addr):
    """ParaSwap /transactions: build the Augustus calldata for the given price route, with
    the Prime Account as userAddress + receiver. partner='paraswap' makes the encoded
    partnerAndFee resolve to partner=0/fee=0, which the facet requires (any other partner
    string injects a non-zero fee/partner the facet would reject). Returns the tx dict."""
    body = {
        "srcToken": src_token, "srcDecimals": src_dec,
        "destToken": dest_token, "destDecimals": dest_dec,
        "srcAmount": str(amount_in_wei),
        "slippage": int(round(slippage_pct * 100)),  # ParaSwap takes slippage in bps
        "priceRoute": price_route,
        "userAddress": user_addr,
        "receiver": user_addr,
        "partner": "paraswap",
    }
    # ignoreChecks: the swapper is the Prime Account (a contract that holds no funds at
    # build time and hasn't approved Augustus yet — the facet does that mid-tx), so the
    # API's balance/allowance pre-checks would reject an otherwise valid build.
    r = requests.post(f"{PARASWAP_API}/transactions/{CHAIN_ID}?ignoreChecks=true&ignoreGasEstimate=true",
                      json=body, headers={"Accept": "application/json"}, timeout=20)
    d = r.json()
    if "data" not in d:
        raise RuntimeError(f"ParaSwap /transactions returned no calldata: {d.get('error', d)}")
    return d

def _paraswap_decode_and_check(selector_hex, data_bytes, src_token, dest_token, expected_from, pa_cs):
    """Mirror the facet's decodeParaSwapData + validateSwapParameters on the built calldata,
    so a preview fails loud here rather than reverting on-chain. Returns the decoded
    (executor, src, dest, fromAmount, toAmount) for display. Only swapExactAmountIn is
    fully field-decoded; the UniV3 variant is sanity-checked on selector + length only."""
    if selector_hex not in PARASWAP_SUPPORTED_SELECTORS:
        raise RuntimeError(f"ParaSwap returned method {selector_hex}, which the facet does not "
                           f"decode (supported: {', '.join(sorted(PARASWAP_SUPPORTED_SELECTORS))}). "
                           "Refusing.")
    if len(data_bytes) < 288:
        raise RuntimeError(f"ParaSwap calldata body too short ({len(data_bytes)} bytes, need >=288).")

    if selector_hex == "0xe3ead59e":  # swapExactAmountIn — executor ++ GenericData ++ partnerAndFee
        executor = "0x" + data_bytes[:32][-20:].hex()
        src, dest, from_amt, to_amt, _quoted, _meta, beneficiary = abi_decode(
            ["address", "address", "uint256", "uint256", "uint256", "bytes32", "address"],
            data_bytes[32:256])
        partner_and_fee = int.from_bytes(data_bytes[256:288], "big")
        partner = (partner_and_fee >> 96) & ((1 << 160) - 1)
        fee_bps = partner_and_fee & 0x3FFF
        if executor.lower() not in PARASWAP_EXECUTORS:
            print(f"  ⚠ ParaSwap executor {executor} not in the KNOWN whitelist — the on-chain facet")
            print(f"    may reject it with InvalidExecutor(). Proceeding anyway; verify on-chain.")
        if partner != 0 or fee_bps != 0:
            raise RuntimeError(f"ParaSwap calldata carries a non-zero partner/fee "
                               f"(partner={hex(partner)}, feeBps={fee_bps}); the facet would revert. Refusing.")
        if Web3.to_checksum_address(src) != Web3.to_checksum_address(src_token) or \
           Web3.to_checksum_address(dest) != Web3.to_checksum_address(dest_token):
            raise RuntimeError("ParaSwap calldata src/dest token mismatch vs request. Refusing.")
        zero = "0x" + "00" * 20
        if Web3.to_checksum_address(beneficiary) not in (Web3.to_checksum_address(zero), pa_cs):
            raise RuntimeError(f"ParaSwap beneficiary {Web3.to_checksum_address(beneficiary)} "
                               f"is neither zero nor the Prime Account. Refusing.")
        if from_amt != expected_from:
            raise RuntimeError(f"ParaSwap fromAmount {from_amt} != expected {expected_from}. Refusing.")
        return executor, src, dest, from_amt, to_amt
    return None, src_token, dest_token, expected_from, None

def _swap_via_paraswap(w3, acct, pa_cs, account, from_sym, to_sym, from_cfg, to_cfg,
                       amount, amount_in, slippage_pct, execute):
    """ParaSwap leg of cmd_swap. Builds the Velora calldata for an in-account
    from_sym->to_sym swap and (on --execute) calls paraSwapV6 with a RedStone payload."""
    price_route = _paraswap_price_route(from_cfg["token"], from_cfg["decimals"],
                                        to_cfg["token"], to_cfg["decimals"], amount_in, pa_cs)
    quoted_out = int(price_route["destAmount"])
    tx_built = _paraswap_build_tx(price_route, from_cfg["token"], from_cfg["decimals"],
                                  to_cfg["token"], to_cfg["decimals"], amount_in,
                                  slippage_pct, pa_cs)
    full = bytes.fromhex(tx_built["data"][2:])
    selector_hex, data_bytes = "0x" + full[:4].hex(), full[4:]
    _exec, _src, _dest, from_amt, min_out = _paraswap_decode_and_check(
        selector_hex, data_bytes, from_cfg["token"], to_cfg["token"], amount_in, pa_cs)
    # Same executor-patching as swap-debt (see cmd_swap_debt for full rationale).
    _PARASWAP_FALLBACK_EXECUTOR = "0x000010036C0190E009a000d0fc3541100A07380A"
    if _exec is not None and _exec.lower() not in PARASWAP_EXECUTORS:
        fallback_bytes = bytes(12) + bytes.fromhex(_PARASWAP_FALLBACK_EXECUTOR[2:])
        data_bytes = fallback_bytes + data_bytes[32:]
        print(f"  ⚠ Executor {_exec} not whitelisted; patching to {_PARASWAP_FALLBACK_EXECUTOR}")
        _paraswap_decode_and_check(selector_hex, data_bytes, from_cfg["token"], to_cfg["token"],
                                   amount_in, pa_cs)

    print(f"Swap {amount} {from_sym} -> {to_sym} on Prime Account {pa_cs}  (via ParaSwap/Velora)")
    print(f"  Router method: {price_route['contractMethod']} ({selector_hex})")
    print(f"  Augustus router: {tx_built['to']}")
    print(f"  Expected out: {quoted_out / 10**to_cfg['decimals']:.6f} {to_sym}")
    if min_out is not None:
        print(f"  Min out (@{slippage_pct}% slippage): {min_out / 10**to_cfg['decimals']:.6f} {to_sym}")
    print(f"  ParaSwap srcUSD ${price_route.get('srcUSD','?')} -> destUSD ${price_route.get('destUSD','?')}")
    print("  Facet enforces a 5% hard slippage cap (RedStone-priced) on top of this.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    feeds = prime_account_price_feeds(account)
    for s in (from_sym, to_sym):
        if s not in feeds:
            feeds.append(s)
    payload = build_redstone_payload(feeds)
    base_calldata = account.encode_abi("paraSwapV6", args=[full[:4], data_bytes])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Swap {amount} {from_sym} -> {to_sym} "
          f"{'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    return ok

def cmd_swap(from_sym: str, to_sym: str, amount: float, slippage_pct: float = 1.0,
             via: str = "yak", execute: bool = False):
    """Swap one in-account asset for another via the Prime Account, on either aggregator.

    The swap sells the account's in-account balance of --from for --to.
      via='yak'      — YieldYakSwapFacet.yakSwap; route (path+adapters) from the YieldYak
                       router's findBestPath, each adapter whitelisted on the facet.
      via='paraswap' — ParaSwapFacet.paraSwapV6; calldata built from the ParaSwap/Velora
                       v6.2 API (/prices then /transactions) with the Prime Account as
                       swapper+receiver, split into (selector, data) for the facet.
    Both carry remainsSolvent, so the --execute path appends a RedStone signed-price
    payload to the calldata.
    """
    via = (via or "yak").lower()
    if via not in ("yak", "paraswap"):
        print(f"Unknown --via '{via}'. Choose 'yak' or 'paraswap'.")
        return
    from_sym, to_sym = from_sym.upper(), to_sym.upper()
    if from_sym not in SWAP_ASSETS:
        print(f"Unknown --from asset '{from_sym}'. Choose from: {', '.join(SWAP_ASSETS)}")
        return
    if to_sym not in SWAP_ASSETS:
        print(f"Unknown --to asset '{to_sym}'. Choose from: {', '.join(SWAP_ASSETS)}")
        return
    if from_sym == to_sym:
        print("--from and --to must differ.")
        return

    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to swap.")
        print("Create and fund one first: deltaprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    from_cfg, to_cfg = SWAP_ASSETS[from_sym], SWAP_ASSETS[to_sym]
    amount_in = int(amount * 10**from_cfg["decimals"])

    # In-account balance check (oracle-free view).
    in_balance = account.functions.getBalance(asset_b32(from_sym)).call()
    if amount_in > in_balance:
        print(f"Prime Account holds only {in_balance / 10**from_cfg['decimals']:.6f} {from_sym} "
              f"in-account; cannot swap {amount} {from_sym}.")
        print("Fund or borrow more of the asset into the account first.")
        return

    if via == "paraswap":
        return _swap_via_paraswap(w3, acct, pa_cs, account, from_sym, to_sym, from_cfg, to_cfg,
                                  amount, amount_in, slippage_pct, execute)

    # Route via YieldYak router.
    amounts, adapters, path = _yak_find_best_path(w3, amount_in, from_cfg["token"], to_cfg["token"])
    expected_out = amounts[-1]
    min_out = int(expected_out * (1 - slippage_pct / 100))

    # Every adapter must be whitelisted on the facet, else yakSwap reverts.
    not_whitelisted = [a for a in adapters
                       if not account.functions.isWhitelistedAdapterOptimized(
                           Web3.to_checksum_address(a)).call()]

    print(f"Swap {amount} {from_sym} -> {to_sym} on Prime Account {pa}")
    print(f"  Route ({len(adapters)} hop{'s' if len(adapters) != 1 else ''}): "
          f"{' -> '.join(path)}")
    print(f"  Adapters: {', '.join(adapters)}")
    print(f"  Expected out: {expected_out / 10**to_cfg['decimals']:.6f} {to_sym}")
    print(f"  Min out (@{slippage_pct}% slippage): {min_out / 10**to_cfg['decimals']:.6f} {to_sym}")
    if not_whitelisted:
        print(f"  ✗ Non-whitelisted adapter(s) in route: {', '.join(not_whitelisted)}")
        print("  yakSwap would revert. Refusing.")
        return
    print("  All adapters whitelisted.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # remainsSolvent gating: append a RedStone signed-price payload covering the
    # account's assets + debts plus the swap's from/to. Fetched fresh at send time —
    # the payload is only valid for ~3 minutes (RedStone staleness window).
    feeds = prime_account_price_feeds(account)
    for s in (from_sym, to_sym):
        if s not in feeds:
            feeds.append(s)
    payload = build_redstone_payload(feeds)

    base_calldata = account.encode_abi("yakSwap", args=[
        amount_in, min_out,
        [Web3.to_checksum_address(p) for p in path],
        [Web3.to_checksum_address(a) for a in adapters],
    ])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Swap {amount} {from_sym} -> {to_sym} "
          f"{'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    return ok

# ─── Swap debt / refinance (SwapDebtFacet) ───────────────────────────────────
# swapDebtParaSwap borrows _borrowAmount of _toAsset, ParaSwaps it into _fromAsset, and
# repays _repayAmount of the _fromAsset debt — all in one tx. The facet hard-caps the USD
# value difference between the repay and borrow legs at 5% (RedStone-priced) and requires
# the ParaSwap quote's fromAmount to equal _borrowAmount exactly.

_SYMBOL_TO_POOL = {cfg["symbol"]: name for name, cfg in POOLS.items()}

def _read_prices_usd(w3, account, symbols, payload):
    """RedStone-gated getPrices read for `symbols` (1e8-scaled USD), payload appended."""
    data = account.encode_abi("getPrices", args=[[asset_b32(s) for s in symbols]]) + payload.hex()
    raw = w3.eth.call({"to": account.address, "data": data})
    return w3.codec.decode(["uint256[]"], bytes(raw))[0]

def cmd_swap_debt(from_sym: str, to_sym: str, amount: float, slippage_pct: float = 1.0,
                  execute: bool = False):
    """Refinance debt from --from (existing debt) into --to (new debt) via
    SwapDebtFacet.swapDebtParaSwap. --amount is how much of the OLD (--from) debt to repay,
    in --from units. We value-match the new borrow to the repay using the facet's own
    RedStone prices, build the ParaSwap calldata for the internal --to -> --from swap, and
    preview the 5% USD-diff cap. RedStone-gated on execute."""
    from_sym, to_sym = from_sym.upper(), to_sym.upper()
    if from_sym not in SWAP_ASSETS:
        print(f"Unknown --from (old debt) asset '{from_sym}'. Choose from: {', '.join(SWAP_ASSETS)}")
        return
    if to_sym not in SWAP_ASSETS:
        print(f"Unknown --to (new debt) asset '{to_sym}'. Choose from: {', '.join(SWAP_ASSETS)}")
        return
    if from_sym == to_sym:
        print("--from and --to must differ.")
        return

    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — no debt to swap.")
        print("Swap-debt only applies to a Prime Account with an outstanding loan.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    from_cfg, to_cfg = SWAP_ASSETS[from_sym], SWAP_ASSETS[to_sym]

    # Current borrowed of the OLD debt asset, read from its pool (the facet caps
    # _repayAmount to exactly this).
    from_pool, _, _ = get_pool_contract(_SYMBOL_TO_POOL[from_sym])
    borrowed = from_pool.functions.getBorrowed(pa_cs).call()
    if borrowed == 0:
        print(f"Prime Account has no {from_sym} debt to refinance.")
        return
    repay_amount = min(int(amount * 10**from_cfg["decimals"]), borrowed)

    # Value-match the new borrow to the repay using the facet's own RedStone prices, so the
    # 5% USD-diff cap is computed against the same numbers the contract will see.
    feeds = prime_account_price_feeds(account)
    for s in (from_sym, to_sym):
        if s not in feeds:
            feeds.append(s)
    payload = build_redstone_payload(feeds)
    price_from, price_to = _read_prices_usd(w3, account, [from_sym, to_sym], payload)
    # borrow_amount such that its USD value ≈ repay USD value:
    #   repay_usd  = price_from * repay_amount  / 10**from_dec
    #   borrow_amt = repay_usd * 10**to_dec / price_to
    borrow_amount = (price_from * repay_amount * 10**to_cfg["decimals"]) // (price_to * 10**from_cfg["decimals"])
    if borrow_amount == 0:
        print("Computed borrow amount rounds to zero — repay amount too small. Refusing.")
        return

    # USD values + diff (mirror of the facet's maxDiff math, prices 1e8-scaled).
    repay_usd = price_from * repay_amount / 10**from_cfg["decimals"] / 1e8
    borrow_usd = price_to * borrow_amount / 10**to_cfg["decimals"] / 1e8
    diff_bps = (abs(repay_usd - borrow_usd) / max(repay_usd, borrow_usd)) * 10000 if max(repay_usd, borrow_usd) else 0

    # ParaSwap calldata for the INTERNAL swap: sell exactly borrow_amount of _toAsset for
    # _fromAsset (facet requires fromAmount == _borrowAmount). srcToken=to, destToken=from.
    price_route = _paraswap_price_route(to_cfg["token"], to_cfg["decimals"],
                                        from_cfg["token"], from_cfg["decimals"], borrow_amount, pa_cs)
    quoted_out = int(price_route["destAmount"])
    tx_built = _paraswap_build_tx(price_route, to_cfg["token"], to_cfg["decimals"],
                                  from_cfg["token"], from_cfg["decimals"], borrow_amount,
                                  slippage_pct, pa_cs)
    full = bytes.fromhex(tx_built["data"][2:])
    selector_hex, data_bytes = "0x" + full[:4].hex(), full[4:]
    _exec, _src, _dest, swap_from_amt, swap_min_out = _paraswap_decode_and_check(
        selector_hex, data_bytes, to_cfg["token"], from_cfg["token"], borrow_amount, pa_cs)

    # If the ParaSwap API returned a new executor not on the DeltaPrime whitelist, patch
    # it to EXECUTOR_3 (0x00001003…A07380A) — the only legacy executor whose calldata
    # format is compatible with the current API's output (tested on-chain 2026-05-28).
    _PARASWAP_FALLBACK_EXECUTOR = "0x000010036C0190E009a000d0fc3541100A07380A"
    if _exec.lower() not in PARASWAP_EXECUTORS:
        fallback_bytes = bytes(12) + bytes.fromhex(_PARASWAP_FALLBACK_EXECUTOR[2:])
        data_bytes = fallback_bytes + data_bytes[32:]
        print(f"  ⚠ Executor {_exec} not whitelisted; patching to {_PARASWAP_FALLBACK_EXECUTOR}")
        _paraswap_decode_and_check(selector_hex, data_bytes, to_cfg["token"], from_cfg["token"],
                                   borrow_amount, pa_cs)

    print(f"Swap debt on Prime Account {pa}")
    print(f"  Refinance: {from_sym} debt -> {to_sym} debt")
    print(f"  Old debt ({from_sym}): {borrowed / 10**from_cfg['decimals']:.6f} total; "
          f"repaying {repay_amount / 10**from_cfg['decimals']:.6f}")
    print(f"  New debt ({to_sym}): borrow {borrow_amount / 10**to_cfg['decimals']:.6f}")
    print(f"  Internal swap (ParaSwap): {borrow_amount / 10**to_cfg['decimals']:.6f} {to_sym} "
          f"-> {from_sym}  ({price_route['contractMethod']} {selector_hex})")
    print(f"  Expected {from_sym} out: {quoted_out / 10**from_cfg['decimals']:.6f}", end="")
    if swap_min_out is not None:
        print(f" (min {swap_min_out / 10**from_cfg['decimals']:.6f} @{slippage_pct}% slippage)")
    else:
        print()
    print(f"  RedStone USD: repay ${repay_usd:,.4f} vs borrow ${borrow_usd:,.4f} "
          f"-> diff {diff_bps:.1f} bps (facet cap: 500 bps / 5%)")
    if diff_bps > 500:
        print("  ✗ USD-value diff exceeds the facet's 5% cap. swapDebtParaSwap would revert. Refusing.")
        return
    if quoted_out < repay_amount:
        print(f"  Note: quoted {from_sym} out is below the repay target; the facet repays "
              f"min(swap output, {repay_amount / 10**from_cfg['decimals']:.6f}, debt) — any shortfall "
              "leaves residual old debt.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    base_calldata = account.encode_abi("swapDebtParaSwap", args=[
        asset_b32(from_sym), asset_b32(to_sym), repay_amount, borrow_amount,
        full[:4], data_bytes,
    ])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 4000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Swap debt {from_sym} -> {to_sym} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

# ─── Collateral withdrawal (WithdrawalIntentFacet) ──────────────────────────
# Pulling collateral out of the Prime Account to the EOA is a two-step, time-delayed
# flow — there is NO instant withdraw. createWithdrawalIntent registers an intent,
# then executeWithdrawalIntent pulls it after maturity. Window (from source, also
# exposed on-chain via the IntentInfo flags): actionableAt = createdAt + 24h,
# expiresAt = actionableAt + 48h. So an intent is executable in a 24h–72h window.

def _fmt_window(actionable_at: int, expires_at: int) -> str:
    """Human one-liner for an intent's maturity window, anchored to chain time."""
    now = int(time.time())
    def rel(ts):
        d = ts - now
        sign = "in" if d >= 0 else "ago"
        d = abs(d)
        h, m = d // 3600, (d % 3600) // 60
        span = f"{h}h{m:02d}m" if h else f"{m}m"
        return f"{sign} {span}" if sign == "in" else f"{span} {sign}"
    a = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(actionable_at))
    e = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires_at))
    return f"actionable {a} ({rel(actionable_at)}), expires {e} ({rel(expires_at)})"

def cmd_withdraw_collateral(pool_name: str, amount: float, execute: bool = False):
    """Step 1 of collateral withdrawal: register a WithdrawalIntent on the Prime
    Account via createWithdrawalIntent(bytes32 asset, uint256 amount). Per the
    capabilities doc this does NOT need a RedStone payload (the solvency check is
    deferred to the execute step). After ~24h the intent becomes executable for a 48h
    window (see execute-withdrawal). Preview by default."""
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to withdraw.")
        print("Collateral withdrawal only applies to a Prime Account.")
        return

    symbol = pool_to_asset_symbol(pool_name)
    amount_wei = int(amount * 10**cfg["decimals"])
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    # getAvailableBalance is oracle-free: in-account balance minus staked minus pending
    # intents — the amount still free to register. A useful guard for the preview.
    available = account.functions.getAvailableBalance(asset_b32(symbol)).call()
    print(f"Create withdrawal intent: {amount} {symbol} from Prime Account {pa}")
    print(f"  Available to withdraw now: {available / 10**cfg['decimals']:.6f} {symbol}")
    if amount_wei > available:
        print(f"  ✗ Requested {amount} {symbol} exceeds available balance. Refusing.")
        return
    print(f"  Calls createWithdrawalIntent(bytes32 '{symbol}', {amount_wei}) — no RedStone payload needed.")
    print("  Delayed flow: becomes executable ~24h later, then has a 48h window (24h-72h total).")
    print("  Run `execute-withdrawal --pool <p>` after maturity to pull the funds to the wallet.")

    if not execute:
        print("Run with --execute to broadcast (registers the intent on-chain).")
        return

    tx = account.functions.createWithdrawalIntent(asset_b32(symbol), amount_wei).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 1000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Withdrawal intent {'registered' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_withdrawal_intents():
    """Read-only: list pending withdrawal intents per owned asset, with per-asset
    available balance. Uses the oracle-free WithdrawalIntentFacet views getUserIntents /
    getAvailableBalance / getTotalIntentAmount — no RedStone, no tx."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Owner wallet: {acct.address}")
    if not pa:
        print("No Prime Account yet — no withdrawal intents.")
        return

    print(f"Prime Account: {pa}")
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    owned = account.functions.getAllOwnedAssets().call()
    if not owned:
        print("  Account holds no assets — nothing to withdraw.")
        return

    any_pending = False
    for a in owned:
        sym = a.rstrip(b"\x00").decode(errors="replace")
        dec = _asset_decimals(sym)
        available = account.functions.getAvailableBalance(a).call()
        total_intent = account.functions.getTotalIntentAmount(a).call()
        intents = account.functions.getUserIntents(a).call()
        print(f"  {sym}: available {available / 10**dec:,.6f}, "
              f"pending intents {total_intent / 10**dec:,.6f}")
        for idx, (amt, actionable_at, expires_at, is_pending, is_actionable, is_expired) in enumerate(intents):
            any_pending = True
            if is_expired:
                state = "EXPIRED"
            elif is_actionable:
                state = "READY to execute"
            elif is_pending:
                state = "maturing"
            else:
                state = "inactive"
            print(f"    [{idx}] {amt / 10**dec:,.6f} {sym} — {state}")
            print(f"         {_fmt_window(actionable_at, expires_at)}")
    if not any_pending:
        print("  No pending withdrawal intents.")

def cmd_execute_withdrawal(pool_name: str, index: int = None, execute: bool = False):
    """Step 2 of collateral withdrawal: executeWithdrawalIntent(bytes32 asset,
    uint256[] indices) pulls matured intent(s) to the EOA. This DOES carry remainsSolvent
    (+ canRepayDebtFully), so --execute appends a fresh RedStone price payload. Refuses
    any intent that has not matured (isActionable=false) or has expired. --index selects
    one intent; default executes all currently-actionable intents for the asset (indices
    passed strictly increasing, as the contract requires)."""
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to execute.")
        return

    symbol = pool_to_asset_symbol(pool_name)
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    intents = account.functions.getUserIntents(asset_b32(symbol)).call()
    if not intents:
        print(f"No withdrawal intents registered for {symbol}.")
        print("Register one first: withdraw-collateral --pool <p> --amount <n> --execute")
        return

    # Pick indices: a specific --index, or every currently-actionable intent.
    if index is not None:
        if index < 0 or index >= len(intents):
            print(f"--index {index} out of range (asset has {len(intents)} intent(s)).")
            return
        candidates = [index]
    else:
        candidates = [i for i, it in enumerate(intents) if it[4]]  # isActionable

    print(f"Execute withdrawal of {symbol} from Prime Account {pa}")
    ready = []
    for i in candidates:
        amt, actionable_at, expires_at, is_pending, is_actionable, is_expired = intents[i]
        print(f"  [{i}] {amt / 10**cfg['decimals']:,.6f} {symbol} — "
              f"{'EXPIRED' if is_expired else 'READY' if is_actionable else 'NOT MATURED'}")
        print(f"       {_fmt_window(actionable_at, expires_at)}")
        if is_expired:
            print(f"       ✗ intent [{i}] has expired — cannot execute (cancel/clear it instead).")
        elif not is_actionable:
            print(f"       ✗ intent [{i}] has not matured yet — refusing.")
        else:
            ready.append(i)

    if not ready:
        print("  No matured, non-expired intents to execute. Refusing.")
        return
    ready.sort()  # contract requires strictly-increasing indices
    print(f"  Will execute indices {ready} via executeWithdrawalIntent(bytes32 '{symbol}', {ready}).")
    print("  Carries remainsSolvent + canRepayDebtFully — appends a fresh RedStone payload.")

    if not execute:
        print("Run with --execute to broadcast (pulls the funds to the wallet).")
        return

    feeds = prime_account_price_feeds(account)
    if symbol not in feeds:
        feeds.append(symbol)
    payload = build_redstone_payload(feeds)
    base_calldata = account.encode_abi("executeWithdrawalIntent", args=[asset_b32(symbol), ready])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Execute withdrawal {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

# ─── GMX V2 GM / GM+ LP (GmxV2FacetAvalanche / GmxV2PlusFacetAvalanche) ───────
# GM tokens are GMX V2 market LP (two-sided long+short for GM, single-sided for GM+).
# The deposit/withdraw flow is PAYABLE + ASYNC: the facet forwards a GMX execution fee as
# msg.value (== the executionFee arg) to the GMX ExchangeRouter, which queues the request;
# a GMX keeper executes it some blocks later and fires a callback. The Prime Account is
# FROZEN for that market until the callback resolves — the position does NOT appear (or
# disappear) instantly. minGmAmount / min long+short token amounts are slippage floors,
# hard-bounded to ±5% of the oracle estimate by the facet's isWithinBounds check.

def _gmx_datastore_key(name: str) -> bytes:
    """GMX DataStore key = keccak256(abi.encode(string name)). The gas-limit params live
    under these string keys (see gmx-synthetics Keys.sol)."""
    return Web3.keccak(abi_encode(["string"], [name]))

def _estimate_gmx_execution_fee(w3, is_deposit: bool, buffer_mult: float = 2.0):
    """Estimate the GMX V2 execution fee (in wei of AVAX) the keeper will require, mirroring
    gmx-synthetics GasUtils. The keeper executes only when executionFee >= adjustedGasLimit *
    tx.gasprice AT EXECUTION TIME, so we pad the current gas price by `buffer_mult` to survive
    a gas-price rise between submission and keeper execution. Any excess is refunded by GMX to
    the receiver (the Prime Account). Returns (fee_wei, detail dict)."""
    ds = w3.eth.contract(address=Web3.to_checksum_address(GMX_DATASTORE), abi=GMX_DATASTORE_ABI)
    base_gas = ds.functions.getUint(_gmx_datastore_key(
        "DEPOSIT_GAS_LIMIT" if is_deposit else "WITHDRAWAL_GAS_LIMIT")).call()
    base_amount = ds.functions.getUint(_gmx_datastore_key("ESTIMATED_GAS_FEE_BASE_AMOUNT_V2_1")).call()
    per_oracle = ds.functions.getUint(_gmx_datastore_key("ESTIMATED_GAS_FEE_PER_ORACLE_PRICE")).call()
    mult_factor = ds.functions.getUint(_gmx_datastore_key("ESTIMATED_GAS_FEE_MULTIPLIER_FACTOR")).call()
    # estimateExecute{Deposit,Withdrawal}GasLimit (no swap path) = baseGasLimit + callbackGasLimit.
    # adjustGasLimitForEstimate: base + perOracle*oracleCount + applyFactor(estimate, multiplier),
    # applyFactor(v, f) = v * f / 1e30. Deposit/withdraw with no swaps prices 2 oracle feeds.
    oracle_count = 2
    estimate = base_gas + GMX_CALLBACK_GAS_LIMIT
    adjusted = base_amount + per_oracle * oracle_count + estimate * mult_factor // 10**30
    # Floor the gas price: Avalanche's live base fee (seen at 0.01-0.02 gwei) is far below the
    # gas price a GMX keeper uses at execution, so an unfloored estimate yields a fee the keeper
    # would never accept (the request expires and refunds without minting the GM tokens). 25 gwei
    # is Avalanche's normal-load price; with buffer_mult this comfortably clears GMX's requirement
    # (~0.08 AVAX, matching the DeltaPrime app). Any excess is refunded by GMX to the account.
    gas_price = max(w3.eth.gas_price, 25 * 10**9)
    fee_wei = int(adjusted * gas_price * buffer_mult)
    return fee_wei, {"adjusted_gas": adjusted, "gas_price": gas_price, "buffer_mult": buffer_mult}

def _gmx_underlying_price_usd(w3, account, payload, symbol: str) -> int:
    """1e8-scaled USD price of a lending underlying (AVAX/BTC/ETH/USDC) via the
    RedStone-gated SolvencyFacet.getPrices. (The GM token symbol itself has no SolvencyFacet
    feed — getPrices reverts 0xec459bc0 on it — so GM prices come from the gateway median.)"""
    return _read_prices_usd(w3, account, [symbol], payload)[0]

def _gmx_gm_price_usd(gm_feed: str) -> float:
    """USD price of a GM/GM+ token, taken as the median of the RedStone gateway packages for
    its feed id. This is the same on-demand value the facet aggregates from the calldata
    payload, so a minGmAmount computed against it matches what the on-chain isWithinBounds
    check sees (both read the same gateway snapshot in the same ~3-minute window)."""
    import statistics
    gw = _redstone_fetch_packages()
    vals = []
    for pkg in gw.get(gm_feed, []):
        for dp in pkg["dataPoints"]:
            if dp["dataFeedId"] == gm_feed:
                vals.append(float(dp["value"]))
    if not vals:
        raise RuntimeError(f"RedStone gateway has no GM feed '{gm_feed}'")
    return statistics.median(vals)

def _gmx_market_reserve_split(w3, mkt: dict, p_long: int, p_short: int):
    """Long/short USD weighting of a two-sided GM market, from the underlying token balances
    held by the market contract. GMX redeems GM pro-rata across this composition, so it's how
    a withdrawal's min long/short token floors are split. Returns (long_frac, short_frac)."""
    erc = json.loads('[{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')
    long_cfg, short_cfg = SWAP_ASSETS[mkt["long"]], SWAP_ASSETS[mkt["short"]]
    gm = Web3.to_checksum_address(mkt["gm_token"])
    long_tok = w3.eth.contract(address=Web3.to_checksum_address(long_cfg["token"]), abi=erc)
    short_tok = w3.eth.contract(address=Web3.to_checksum_address(short_cfg["token"]), abi=erc)
    long_usd = long_tok.functions.balanceOf(gm).call() / 10**long_cfg["decimals"] * p_long / 1e8
    short_usd = short_tok.functions.balanceOf(gm).call() / 10**short_cfg["decimals"] * p_short / 1e8
    tot = long_usd + short_usd
    if tot <= 0:
        return 0.5, 0.5
    return long_usd / tot, short_usd / tot

GMX_READER_ABI = json.loads('''[
  {"inputs":[{"internalType":"contract DataStore","name":"dataStore","type":"address"},{"internalType":"address","name":"key","type":"address"}],"name":"getMarket","outputs":[{"components":[{"internalType":"address","name":"marketToken","type":"address"},{"internalType":"address","name":"indexToken","type":"address"},{"internalType":"address","name":"longToken","type":"address"},{"internalType":"address","name":"shortToken","type":"address"}],"internalType":"struct Market.Props","name":"","type":"tuple"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"internalType":"contract DataStore","name":"dataStore","type":"address"},{"components":[{"internalType":"address","name":"marketToken","type":"address"},{"internalType":"address","name":"indexToken","type":"address"},{"internalType":"address","name":"longToken","type":"address"},{"internalType":"address","name":"shortToken","type":"address"}],"internalType":"struct Market.Props","name":"market","type":"tuple"},{"components":[{"components":[{"internalType":"uint256","name":"min","type":"uint256"},{"internalType":"uint256","name":"max","type":"uint256"}],"internalType":"struct Price.Props","name":"indexTokenPrice","type":"tuple"},{"components":[{"internalType":"uint256","name":"min","type":"uint256"},{"internalType":"uint256","name":"max","type":"uint256"}],"internalType":"struct Price.Props","name":"longTokenPrice","type":"tuple"},{"components":[{"internalType":"uint256","name":"min","type":"uint256"},{"internalType":"uint256","name":"max","type":"uint256"}],"internalType":"struct Price.Props","name":"shortTokenPrice","type":"tuple"}],"internalType":"struct MarketUtils.MarketPrices","name":"prices","type":"tuple"},{"internalType":"uint256","name":"marketTokenAmount","type":"uint256"},{"internalType":"address","name":"uiFeeReceiver","type":"address"},{"internalType":"enum ISwapPricingUtils.SwapPricingType","name":"swapPricingType","type":"uint8"}],"name":"getWithdrawalAmountOut","outputs":[{"internalType":"uint256","name":"","type":"uint256"},{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]''')

def _gmx_withdrawal_amount_out(w3, account, mkt: dict, gm_amount: int, p_long: int, p_short: int):
    """Simulate a withdrawal via the GMX Reader to get the exact expected long/short token
    output amounts (in the tokens' native decimals). This eliminates the divergence between
    the reserve-based split estimate and the actual market redemption that causes keeper
    cancellations with InsufficientOutputAmount."""
    reader = w3.eth.contract(address=Web3.to_checksum_address(GMX_READER), abi=GMX_READER_ABI)
    gm_addr = Web3.to_checksum_address(mkt["gm_token"])
    # Get the on-chain market struct (marketToken, indexToken, longToken, shortToken)
    market = reader.functions.getMarket(GMX_DATASTORE, gm_addr).call()
    # Convert RedStone 1e8 USD prices to GMX's internal 1e30 precision
    PRICE_SCALE = 10 ** 22  # 1e30 / 1e8
    price_long = int(p_long * PRICE_SCALE)
    price_short = price_long if mkt["plus"] else int(p_short * PRICE_SCALE)
    # Price.Props(min, max) — both set to the same oracle median
    long_pp = (price_long, price_long)
    short_pp = (price_short, price_short)
    # MarketPrices(indexTokenPrice, longTokenPrice, shortTokenPrice) — index==long for all GMX markets
    prices = (long_pp, long_pp, short_pp)
    long_out, short_out = reader.functions.getWithdrawalAmountOut(
        GMX_DATASTORE, market, prices, gm_amount, ZERO_ADDRESS, 0
    ).call()
    return long_out, short_out

def gather_gmx(w3, account):
    """Read-only GM / GM+ LP positions on a Prime Account. Per owned market: raw GM balance,
    balance after the accrued performance fee (getGmTokenBalanceAfterFees), annualised
    performance (getGm[Plus]Performance), and best-effort USD via the RedStone gateway GM
    price. Both views are RedStone-gated — a fresh signed payload (GM feed + underlyings) is
    appended per market. Returns a list of position dicts (empty if none). Shared by
    cmd_gmx_positions (print) and cmd_defi (--json)."""
    pa_cs = account.address
    owned = {a.rstrip(b"\x00").decode(errors="replace") for a in account.functions.getAllOwnedAssets().call()}
    erc = json.loads('[{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')
    positions = []
    for key, mkt in GMX_MARKETS.items():
        gm_cs = Web3.to_checksum_address(mkt["gm_token"])
        raw_bal = w3.eth.contract(address=gm_cs, abi=erc).functions.balanceOf(pa_cs).call()
        gm_sym = mkt["gm_feed"]
        if raw_bal == 0 and gm_sym not in owned:
            continue
        pos = {"market": key, "kind": "GM+" if mkt["plus"] else "GM", "gm_feed": gm_sym,
               "gm_token": mkt["gm_token"], "raw": raw_bal, "decimals": GM_TOKEN_DECIMALS,
               "balance": f"{raw_bal / 10**GM_TOKEN_DECIMALS:.6f}",
               "after_fees": None, "perf_pct": None, "gm_price_usd": None, "usd": None}
        # Feeds: GM symbol + underlyings, deduped (GM+ long==short).
        feeds = [gm_sym, mkt["long"]] + ([] if mkt["plus"] else [mkt["short"]])
        try:
            payload = build_redstone_payload(feeds)
            perf_fn = "getGmPlusPerformance" if mkt["plus"] else "getGmPerformance"
            after_fees = redstone_view_call(w3, account, "getGmTokenBalanceAfterFees", payload, args=[gm_cs])[0]
            perf = redstone_view_call(w3, account, perf_fn, payload, args=[gm_cs])[0]
            gm_usd = _gmx_gm_price_usd(gm_sym)
            pos["after_fees"] = after_fees / 10**GM_TOKEN_DECIMALS
            pos["perf_pct"] = perf / 1e16
            pos["gm_price_usd"] = gm_usd
            pos["usd"] = after_fees / 10**GM_TOKEN_DECIMALS * gm_usd
        except Exception as e:
            pos["error"] = type(e).__name__
        positions.append(pos)
    return positions

def cmd_gmx_positions():
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Owner wallet: {acct.address}")
    if not pa:
        print("No Prime Account yet — no GMX LP positions.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Prime Account: {pa}")

    positions = gather_gmx(w3, account)
    if not positions:
        print("  No GM / GM+ LP positions.")
        return
    for pos in positions:
        print(f"  [{pos['market']}] {pos['kind']} {pos['gm_feed']}  ({pos['gm_token']})")
        print(f"    Raw GM balance:        {float(pos['balance']):,.6f}")
        if pos.get("error"):
            print(f"    Performance/value:     RedStone fetch/call failed ({pos['error']})")
            continue
        print(f"    Balance after fees:    {pos['after_fees']:,.6f} "
              f"(perf fee accrued: {pos['raw'] / 10**GM_TOKEN_DECIMALS - pos['after_fees']:,.6f})")
        print(f"    Annualised perf:       {pos['perf_pct']:,.2f}%")
        print(f"    GM price (gateway):    ${pos['gm_price_usd']:,.6f}  -> position ~${pos['usd']:,.2f}")

def cmd_gmx_deposit(market: str, amount: float, is_long: bool = True,
                    slippage_pct: float = 1.0, fee_buffer: float = 2.0, execute: bool = False):
    """Open/add a GMX V2 GM (two-sided) or GM+ (single-sided) LP position by depositing an
    in-account underlying. Two-sided markets take --side long|short (long = volatile leg,
    short = USDC); GM+ markets ignore --side. minGmAmount is set to the fair GM amount
    (depositUSD / gmPrice) minus --slippage, kept within the facet's ±5% isWithinBounds band.
    PAYABLE + ASYNC: pays a GMX execution fee as msg.value and queues the request; a GMX
    keeper mints the GM tokens later and the account is frozen until then. RedStone-gated on
    --execute."""
    if market not in GMX_MARKETS:
        print(f"Unknown --market '{market}'. Choose from: {', '.join(GMX_MARKETS)}")
        return
    if slippage_pct > GMX_MAX_SLIPPAGE_PCT:
        print(f"--slippage {slippage_pct}% exceeds the facet's {GMX_MAX_SLIPPAGE_PCT}% isWithinBounds cap; "
              "the deposit would revert InvalidMinOutputValue. Refusing.")
        return
    mkt = GMX_MARKETS[market]
    dep_sym = mkt["long"] if (mkt["plus"] or is_long) else mkt["short"]
    dep_cfg = SWAP_ASSETS[dep_sym]

    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to deposit.")
        print("Create and fund one first: deltaprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    amount_wei = int(amount * 10**dep_cfg["decimals"])
    in_balance = account.functions.getBalance(asset_b32(dep_sym)).call()
    if amount_wei > in_balance:
        print(f"Prime Account holds only {in_balance / 10**dep_cfg['decimals']:.6f} {dep_sym} "
              f"in-account; cannot deposit {amount} {dep_sym}.")
        print("Fund or borrow more of the asset into the account first.")
        return

    # Fair minGmAmount: depositUSD / gmPrice, scaled to GM decimals, minus slippage.
    # Two payloads: the underlyings-only one prices the deposit token via SolvencyFacet
    # getPrices (which has no feed for the GM symbol); the full one (GM feed + underlyings)
    # is what the write tx and the inline solvency simulation in the facet consume.
    underlyings = [mkt["long"]] + ([] if mkt["plus"] else [mkt["short"]])
    price_payload = build_redstone_payload(underlyings)
    # The facet runs an inline solvency simulation before minting that prices EVERY debt-registry
    # asset (the full pool set: AVAX/USDC/BTC/ETH/USDT/EUROC, even at zero balance/debt), each
    # needing 3 unique RedStone signers; a feed missing from the payload reverts the whole deposit
    # with InsufficientNumberOfUniqueSigners(0,3). So the write payload must cover the full solvency
    # feed set + the GM feed (deduped) — not just [gm_feed, long, short]. (price_payload above stays
    # underlyings-only; it's just the off-chain deposit-token price read, which has no GM feed.)
    _solv_feeds = prime_account_price_feeds(account)
    _extra_feeds = [f for f in ([mkt["gm_feed"]] + underlyings) if f not in _solv_feeds]
    payload = build_redstone_payload(_solv_feeds + _extra_feeds)
    p_dep = _gmx_underlying_price_usd(w3, account, price_payload, dep_sym)
    gm_usd = _gmx_gm_price_usd(mkt["gm_feed"])
    deposit_usd = amount_wei / 10**dep_cfg["decimals"] * p_dep / 1e8
    fair_gm = deposit_usd / gm_usd
    min_gm = int(fair_gm * (1 - slippage_pct / 100) * 10**GM_TOKEN_DECIMALS)
    exec_fee, fee_d = _estimate_gmx_execution_fee(w3, is_deposit=True, buffer_mult=fee_buffer)
    fn = mkt["deposit_fn"]

    kind = "GM+" if mkt["plus"] else "GM"
    leg = "" if mkt["plus"] else f" ({'long ' + dep_sym if is_long else 'short ' + dep_sym} leg)"
    print(f"GMX V2 {kind} deposit into [{market}] {mkt['gm_feed']} on Prime Account {pa}")
    print(f"  Deposit: {amount} {dep_sym}{leg}  (~${deposit_usd:,.2f})")
    print(f"  Fair GM out: {fair_gm:,.6f}  (GM ${gm_usd:,.6f}); minGmAmount @{slippage_pct}% "
          f"slippage: {min_gm / 10**GM_TOKEN_DECIMALS:,.6f}")
    print(f"  Facet: {fn}(...)  — isWithinBounds caps minGmAmount within ±5% of the oracle estimate.")
    print(f"  GMX execution fee: {exec_fee / 1e18:.6f} AVAX  (msg.value; {fee_d['buffer_mult']}x of "
          f"{fee_d['adjusted_gas']:,} gas @ {fee_d['gas_price'] / 1e9:.2f} gwei). Excess is refunded by GMX.")
    print("  ASYNC: queues a GMX deposit request; a GMX keeper mints the GM tokens in a later")
    print(f"  block. The Prime Account is FROZEN for {mkt['gm_feed']} until the keeper callback fires.")
    print(f"  The EOA must also hold ~{exec_fee / 1e18:.6f} AVAX (gas) on top of the execution fee.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    if mkt["plus"]:
        base_calldata = account.encode_abi(fn, args=[amount_wei, min_gm, exec_fee])
    else:
        base_calldata = account.encode_abi(fn, args=[is_long, amount_wei, min_gm, exec_fee])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data, "value": exec_fee,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} GMX {kind} deposit request {'submitted' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    if ok:
        print("  Request queued — wait for the GMX keeper callback to mint the GM tokens.")
    return ok

def cmd_gmx_withdraw(market: str, amount: float, slippage_pct: float = 1.0,
                     fee_buffer: float = 2.0, execute: bool = False):
    """Close/reduce a GMX V2 GM / GM+ LP position by burning --amount GM tokens. The min
    long/short token floors are derived from the burned GM's USD value split by the market's
    current reserve ratio (GM, pro-rata redemption) or 50/50 (GM+, single underlying), each
    minus --slippage and kept within the facet's ±5% isWithinBounds band. PAYABLE + ASYNC:
    pays a GMX execution fee as msg.value and queues the request; a GMX keeper returns the
    underlying(s) later and the account is frozen until then. RedStone-gated on --execute."""
    if market not in GMX_MARKETS:
        print(f"Unknown --market '{market}'. Choose from: {', '.join(GMX_MARKETS)}")
        return
    if slippage_pct > GMX_MAX_SLIPPAGE_PCT:
        print(f"--slippage {slippage_pct}% exceeds the facet's {GMX_MAX_SLIPPAGE_PCT}% isWithinBounds cap; "
              "the withdrawal would revert InvalidMinOutputValue. Refusing.")
        return
    mkt = GMX_MARKETS[market]

    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — no GM position to withdraw.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    gm_cs = Web3.to_checksum_address(mkt["gm_token"])
    erc = json.loads('[{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')
    gm_bal = w3.eth.contract(address=gm_cs, abi=erc).functions.balanceOf(pa_cs).call()
    gm_amount = int(amount * 10**GM_TOKEN_DECIMALS)
    if gm_bal == 0:
        print(f"Prime Account holds no {mkt['gm_feed']} GM tokens — nothing to withdraw.")
        return
    if gm_amount > gm_bal:
        print(f"Prime Account holds only {gm_bal / 10**GM_TOKEN_DECIMALS:,.6f} GM; "
              f"clamping withdrawal to that (the facet caps to balance anyway).")
        gm_amount = gm_bal

    # Underlyings-only payload for the SolvencyFacet price reads (no GM feed there).
    underlyings = [mkt["long"]] + ([] if mkt["plus"] else [mkt["short"]])
    price_payload = build_redstone_payload(underlyings)
    # Write payload: the facet's inline solvency check prices the FULL debt registry
    # (every pool, even at zero balance), each needing 3 RedStone signers — so it must
    # carry prime_account_price_feeds + the GM feed, or it reverts with
    # InsufficientNumberOfUniqueSigners(0,3). (Same fix as cmd_gmx_deposit.)
    _solv_feeds = prime_account_price_feeds(account)
    _extra_feeds = [f for f in ([mkt["gm_feed"]] + underlyings) if f not in _solv_feeds]
    payload = build_redstone_payload(_solv_feeds + _extra_feeds)
    long_cfg = SWAP_ASSETS[mkt["long"]]
    short_cfg = SWAP_ASSETS[mkt["short"]]
    p_long = _gmx_underlying_price_usd(w3, account, price_payload, mkt["long"])
    p_short = p_long if mkt["plus"] else _gmx_underlying_price_usd(w3, account, price_payload, mkt["short"])
    gm_usd = _gmx_gm_price_usd(mkt["gm_feed"])
    burn_usd = gm_amount / 10**GM_TOKEN_DECIMALS * gm_usd

    slip = 1 - slippage_pct / 100
    if mkt["plus"]:
        # GM+: single underlying, 50/50 split as before
        long_frac, short_frac = 0.5, 0.5
        min_long = int((burn_usd * long_frac) / (p_long / 1e8) * slip * 10**long_cfg["decimals"])
        min_short = int((burn_usd * short_frac) / (p_short / 1e8) * slip * 10**short_cfg["decimals"])
    else:
        # GM two-sided: use GMX Reader to get the correct redemption split ratio, then
        # apply it to burn_usd (the GM value our facet sees). This avoids the
        # reserve-based split divergence that causes keeper cancellation.
        expected_long, expected_short = _gmx_withdrawal_amount_out(w3, account, mkt, gm_amount, p_long, p_short)
        expected_long_usd = expected_long / 10**long_cfg["decimals"] * p_long / 1e8
        expected_short_usd = expected_short / 10**short_cfg["decimals"] * p_short / 1e8
        expected_total_usd = expected_long_usd + expected_short_usd
        long_frac = expected_long_usd / expected_total_usd if expected_total_usd > 0 else 0.5
        short_frac = expected_short_usd / expected_total_usd if expected_total_usd > 0 else 0.5
        min_long = int((burn_usd * long_frac) / (p_long / 1e8) * slip * 10**long_cfg["decimals"])
        min_short = int((burn_usd * short_frac) / (p_short / 1e8) * slip * 10**short_cfg["decimals"])
    exec_fee, fee_d = _estimate_gmx_execution_fee(w3, is_deposit=False, buffer_mult=fee_buffer)
    fn = mkt["withdraw_fn"]

    kind = "GM+" if mkt["plus"] else "GM"
    print(f"GMX V2 {kind} withdraw from [{market}] {mkt['gm_feed']} on Prime Account {pa}")
    print(f"  Burn: {gm_amount / 10**GM_TOKEN_DECIMALS:,.6f} GM  (~${burn_usd:,.2f}; GM ${gm_usd:,.6f})")
    if mkt["plus"]:
        tot_min = (min_long / 10**long_cfg["decimals"]) + (min_short / 10**short_cfg["decimals"])
        print(f"  Min {mkt['long']} out @{slippage_pct}% slippage: {tot_min:,.6f} "
              f"(facet sums minLong {min_long / 10**long_cfg['decimals']:,.6f} + "
              f"minShort {min_short / 10**short_cfg['decimals']:,.6f}, both the single underlying)")
    else:
        print(f"  Expected {mkt['long']}: {expected_long / 10**long_cfg['decimals']:,.6f}  Expected {mkt['short']}: {expected_short / 10**short_cfg['decimals']:,.6f}")
        print(f"  Min {mkt['long']} out @{slippage_pct}% slippage: {min_long / 10**long_cfg['decimals']:,.6f}")
        print(f"  Min {mkt['short']} out @{slippage_pct}% slippage: {min_short / 10**short_cfg['decimals']:,.6f}")
    print(f"  Facet: {fn}(...)  — isWithinBounds caps the min-out USD within ±5% of the oracle estimate.")
    print(f"  GMX execution fee: {exec_fee / 1e18:.6f} AVAX  (msg.value; {fee_d['buffer_mult']}x of "
          f"{fee_d['adjusted_gas']:,} gas @ {fee_d['gas_price'] / 1e9:.2f} gwei). Excess is refunded by GMX.")
    print("  ASYNC: queues a GMX withdrawal request; a GMX keeper returns the underlying(s) in")
    print(f"  a later block. The Prime Account is FROZEN for {mkt['gm_feed']} until the callback fires.")
    print(f"  The EOA must also hold ~{exec_fee / 1e18:.6f} AVAX (gas) on top of the execution fee.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    base_calldata = account.encode_abi(fn, args=[gm_amount, min_long, min_short, exec_fee])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data, "value": exec_fee,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} GMX {kind} withdrawal request {'submitted' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    if ok:
        print("  Request queued — wait for the GMX keeper callback to return the underlying(s).")

# ─── TraderJoe V2 Liquidity Book (TraderJoeV2AvalancheFacet) ─────────────────
# Concentrated liquidity across discrete price bins. addLiquidityTraderJoeV2 encodes a
# position via deltaIds[] (bin offsets from the active bin) + distributionX[]/distributionY[]
# (per-bin token weightings, each side summing to 1e18). The shape (Spot/Curve/Bid-Ask) is
# only those distribution arrays over the bin range. Token X (the base) is placed in bins
# at/above the active bin (deltaId >= 0); token Y (the quote) in bins at/below it
# (deltaId <= 0); the active bin (deltaId 0) can carry both. addLiquidity is RedStone-gated
# (remainsSolvent); removeLiquidity is not. Max 80 bins per Prime Account, cumulative.

LB_ONE = 10**18  # 1e18 == 100% in the distribution arrays


def _lb_pair_contract(w3, pair_addr):
    return w3.eth.contract(address=Web3.to_checksum_address(pair_addr), abi=LB_PAIR_ABI)


def _lb_shape_weights(n: int, shape: str) -> list:
    """Relative (un-normalised) weights for n bins on ONE token side, ordered from the
    active bin outward to the edge (index 0 == nearest the active price). Spot = uniform;
    Curve = concentrated near the active price (linear decay to the edge); Bid-Ask =
    concentrated at the edge (linear rise outward)."""
    if n <= 0:
        return []
    if n == 1 or shape == "spot":
        return [1.0] * n
    if shape == "curve":
        # Heaviest nearest the active bin, lightest at the far edge.
        return [float(n - i) for i in range(n)]
    if shape == "bidask":
        # Lightest nearest the active bin, heaviest at the far edge.
        return [float(i + 1) for i in range(n)]
    raise ValueError(f"unknown shape '{shape}'")


def _lb_normalise(weights: list) -> list:
    """Scale relative weights to integers summing to exactly 1e18 (the router requires each
    populated side to sum to 1e18). Any rounding remainder is folded into the largest bin so
    the sum is exact."""
    if not weights:
        return []
    total = sum(weights)
    out = [int(w / total * LB_ONE) for w in weights]
    out[max(range(len(out)), key=lambda i: out[i])] += LB_ONE - sum(out)
    return out


def _lb_build_distributions(active_id: int, range_bins: int, shape: str,
                            has_x: bool, has_y: bool):
    """Build (deltaIds, distributionX, distributionY) for a position spanning range_bins on
    each side of the active bin. tokenX fills deltaId>=0 bins, tokenY fills deltaId<=0; the
    active bin (deltaId 0) is shared. Distributions sum to 1e18 on each populated side."""
    deltas = list(range(-range_bins, range_bins + 1))

    # Per-side bin lists, ordered from the active bin outward (so shape weights line up).
    x_deltas = [d for d in deltas if d >= 0]                       # 0, +1, ... (outward)
    y_deltas = sorted([d for d in deltas if d <= 0], reverse=True)  # 0, -1, ... (outward)
    x_w = _lb_normalise(_lb_shape_weights(len(x_deltas), shape)) if has_x else [0] * len(x_deltas)
    y_w = _lb_normalise(_lb_shape_weights(len(y_deltas), shape)) if has_y else [0] * len(y_deltas)
    x_by_delta = dict(zip(x_deltas, x_w))
    y_by_delta = dict(zip(y_deltas, y_w))

    dist_x = [x_by_delta.get(d, 0) for d in deltas]
    dist_y = [y_by_delta.get(d, 0) for d in deltas]
    return deltas, dist_x, dist_y


def gather_lb(w3, account):
    """Read-only TraderJoe V2 LB positions on a Prime Account. getOwnedTraderJoeV2Bins
    (oracle-free) gives the (pair, binId) list; per pair we read the active bin and the
    account's share of each owned bin's reserves (balanceOf / totalSupply * getBin) to derive
    the per-token totals. No RedStone, no tx. Returns a list of per-pair dicts (empty if none).
    Shared by cmd_lb_positions (print) and cmd_defi (--json)."""
    pa_cs = account.address
    bins = account.functions.getOwnedTraderJoeV2Bins().call()
    if not bins:
        return []
    # Pair address -> tool key + token metadata (for labels + decimals).
    by_addr = {Web3.to_checksum_address(p["pair"]): (key, p) for key, p in TJ_LB_PAIRS.items()}
    # Group owned bins by pair, preserving the canonical pair token order.
    grouped = {}
    for pair, binid in bins:
        grouped.setdefault(Web3.to_checksum_address(pair), []).append(int(binid))

    out = []
    for pair_cs, ids in grouped.items():
        ids.sort()
        meta = by_addr.get(pair_cs)
        c = _lb_pair_contract(w3, pair_cs)
        try:
            active = c.functions.getActiveId().call()
            tx_addr = Web3.to_checksum_address(c.functions.getTokenX().call())
            ty_addr = Web3.to_checksum_address(c.functions.getTokenY().call())
        except Exception as e:
            out.append({"pair": pair_cs, "error": type(e).__name__})
            continue
        if meta:
            key, p = meta
            x_cfg, y_cfg = p["tokenX"], p["tokenY"]
            label = f"[{key}] {x_cfg['symbol']}/{y_cfg['symbol']} (binStep {p['binStep']})"
        else:
            # Unmapped (e.g. an aUSD pair) — fall back to raw addresses + 18 decimals.
            x_cfg = {"symbol": tx_addr[:8], "decimals": 18}
            y_cfg = {"symbol": ty_addr[:8], "decimals": 18}
            label = f"[pair {pair_cs}]"
        sum_x = sum_y = 0.0
        for binid in ids:
            try:
                bal = c.functions.balanceOf(pa_cs, binid).call()
                ts = c.functions.totalSupply(binid).call()
                rx, ry = c.functions.getBin(binid).call()
            except Exception:
                continue
            share = (bal / ts) if ts else 0
            sum_x += rx * share / 10**x_cfg["decimals"]
            sum_y += ry * share / 10**y_cfg["decimals"]
        out.append({"pair": pair_cs, "label": label, "active_bin": int(active),
                    "bins": len(ids), "bin_range": [ids[0], ids[-1]],
                    "token_x": {"symbol": x_cfg["symbol"], "amount": sum_x},
                    "token_y": {"symbol": y_cfg["symbol"], "amount": sum_y}})
    return out

def cmd_lb_positions():
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Owner wallet: {acct.address}")
    if not pa:
        print("No Prime Account yet — no TraderJoe LB positions.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Prime Account: {pa}")

    positions = gather_lb(w3, account)
    if not positions:
        print("  No TraderJoe V2 LB bins owned.")
        return
    total = sum(p.get("bins", 0) for p in positions)
    print(f"  Owned bins: {total} / {TJ_MAX_BINS} max")
    for p in positions:
        if p.get("error"):
            print(f"  [pair {p['pair']}] read failed ({p['error']})")
            continue
        lo, hi, act = p['bin_range'][0], p['bin_range'][1], p['active_bin']
        if lo <= act <= hi:
            rng = f"IN RANGE (active at bin {act - lo + 1} of {hi - lo + 1})"
        elif act < lo:
            rng = (f"OUT OF RANGE — active {lo - act} bin(s) below your range, "
                   f"position now ~all {p['token_x']['symbol']}, earning NO fees")
        else:
            rng = (f"OUT OF RANGE — active {act - hi} bin(s) above your range, "
                   f"position now ~all {p['token_y']['symbol']}, earning NO fees")
        print(f"  {p['label']}  {p['bins']} bin(s) — {rng}")
        print(f"    Totals: {p['token_x']['amount']:,.6f} {p['token_x']['symbol']} + "
              f"{p['token_y']['amount']:,.6f} {p['token_y']['symbol']} "
              f"across bins {lo}..{hi} (active {act}). Value skew: see `defi --json`.")


def cmd_lb_add(pair_key: str, amount_x: float, amount_y: float, shape: str = "spot",
               range_bins: int = 5, slippage_pct: float = 1.0, id_slippage: int = 5,
               execute: bool = False):
    """Add TraderJoe V2 LB liquidity for a whitelisted pair. Distributes amount_x (token X)
    and amount_y (token Y) across `range_bins` bins on each side of the active bin per the
    chosen shape (spot|curve|bidask). amountXMin/amountYMin are slippage floors on the
    total deposited; idSlippage guards the active-bin id moving before inclusion. Refuses if
    no Prime Account, if either token's in-account balance is short, or if the resulting bin
    count would exceed the 80-bin cap. RedStone-gated on --execute (addLiquidity carries
    remainsSolvent)."""
    pair_key = pair_key.lower()
    if pair_key not in TJ_LB_PAIRS:
        print(f"Unknown --pair '{pair_key}'. Choose from: {', '.join(TJ_LB_PAIRS)}")
        return
    shape = shape.lower()
    if shape not in ("spot", "curve", "bidask"):
        print("--shape must be spot, curve or bidask.")
        return
    if range_bins < 0:
        print("--range must be >= 0.")
        return
    if amount_x <= 0 and amount_y <= 0:
        print("Provide --amount-x and/or --amount-y (at least one must be > 0).")
        return

    p = TJ_LB_PAIRS[pair_key]
    x_cfg, y_cfg = p["tokenX"], p["tokenY"]
    has_x, has_y = amount_x > 0, amount_y > 0
    n_bins = 2 * range_bins + 1
    if n_bins > TJ_MAX_BINS:
        print(f"--range {range_bins} spans {n_bins} bins, over the {TJ_MAX_BINS}-bin cap. "
              f"Use --range <= {(TJ_MAX_BINS - 1) // 2}.")
        return

    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to LP.")
        print("Create and fund one first: deltaprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    amount_x_wei = int(amount_x * 10**x_cfg["decimals"]) if has_x else 0
    amount_y_wei = int(amount_y * 10**y_cfg["decimals"]) if has_y else 0

    # In-account balances (oracle-free), keyed by the TokenManager symbol the facet uses.
    bal_x = account.functions.getBalance(asset_b32(x_cfg["symbol"])).call()
    bal_y = account.functions.getBalance(asset_b32(y_cfg["symbol"])).call()
    if amount_x_wei > bal_x:
        print(f"Prime Account holds only {bal_x / 10**x_cfg['decimals']:.6f} {x_cfg['symbol']} "
              f"in-account; cannot add {amount_x} {x_cfg['symbol']}.")
        return
    if amount_y_wei > bal_y:
        print(f"Prime Account holds only {bal_y / 10**y_cfg['decimals']:.6f} {y_cfg['symbol']} "
              f"in-account; cannot add {amount_y} {y_cfg['symbol']}.")
        return

    # Cumulative 80-bin cap: bins this pair already owns are re-used (not double-counted);
    # only NET-NEW bins grow the count. Approximate conservatively by counting bins on OTHER
    # pairs as fixed and assuming all n_bins here are new (worst case).
    owned = account.functions.getOwnedTraderJoeV2Bins().call()
    owned_here = {int(b) for pr, b in owned if Web3.to_checksum_address(pr) == Web3.to_checksum_address(p["pair"])}
    owned_other = len(owned) - len(owned_here)

    pair_c = _lb_pair_contract(w3, p["pair"])
    active_id = pair_c.functions.getActiveId().call()

    deltas, dist_x, dist_y = _lb_build_distributions(active_id, range_bins, shape, has_x, has_y)
    target_ids = {active_id + d for d in deltas}
    new_bins = len([i for i in target_ids if i not in owned_here])
    projected = owned_other + len(owned_here) + new_bins
    bin_ids_sorted = sorted(target_ids)

    amount_x_min = int(amount_x_wei * (1 - slippage_pct / 100))
    amount_y_min = int(amount_y_wei * (1 - slippage_pct / 100))

    print(f"TraderJoe V2 LB add into [{pair_key}] {x_cfg['symbol']}/{y_cfg['symbol']} "
          f"(binStep {p['binStep']}) on Prime Account {pa}")
    print(f"  Router: {p['router']}  ({'v2.1' if p['router'] == TJ_ROUTER_V21 else 'v2.2'})")
    print(f"  Shape: {shape}  | range: ±{range_bins} bins ({n_bins} bins, ids {bin_ids_sorted[0]}..{bin_ids_sorted[-1]})")
    print(f"  Active bin: {active_id}  (idSlippage ±{id_slippage})")
    if has_x:
        print(f"  Deposit X: {amount_x} {x_cfg['symbol']}  (min {amount_x_min / 10**x_cfg['decimals']:.6f} @{slippage_pct}%) "
              f"-> bins deltaId >= 0")
    if has_y:
        print(f"  Deposit Y: {amount_y} {y_cfg['symbol']}  (min {amount_y_min / 10**y_cfg['decimals']:.6f} @{slippage_pct}%) "
              f"-> bins deltaId <= 0")
    # Distribution summary: show the non-zero weighting per side as percentages.
    if has_x:
        xs = [(active_id + d, dist_x[i] / LB_ONE * 100) for i, d in enumerate(deltas) if dist_x[i] > 0]
        print(f"    distributionX: " + ", ".join(f"{bid}:{pct:.1f}%" for bid, pct in xs))
    if has_y:
        ys = [(active_id + d, dist_y[i] / LB_ONE * 100) for i, d in enumerate(deltas) if dist_y[i] > 0]
        print(f"    distributionY: " + ", ".join(f"{bid}:{pct:.1f}%" for bid, pct in ys))
    print(f"  Bins: {len(owned_here)} already owned on this pair, {new_bins} net-new "
          f"-> projected total {projected} / {TJ_MAX_BINS}")
    if projected > TJ_MAX_BINS:
        print(f"  ✗ Would exceed the {TJ_MAX_BINS}-bin cap (the facet reverts TooManyBins). "
              f"Reduce --range or remove other bins first. Refusing.")
        return
    print("  The facet overrides to/refundTo to the account and re-checks the 80-bin cap on-chain.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # remainsSolvent gating: append a RedStone payload covering the account's assets + debts
    # plus both LB tokens. EURC's RedStone feed id is its account symbol "EUROC".
    feeds = prime_account_price_feeds(account)
    for s in (x_cfg["symbol"], y_cfg["symbol"]):
        if s not in feeds:
            feeds.append(s)
    payload = build_redstone_payload(feeds)

    deadline = int(time.time()) + 1200
    # to/refundTo are overridden by the facet; pass the account anyway for a clean preview.
    liquidity_params = (
        Web3.to_checksum_address(x_cfg["addr"]), Web3.to_checksum_address(y_cfg["addr"]),
        p["binStep"], amount_x_wei, amount_y_wei, amount_x_min, amount_y_min,
        active_id, id_slippage, deltas, dist_x, dist_y, pa_cs, pa_cs, deadline,
    )
    base_calldata = account.encode_abi("addLiquidityTraderJoeV2",
                                       args=[Web3.to_checksum_address(p["router"]), liquidity_params])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} LB add {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    return ok


def cmd_lb_remove(pair_key: str, slippage_pct: float = 1.0, execute: bool = False):
    """Remove ALL of the Prime Account's TraderJoe V2 LB liquidity for a whitelisted pair.
    Reads the owned bin ids for the pair (getOwnedTraderJoeV2Bins) and the account's LB
    balance per bin, then calls removeLiquidityTraderJoeV2 with those ids+amounts.
    amountXMin/amountYMin are slippage floors on the totals withdrawn, derived from the
    account's current share of each bin's reserves. removeLiquidity is NOT solvency-gated,
    so no RedStone payload is appended. Refuses if no Prime Account or no position."""
    pair_key = pair_key.lower()
    if pair_key not in TJ_LB_PAIRS:
        print(f"Unknown --pair '{pair_key}'. Choose from: {', '.join(TJ_LB_PAIRS)}")
        return

    p = TJ_LB_PAIRS[pair_key]
    x_cfg, y_cfg = p["tokenX"], p["tokenY"]

    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — no LB position to remove.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    pair_cs = Web3.to_checksum_address(p["pair"])
    owned = account.functions.getOwnedTraderJoeV2Bins().call()
    ids = sorted({int(b) for pr, b in owned if Web3.to_checksum_address(pr) == pair_cs})
    if not ids:
        print(f"Prime Account owns no {x_cfg['symbol']}/{y_cfg['symbol']} LB bins on [{pair_key}].")
        return

    pair_c = _lb_pair_contract(w3, pair_cs)
    active_id = pair_c.functions.getActiveId().call()
    amounts = []
    est_x = est_y = 0.0
    for binid in ids:
        bal = pair_c.functions.balanceOf(pa_cs, binid).call()
        ts = pair_c.functions.totalSupply(binid).call()
        rx, ry = pair_c.functions.getBin(binid).call()
        amounts.append(bal)
        share = (bal / ts) if ts else 0
        est_x += rx * share / 10**x_cfg["decimals"]
        est_y += ry * share / 10**y_cfg["decimals"]

    if all(a == 0 for a in amounts):
        print(f"All {len(ids)} tracked bins on [{pair_key}] hold zero LB balance — nothing to remove.")
        return

    amount_x_min = int(est_x * (1 - slippage_pct / 100) * 10**x_cfg["decimals"])
    amount_y_min = int(est_y * (1 - slippage_pct / 100) * 10**y_cfg["decimals"])

    print(f"TraderJoe V2 LB remove from [{pair_key}] {x_cfg['symbol']}/{y_cfg['symbol']} "
          f"(binStep {p['binStep']}) on Prime Account {pa}")
    print(f"  Router: {p['router']}  ({'v2.1' if p['router'] == TJ_ROUTER_V21 else 'v2.2'})")
    print(f"  Active bin: {active_id}  | removing {len(ids)} bin(s): {ids[0]}..{ids[-1]}")
    print(f"  Est. out: {est_x:,.6f} {x_cfg['symbol']} + {est_y:,.6f} {y_cfg['symbol']}")
    print(f"  Mins @{slippage_pct}%: {amount_x_min / 10**x_cfg['decimals']:.6f} {x_cfg['symbol']} + "
          f"{amount_y_min / 10**y_cfg['decimals']:.6f} {y_cfg['symbol']}")
    print("  removeLiquidity is NOT solvency-gated per the facet source, but on-chain calls revert without a RedStone payload — appending one.")

    if not execute:
        print("Run with --execute to broadcast.")
        return

    deadline = int(time.time()) + 1200
    remove_params = (
        Web3.to_checksum_address(x_cfg["addr"]), Web3.to_checksum_address(y_cfg["addr"]),
        p["binStep"], amount_x_min, amount_y_min, ids, amounts, deadline,
    )
    # Despite the comment that removeLiquidityTraderJoeV2 is NOT remainsSolvent,
    # on-chain calls revert with 0xe7764c9e (missing RedStone price data) without a
    # payload. Append the full account price-feeds set to be safe (tested 2026-05-24).
    feeds = prime_account_price_feeds(account)
    for s in (x_cfg["symbol"], y_cfg["symbol"]):
        if s not in feeds:
            feeds.append(s)
    payload = build_redstone_payload(feeds)
    base_calldata = account.encode_abi("removeLiquidityTraderJoeV2",
                                        args=[Web3.to_checksum_address(p["router"]), remove_params])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} LB remove {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

# ─── sJOE staking (SJoeFacet) ────────────────────────────────────────────────
# Stake in-account JOE into TraderJoe's sJOE for USDC fee rewards, unstake it back, or claim
# accrued USDC. All run on the Prime Account. stakeJoe + claimSJoeRewards carry remainsSolvent
# so --execute appends a RedStone signed-price payload; unstakeJoe does not (onlyOwnerOrInsolvent),
# matching the lb-remove no-payload pattern. The two position views are oracle-free.

def gather_sjoe(account):
    """Read-only sJOE staking position on a Prime Account. joeBalanceInSJoe (staked JOE) and
    rewardsInSJoe (pending USDC rewards) are oracle-free SJoeFacet views — no RedStone, no tx.
    Returns a dict with the raw + formatted staked/pending amounts. Shared by cmd_sjoe_position
    (print) and cmd_defi (--json)."""
    staked = account.functions.joeBalanceInSJoe().call()
    pending = account.functions.rewardsInSJoe().call()
    return {
        "staked_raw": staked, "pending_raw": pending,
        "staked": f"{staked / 10**SJOE_JOE['decimals']:.6f}",
        "pending": f"{pending / 10**SJOE_REWARD['decimals']:.6f}",
        "joe_symbol": SJOE_JOE["symbol"], "reward_symbol": SJOE_REWARD["symbol"],
    }

def cmd_sjoe_position():
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Owner wallet: {acct.address}")
    if not pa:
        print("No Prime Account yet — no sJOE staking position.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Prime Account: {pa}")

    s = gather_sjoe(account)
    staked, pending = s["staked_raw"], s["pending_raw"]
    print(f"  Staked JOE:        {staked / 10**SJOE_JOE['decimals']:,.6f} JOE")
    print(f"  Pending rewards:   {pending / 10**SJOE_REWARD['decimals']:,.6f} USDC", end="")
    if pending > 0:
        net = pending * (1 - SJOE_CLAIMING_FEE_PCT / 100)
        print(f"  (~{net / 10**SJOE_REWARD['decimals']:,.6f} net after the {SJOE_CLAIMING_FEE_PCT:.0f}% claim fee)")
    else:
        print()
    if staked == 0 and pending == 0:
        print("  No active sJOE position.")

def cmd_sjoe_stake(amount: float, execute: bool = False):
    """Stake JOE from the Prime Account's in-account balance into sJOE (stakeJoe). Carries
    remainsSolvent, so --execute appends a fresh RedStone price payload. The facet caps the
    amount to the account's in-account JOE; staking also auto-claims any pending USDC (net of
    the 10% claim fee). Preview by default."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to stake.")
        print("Create and fund one first: deltaprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    amount_wei = int(amount * 10**SJOE_JOE["decimals"])
    in_balance = account.functions.getBalance(asset_b32(SJOE_JOE["symbol"])).call()
    print(f"sJOE stake: {amount} JOE from Prime Account {pa}")
    print(f"  In-account JOE: {in_balance / 10**SJOE_JOE['decimals']:,.6f}")
    if amount_wei > in_balance:
        print(f"  ✗ Requested {amount} JOE exceeds the in-account balance "
              f"(facet reverts 'Not enough JOE to stake'). Refusing.")
        print("  Fund or swap JOE into the account first.")
        return
    pending = account.functions.rewardsInSJoe().call()
    print(f"  Calls stakeJoe({amount_wei}) on the Prime Account.")
    print(f"  Pending USDC auto-claimed on stake: {pending / 10**SJOE_REWARD['decimals']:,.6f} "
          f"(net of the {SJOE_CLAIMING_FEE_PCT:.0f}% claim fee).")
    print("  Carries remainsSolvent — appends a fresh RedStone payload on --execute.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # remainsSolvent gating: cover the account's assets + debts plus JOE (about to be staked)
    # and USDC (the reward asset the facet adds on claim). Fetched fresh — valid ~3 minutes.
    feeds = prime_account_price_feeds(account)
    for s in (SJOE_JOE["symbol"], SJOE_REWARD["symbol"]):
        if s not in feeds:
            feeds.append(s)
    payload = build_redstone_payload(feeds)
    data = account.encode_abi("stakeJoe", args=[amount_wei]) + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} sJOE stake {amount} JOE {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_sjoe_unstake(amount: float, execute: bool = False):
    """Unstake JOE from sJOE back into the Prime Account (unstakeJoe). NOT remainsSolvent
    (onlyOwnerOrInsolvent), so no RedStone payload is appended — same as lb-remove. Caps to the
    staked balance; unstaking also auto-claims pending USDC (net of the 10% claim fee). Preview
    by default."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to unstake.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    staked = account.functions.joeBalanceInSJoe().call()
    if staked == 0:
        print(f"Prime Account {pa} has no staked JOE in sJOE — nothing to unstake.")
        return
    amount_wei = int(amount * 10**SJOE_JOE["decimals"])
    if amount_wei > staked:
        print(f"Staked balance is {staked / 10**SJOE_JOE['decimals']:,.6f} JOE; "
              f"clamping unstake to that.")
        amount_wei = staked
    pending = account.functions.rewardsInSJoe().call()

    print(f"sJOE unstake: {amount_wei / 10**SJOE_JOE['decimals']:,.6f} JOE from Prime Account {pa}")
    print(f"  Staked JOE: {staked / 10**SJOE_JOE['decimals']:,.6f}")
    print(f"  Calls unstakeJoe({amount_wei}) on the Prime Account.")
    print(f"  Pending USDC auto-claimed on unstake: {pending / 10**SJOE_REWARD['decimals']:,.6f} "
          f"(net of the {SJOE_CLAIMING_FEE_PCT:.0f}% claim fee).")
    print("  unstakeJoe is NOT solvency-gated — no RedStone payload needed.")

    if not execute:
        print("Run with --execute to broadcast.")
        return

    tx = account.functions.unstakeJoe(amount_wei).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} sJOE unstake {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_sjoe_claim(execute: bool = False):
    """Claim accrued USDC rewards from sJOE into the Prime Account (claimSJoeRewards, via the
    sJOE withdraw(0) path). Carries remainsSolvent, so --execute appends a fresh RedStone price
    payload. The account receives the pending USDC minus the 10% claim fee. Preview by default."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to claim.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    pending = account.functions.rewardsInSJoe().call()
    net = pending * (1 - SJOE_CLAIMING_FEE_PCT / 100)
    print(f"sJOE claim rewards on Prime Account {pa}")
    print(f"  Pending rewards: {pending / 10**SJOE_REWARD['decimals']:,.6f} USDC")
    print(f"  Net after the {SJOE_CLAIMING_FEE_PCT:.0f}% claim fee: "
          f"{net / 10**SJOE_REWARD['decimals']:,.6f} USDC")
    if pending == 0:
        print("  No pending rewards to claim.")
        return
    print("  Calls claimSJoeRewards() on the Prime Account.")
    print("  Carries remainsSolvent — appends a fresh RedStone payload on --execute.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # remainsSolvent gating: cover the account's assets + debts plus USDC (the reward asset the
    # facet adds on claim). Fetched fresh — valid ~3 minutes.
    feeds = prime_account_price_feeds(account)
    if SJOE_REWARD["symbol"] not in feeds:
        feeds.append(SJOE_REWARD["symbol"])
    payload = build_redstone_payload(feeds)
    data = account.encode_abi("claimSJoeRewards", args=[]) + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} sJOE claim {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

# ─── PRIME-token leverage tiers (PrimeLeverageFacet) ─────────────────────────

def _prime_token_contract(w3):
    """Minimal PRIME ERC20 (balanceOf + approve) for the deposit/balance reads."""
    abi = json.loads('[{"constant":true,"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
                     '{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]')
    return w3.eth.contract(address=Web3.to_checksum_address(PRIME_TOKEN["addr"]), abi=abi)

def gather_prime_tier(w3, acct, account):
    """Read-only PRIME leverage-tier status. getLeverageTierFullInfo (tier, staked PRIME,
    recorded PRIME debt) + the EOA and in-account PRIME balances — all oracle-free. `account`
    may be None when no Prime Account exists yet (then only the wallet PRIME balance is read,
    tier defaults to BASIC). shouldLiquidatePrimeDebt is intentionally NOT included here (it
    mutates and is RedStone-gated; cmd_prime_tier reads it separately). Shared by
    cmd_prime_tier (print) and cmd_defi (--json). PRIME (18-dec) is a normalised float."""
    dec = 10**PRIME_TOKEN["decimals"]
    eoa_prime = _prime_token_contract(w3).functions.balanceOf(acct.address).call()
    out = {"wallet_prime": eoa_prime / dec, "tier": "BASIC", "tier_code": 0,
           "staked": 0.0, "in_account": 0.0, "recorded_debt": 0.0}
    if account is None:
        return out
    tier, staked, recorded_debt = account.functions.getLeverageTierFullInfo().call()
    in_acct_prime = account.functions.getBalance(asset_b32(PRIME_TOKEN["symbol"])).call()
    out.update({"tier_code": tier, "tier": PRIME_TIER_NAMES.get(tier, str(tier)),
                "staked": staked / dec, "in_account": in_acct_prime / dec,
                "recorded_debt": recorded_debt / dec})
    return out

def cmd_prime_tier():
    """Read-only: the Prime Account's PRIME leverage-tier status. getLeverageTierFullInfo
    (current tier, staked PRIME, recorded PRIME debt), getPrimeStakedAmount, the EOA + in-account
    PRIME balances, and shouldLiquidatePrimeDebt() via eth_call. All five getters are oracle-free;
    shouldLiquidatePrimeDebt is a state-mutating fn (it snapshots debt), so we only simulate it with
    eth_call — never broadcast. recordedDebt is the last on-chain snapshot; accrual since then is not
    reflected until updatePrimeDebt/a write runs (getCurrentPrimeDebt is internal, not callable)."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Owner wallet: {acct.address}")
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI) if pa else None
    t = gather_prime_tier(w3, acct, account)
    print(f"  Wallet PRIME:      {t['wallet_prime']:,.6f}")
    if not pa:
        print("No Prime Account yet — no leverage tier. (Tier defaults to BASIC once created.)")
        return
    print(f"Prime Account: {pa}")
    print(f"  Tier:              {t['tier']} (BASIC ~5x, PREMIUM 10x)")
    print(f"  Staked PRIME:      {t['staked']:,.6f}")
    print(f"  In-account PRIME:  {t['in_account']:,.6f}")
    print(f"  Recorded PRIME debt: {t['recorded_debt']:,.6f}  "
          "(last snapshot; accrual since not included)")
    # shouldLiquidatePrimeDebt MUTATES (snapshots debt) -> simulate read-only with eth_call, never
    # broadcast. It reads _getDebt() internally, so despite being a PRIME-side check it hits the
    # solvency oracle path and reverts 0xe7764c9e on a bare call — a RedStone payload must be
    # appended, same as the solvency views in prime-summary. (The other four PRIME getters above
    # ARE oracle-free; only this one touches debt.) Falls back gracefully if the gateway is down.
    try:
        payload = build_redstone_payload(prime_account_price_feeds(account))
        flag = redstone_view_call(w3, account, "shouldLiquidatePrimeDebt", payload)[0]
        print(f"  PRIME-debt liquidatable: {'YES — staked PRIME no longer covers debt' if flag else 'no'}")
    except Exception as e:
        print(f"  PRIME-debt liquidatable: RedStone fetch/call failed ({type(e).__name__})")

def cmd_prime_needed(borrow_usd: float, tier: str = "premium"):
    """Read-only quote: PRIME needed to back a given USD borrow at the chosen tier. Calls
    getRequiredPrimeStake(tier, int(borrow_usd * 1e18)) on the facet — it reads the LIVE
    tieredPrimeStakingRatio from the TokenManager (governance-mutable), so this is the
    authoritative figure, not a hard-coded ratio. Oracle-free, no tx."""
    if tier not in PRIME_TIERS:
        print(f"Unknown --tier '{tier}'. Choose from: {', '.join(PRIME_TIERS)}")
        return
    w3 = get_w3()
    # The view is oracle-free and state-independent, so any deployed Prime Account works as the
    # call target. Fall back to the facet address itself if the wallet has no account yet.
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    target = Web3.to_checksum_address(pa) if pa else Web3.to_checksum_address(PRIME_LEVERAGE_FACET)
    account = w3.eth.contract(address=target, abi=PRIME_ACCOUNT_ABI)
    borrowed_value = int(borrow_usd * 1e18)
    required = account.functions.getRequiredPrimeStake(PRIME_TIERS[tier], borrowed_value).call()
    print(f"To back a ${borrow_usd:,.2f} borrow at {tier.upper()} tier:")
    print(f"  PRIME required:    {required / 10**PRIME_TOKEN['decimals']:,.6f} PRIME")
    print("  (live tieredPrimeStakingRatio from TokenManager — proportional to USD borrow)")

def cmd_prime_activate(amount: float = None, execute: bool = False):
    """Activate PREMIUM (10x) tier. The on-chain flow (verified PrimeLeverageFacet source):
    stakePrimeAndActivatePremium() stakes getRequiredPrimeStake(PREMIUM, (totalValue-debt)*10)
    from the account's IN-ACCOUNT PRIME balance, then sets tier=PREMIUM. So PRIME must already
    sit inside the account. --amount N first runs depositPrime(N) to move PRIME from the EOA in
    (ERC20 approve -> depositPrime, which is remainsSolvent-gated so a RedStone payload is appended
    on --execute); omit --amount to stake from PRIME already in the account. Preview prints the plan
    and the projected required stake; fails closed if the in-account PRIME would be short."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — create and fund one first:")
        print("  deltaprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    prime = _prime_token_contract(w3)

    tier = account.functions.getLeverageTier().call()
    if tier == PRIME_TIERS["premium"]:
        print(f"Prime Account {pa} is already in PREMIUM tier — nothing to do.")
        return

    deposit_wei = int(amount * 10**PRIME_TOKEN["decimals"]) if amount else 0
    eoa_prime = prime.functions.balanceOf(acct.address).call()
    in_acct_prime = account.functions.getBalance(asset_b32(PRIME_TOKEN["symbol"])).call()
    # depositPrime caps to the EOA balance on-chain; mirror that for an honest preview.
    deposit_wei = min(deposit_wei, eoa_prime) if deposit_wei else 0
    projected_in_acct = in_acct_prime + deposit_wei

    print(f"PRIME activate PREMIUM tier on Prime Account {pa}")
    print(f"  Wallet PRIME:      {eoa_prime / 10**PRIME_TOKEN['decimals']:,.6f}")
    print(f"  In-account PRIME:  {in_acct_prime / 10**PRIME_TOKEN['decimals']:,.6f}")

    # Projected required stake = getRequiredPrimeStake(PREMIUM, (totalValue - debt) * 10). totalValue
    # and debt are RedStone-gated solvency views — fetch best-effort like prime-summary does.
    required = None
    try:
        payload = build_redstone_payload(prime_account_price_feeds(account))
        total_value = redstone_view_call(w3, account, "getTotalValue", payload)[0]
        debt = redstone_view_call(w3, account, "getDebt", payload)[0]
        free_collateral = total_value - debt if total_value > debt else 0
        required = account.functions.getRequiredPrimeStake(
            PRIME_TIERS["premium"], free_collateral * 10).call()
        print(f"  Free collateral:   ${free_collateral / 1e18:,.2f}  "
              f"-> stakes against 10x = ${free_collateral * 10 / 1e18:,.2f} max debt")
        print(f"  Required stake:    {required / 10**PRIME_TOKEN['decimals']:,.6f} PRIME")
    except Exception as e:
        print(f"  Required stake:    could not compute (RedStone fetch/call failed: {type(e).__name__})")

    if deposit_wei:
        print(f"  Step 1: approve + depositPrime({deposit_wei}) "
              f"({deposit_wei / 10**PRIME_TOKEN['decimals']:,.6f} PRIME from wallet, RedStone-gated)")
        print(f"  Step 2: stakePrimeAndActivatePremium()")
    else:
        print(f"  Step 1: stakePrimeAndActivatePremium() (stakes from in-account PRIME; no deposit)")

    if required is not None and projected_in_acct < required:
        print(f"  ✗ Projected in-account PRIME "
              f"({projected_in_acct / 10**PRIME_TOKEN['decimals']:,.6f}) is below the required stake "
              f"({required / 10**PRIME_TOKEN['decimals']:,.6f}). stakePrimeAndActivatePremium would "
              "revert 'Insufficient PRIME balance'.")
        print(f"  Deposit more PRIME first: deltaprime prime-activate --amount <N> --execute")
        return

    if not execute:
        print("Run with --execute to broadcast"
              + (" (depositPrime appends a fresh RedStone price payload)." if deposit_wei else "."))
        return

    if deposit_wei:
        # Build the RedStone payload FIRST (PRIME has no RedStone feed — only the account's
        # collateral assets are priced for remainsSolvent) so a gateway failure broadcasts nothing.
        payload = build_redstone_payload(prime_account_price_feeds(account))
        nonce = w3.eth.get_transaction_count(acct.address)
        app_tx = prime.functions.approve(pa_cs, deposit_wei).build_transaction({
            "from": acct.address, "nonce": nonce,
            "gas": 100000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
        w3.eth.send_raw_transaction(acct.sign_transaction(app_tx).raw_transaction)
        data = account.encode_abi("depositPrime", args=[deposit_wei]) + payload.hex()
        dep_tx = {
            "from": acct.address, "to": pa_cs, "data": data,
            "nonce": nonce + 1,
            "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        }
        dep_hash = w3.eth.send_raw_transaction(acct.sign_transaction(dep_tx).raw_transaction)
        dep_ok = w3.eth.wait_for_transaction_receipt(dep_hash, timeout=180)["status"] == 1
        print(f"{'✓' if dep_ok else '✗'} depositPrime {'confirmed' if dep_ok else 'failed'}")
        print(f"  Tx: {EXPLORER}/tx/{dep_hash.hex()}")
        if not dep_ok:
            print("  Aborting — not activating PREMIUM after a failed deposit.")
            return

    # stakePrimeAndActivatePremium ALSO requires a RedStone payload appended — a bare call reverts
    # CalldataMustHaveValidPayload / 0xe7764c9e (verified read-only 24-05-2026). This is the on-chain
    # equivalent of the frontend "UNLOCK 10X" button.
    payload = build_redstone_payload(prime_account_price_feeds(account))
    data = account.encode_abi("stakePrimeAndActivatePremium", args=[]) + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    tx_hash = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    ok = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)["status"] == 1
    print(f"{'✓' if ok else '✗'} PREMIUM tier {'activated' if ok else 'activation failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_prime_deposit(amount: float, execute: bool = False):
    """Deposit PRIME from the wallet (EOA) INTO the Prime Account, without activating PREMIUM.
    ERC20 approve -> depositPrime(amount); depositPrime is remainsSolvent-gated, so a fresh
    RedStone payload is appended on --execute. The PRIME then sits in-account (ready for
    prime-activate). Caps to the wallet's PRIME balance. Approve (nonce N) and depositPrime
    (nonce N+1) are sent as a sequential pair so the allowance is in place before the move."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — create one first:")
        print("  deltaprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    prime = _prime_token_contract(w3)

    eoa_prime = prime.functions.balanceOf(acct.address).call()
    in_acct_prime = account.functions.getBalance(asset_b32(PRIME_TOKEN["symbol"])).call()
    deposit_wei = int(amount * 10**PRIME_TOKEN["decimals"])
    deposit_wei = min(deposit_wei, eoa_prime)  # depositPrime caps to the EOA balance on-chain

    print(f"PRIME deposit into Prime Account {pa}")
    print(f"  Wallet PRIME:      {eoa_prime / 10**PRIME_TOKEN['decimals']:,.6f}")
    print(f"  In-account PRIME:  {in_acct_prime / 10**PRIME_TOKEN['decimals']:,.6f}")
    if deposit_wei <= 0:
        print("  Nothing to deposit (wallet PRIME is 0).")
        return
    print(f"  Deposit:           {deposit_wei / 10**PRIME_TOKEN['decimals']:,.6f} PRIME "
          "(approve + depositPrime, RedStone-gated)")
    print(f"  Resulting in-account PRIME: "
          f"{(in_acct_prime + deposit_wei) / 10**PRIME_TOKEN['decimals']:,.6f}")

    if not execute:
        print("Run with --execute to broadcast (depositPrime appends a fresh RedStone price payload).")
        return

    # Build the RedStone payload FIRST (PRIME itself has no RedStone feed — only the account's
    # collateral assets are priced for remainsSolvent) so a gateway failure broadcasts nothing.
    payload = build_redstone_payload(prime_account_price_feeds(account))
    nonce = w3.eth.get_transaction_count(acct.address)
    app_tx = prime.functions.approve(pa_cs, deposit_wei).build_transaction({
        "from": acct.address, "nonce": nonce,
        "gas": 100000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    })
    w3.eth.send_raw_transaction(acct.sign_transaction(app_tx).raw_transaction)
    data = account.encode_abi("depositPrime", args=[deposit_wei]) + payload.hex()
    dep_tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": nonce + 1,
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    dep_hash = w3.eth.send_raw_transaction(acct.sign_transaction(dep_tx).raw_transaction)
    dep_ok = w3.eth.wait_for_transaction_receipt(dep_hash, timeout=180)["status"] == 1
    print(f"{'✓' if dep_ok else '✗'} depositPrime {'confirmed' if dep_ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{dep_hash.hex()}")

def cmd_prime_deactivate(withdraw: bool = False, execute: bool = False):
    """Drop back to BASIC tier (deactivatePremiumTier(withdrawStake)). The facet REPAYS ALL PRIME
    debt first and reverts if the in-account PRIME can't cover it (50% of the repaid PRIME is burned,
    50% to treasury). --withdraw maps to withdrawStake=true, which also releases staked PRIME above
    the new BASIC requirement (which is 0, so all of it) back into the account. onlyOwner, NOT
    solvency-gated — no RedStone payload. Preview by default."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to deactivate.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    tier, staked, recorded_debt = account.functions.getLeverageTierFullInfo().call()
    if tier == PRIME_TIERS["basic"]:
        print(f"Prime Account {pa} is already in BASIC tier — nothing to deactivate.")
        return
    in_acct_prime = account.functions.getBalance(asset_b32(PRIME_TOKEN["symbol"])).call()

    print(f"PRIME deactivate PREMIUM -> BASIC on Prime Account {pa}")
    print(f"  Recorded PRIME debt: {recorded_debt / 10**PRIME_TOKEN['decimals']:,.6f}  "
          "(facet repays the FULL current debt, incl. accrual, before downgrading)")
    print(f"  In-account PRIME:  {in_acct_prime / 10**PRIME_TOKEN['decimals']:,.6f}  (must cover the debt)")
    print(f"  Staked PRIME:      {staked / 10**PRIME_TOKEN['decimals']:,.6f}")
    print(f"  Calls deactivatePremiumTier(withdrawStake={str(withdraw).lower()}).")
    print("  Repays all PRIME debt first (50% burn / 50% treasury); reverts if PRIME can't cover it.")
    if withdraw:
        print("  --withdraw: releases excess staked PRIME (BASIC requires 0) back into the account.")
    else:
        print("  Without --withdraw: stake stays put; release it later with prime-unstake.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # PrimeLeverageFacet requires a RedStone payload appended (its sibling repayPrimeDebt reverts
    # CalldataMustHaveValidPayload / 0xe7764c9e without one — verified read-only 24-05-2026; deactivate
    # repays the PRIME debt internally, so it needs the same price context).
    payload = build_redstone_payload(prime_account_price_feeds(account))
    data = account.encode_abi("deactivatePremiumTier", args=[withdraw]) + payload.hex()
    tx = {
        "from": acct.address, "to": account.address, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    tx_hash = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    ok = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)["status"] == 1
    print(f"{'✓' if ok else '✗'} PREMIUM tier {'deactivated' if ok else 'deactivation failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_prime_unstake(amount: float, execute: bool = False):
    """Unstake PRIME from the leverage stake back into the account (unstakePrime). onlyOwner, NOT
    solvency-gated — no RedStone payload. The facet guards (when still PREMIUM): the remaining stake
    must cover BOTH the PREMIUM USD ratio against current debt AND the accrued PRIME debt — it
    snapshots debt first, so a short unstake reverts. Caps the request to the staked balance.
    Preview by default."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to unstake.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    staked = account.functions.getPrimeStakedAmount().call()
    if staked == 0:
        print(f"Prime Account {pa} has no staked PRIME — nothing to unstake.")
        return
    amount_wei = int(amount * 10**PRIME_TOKEN["decimals"])
    if amount_wei > staked:
        print(f"Staked PRIME is {staked / 10**PRIME_TOKEN['decimals']:,.6f}; clamping unstake to that.")
        amount_wei = staked

    print(f"PRIME unstake: {amount_wei / 10**PRIME_TOKEN['decimals']:,.6f} PRIME from Prime Account {pa}")
    print(f"  Staked PRIME:      {staked / 10**PRIME_TOKEN['decimals']:,.6f}")
    print(f"  Calls unstakePrime({amount_wei}).")
    print("  In PREMIUM tier the remaining stake must still cover the USD ratio + accrued PRIME debt,")
    print("  else the facet reverts. Appends a RedStone price payload (the facet requires one).")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # PrimeLeverageFacet requires a RedStone payload appended (sibling repayPrimeDebt confirmed via
    # probe 24-05-2026; unstakePrime checks the USD ratio + accrued PRIME debt, so it needs price context).
    payload = build_redstone_payload(prime_account_price_feeds(account))
    data = account.encode_abi("unstakePrime", args=[amount_wei]) + payload.hex()
    tx = {
        "from": acct.address, "to": account.address, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    tx_hash = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    ok = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)["status"] == 1
    print(f"{'✓' if ok else '✗'} PRIME unstake {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_prime_repay(amount: float, execute: bool = False):
    """Repay accrued PRIME rent-debt (repayPrimeDebt) using in-account PRIME. onlyOwner, NOT
    solvency-gated — no RedStone payload. The facet snapshots debt, caps the amount to the current
    debt (no overpayment), and splits the repaid PRIME 50% burn / 50% treasury. Preview by default.
    recordedDebt shown is the last snapshot; the on-chain repay re-snapshots, so the true current
    debt may be slightly higher (unsnapshotted accrual)."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to repay.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    _tier, _staked, recorded_debt = account.functions.getLeverageTierFullInfo().call()
    in_acct_prime = account.functions.getBalance(asset_b32(PRIME_TOKEN["symbol"])).call()
    amount_wei = int(amount * 10**PRIME_TOKEN["decimals"])

    print(f"PRIME repay debt: {amount} PRIME on Prime Account {pa}")
    print(f"  Recorded PRIME debt: {recorded_debt / 10**PRIME_TOKEN['decimals']:,.6f}  "
          "(last snapshot; repay re-snapshots and caps to the true current debt)")
    print(f"  In-account PRIME:  {in_acct_prime / 10**PRIME_TOKEN['decimals']:,.6f}")
    if amount_wei > in_acct_prime:
        print(f"  ✗ Requested {amount} PRIME exceeds in-account PRIME "
              "(facet reverts 'Not enough PRIME to repay the debt'). Refusing.")
        print("  Deposit PRIME into the account first: deltaprime prime-activate --amount <N> --execute")
        return
    print(f"  Calls repayPrimeDebt({amount_wei}) — caps to current debt, 50% burn / 50% treasury.")
    print("  Appends a RedStone price payload (the facet requires one — verified read-only 24-05-2026).")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # The PrimeLeverageFacet requires a RedStone payload appended, exactly like lb-remove:
    # a bare call reverts CalldataMustHaveValidPayload / 0xe7764c9e (verified read-only 24-05-2026).
    payload = build_redstone_payload(prime_account_price_feeds(account))
    base_calldata = account.encode_abi("repayPrimeDebt", args=[amount_wei])
    tx = {
        "from": acct.address, "to": pa_cs, "data": base_calldata + payload.hex(),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    }
    tx_hash = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    ok = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)["status"] == 1
    print(f"{'✓' if ok else '✗'} PRIME debt repay {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

# ─── Zaps (tool-level macros) ────────────────────────────────────────────────
# DeltaPrime zaps are NOT a separate on-chain facet (capabilities §7) — they are front-end
# orchestration that chains the existing primitives into a one-click leveraged entry. We
# replicate that as a macro over the existing in-tool leg commands (cmd_fund / cmd_borrow /
# cmd_swap / cmd_gmx_deposit), reusing their on-chain encoding verbatim — no new ABI.
#
# Design: ONE clean, bounded "leveraged long" flow (the canonical DeltaPrime first-zap),
# terminating in a GMX V2 GM market deposit. Legs, in order:
#   1. fund      — move volatile collateral from the EOA into the Prime Account.
#   2. borrow    — borrow USDC against it (the leverage).
#   3. swap      — OPTIONAL (--swap): YieldYak-swap the borrowed USDC into the market's long
#                  token, to deposit the volatile (long) leg instead of the USDC (short) leg.
#   4. gmx-deposit — deposit --deposit-amount of the chosen leg into --market.
# Each leg is its OWN transaction with an EXPLICIT amount (no fragile auto-sizing across an
# oracle-priced/async boundary — the brief's correctness bar). Preview prints the full ordered
# plan; --execute runs the legs sequentially and STOPS on the first failure, reporting exactly
# which leg succeeded and which failed (partial-state safety). The terminal GMX leg is ASYNC:
# --execute can only FIRE the deposit request — a GMX keeper mints the GM tokens later and the
# account is FROZEN until then (re-check `gmx-positions` once the keeper settles).
#
# Only the GMX terminal is built (it is the canonical leveraged-long zap and exercises the
# async/freeze path). An LB-terminal leveraged long is reachable by running the same fund ->
# borrow -> [swap] legs then `lb-add` manually; kept out to hold the surface small.

def cmd_zap(market: str, collateral_pool: str, collateral_amount: float, borrow_amount: float,
            deposit_amount: float, side: str = "short", swap_to_long: bool = False,
            slippage_pct: float = 1.0, fee_buffer: float = 2.0, execute: bool = False):
    """Leveraged-long zap: fund collateral -> borrow USDC -> [swap USDC->long] -> GMX GM deposit.
    Composes the existing leg commands; each leg is its own tx. Preview prints the ordered plan;
    --execute runs the legs in order and stops on the first failure (partial-state safe). The GMX
    leg is async (fires the request; the keeper settles later, account frozen until then)."""
    if market not in GMX_MARKETS:
        print(f"Unknown --market '{market}'. Choose from: {', '.join(GMX_MARKETS)}")
        return
    mkt = GMX_MARKETS[market]
    if mkt["plus"]:
        print(f"--market '{market}' is a single-sided GM+ market. This zap targets two-sided GM "
              f"markets (USDC short leg + volatile long leg). Choose a GM market: "
              f"{', '.join(k for k, m in GMX_MARKETS.items() if not m['plus'])}.")
        return
    if collateral_pool not in POOLS:
        print(f"Unknown --collateral '{collateral_pool}'. Choose from: {', '.join(POOLS)}")
        return
    if side not in ("long", "short"):
        print("--side must be 'long' or 'short'.")
        return
    if collateral_amount <= 0 or borrow_amount <= 0 or deposit_amount <= 0:
        print("--collateral-amount, --borrow-amount and --deposit-amount must all be > 0.")
        return
    if slippage_pct > GMX_MAX_SLIPPAGE_PCT:
        print(f"--slippage {slippage_pct}% exceeds the GMX {GMX_MAX_SLIPPAGE_PCT}% cap; the deposit "
              "leg would revert. Lower it.")
        return

    long_sym, short_sym = mkt["long"], mkt["short"]   # short is always USDC on these markets
    deposit_leg_sym = long_sym if side == "long" else short_sym

    # Ordered leg plan. Each entry: (label, callable taking execute=bool, gated?, note).
    legs = []
    legs.append((
        f"1. fund {collateral_amount} {pool_to_asset_symbol(collateral_pool)} (collateral) into the Prime Account",
        lambda ex: cmd_fund(collateral_pool, collateral_amount, ex),
        False, "EOA wallet must hold the collateral; ERC20 approves the account."))
    legs.append((
        f"2. borrow {borrow_amount} {short_sym} against the collateral (leverage)",
        lambda ex: cmd_borrow(_SYMBOL_TO_POOL[short_sym], borrow_amount, ex),
        True, "borrow() — needs the account solvent after the draw."))
    if swap_to_long:
        legs.append((
            f"3. swap {borrow_amount} {short_sym} -> {long_sym} (YieldYak) to fund the long leg",
            lambda ex: cmd_swap(short_sym, long_sym, borrow_amount, slippage_pct, "yak", ex),
            True, "yakSwap on in-account USDC; RedStone-gated."))
    legs.append((
        f"{'4' if swap_to_long else '3'}. gmx-deposit {deposit_amount} {deposit_leg_sym} "
        f"({'long' if side == 'long' else 'short'} leg) into [{market}] {mkt['gm_feed']}",
        lambda ex: cmd_gmx_deposit(market, deposit_amount, side == "long", slippage_pct, fee_buffer, ex),
        True, "PAYABLE + ASYNC: fires a GMX deposit request, pays the keeper execution fee, "
              "FREEZES the account until the keeper callback mints the GM tokens."))

    print(f"=== Leveraged-long zap into [{market}] {mkt['gm_feed']} (Prime Account macro) ===")
    print(f"  Collateral: {collateral_amount} {pool_to_asset_symbol(collateral_pool)}  |  "
          f"Borrow: {borrow_amount} {short_sym}  |  GM deposit: {deposit_amount} {deposit_leg_sym} "
          f"({'long' if side == 'long' else 'short'} leg){'  |  swap USDC->'+long_sym if swap_to_long else ''}")
    print(f"  {len(legs)} legs, each its own transaction. Solvency-gated legs append a RedStone "
          "payload on --execute.")
    print("  Ordered plan:")
    for label, _fn, gated, note in legs:
        print(f"    {label}   [{'RedStone-gated' if gated else 'not gated'}]")
        print(f"        {note}")
    print("  Terminal GMX leg is ASYNC — --execute only FIRES the deposit request; a GMX keeper")
    print("  mints the GM tokens later and the account is FROZEN until then. Re-check gmx-positions")
    print("  once the keeper settles.")

    if not execute:
        print("\n  PREVIEW per leg (each shown as it would run, nothing broadcast):")
        for label, fn, _gated, _note in legs:
            print(f"\n  --- {label} ---")
            fn(False)
        print("\nRun with --execute to broadcast the legs in order (stops on the first failure).")
        return

    print("\n  EXECUTING legs in order — stops immediately on any failure.\n")
    done = []
    for idx, (label, fn, _gated, _note) in enumerate(legs, 1):
        print(f"  --- Running {label} ---")
        result = fn(True)
        if result is True:
            done.append(label)
            continue
        # Any non-True return (False = broadcast failed; None = pre-flight refusal/short balance)
        # stops the chain. The leg already printed why.
        print(f"\n  ✗ ZAP HALTED at leg {idx}: {label}")
        print(f"    Result: {'transaction failed on-chain' if result is False else 'leg refused / did not broadcast (see its output above)'}.")
        if done:
            print(f"    Legs that DID complete: {len(done)}")
            for d in done:
                print(f"      ✓ {d}")
            print("    The Prime Account is now in a PARTIAL state — the completed legs are live")
            print("    on-chain. Review with prime-summary before retrying; do NOT blindly re-run")
            print("    the whole zap (it would repeat the completed legs).")
        else:
            print("    No legs completed; nothing changed on-chain.")
        return

    print(f"\n  ✓ ZAP COMPLETE — all {len(legs)} legs fired.")
    print("    NOTE: the final GMX deposit is ASYNC. The GM tokens are not minted yet — a GMX")
    print("    keeper settles the request in a later block and the account stays FROZEN until")
    print(f"    then. Check `deltaprime gmx-positions` for the {mkt['gm_feed']} balance once it settles.")

def gather_defi() -> dict:
    """Aggregate ALL DeltaPrime positions for the selected wallet into one DeBank-style dict.
    Read-only: reuses the gather_* helpers (lending/solvency, GMX V2 LP, TraderJoe V2 LB, sJOE,
    PRIME tier), each of which only does eth_calls. Empty groups are omitted. total_usd /
    health_ratio / solvent come from the RedStone-gated solvency views; per-asset USD is
    best-effort (null where a RedStone feed is missing). Never broadcasts."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    result = {
        "protocol": "DeltaPrime", "url": "https://app.deltaprime.io", "chain": "avalanche",
        "wallet": acct.address, "prime_account": pa,
        "total_usd": None, "health_ratio": None, "solvent": None,
        "groups": [], "status": "ok",
    }
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI) if pa else None

    # PRIME tier reads the EOA balance even with no Prime Account, so always gather it.
    tier = gather_prime_tier(w3, acct, account)

    if account is not None:
        lending = gather_lending(w3, account)
        result["total_usd"] = lending["total_value_usd"]
        result["health_ratio"] = lending["health_ratio"]
        result["solvent"] = lending["solvent"]
        if lending["supplied"] or lending["borrowed"]:
            result["groups"].append({
                "type": "Lending / Leverage", "health_ratio": lending["health_ratio"],
                "supplied": [{"symbol": r["symbol"], "balance": r["balance"], "usd": r.get("usd")}
                             for r in lending["supplied"]],
                "borrowed": [{"symbol": r["symbol"], "balance": r["balance"], "usd": r.get("usd")}
                             for r in lending["borrowed"]],
            })

        gmx = gather_gmx(w3, account)
        if gmx:
            result["groups"].append({"type": "GMX V2 LP", "items": [
                {"label": p["gm_feed"], "balance": p["balance"], "symbol": p["kind"], "usd": p.get("usd")}
                for p in gmx]})

        lb = gather_lb(w3, account)
        lb_items = []
        for p in lb:
            if p.get("error"):
                continue
            lb_items.append({"label": p["label"], "active_bin": p["active_bin"], "bins": p["bins"],
                             "token_x": {"symbol": p["token_x"]["symbol"], "balance": f"{p['token_x']['amount']:.6f}"},
                             "token_y": {"symbol": p["token_y"]["symbol"], "balance": f"{p['token_y']['amount']:.6f}"}})
        if lb_items:
            result["groups"].append({"type": "TraderJoe V2 LB", "items": lb_items})

        sjoe = gather_sjoe(account)
        if sjoe["staked_raw"] > 0 or sjoe["pending_raw"] > 0:
            grp = {"type": "sJOE Staking",
                   "items": [{"symbol": sjoe["joe_symbol"], "balance": sjoe["staked"], "usd": None}]}
            if sjoe["pending_raw"] > 0:
                grp["rewards"] = [{"symbol": sjoe["reward_symbol"], "balance": sjoe["pending"]}]
            result["groups"].append(grp)

    # PRIME group: include whenever there is any PRIME stake or in-account balance (the
    # in-account ~200 PRIME with an otherwise empty account is the expected current state).
    if tier["staked"] > 0 or tier["in_account"] > 0:
        result["groups"].append({
            "type": "PRIME", "tier": tier["tier"],
            "staked": tier["staked"], "in_account": tier["in_account"],
        })
    return result

def cmd_defi(as_json: bool = True):
    """Aggregate all DeltaPrime positions for the wallet. Default output is the DeBank-style
    JSON (the dashboard consumer). On error, emits {"status":"error", ...} rather than raising,
    so the caller always gets parseable JSON."""
    try:
        data = gather_defi()
    except Exception as e:
        data = {"protocol": "DeltaPrime", "chain": "avalanche",
                "status": "error", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(data, indent=2))

def main():
    try:
        _dispatch()
    except RuntimeError as e:
        print(f"deltaprime: {e}", file=sys.stderr)
        sys.exit(1)

def _dispatch():
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    # Global signing-key override: --key <0xhex>, stripped before command dispatch.
    global _CLI_KEY
    if "--key" in args:
        i = args.index("--key")
        if i + 1 >= len(args):
            print("--key requires a hex key. Example: --key 0xabc...")
            return
        _CLI_KEY = args[i + 1]
        del args[i:i + 2]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    cmd = args[0]
    if cmd == "pool-info":
        pool = args[1] if len(args) > 1 else "all"
        if pool != "all" and pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}, all")
            return
        cmd_pool_info(pool)
    elif cmd == "my-positions":
        cmd_my_positions()
    elif cmd == "deposit":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: deltaprime deposit --pool usdc --amount 100 [--execute]")
            return
        cmd_deposit(pool, amount, execute)
    elif cmd == "withdraw":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: deltaprime withdraw --pool usdc --amount 100 [--execute]")
            return
        cmd_withdraw(pool, amount, execute)
    elif cmd in ("create-prime-account", "create-account"):
        fund_pool, fund_amount = None, None
        for i, a in enumerate(args):
            if a == "--fund-pool" and i + 1 < len(args): fund_pool = args[i + 1]
            if a == "--fund-amount" and i + 1 < len(args): fund_amount = float(args[i + 1])
        if (fund_pool is None) != (fund_amount is None):
            print("Pass both --fund-pool and --fund-amount, or neither.")
            return
        if fund_pool is not None and fund_pool not in POOLS:
            print(f"Unknown pool '{fund_pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_create_prime_account("--execute" in args, fund_pool, fund_amount)
    elif cmd == "prime-summary":
        cmd_prime_summary()
    elif cmd == "defi":
        cmd_defi("--json" in args)
    elif cmd == "fund":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: deltaprime fund --pool usdc --amount 100 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_fund(pool, amount, execute)
    elif cmd in ("borrow", "repay"):
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print(f"Usage: deltaprime {cmd} --pool usdc --amount 100 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        (cmd_borrow if cmd == "borrow" else cmd_repay)(pool, amount, execute)
    elif cmd == "swap":
        from_sym, to_sym, amount, slippage, via = None, None, None, 1.0, "yak"
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--from" and i + 1 < len(args): from_sym = args[i + 1]
            if a == "--to" and i + 1 < len(args): to_sym = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--via" and i + 1 < len(args): via = args[i + 1]
        if not from_sym or not to_sym or amount is None:
            print("Usage: deltaprime swap --from USDC --to AVAX --amount 10 [--via yak|paraswap] [--slippage 0.5] [--execute]")
            return
        cmd_swap(from_sym, to_sym, amount, slippage, via, execute)
    elif cmd == "swap-debt":
        from_sym, to_sym, amount, slippage = None, None, None, 1.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--from" and i + 1 < len(args): from_sym = args[i + 1]
            if a == "--to" and i + 1 < len(args): to_sym = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
        if not from_sym or not to_sym or amount is None:
            print("Usage: deltaprime swap-debt --from AVAX --to USDC --amount 100 [--slippage 0.5] [--execute]")
            return
        cmd_swap_debt(from_sym, to_sym, amount, slippage, execute)
    elif cmd == "withdraw-collateral":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: deltaprime withdraw-collateral --pool usdc --amount 100 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_withdraw_collateral(pool, amount, execute)
    elif cmd == "withdrawal-intents":
        cmd_withdrawal_intents()
    elif cmd == "execute-withdrawal":
        pool, index = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--index" and i + 1 < len(args): index = int(args[i + 1])
        if not pool:
            print("Usage: deltaprime execute-withdrawal --pool usdc [--index N] [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_execute_withdrawal(pool, index, execute)
    elif cmd == "gmx-positions":
        cmd_gmx_positions()
    elif cmd == "gmx-deposit":
        market, amount, side, slippage, fee_buffer = None, None, "long", 1.0, 2.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--market" and i + 1 < len(args): market = args[i + 1].lower()
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--side" and i + 1 < len(args): side = args[i + 1].lower()
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--fee-buffer" and i + 1 < len(args): fee_buffer = float(args[i + 1])
        if not market or amount is None:
            print("Usage: deltaprime gmx-deposit --market avax-usdc --amount 10 "
                  "[--side long|short] [--slippage 1] [--fee-buffer 2] [--execute]")
            print(f"  markets: {', '.join(GMX_MARKETS)}")
            return
        if side not in ("long", "short"):
            print("--side must be 'long' or 'short' (ignored for single-sided GM+ markets).")
            return
        cmd_gmx_deposit(market, amount, side == "long", slippage, fee_buffer, execute)
    elif cmd == "gmx-withdraw":
        market, amount, slippage, fee_buffer = None, None, 1.0, 2.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--market" and i + 1 < len(args): market = args[i + 1].lower()
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--fee-buffer" and i + 1 < len(args): fee_buffer = float(args[i + 1])
        if not market or amount is None:
            print("Usage: deltaprime gmx-withdraw --market avax-usdc --amount 5 "
                  "[--slippage 1] [--fee-buffer 2] [--execute]")
            print(f"  markets: {', '.join(GMX_MARKETS)}")
            return
        cmd_gmx_withdraw(market, amount, slippage, fee_buffer, execute)
    elif cmd == "lb-positions":
        cmd_lb_positions()
    elif cmd == "lb-add":
        pair, amount_x, amount_y, shape, rng, slippage, id_slip = None, 0.0, 0.0, "spot", 5, 1.0, 5
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pair" and i + 1 < len(args): pair = args[i + 1].lower()
            if a == "--amount-x" and i + 1 < len(args): amount_x = float(args[i + 1])
            if a == "--amount-y" and i + 1 < len(args): amount_y = float(args[i + 1])
            if a == "--shape" and i + 1 < len(args): shape = args[i + 1].lower()
            if a == "--range" and i + 1 < len(args): rng = int(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--id-slippage" and i + 1 < len(args): id_slip = int(args[i + 1])
        if not pair or (amount_x <= 0 and amount_y <= 0):
            print("Usage: deltaprime lb-add --pair avax-usdc --amount-x N --amount-y M "
                  "[--shape spot|curve|bidask] [--range 5] [--slippage 1] [--id-slippage 5] [--execute]")
            print(f"  pairs: {', '.join(TJ_LB_PAIRS)}")
            return
        cmd_lb_add(pair, amount_x, amount_y, shape, rng, slippage, id_slip, execute)
    elif cmd == "lb-remove":
        pair, slippage = None, 1.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pair" and i + 1 < len(args): pair = args[i + 1].lower()
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
        if not pair:
            print("Usage: deltaprime lb-remove --pair avax-usdc [--slippage 1] [--execute]")
            print(f"  pairs: {', '.join(TJ_LB_PAIRS)}")
            return
        cmd_lb_remove(pair, slippage, execute)
    elif cmd == "sjoe-position":
        cmd_sjoe_position()
    elif cmd in ("sjoe-stake", "sjoe-unstake"):
        amount = None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if amount is None:
            print(f"Usage: deltaprime {cmd} --amount 100 [--execute]")
            return
        (cmd_sjoe_stake if cmd == "sjoe-stake" else cmd_sjoe_unstake)(amount, execute)
    elif cmd == "sjoe-claim":
        cmd_sjoe_claim("--execute" in args)
    elif cmd == "prime-tier":
        cmd_prime_tier()
    elif cmd == "prime-needed":
        borrow, tier = None, "premium"
        for i, a in enumerate(args):
            if a == "--borrow" and i + 1 < len(args): borrow = float(args[i + 1])
            if a == "--tier" and i + 1 < len(args): tier = args[i + 1].lower()
        if borrow is None:
            print("Usage: deltaprime prime-needed --borrow 1000 [--tier premium|basic]")
            return
        cmd_prime_needed(borrow, tier)
    elif cmd == "prime-activate":
        amount = None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        cmd_prime_activate(amount, execute)
    elif cmd == "prime-deposit":
        amount = None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if amount is None:
            print("Usage: deltaprime prime-deposit --amount 200 [--execute]")
            return
        cmd_prime_deposit(amount, execute)
    elif cmd == "prime-deactivate":
        cmd_prime_deactivate("--withdraw" in args, "--execute" in args)
    elif cmd in ("prime-unstake", "prime-repay"):
        amount = None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if amount is None:
            print(f"Usage: deltaprime {cmd} --amount 100 [--execute]")
            return
        (cmd_prime_unstake if cmd == "prime-unstake" else cmd_prime_repay)(amount, execute)
    elif cmd == "zap":
        market, collateral, side = None, None, "short"
        collateral_amount, borrow_amount, deposit_amount = None, None, None
        slippage, fee_buffer = 1.0, 2.0
        swap_to_long = "--swap" in args
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--market" and i + 1 < len(args): market = args[i + 1].lower()
            if a == "--collateral" and i + 1 < len(args): collateral = args[i + 1].lower()
            if a == "--collateral-amount" and i + 1 < len(args): collateral_amount = float(args[i + 1])
            if a == "--borrow-amount" and i + 1 < len(args): borrow_amount = float(args[i + 1])
            if a == "--deposit-amount" and i + 1 < len(args): deposit_amount = float(args[i + 1])
            if a == "--side" and i + 1 < len(args): side = args[i + 1].lower()
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--fee-buffer" and i + 1 < len(args): fee_buffer = float(args[i + 1])
        if not market or not collateral or collateral_amount is None \
                or borrow_amount is None or deposit_amount is None:
            print("Usage: deltaprime zap --market avax-usdc --collateral wavax "
                  "--collateral-amount 1 --borrow-amount 30 --deposit-amount 30 "
                  "[--side long|short] [--swap] [--slippage 1] [--fee-buffer 2] [--execute]")
            print(f"  GM markets: {', '.join(k for k, m in GMX_MARKETS.items() if not m['plus'])}")
            print("  Leveraged-long macro: fund collateral -> borrow USDC -> [--swap USDC->long] "
                  "-> GMX GM deposit. Each leg is its own tx; --execute stops on the first failure.")
            return
        cmd_zap(market, collateral, collateral_amount, borrow_amount, deposit_amount,
                side, swap_to_long, slippage, fee_buffer, execute)
    else:
        print(f"Unknown command: {cmd}\n{__doc__}")

if __name__ == "__main__":
    main()
