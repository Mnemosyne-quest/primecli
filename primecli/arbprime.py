#!/usr/bin/env python3
"""DeltaPrime Protocol interaction module (Arbitrum One, chain 42161).

Sister deployment to DeltaPrime on Avalanche, same team + EIP-2535 Diamond +
per-user Smart Loan architecture. Lending pools take direct EOA
deposits/withdrawals. Borrowing and leverage go through a Prime Account: a
per-user SmartLoan (diamond proxy) created via the SmartLoansFactory. The EOA
owns it; borrow/repay/fund run on the Prime Account, which itself talks to the
pools. The native wrapped asset on Arbitrum is WETH (account symbol "ETH").

Usage:
  arbprime pool-info [usdc|eth|arb|btc|all] [--json]
  arbprime my-positions
  arbprime deposit --pool usdc --amount 100 [--execute]
  arbprime withdraw --pool usdc --amount 100 [--execute]   (step 1: 24h WithdrawalIntent, NOT instant)
  arbprime withdrawal-requests                                          (lists pending lender intents)
  arbprime execute-withdrawal-request --pool usdc [--index N] [--execute]   (step 2; post-maturity)
  arbprime cancel-withdrawal-request --pool usdc --index N [--execute]
  arbprime create-prime-account [--execute]   (alias: create-account)
  arbprime create-prime-account --fund-pool usdc --fund-amount 100 [--execute]
  arbprime prime-summary
  arbprime defi --json          (aggregate ALL positions as DeBank-style JSON; read-only)
  arbprime equity [--json] [--account 0x..]   (net equity = total value - debt + unclaimed rewards; read-only)
  arbprime fund --pool usdc --amount 100 [--execute]
  arbprime borrow --pool usdc --amount 100 [--execute]
  arbprime repay --pool usdc --amount 100 [--execute]
  arbprime swap --from USDC --to ETH --amount 10 [--via yak|paraswap] [--slippage 0.5] [--execute]
  arbprime swap-debt --from ETH --to USDC --amount 100 [--slippage 0.5] [--fallback] [--execute]
  arbprime withdraw-collateral --pool usdc --amount 100 [--execute]
  arbprime withdrawal-intents
  arbprime execute-withdrawal --pool usdc [--index N] [--execute]
  arbprime cancel-withdrawal --pool usdc --index N [--execute]
  arbprime gmx-positions
  arbprime gmx-deposit --market eth-usdc --amount 500 [--side auto|long|short] [--slippage 1] [--fee-buffer 2] [--execute]
  arbprime gmx-withdraw --market eth+ --amount 5 [--slippage 1] [--fee-buffer 2] [--execute]
  arbprime glv-positions
  arbprime glv-deposit --vault weth-usdc --amount 500 [--side auto|long|short] [--target-market GM_ETH_WETH_USDC] [--slippage 1] [--fee-buffer 2] [--execute]
  arbprime glv-withdraw --vault weth-usdc --amount 5 [--target-market GM_ETH_WETH_USDC] [--slippage 1] [--fee-buffer 2] [--execute]
  arbprime lb-positions
  arbprime lb-add --pair eth-usdc --amount-x 0.1 --amount-y 300 [--shape spot|curve|bidask] [--range 5] [--slippage 1] [--id-slippage 5] [--execute]
  arbprime lb-remove --pair eth-usdc [--slippage 1] [--execute]
  arbprime lb-positions
  arbprime prime-tier
  arbprime prime-needed --borrow 1000 [--tier premium|basic]
  arbprime prime-deposit --amount 200 [--execute]
  arbprime prime-activate [--amount N] [--execute]
  arbprime prime-deactivate [--withdraw] [--execute]
  arbprime prime-unstake --amount N [--execute]
  arbprime prime-repay --amount N [--execute]
  arbprime zap --market eth-usdc --collateral usdc --collateral-amount 100 --borrow-amount 400 [--side auto|long|short] [--deposit-amount 500] [--swap] [--slippage 1] [--fee-buffer 2] [--execute]

GMX V2 markets (--market): two-sided GM (eth-usdc, btc-usdc, arb-usdc, link-usdc, uni-usdc,
gmx-usdc, near-usdc, atom-usdc, sui-usdc, sei-usdc); single-sided GM+ (eth+, btc+, gmx+).
The four synthetics (near/atom/sui/sei) deposit WETH as the long leg but price off their own
RedStone feed. GLV vaults (--vault): weth-usdc, btc-usdc.

prime-summary reports live solvency (health ratio, total value, debt, solvent flag) from
SolvencyFacetProdArbitrum, read via eth_call with a RedStone price payload appended (falls
back to balances-only if the gateway is unreachable).

NOTE: prime-summary shows TWO health metrics — don't confuse them:
  - "Health ratio (chain)":  on-chain getHealthRatio. 1.0 = liquidation, >1.0 = solvent.
  - "Health (0-100%)": equity-based, frontend-style. 0% = liquidation, 50% = half
    borrowing power used, 100% = no debt.
  Formula for the latter: equity=supplied-debt, max_debt=equity*(tier-1),
  pct=(max_debt-debt)/max_debt*100. tier=5 (BASIC) or 10 (PREMIUM).

Collateral withdrawal is a two-step, time-delayed flow on the Prime Account (there is NO
instant withdraw of in-account collateral). The savings-pool `withdraw` above is a separate
two-step intent flow on the pool itself, not the Prime Account. Step 1: `withdraw --pool X
--amount Y` calls the pool's createWithdrawalIntent(uint256), oracle-free. Step 2 (after the
24h time-lock, within the following 48h execute window — 72h total): `execute-withdrawal-request
--pool X [--index N]` consumes the matured intent via the pool's two-arg intent-gated executor
`withdraw(uint256 _amount, uint256[] intentIndices)` (selector 0x5915d806, same as the
DegenPrime pool — NOT the single-arg withdraw, which never resolves a named intent).
`withdrawal-requests` lists pending lender intents per pool; `cancel-withdrawal-request --pool X
--index N` cancels a pending one via cancelWithdrawalIntent(uint256). No RedStone on any of
these; storage is per-EOA on the pool (NOT the Prime Account). The separate Prime Account
collateral flow is withdraw-collateral registers a WithdrawalIntent (createWithdrawalIntent, no
RedStone). The intent becomes executable ~24h later for a 48h execute window (72h total);
execute-withdrawal then pulls it to the wallet (executeWithdrawalIntent, RedStone-gated). withdrawal-intents
lists pending intents + per-asset available balance (oracle-free reads). The maturity window
and ready/expired state come straight off-chain from the IntentInfo struct.

Leverage flow: create-prime-account -> fund (collateral) -> borrow -> repay -> withdraw.
fund moves collateral from the wallet into the Prime Account; borrow needs a funded
account. ERC20 assets approve the account then call fund(); native ETH (eth pool)
uses the payable depositNativeToken(). create-prime-account --fund-* does both in one
tx via createAndFundLoan() (ERC20 only).

swap trades one in-account asset for another on the Prime Account, via either aggregator
route (--via, default yak):
  - yak (YieldYakSwapArbitrumFacet.yakSwap): the YieldYak router's findBestPath derives the
    path+adapters off-chain; the swap runs against the account's in-account balance of
    the --from asset. Every adapter must be whitelisted on the facet.
  - paraswap (ParaSwapFacet.paraSwapV6): the ParaSwap/Velora v6.2 API for Arbitrum
    builds the swap calldata (/prices price route -> /transactions tx data). The facet
    takes paraSwapV6(bytes4 selector, bytes data) — we split the API calldata into its
    4-byte selector + remaining bytes and pass them through. Only the two router methods
    the facet decodes are accepted: swapExactAmountIn (0xe3ead59e) and
    swapExactAmountInOnUniswapV3 (0x876a02f6). The facet enforces a hard 5% slippage cap
    (RedStone-priced) regardless of --slippage.
Both routes carry remainsSolvent, so --execute appends a RedStone signed-price payload to
the calldata (see the RedStone wrapping helpers below). Asset names are the bytes32
symbols (ETH/BTC/ARB/USDC), not the wrapped-token names.

swap-debt refinances debt from one asset into another in a single tx via
SwapDebtFacet.swapDebtParaSwap(_fromAsset, _toAsset, _repayAmount, _borrowAmount, selector,
data): it borrows --amount of the NEW debt asset (--to), ParaSwaps it into the OLD debt
asset (--from), and repays the old debt. --from is the existing debt being refinanced;
--to is the new debt taken on. The facet enforces a hard 5% cap on the USD-value
difference between the repaid and borrowed amounts (RedStone-priced), and requires the
ParaSwap quote's fromAmount to equal the borrow amount exactly. RedStone-gated on execute.

gmx-deposit / gmx-withdraw open/close GMX V2 GM (two-sided) and GM+ (single-sided) LP
positions on the Prime Account, via GmxV2FacetArbitrum (deposit{Eth,Btc,Arb,...}UsdcGmxV2 /
withdraw{...}UsdcGmxV2) and GmxV2PlusFacetArbitrum (deposit{Eth,Btc,Gmx}GmxV2Plus /
withdraw{...}GmxV2Plus). Markets (--market): eth-usdc, btc-usdc, arb-usdc, link-usdc,
uni-usdc, gmx-usdc, near-usdc, atom-usdc, sui-usdc, sei-usdc (GM); eth+, btc+, gmx+ (GM+).
gmx-deposit takes an in-account underlying (two-sided: --side long|short, long = volatile
leg, short = USDC; GM+ ignores --side); gmx-withdraw burns GM tokens. The near/atom/sui/sei
markets are GMX synthetics: the long leg DEPOSITS WETH (per the facet's _deposit), but the
position is PRICED off the synthetic's own RedStone feed (NEAR/ATOM/SUI/SEI). So each market
carries a separate "long" (price-feed symbol) and "long_token" (the ERC20 to deposit/decimals).
  - These functions are PAYABLE + ASYNC. They pay a GMX execution fee as msg.value (== the
    executionFee arg; the facet reverts InvalidExecutionFee if they differ), queue the
    request on the GMX ExchangeRouter, and a GMX KEEPER executes it some blocks later via a
    callback. The position does NOT appear/disappear instantly, and the Prime Account is
    FROZEN for that market until the keeper callback fires. The fee is estimated from the GMX
    DataStore gas params (callbackGasLimit 600000) times the gas price, padded by --fee-buffer
    (default 2x) to survive a gas-price rise before keeper execution; GMX refunds any excess
    to the account. The EOA also needs ETH for its own tx gas on top of the execution fee.
  - minGmAmount (deposit) / min long+short token amounts (withdraw) are slippage floors set
    from the RedStone oracle prices minus --slippage. The facet's isWithinBounds check
    HARD-CAPS slippage at 5% (±5% of the oracle estimate) — looser reverts InvalidMinOutput.
  - RedStone-gated: --execute appends a signed price payload (GM feed + underlyings). The GM
    token price has no SolvencyFacet feed, so it is read from the RedStone gateway median (the
    same on-demand value the facet aggregates from calldata).
gmx-positions is read-only: per owned market it shows the GM balance after the accrued
performance fee (SmartLoanViewFacet.getGmTokenBalanceAfterFees) and the annualised
performance (getGm[Plus]Performance) — both RedStone-gated views, eth_call'd with a payload.

glv-deposit / glv-withdraw open/close GMX GLV (GMX Liquidity Vault) positions on the Prime
Account via GlvFacetArbitrum. A GLV is a vault of GM markets; deposit{Weth,Btc}UsdcGlv take an
EXTRA `targetMarket` arg vs GM (the GM market within the GLV to route liquidity into).
Vaults (--vault): weth-usdc, btc-usdc. Same PAYABLE + ASYNC + RedStone-gated + execution-fee
mechanic as GMX V2. --target-market defaults to the vault's primary GM market and can be
overridden. glv-positions is a best-effort balance read (GLV token balanceOf + gateway price
where available).

lb-add / lb-remove open/close TraderJoe (LFJ) V2 Liquidity Book positions on the Prime
Account via TraderJoeV2ArbitrumFacet (addLiquidityTraderJoeV2 / removeLiquidityTraderJoeV2),
exactly as on Avalanche: deltaIds[] bin offsets + distributionX/Y weightings (--shape
spot|curve|bidask over --range bins each side), amountX/Y slippage floors, --id-slippage
active-bin guard. addLiquidity is RedStone-gated; removeLiquidity is not. lb-remove closes
the account's ENTIRE position for the pair. lb-positions is the oracle-free position view.
Pairs (11, both tokens registered assets; facet whitelist verified on-chain 03-06-2026):
eth-usdc, eth-usdc-10, eth-usdt, eth-usdt-10, arb-eth, arb-eth-v22, btc-eth, gmx-eth,
joe-eth, wsteth-eth, weeth-eth. Max 300 bins per Prime Account on Arbitrum (the facet
overrides Avalanche's 80); both the preview and the on-chain facet enforce it.

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
hard-codes them. PRIME (18-dec) is a separate token from sPRIME and must be acquired on a DEX (the
Arbitrum PRIME-WETH pair). Verified against the verified PrimeLeverageFacet source.
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

import json, os, sys, time, re, base64, struct
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import requests
from eth_account import Account
from eth_keys import keys as eth_keys
from eth_abi import encode as abi_encode, decode as abi_decode
from web3 import Web3

# Health monitoring sub-system
# Try both package import (installed) and local import (standalone script)
_hm = None
for _mod in ('primecli.health_monitor', 'health_monitor'):
    try:
        _hm = __import__(_mod, fromlist=['cli'])
        break
    except ImportError:
        continue
if _hm is None:
    # Last resort: find health_monitor.py next to this script
    import importlib.util
    _hm_path = Path(__file__).parent / 'health_monitor.py'
    if _hm_path.exists():
        _spec = importlib.util.spec_from_file_location('health_monitor', _hm_path)
        _hm = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_hm)
health_monitor = _hm

# Version check (silent on network failure or old install)
try:
    from primecli import check_version
except ImportError:
    def check_version(*a, **kw): pass

# Arbitrum One RPC. Override with ARBPRIME_RPC for a paid Alchemy/Infura endpoint.
ARBITRUM_RPC = os.environ.get("ARBPRIME_RPC", "https://arb1.arbitrum.io/rpc")
EXPLORER = "https://arbiscan.io"  # display/links only — never used for ABI fetch
CHAIN_ID = 42161
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
# ── Agent / wallet selection ─────────────────────────────────────────────────
# Agent-agnostic: any agent (Parakletos, Paraklaudios, or another authorized agent) runs
# this same tool with its OWN wallet. The Prime Account is derived on-chain from the
# wallet owner (getLoanForOwner), so each agent automatically operates on its own
# Prime Account — no per-agent addresses are hardcoded. The same EVM key works on
# every chain, so the ARBPRIME_ env vars fall back to the DELTAPRIME_ equivalents.
#
# Key resolution order (first hit wins; see resolve_private_key):
#   1. --key <0xhex> CLI flag                                       -> raw 0x… key (one-off)
#   2. --as <agent> CLI flag                                        -> AGENTS[<agent>]
#   3. ARBPRIME_PRIVATE_KEY / DELTAPRIME_PRIVATE_KEY env var        -> raw 0x… key
#   4. ARBPRIME_KEY_FILE / DELTAPRIME_KEY_FILE env var              -> path to a file with the 0x key
#   5. ARBPRIME_ENV_FILE+ARBPRIME_KEY_VAR / DELTAPRIME_* equivalent -> read <var> from <file>
#   6. ARBPRIME_AGENT / DELTAPRIME_AGENT env var                    -> AGENTS[<agent>]
# If none resolve, fail closed (no silent default key).
#
# To add another wallet: add a row to AGENTS, export ARBPRIME_PRIVATE_KEY, or pass --key.
AGENTS = {
    "parakletos":   ("/root/.openclaw/.env",                "PARAKLETOS_EVM_PRIVATE_KEY"),
    "paraklaudios": ("/root/paraklaudios/.credentials.env", "PARAKLAUDIOS_EVM_PRIVATE_KEY"),
}
_SELECTED_AGENT = None        # set by the --as CLI flag in main()
_CLI_KEY = None               # set by the --key CLI flag in main()
_OWNER_ADDRESS = None          # set by --owner for keyless read-only commands (main())
# Core protocol addresses — the LIVE Arbitrum deployment (DeploymentConstants.sol),
# on-chain verified 2026-06-03. The stale *TUP.json deployment (factory 0x97f4C81…)
# has only ETH+USDC pools — NOT used here.
FACTORY_PROXY = "0xFf5e3dDaefF411a1dC6CcE00014e4Bca39265c20"  # SmartLoansFactory
# On-chain registry of active pools + collateral assets. getAssetAddress(bytes32,bool)
# is the source of truth (getTokenAddress does NOT exist on this TokenManager).
TOKEN_MANAGER = "0x0a0D954d4b0F0b47a5990C0abd179A90fF74E255"
# SmartLoan diamond beacon (27 facets). Every Prime Account is a per-user proxy that
# delegates here, so the facet ABIs (borrow/repay/fund + view fns) are reachable at any
# deployed account address. Sourced from SmartLoansFactory.smartLoanDiamond().
SMART_LOAN_DIAMOND = "0x62Cf82FB0484aF382714cD09296260edc1DC0c6c"

# YieldYak aggregator router (Arbitrum — DIFFERENT from Avalanche's 0xC472…488c).
# findBestPath() is read-only and returns the optimal multi-hop route (path + per-hop
# adapter addresses). The Prime Account's YieldYakSwapArbitrumFacet executes yakSwap()
# over those, requiring every adapter to be whitelisted (isWhitelistedAdapterOptimized).
YAK_ROUTER = "0xb32C79a25291265eF240Eb32E9faBbc6DcEE3cE3"

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
    # Historical static whitelist. Since the protocol-level facet fix (confirmed
    # 2026-06-04), API-returned executors outside this set can be VALID — Velora
    # rotates executors per quote (seen: 0x8faa…e820, 0x6f05…0900). The swap paths
    # now decide by eth_call simulation of the exact tx, not by this set; it's kept
    # only to label "known" vs "new" executors in output.
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57",
    "0x6a000f20005980200259b80c5102003040001068",
    "0x000010036c0190e009a000d0fc3541100a07380a",
    "0x00c600b30fb0400701010f4b080409018b9006e0",
    "0xa0f408a000017007015e0f00320e470d00090a5b",
}

# RedStone on-demand oracle config for DeltaPrime on Arbitrum — IDENTICAL to Avalanche
# (same data service, signers, threshold, marker, gateways). The Prime Account's solvency
# math (every remainsSolvent-gated facet call, plus oracle views like
# getHealthRatio/isSolvent/getTotalValue) reads signed prices appended to the tx calldata.
# data service "redstone-primary-prod", 3-of-5 unique authorised signers, default 3-minute
# staleness window. The 9-byte marker terminates a RedStone payload. DeltaPrime uses RedStone
# PRIMARY production (PrimaryProdDataServiceConsumerBase), not Classic. The authorised signer
# set and gateway endpoint MUST match.
REDSTONE_DATA_SERVICE = "redstone-primary-prod"
REDSTONE_SIGNERS_THRESHOLD = 3
REDSTONE_MARKER = bytes.fromhex("000002ed57011e0000")
REDSTONE_GATEWAYS = [
    "https://oracle-gateway-1.a.redstone.finance",
    "https://oracle-gateway-2.a.redstone.finance",
]

# Active pool proxies — LIVE Arbitrum deployment, on-chain verified 2026-06-03.
# getAllPoolAssets live = [USDC, DAI, BTC, ARB, ETH]; the DAI pool is DROPPED,
# leaving 4. The native-wrapped pool is `eth` (underlying WETH, account symbol
# "ETH"): its native deposit path uses depositNativeToken() (wraps ETH -> WETH).
POOLS = {
    "eth": {
        "proxy": "0x788A8324943beb1a7A47B76959E6C1e6B87eD360",
        "token": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "symbol": "ETH", "decimals": 18, "native": True,
    },
    "usdc": {
        "proxy": "0x8Ac9Dc27a6174a1CC30873B367A60AcdFAb965cc",
        "token": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "symbol": "USDC", "decimals": 6, "native": False,
    },
    "arb": {
        "proxy": "0xC629E8889350F1BBBf6eD1955095C2198dDC41c2",
        "token": "0x912CE59144191C1204E64559FE8253a0e49E6548",
        "symbol": "ARB", "decimals": 18, "native": False,
    },
    "btc": {
        "proxy": "0x0ed7B42B74F039eda928E1AE6F44Eed5EF195Fb5",
        "token": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
        "symbol": "BTC", "decimals": 8, "native": False,
    },
}

# GMX V2 token metadata for the GM market legs. SWAP_ASSETS only covers the 4 lending
# pools (ETH/USDC/ARB/BTC); GMX markets also touch LINK/UNI/GMX (real ERC20 legs) and the
# synthetic feeds NEAR/ATOM/SUI/SEI (no ERC20 leg — the long leg deposits WETH). Each entry:
# {addr, decimals, symbol}. addr/decimals are used to size the deposited ERC20; synthetics
# point addr at WETH (the deposited token) but keep their own feed symbol for pricing.
# Addresses from the verified Arbitrum address map (getAssetAddress / GMX facet constants).
_GMX_WETH = {"addr": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "symbol": "ETH", "decimals": 18}
_GMX_TOKENS = {
    "ETH":  _GMX_WETH,
    "USDC": {"addr": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "symbol": "USDC", "decimals": 6},
    "ARB":  {"addr": "0x912CE59144191C1204E64559FE8253a0e49E6548", "symbol": "ARB",  "decimals": 18},
    "BTC":  {"addr": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", "symbol": "BTC",  "decimals": 8},
    "LINK": {"addr": "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4", "symbol": "LINK", "decimals": 18},
    "UNI":  {"addr": "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0", "symbol": "UNI",  "decimals": 18},
    "GMX":  {"addr": "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a", "symbol": "GMX",  "decimals": 18},
}

# ─── GMX V2 GM / GM+ markets (LP) ────────────────────────────────────────────
# DeltaPrime mints/redeems GMX V2 market LP tokens (GM = two-sided long+short, GM+ =
# single-sided) through two diamond facets, reachable at any Prime Account address.
# Deposit/withdraw are PAYABLE + ASYNC: a GMX execution fee is paid as msg.value, the
# request is queued on the GMX ExchangeRouter, and a GMX keeper executes it some blocks
# later via the callbacks facet. The Prime Account is FROZEN for that market until the
# keeper callback fires. Facet/function signatures + the executionFee==msg.value rule are
# verified against GmxV2FacetArbitrum / GmxV2PlusFacetArbitrum (signatures identical to
# Avalanche).
#   GmxV2FacetArbitrum      0x3b84303BE9adB0e09d1657534704c9CbbE9d81A3 (two-sided)
#   GmxV2PlusFacetArbitrum  0x736D70bAbBA06FC54E42BBc329Ee82EB62241A11 (single-sided)
# Each market: the GM token, the RedStone GM feed id (priced off the gateway median —
# SolvencyFacet.getPrices has no feed for the GM symbol and reverts 0xec459bc0, so GM
# prices come straight from the gateway), and the facet deposit/withdraw fn stems. "long"
# is the long-leg RedStone PRICE-FEED symbol; "long_token" is the ERC20 actually deposited
# for the long leg + its decimals. For real markets long_token.symbol == long; for the
# synthetics (near/atom/sui/sei) the facet deposits WETH (long_token=WETH) but prices off
# the synthetic feed (long=NEAR/ATOM/SUI/SEI). "short" is always USDC (price + deposit).
# NB: the facet also has a SOL market (GM_SOL_SOL_USDC) but SOL is NOT in the registered
# 29-asset set, so it is intentionally OMITTED.
GMX_MARKETS = {
    # Two-sided GM markets (volatile leg + USDC). depositXUsdcGmxV2(bool isLongToken, ...).
    "arb-usdc": {
        "plus": False, "gm_token": "0xC25cEf6061Cf5dE5eb761b50E4743c1F5D7E5407",
        "long": "ARB", "long_token": _GMX_TOKENS["ARB"], "short": "USDC", "gm_feed": "GM_ARB_ARB_USDC",
        "deposit_fn": "depositArbUsdcGmxV2", "withdraw_fn": "withdrawArbUsdcGmxV2",
    },
    # Synthetic markets: index token is the synthetic, but the long leg DEPOSITS WETH
    # (facet: isLongToken ? WETH : USDC). Price the long leg off the synthetic's RedStone feed.
    "near-usdc": {
        "plus": False, "gm_token": "0x63Dc80EE90F26363B3FCD609007CC9e14c8991BE",
        "long": "NEAR", "long_token": _GMX_WETH, "short": "USDC", "gm_feed": "GM_NEAR_WETH_USDC",
        "synthetic": True,
        "deposit_fn": "depositNearUsdcGmxV2", "withdraw_fn": "withdrawNearUsdcGmxV2",
    },
    "sei-usdc": {
        "plus": False, "gm_token": "0xB489711B1cB86afDA48924730084e23310EB4883",
        "long": "SEI", "long_token": _GMX_WETH, "short": "USDC", "gm_feed": "GM_SEI_WETH_USDC",
        "synthetic": True,
        "deposit_fn": "depositSeiUsdcGmxV2", "withdraw_fn": "withdrawSeiUsdcGmxV2",
    },
    # Single-sided GM+ markets (one asset, no USDC short leg). depositXGmxV2Plus(...).
    # long == short underlying; the facet splits a deposit 50/50 across both legs.
    "gmx+": {
        "plus": True, "gm_token": "0xbD48149673724f9cAeE647bb4e9D9dDaF896Efeb",
        "long": "GMX", "long_token": _GMX_TOKENS["GMX"], "short": "GMX", "gm_feed": "GM_GMX_GMX",
        "deposit_fn": "depositGmxGmxV2Plus", "withdraw_fn": "withdrawGmxGmxV2Plus",
    },
}

# GMX V2 infra used for execution-fee estimation. The DataStore holds the gas-limit
# params; the keeper requires executionFee >= adjustedGasLimit * tx.gasprice at execution
# time, so the fee is estimated as (base + perOracle*count + estimate*multiplier/1e30) *
# gasPrice, then padded (see _estimate_gmx_execution_fee). callbackGasLimit is hard-coded
# to 600000 in both facets. Addresses are the verified Arbitrum GMX infra (address map).
GMX_DATASTORE = "0xFD70de6b91282D8017aA4E741e9Ae325CAb992d8"
GMX_READER = "0x470fbC46bcC0f16532691Df360A07d8Bf5ee0789"
GMX_CALLBACK_GAS_LIMIT = 600000
# GMX market token decimals are 18; the underlyings reuse the lending-pool decimals.
GM_TOKEN_DECIMALS = 18
# isWithinBounds (DiamondMethodsAccess) requires the USD value of the user's min-output to
# be within ±5% of the contract's own oracle estimate. So slippage on minGmAmount /
# min-token-outs is hard-capped at 5% — anything looser reverts InvalidMinOutputValue.
GMX_MAX_SLIPPAGE_PCT = 5.0

# ─── GMX GLV vaults (GlvFacetArbitrum) ───────────────────────────────────────
# A GLV (GMX Liquidity Vault) is a vault of GM markets; depositing routes liquidity into a
# chosen GM market WITHIN the vault. DeltaPrime exposes deposit/withdraw via GlvFacetArbitrum
# (0xCA9676425540D51BD3247c61bb9FC05eC10Ce1AB), reachable at any Prime Account. Same PAYABLE
# + ASYNC + RedStone-gated + execution-fee mechanic as GMX V2. Signatures verified against the
# verified GlvFacetArbitrum source:
#   depositWethUsdcGlv/depositBtcUsdcGlv(bool isLongToken, uint256 tokenAmount,
#       uint256 minGlvAmount, address targetMarket, uint256 executionFee)  — note the EXTRA
#       `targetMarket` arg vs GM (the GM market within the GLV to route into).
#   withdrawWethUsdcGlv/withdrawBtcUsdcGlv(uint256 glvAmount, address targetMarket,
#       uint256 minLongTokenAmount, uint256 minShortTokenAmount, uint256 executionFee)
# Each vault: the GLV token, the long-leg deposit token (WETH/WBTC; isLongToken?token:USDC),
# the long price-feed symbol, the default targetMarket GM token, the facet fn stems.
GLV_VAULTS = {
    "weth-usdc": {
        "glv_token": "0x528A5bac7E746C9A509A1f4F6dF58A03d44279F9",
        "long": "ETH", "long_token": _GMX_TOKENS["ETH"], "short": "USDC",
        "default_target": "0x70d95587d40A2caf56bd97485aB3Eec10Bee6336",  # GM_ETH_WETH_USDC
        "default_target_name": "GM_ETH_WETH_USDC",
        "deposit_fn": "depositWethUsdcGlv", "withdraw_fn": "withdrawWethUsdcGlv",
    },
    "btc-usdc": {
        "glv_token": "0xdF03EEd325b82bC1d4Db8b49c30ecc9E05104b96",
        "long": "BTC", "long_token": _GMX_TOKENS["BTC"], "short": "USDC",
        "default_target": "0x47c031236e19d024b42f8AE6780E44A573170703",  # GM_BTC_WBTC_USDC
        "default_target_name": "GM_BTC_WBTC_USDC",
        "deposit_fn": "depositBtcUsdcGlv", "withdraw_fn": "withdrawBtcUsdcGlv",
    },
}
GLV_TOKEN_DECIMALS = 18  # GLV LP tokens are 18-dec
# GLV Reader, if needed for richer pricing/position reads. glv-positions is a best-effort
# balance read; if a GLV gateway price feed is unavailable, USD is left null.
GLV_READER = "0x2C670A23f1E798184647288072e84054938B5497"

# ─── TraderJoe V2 Liquidity Book (concentrated liquidity) ────────────────────
# DeltaPrime LPs into TraderJoe (LFJ) V2 LB pairs through TraderJoeV2ArbitrumFacet
# (0x9DB8016429f61a0562f20D2C1aC7FA01dFe0aFe4), reachable at any Prime Account. The
# whitelist below is the facet's own getWhitelistedTraderJoeV2Pairs() (verified source,
# contracts/facets/arbitrum/TraderJoeV2ArbitrumFacet.sol, fetched 03-06-2026), filtered to
# pairs whose BOTH tokens are registered in the live TokenManager 29-asset set — the
# source list also whitelists DAI/USDC.e/WOO/GRAIL/MAGIC/ezETH pairs whose tokens are NOT
# registered assets (the facet's _getAvailableBalance(symbol) lookup would fail), so those
# are omitted. Every pair below was verified on-chain 03-06-2026 against arb1.arbitrum.io:
# eth_getCode > 0, canonical getTokenX()/getTokenY() order, and getBinStep() as listed.
# The two LB routers are LFJ's deterministic cross-chain deployments (same addresses as
# Avalanche), straight from the base facet's isRouterWhitelisted(); both verified to have
# code on Arbitrum. NOTE: maxBinsPerPrimeAccount() is 300 on Arbitrum (vs 80 on Avalanche)
# — the Arbitrum facet overrides it.
TJ_LB_FACET = "0x9DB8016429f61a0562f20D2C1aC7FA01dFe0aFe4"
TJ_ROUTER_V21 = "0xb4315e873dBcf96Ffd0acd8EA43f689D8c20fB30"
TJ_ROUTER_V22 = "0x18556DA13313f3532c54711497A8FedAC273220E"
TJ_MAX_BINS = 300

# Per-pair token metadata: ERC20 address, the account bytes32 symbol (for in-account
# balance reads + the RedStone feed), and decimals. Symbols are the live TokenManager
# registrations (note exact case on wstETH / weETH).
_T_WETH   = {"addr": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "symbol": "ETH",    "decimals": 18}
_T_USDC   = {"addr": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "symbol": "USDC",   "decimals": 6}
_T_USDT   = {"addr": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "symbol": "USDT",   "decimals": 6}
_T_ARB    = {"addr": "0x912CE59144191C1204E64559FE8253a0e49E6548", "symbol": "ARB",    "decimals": 18}
_T_WBTC   = {"addr": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", "symbol": "BTC",    "decimals": 8}
_T_GMX    = {"addr": "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a", "symbol": "GMX",    "decimals": 18}
_T_JOE    = {"addr": "0x371c7ec6D8039ff7933a2AA28EB827Ffe1F52f07", "symbol": "JOE",    "decimals": 18}
_T_WSTETH = {"addr": "0x5979D7b546E38E414F7E9822514be443A4800529", "symbol": "wstETH", "decimals": 18}
_T_WEETH  = {"addr": "0x35751007a407ca6FEFfE80b3cB397736D2cf4dbe", "symbol": "weETH",  "decimals": 18}

# Keys follow the deltaprime convention: tokenX-tokenY, with a binStep suffix where the
# same pair exists at two steps ("eth-usdc" bs15 v2.1 vs "eth-usdc-10" bs10 v2.2) and a
# version suffix where the binStep collides ("arb-eth" v2.1 vs "arb-eth-v22", both bs10).
TJ_LB_PAIRS = {
    "eth-usdc":    {"pair": "0x69f1216cB2905bf0852f74624D5Fa7b5FC4dA710", "router": TJ_ROUTER_V21, "binStep": 15, "tokenX": _T_WETH,   "tokenY": _T_USDC},
    "eth-usdc-10": {"pair": "0xb7236B927e03542AC3bE0A054F2bEa8868AF9508", "router": TJ_ROUTER_V22, "binStep": 10, "tokenX": _T_WETH,   "tokenY": _T_USDC},
    "eth-usdt":    {"pair": "0xd387c40a72703B38A5181573724bcaF2Ce6038a5", "router": TJ_ROUTER_V21, "binStep": 15, "tokenX": _T_WETH,   "tokenY": _T_USDT},
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

# ─── PRIME-token leverage tiers (PrimeLeverageFacet) ─────────────────────────
# DeltaPrime gates higher max-leverage behind staking the protocol's own PRIME token,
# via PrimeLeverageFacet reachable at any Prime Account. Two tiers (LeverageTierLib.
# LeverageTier enum, uint8 on the wire): BASIC=0 (~5x default) and PREMIUM=1 (10x).
# PREMIUM requires PRIME staked PROPORTIONAL to USD borrow (tieredPrimeStakingRatio) and
# accrues a PRIME-denominated rent-debt over time (tieredPrimeDebtRatio). BOTH ratios live
# in the TokenManager and are governance-mutable, so the tool NEVER hard-codes them — it
# calls getRequiredPrimeStake on-chain. Mechanism identical to Avalanche; verified against
# the verified PrimeLeverageFacet source (0.8.17, BUSL-1.1):
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
# PRIME token (18-dec) is resolved on-chain via TokenManager.getAssetAddress("PRIME", true)
# (_prime_token_contract prefers that lookup; the hardcoded addr below is the fallback/display
# value). Do NOT confuse with sPRIME; the facet stakes plain PRIME. On Arbitrum the PRIME DEX
# LP pair is PRIME-WETH (not PRIME-WAVAX); PRIME prices best via its RedStone/CoinGecko path.
PRIME_LEVERAGE_FACET = "0x5D3301e8ab82826B7A6761867961B308a7938dcc"
PRIME_TOKEN = {"addr": "0x3De81CE90f5A27C5E6A5aDb04b54ABA488a6d14E", "symbol": "PRIME", "decimals": 18}
PRIME_TIERS = {"basic": 0, "premium": 1}
PRIME_TIER_NAMES = {0: "BASIC", 1: "PREMIUM", 2: "_NON_EXISTENT"}

# Minimal ERC20 ABI: balanceOf is the only function we read off arbitrary tokens (wallet
# balances for cmd_my_positions). The approve selector is hot-loaded inline at write sites.
ERC20_BALANCE_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",'
    '"outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]'
)

# Pool ABI — hand-curated subset (totalSupply, totalBorrowed, balanceOf, getBorrowed,
# deposit, withdraw, getDepositRate, getBorrowingRate). DeltaPrime's Pool implementation
# is shared across Avalanche/Arbitrum/Base, and every pool function this tool calls is in
# this subset. Bound directly (no block-explorer ABI fetch — Arbiscan is display-only).
# Rates are 1e18-scaled annualised (same shape as the DegenPrime pool).
# The lender side runs the same 24h delayed-intent flow as the Prime Account collateral
# side: createWithdrawalIntent registers, cancelWithdrawalIntent kills a pending one,
# getUserIntents / getTotalIntentAmount are oracle-free reads. The matured-intent executor
# is `withdraw(uint256 _amount, uint256[] intentIndices)` (selector 0x5915d806) — the same
# two-arg intent-gated executor as the DegenPrime pool, NOT the single-arg `withdraw(uint256)`.
# The two-arg form is the one that resolves a lender's named intent (the single-arg
# withdraw(uint256) reverts without reaching the intent lookup). IntentInfo shape matches
# the Prime Account WithdrawalIntentFacet.
POOL_ABI = json.loads(
    '['
    # deposit(uint256) is non-payable on every pool. The native ETH path is the
    # separate depositNativeToken() entry below.
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    # depositNativeToken() is the payable native-ETH entry on the eth pool. Wraps ETH internally.
    '{"inputs":[],"name":"depositNativeToken","outputs":[],"stateMutability":"payable","type":"function"},'
    # withdrawNativeToken(uint256) unwraps on the way out (returns native ETH).
    # withdraw(uint256,uint256[]) is the intent-gated step-2 executor (returns the
    # wrapped token); see _encode_pool_withdraw + cmd_execute_withdrawal_request.
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"withdrawNativeToken","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_amount","type":"uint256"},{"name":"intentIndices","type":"uint256[]"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"amount","type":"uint256"}],"name":"createWithdrawalIntent","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"index","type":"uint256"}],"name":"cancelWithdrawalIntent","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"user","type":"address"}],"name":"getUserIntents","outputs":[{"components":[{"name":"amount","type":"uint256"},{"name":"actionableAt","type":"uint256"},{"name":"expiresAt","type":"uint256"},{"name":"isPending","type":"bool"},{"name":"isActionable","type":"bool"},{"name":"isExpired","type":"bool"}],"type":"tuple[]"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"user","type":"address"}],"name":"getTotalIntentAmount","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"totalSupply","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"totalBorrowed","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"getDepositRate","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"getBorrowingRate","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"getBorrowed","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}'
    ']'
)

# SmartLoansFactory ABI — hand-curated minimum surface. createLoan / createAndFundLoan
# for writes; getLoanForOwner for the per-EOA Prime Account lookup. Bound directly (no
# block-explorer ABI fetch); same factory shape across DeltaPrime deployments.
FACTORY_ABI = json.loads(
    '['
    '{"inputs":[],"name":"createLoan","outputs":[{"type":"address"}],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_fundedAsset","type":"bytes32"},{"name":"_amount","type":"uint256"}],"name":"createAndFundLoan","outputs":[{"type":"address"}],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"getLoanForOwner","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"}'
    ']'
)

# Multicall3 — deterministic deployment at the same address on every EVM chain
# (Avalanche, Base, Arbitrum). aggregate3(Call3[]) batches read-only calls into one
# eth_call; allowFailure=true per-call so a single revert returns success=false for that
# leg instead of blowing up the whole batch. Used to collapse per-pool / per-market
# fan-out loops (cmd_pool_info("all"), gather_lending, gather_gmx, my-positions) from N
# RPCs into 1. Address has code on Arbitrum (verified in the address map).
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_ABI = [
    {"inputs": [{"components": [
        {"name": "target", "type": "address"},
        {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"}], "type": "tuple[]", "name": "calls"}],
     "name": "aggregate3",
     "outputs": [{"components": [
         {"name": "success", "type": "bool"},
         {"name": "returnData", "type": "bytes"}], "type": "tuple[]", "name": "returnData"}],
     "stateMutability": "view", "type": "function"},
]

def multicall(w3, calls):
    """Batch read-only calls via Multicall3.aggregate3. Each call is a (target_address,
    calldata_bytes) tuple. Returns a list of (success, return_bytes) tuples in input order
    — the caller is responsible for decoding return_bytes against the original function's
    output types and treating success=False as a missing/reverted value. Empty input
    returns []. The whole batch round-trips in one eth_call; gas is paid by the simulated
    caller (zero address by default) so no key is required.

    For RedStone-gated views: append the RedStone payload to each leg's calldata before
    putting it in `calls`. Multicall3 only delegate-calls the target with the bytes you
    provide; the on-chain solvency parser still reads the payload from the calldata tail
    per leg. The same payload is replayed once per leg in the batch — redundant on the
    wire, but the gateway is still hit only once."""
    if not calls:
        return []
    mc = w3.eth.contract(address=Web3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI)
    args = [(Web3.to_checksum_address(t), True, d) for t, d in calls]
    raw = mc.functions.aggregate3(args).call()
    return [(bool(ok), bytes(rd)) for ok, rd in raw]

# Process-local Web3 singleton. Each get_w3() call previously constructed a fresh
# HTTPProvider — wasteful on multi-pool reads (cmd_pool_info("all"), gather_defi).
_W3 = None

def get_w3():
    """Process-local Arbitrum RPC client. Arbitrum is a standard rollup (like Base) — no
    POA middleware needed (and injecting it would error on Arbitrum block headers)."""
    global _W3
    if _W3 is None:
        _W3 = Web3(Web3.HTTPProvider(ARBITRUM_RPC))
    return _W3

def _tx_gas_price(w3) -> int:
    """Estimated per-gas cost for balance checks (gas buffer pre-flights). Returns 2x the
    current base fee with a 0.01 gwei floor. NOTE: _tx_gas_price is NOT used for tx building;
    use _set_gas_price() for that (it handles chain-specific EIP-1559 vs legacy gas pricing).
    The GMX keeper execution-fee floor is 1 gwei (see _estimate_gmx_execution_fee)."""
    return max(int(w3.eth.gas_price * 2), 10**7)

def _set_gas_price(w3, tx_dict):
    """Set appropriate gas price fields for the chain, replacing the legacy gasPrice approach.
    On EIP-1559 chains (Arbitrum, Base, Avalanche post-Etna): sets maxFeePerGas +
    maxPriorityFeePerGas with a 2x base-fee hedge (base + prio + 1 gwei buffer).
    Falls back to legacy gasPrice only if the tx dict already lacks EIP-1559 fields
    and the chain doesn't support max_priority_fee.
    (25 gwei was the pre-Etna C-chain minimum; ACP-125 (Dec 2024) lowered the min base
    fee to 1 nAVAX — base now sits at ~0.01 nAVAX, so a 25 gwei floor overpaid ~2500x
    and inflated the upfront balance requirement past small EOAs.)"""
    # If build_transaction already set EIP-1559 fields, don't touch them
    if "maxFeePerGas" in tx_dict or "maxPriorityFeePerGas" in tx_dict:
        tx_dict.pop("gasPrice", None)
        return
    tx_dict.pop("gasPrice", None)
    try:
        base = w3.eth.gas_price
        prio = w3.eth.max_priority_fee
        tx_dict["maxFeePerGas"] = max(int(base * 2), base + prio + 10**9)
        tx_dict["maxPriorityFeePerGas"] = prio
    except Exception:
        # Legacy chain — use gasPrice instead
        tx_dict["gasPrice"] = max(int(w3.eth.gas_price * 2), 1 * 10**9)

def _estimate_gas_limit(w3, tx_dict, fallback_gas: int, buffer_bps: int = 1250) -> int:
    """Estimate gas for final calldata and add a buffer.

    Solvency-gated swap paths append RedStone payloads and can vary materially by
    route. A fixed cap can pass simulation at a high gas allowance, then revert
    out-of-gas on broadcast. If the RPC cannot estimate, keep the old fixed cap.
    """
    try:
        call_tx = {k: tx_dict[k] for k in ("from", "to", "data", "value") if k in tx_dict}
        estimated = int(w3.eth.estimate_gas(call_tx))
        return max(int(fallback_gas), (estimated * int(buffer_bps) + 999) // 1000)
    except Exception:
        return int(fallback_gas)

def _set_gas_price_for(chain_id, w3, tx_dict):
    """Set gas fields for an EXPLICIT chain_id rather than the module CHAIN_ID. Needed by
    cross-chain flows (prime-bridge) where a tx may target Avalanche or Arbitrum regardless
    of which tool built it."""
    # If build_transaction already set EIP-1559 fields, don't touch them
    if "maxFeePerGas" in tx_dict or "maxPriorityFeePerGas" in tx_dict:
        tx_dict.pop("gasPrice", None)
        return
    tx_dict.pop("gasPrice", None)
    try:
        base = w3.eth.gas_price
        prio = w3.eth.max_priority_fee
        tx_dict["maxFeePerGas"] = max(int(base * 2), base + prio + 10**9)
        tx_dict["maxPriorityFeePerGas"] = prio
    except Exception:
        tx_dict["gasPrice"] = max(int(w3.eth.gas_price * 2), 1 * 10**9)

def _read_env_var(path, var):
    """Return the value of `var` from a KEY=VALUE env file, or None if absent."""
    try:
        for line in Path(path).read_text().splitlines():
            s = line.strip()
            if s.startswith(var + "="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    return None

def _agent_key(agent):
    if agent not in AGENTS:
        raise RuntimeError(
            f"Unknown agent '{agent}'. Known agents: {', '.join(AGENTS)}. "
            f"Or set ARBPRIME_PRIVATE_KEY, or ARBPRIME_ENV_FILE + ARBPRIME_KEY_VAR."
        )
    path, var = AGENTS[agent]
    key = _read_env_var(path, var)
    if not key:
        raise RuntimeError(f"{var} not found in {path} (agent '{agent}').")
    return key

def _sign_and_send(w3, acct, tx, label, timeout=180, fallback_gas=3000000, buffer_bps=1250, gas_price_fn=None):
    """Sign, send, wait for a tx with gas estimation + OOG retry + error surfacing.

    Always estimates gas from final calldata (incl. RedStone payload) then adds a
    buffer. If the tx fails with status=0 and gasUsed == gasLimit (out of gas),
    retries once with 50% more buffer. Surfaces the gas stats on any failure.

    Gas limit override logic:
    - If tx dict has a non-None "gas" key, use that as the starting fallback_gas
      (removes the dict key so estimation is authoritative).
      This lets callers set a minimum via fallback_gas without hardcoding the
      broadcast limit.
    - Always runs _estimate_gas_limit to compute the final cap.
    - Ignores any "gas" key set in the tx dict before calling this function.

    Returns the receipt (status 0 or 1). Prints status and tx link.
    """
    # If caller left a stale gas value in the dict, discard it — estimation is authoritative
    tx.pop("gas", None)
    tx["gas"] = _estimate_gas_limit(w3, tx, fallback_gas, buffer_bps)
    if gas_price_fn:
        gas_price_fn(w3, tx)
    else:
        _set_gas_price(w3, tx)
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    ok = receipt["status"] == 1
    if ok:
        print(f"{'✓'} {label} confirmed")
        print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
        return receipt

    # Failure analysis
    gas_used = receipt.get("gasUsed", 0)
    gas_limit = tx.get("gas", 1)
    is_oog = gas_used >= gas_limit
    print(f"{'✗'} {label} failed (gasUsed={gas_used:,} / limit={gas_limit:,})")
    if is_oog:
        new_bps = buffer_bps * 3 // 2
        print(f"  Out of gas. Retrying with {new_bps//10}% buffer...")
        tx["nonce"] = w3.eth.get_transaction_count(acct.address)
        tx.pop("gas", None)
        tx["gas"] = _estimate_gas_limit(w3, tx, int(fallback_gas * 1.5), new_bps)
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        ok = receipt["status"] == 1
        if ok:
            print(f"{'✓'} {label} confirmed on retry")
            print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
        else:
            print(f"{'✗'} {label} failed again on retry")
        return receipt
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    return receipt


def resolve_private_key():
    # The same EVM key works on every chain, so each ARBPRIME_ var falls back to its
    # DELTAPRIME_ equivalent (exactly how degenprime falls back to DELTAPRIME_*).
    # 1. --key <0xhex> CLI flag (set in main)
    if _CLI_KEY:
        return _CLI_KEY.strip()
    # 2. --as <agent> CLI flag (set in main)
    if _SELECTED_AGENT:
        return _agent_key(_SELECTED_AGENT)
    # 3. raw key directly in the environment
    for env_var in ("ARBPRIME_PRIVATE_KEY", "DELTAPRIME_PRIVATE_KEY"):
        raw = os.environ.get(env_var)
        if raw:
            return raw.strip()
    # 4. path to a file containing the 0x key
    for path_var in ("ARBPRIME_KEY_FILE", "DELTAPRIME_KEY_FILE"):
        key_file = os.environ.get(path_var)
        if key_file:
            try:
                return Path(key_file).read_text().strip()
            except FileNotFoundError:
                raise RuntimeError(f"{path_var} points at {key_file} but the file does not exist.")
    # 5. explicit env-file + var-name
    env_file = os.environ.get("ARBPRIME_ENV_FILE") or os.environ.get("DELTAPRIME_ENV_FILE")
    key_var = os.environ.get("ARBPRIME_KEY_VAR") or os.environ.get("DELTAPRIME_KEY_VAR")
    if env_file and key_var:
        key = _read_env_var(env_file, key_var)
        if not key:
            raise RuntimeError(f"{key_var} not found in {env_file}.")
        return key
    # 6. named agent in the environment
    agent = os.environ.get("ARBPRIME_AGENT") or os.environ.get("DELTAPRIME_AGENT")
    if agent:
        return _agent_key(agent)
    # No silent default — fail closed.
    raise RuntimeError(
        "No signing key found. Pass --key <0xhex> or --as <agent>, or set "
        "ARBPRIME_PRIVATE_KEY (raw 0x... key), ARBPRIME_KEY_FILE (path to a file with "
        "the key), or ARBPRIME_ENV_FILE + ARBPRIME_KEY_VAR. DELTAPRIME_* equivalents "
        "also work (same key, all chains)."
    )

def get_account() -> Account:
    # --owner provides a keyless read-only account (address only, cannot sign) for
    # monitoring/sim reads that need the wallet owner (e.g. to locate a Prime Account)
    # but never broadcast. Write paths are blocked in main() when --owner is set.
    if _OWNER_ADDRESS:
        class _ReadOnlyAccount:
            def __init__(self, address):
                self.address = Web3.to_checksum_address(address)
        return _ReadOnlyAccount(_OWNER_ADDRESS)
    return Account.from_key(resolve_private_key())

def to_wei_units(amount, decimals):
    """Convert a human amount to integer base units without float drift."""
    return int(Decimal(str(amount)) * (10 ** int(decimals)))

def get_pool_contract(pool_name: str):
    """Pool proxy contract bound directly to the hand-curated POOL_ABI (no block-explorer
    ABI fetch — Arbiscan is display-only here; the hand-curated subset covers every
    function the tool calls)."""
    cfg = POOLS[pool_name]
    proxy = Web3.to_checksum_address(cfg["proxy"])
    w3 = get_w3()
    return w3.eth.contract(address=proxy, abi=POOL_ABI), cfg, w3

# Minimal Prime Account ABI: only the facet functions this tool calls. The diamond
# beacon's own ABI exposes beacon-management only, so the borrow/repay/fund and
# view selectors live in facets — we hand-pick the verified signatures here rather
# than enumerate 26 facet contracts at runtime.
# Facet addresses below are the LIVE Arbitrum diamond facets (re-derived by selector off
# the beacon, address map 2026-06-03). The diamond routes by selector, so all are reachable
# at any Prime Account address — these are just provenance notes.
#   borrow/repay/fund: AssetsOperations 0x53C1F700211BBBdcb3077BaaED5C76b2Bb64A567
#   depositNativeToken: DepositNativeToken facet 0x8D784A9bEab8eE3517b2B686616F9889e6994D95
#   getDebts/getBalance/getAllOwnedAssets/getGm*: Balances/StakedPositions view 0xf33ca4515d75DDC22765dB156264b69530cCfa51
#   yakSwap/isWhitelistedAdapterOptimized: YieldYakSwapArbitrumFacet 0xa60cD8eBbB1C612177aE1098C80c6c30da8ec6B3 (yakSwap RedStone-gated)
#   getHealthRatio/isSolvent/getTotalValue/getDebt/getPrices: SolvencyFacetProdArbitrum 0x2a43C8Db8DAc47fA5B62E5343005458ac7Bf2a8F (RedStone-gated)
#   createWithdrawalIntent/executeWithdrawalIntent/getUserIntents/getAvailableBalance/getTotalIntentAmount:
#     WithdrawalIntentFacet 0xa8DF1C6Aa5E04e8Aa473EaAE56B1216717e9c52A (executeWithdrawalIntent RedStone-gated; others oracle-free)
#   paraSwapV6: ParaSwapFacet 0x641493cB5143980E9e71f45442144D65CB19f90A; swapDebtParaSwap: SwapDebtFacet 0xdc168a1F130F6416a8D77b1F8A49D232520Bc576
#   GMX V2: GmxV2FacetArbitrum 0x3b84303BE9adB0e09d1657534704c9CbbE9d81A3 / GmxV2PlusFacetArbitrum 0x736D70bAbBA06FC54E42BBc329Ee82EB62241A11
#   GLV: GlvFacetArbitrum 0xCA9676425540D51BD3247c61bb9FC05eC10Ce1AB; PRIME: PrimeLeverageFacet 0x5D3301e8ab82826B7A6761867961B308a7938dcc
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
    # 4-byte method selector and the remaining ABI-encoded args. Signatures identical to
    # Avalanche.
    {"inputs": [{"name": "selector", "type": "bytes4"}, {"name": "data", "type": "bytes"}],
     "name": "paraSwapV6", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_fromAsset", "type": "bytes32"}, {"name": "_toAsset", "type": "bytes32"},
                {"name": "_repayAmount", "type": "uint256"}, {"name": "_borrowAmount", "type": "uint256"},
                {"name": "selector", "type": "bytes4"}, {"name": "data", "type": "bytes"}],
     "name": "swapDebtParaSwap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "owner", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    # SolvencyFacetProdArbitrum views — RedStone-gated. getTotalValue/getDebt are
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
    # ─── GMX V2 GM / GM+ LP + GLV (GmxV2FacetArbitrum / GmxV2PlusFacetArbitrum / GlvFacetArbitrum) ───
    # All deposit/withdraw fns are PAYABLE and require executionFee == msg.value (the facet
    # reverts InvalidExecutionFee otherwise). Two-sided GM deposits take a leading bool
    # isLongToken (true = volatile/synthetic leg, false = USDC); GM+ deposits omit it. GLV
    # deposits add a `targetMarket` address before executionFee. Withdraws take gmAmount + min
    # long/short token floors (GLV withdraws add targetMarket after gmAmount). Gated by an
    # inline RedStone-priced solvency simulation + isWithinBounds, so --execute appends a
    # signed price payload. getGm[Plus]Performance / getGmTokenBalanceAfterFees read RedStone
    # prices too (they revert 0xe7764c9e on a bare eth_call). The per-market/per-vault function
    # entries are GENERATED from GMX_MARKETS + GLV_VAULTS just below (signatures identical across
    # markets; this keeps them in lockstep with the market table and avoids transcription drift).
    {"inputs": [{"name": "gmToken", "type": "address"}], "name": "getGmPerformance",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "gmToken", "type": "address"}], "name": "getGmPlusPerformance",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "gmToken", "type": "address"}], "name": "getGmTokenBalanceAfterFees",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    # ─── TraderJoe V2 Liquidity Book (TraderJoeV2ArbitrumFacet) ───────────────
    # LB write paths are STUBBED on Arbitrum (TJ_LB_PAIRS empty — see config note). These
    # ABI entries are kept because getOwnedTraderJoeV2Bins is a harmless oracle-free read; the
    # add/remove entries are unused until the whitelisted Arbitrum pairs are verified. Shapes
    # match the shared TraderJoeV2*Facet + ILBRouter source.
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
    # ─── PRIME-token leverage tiers (PrimeLeverageFacet) ──────────────────────
    # depositPrime carries remainsSolvent (RedStone-gated on --execute); the other writes
    # (stake/activate, deactivate, unstake, repay) are onlyOwner only, so they need no
    # payload. The four getters are oracle-free views. shouldLiquidatePrimeDebt is declared
    # nonpayable because it MUTATES (snapshots debt) — we only eth_call it (read-only sim),
    # never broadcast it. The LeverageTier enum is a uint8 on the wire (BASIC=0, PREMIUM=1).
    # Mechanism identical to Avalanche; verified against the verified PrimeLeverageFacet source.
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

# Generated GMX V2 / GLV deposit+withdraw ABI entries — one per market/vault, signatures
# identical across markets (verified against GmxV2FacetArbitrum / GmxV2PlusFacetArbitrum /
# GlvFacetArbitrum). Generated from GMX_MARKETS + GLV_VAULTS so the ABI never drifts from the
# market table. All are PAYABLE (executionFee == msg.value).
def _gmx_two_sided_deposit_abi(fn):
    return {"inputs": [{"name": "isLongToken", "type": "bool"}, {"name": "tokenAmount", "type": "uint256"},
                       {"name": "minGmAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
            "name": fn, "outputs": [], "stateMutability": "payable", "type": "function"}
def _gmx_plus_deposit_abi(fn):
    return {"inputs": [{"name": "tokenAmount", "type": "uint256"}, {"name": "minGmAmount", "type": "uint256"},
                       {"name": "executionFee", "type": "uint256"}],
            "name": fn, "outputs": [], "stateMutability": "payable", "type": "function"}
def _gmx_withdraw_abi(fn):
    return {"inputs": [{"name": "gmAmount", "type": "uint256"}, {"name": "minLongTokenAmount", "type": "uint256"},
                       {"name": "minShortTokenAmount", "type": "uint256"}, {"name": "executionFee", "type": "uint256"}],
            "name": fn, "outputs": [], "stateMutability": "payable", "type": "function"}
def _glv_deposit_abi(fn):
    return {"inputs": [{"name": "isLongToken", "type": "bool"}, {"name": "tokenAmount", "type": "uint256"},
                       {"name": "minGlvAmount", "type": "uint256"}, {"name": "targetMarket", "type": "address"},
                       {"name": "executionFee", "type": "uint256"}],
            "name": fn, "outputs": [], "stateMutability": "payable", "type": "function"}
def _glv_withdraw_abi(fn):
    return {"inputs": [{"name": "glvAmount", "type": "uint256"}, {"name": "targetMarket", "type": "address"},
                       {"name": "minLongTokenAmount", "type": "uint256"}, {"name": "minShortTokenAmount", "type": "uint256"},
                       {"name": "executionFee", "type": "uint256"}],
            "name": fn, "outputs": [], "stateMutability": "payable", "type": "function"}
for _m in GMX_MARKETS.values():
    if _m["plus"]:
        PRIME_ACCOUNT_ABI.append(_gmx_plus_deposit_abi(_m["deposit_fn"]))
    else:
        PRIME_ACCOUNT_ABI.append(_gmx_two_sided_deposit_abi(_m["deposit_fn"]))
    PRIME_ACCOUNT_ABI.append(_gmx_withdraw_abi(_m["withdraw_fn"]))
for _v in GLV_VAULTS.values():
    PRIME_ACCOUNT_ABI.append(_glv_deposit_abi(_v["deposit_fn"]))
    PRIME_ACCOUNT_ABI.append(_glv_withdraw_abi(_v["withdraw_fn"]))

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

def get_factory_contract(w3):
    return w3.eth.contract(address=Web3.to_checksum_address(FACTORY_PROXY), abi=FACTORY_ABI)

def get_prime_account(w3, owner: str) -> str:
    """Owner -> Prime Account address. Zero address means none exists yet."""
    pa = get_factory_contract(w3).functions.getLoanForOwner(Web3.to_checksum_address(owner)).call()
    return None if int(pa, 16) == 0 else pa

def asset_b32(symbol: str) -> bytes:
    return symbol.encode().ljust(32, b"\x00")

def pool_to_asset_symbol(pool_name: str) -> str:
    """Pool key -> on-chain bytes32 asset symbol (the contracts use 'ETH', not 'WETH';
    'BTC', not 'WBTC')."""
    return POOLS[pool_name]["symbol"]

# KuCoin ticker normalisation: the account uses the unwrapped symbol for its wrapped
# assets, so WETH/WBTC -> ETH/BTC; KuCoin lists the spot ticker under the unwrapped name.
# All Arbitrum pool + GMX feed symbols (ETH/USDC/ARB/BTC/GMX/LINK/UNI/DAI/USDT/JOE) resolve
# directly; stablecoins (USDC/USDT/DAI) price ~$1 if the ticker is missing.
_KUCOIN_TICKER = {"WETH": "ETH", "WBTC": "BTC"}

def token_price(symbol: str) -> float:
    """Spot USD price from KuCoin (best-effort; 0.0 on miss). Wrapped symbols normalise to
    their KuCoin spot ticker."""
    ticker = _KUCOIN_TICKER.get(symbol, symbol)
    try:
        r = requests.get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={ticker}-USDT", timeout=3)
        if r.status_code == 200 and r.json().get("code") == "200000":
            data = r.json().get("data")
            if data and data.get("price"):
                return float(data["price"])
    except: pass
    # Stablecoins: fall back to $1 when the spot ticker is unavailable.
    if symbol in ("USDC", "USDT", "DAI"):
        return 1.0
    return 0.0

# ─── RedStone on-demand price wrapping ───────────────────────────────────────
# DeltaPrime's Prime Account uses RedStone's on-demand model: signed price packages
# are fetched off-chain and APPENDED to the function calldata (after the normal
# ABI-encoded args). The solvency math (remainsSolvent modifier, and oracle views)
# parses them from the calldata tail, verifies the signatures, and aggregates by
# median. Without the payload these calls revert with 0xe7764c9e.
#
# Payload layout (matches @redstone-finance/evm-connector; the SolvencyFacetProdArbitrum
# parser is identical to Avalanche's). Each signed data package:
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
    native ETH symbol, every owned asset, and every debt-registry asset. The solvency
    math prices ALL debt-registry assets (getDebts() returns the full pool set, not
    just non-zero balances), so every symbol it returns must be in the payload even at
    zero debt — otherwise that feed shows 0 signers and the call reverts with
    InsufficientNumberOfUniqueSigners. Deduped, ETH first (priced as element 0)."""
    feeds = ["ETH"]
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

def _pool_info_data(pool_name: str) -> dict:
    """Read every pool-info field for one pool in a SINGLE Multicall3 eth_call:
    totalSupply, totalBorrowed, getDepositRate, getBorrowingRate, and (when a signer is
    configured) the EOA's pool balance. Returns the raw + decoded values plus the off-chain
    KuCoin USD price. Shared by the human-facing print path and the --json path.

    Single call regardless of signer presence: my-deposit's leg is appended only when a
    key is resolvable; without a key it's skipped entirely (still one eth_call total)."""
    contract, cfg, w3 = get_pool_contract(pool_name)
    proxy_cs = Web3.to_checksum_address(cfg["proxy"])
    # Build leg set: 4 always-on view reads + optional balanceOf.
    legs = [
        ("totalSupply", contract.encode_abi("totalSupply", args=[])),
        ("totalBorrowed", contract.encode_abi("totalBorrowed", args=[])),
        ("getDepositRate", contract.encode_abi("getDepositRate", args=[])),
        ("getBorrowingRate", contract.encode_abi("getBorrowingRate", args=[])),
    ]
    try:
        acct = get_account()
        signer_addr = acct.address
        legs.append(("balanceOf", contract.encode_abi("balanceOf", args=[signer_addr])))
    except RuntimeError:
        signer_addr = None
    results = multicall(w3, [(proxy_cs, bytes.fromhex(d[2:]) if d.startswith("0x") else bytes.fromhex(d))
                             for _, d in legs])
    out_types = {"totalSupply": ["uint256"], "totalBorrowed": ["uint256"],
                 "getDepositRate": ["uint256"], "getBorrowingRate": ["uint256"],
                 "balanceOf": ["uint256"]}
    decoded = {}
    for (name, _data), (ok, rd) in zip(legs, results):
        if not ok or not rd:
            decoded[name] = None
            continue
        try:
            decoded[name] = w3.codec.decode(out_types[name], rd)[0]
        except Exception:
            decoded[name] = None
    price = token_price(cfg["symbol"])
    return {"name": pool_name, "cfg": cfg, "signer": signer_addr,
            "raw": decoded, "price": price}


def _compact_num(value: float, places: int = 2) -> float:
    """Round to `places` decimal places, defaulting to 2. Compact enough for an LLM
    consumer without lying about the underlying number — a balance like 1039858.6604
    becomes 1039858.66, a rate like 4.0357 stays 4.04. Used for amount/USD fields in
    pool-info --json. Drops any trailing-zero precision (Python prints 0.0 for clean
    integers; we leave it that way)."""
    if value is None:
        return None
    return round(float(value), places)


def _pool_json_shape(data: dict) -> dict:
    """Build the per-pool JSON object for `pool-info --json`. Same key names as
    degenprime's _pool_json_shape so an agent can consume both chains uniformly. Numbers
    are floats rounded to 2 dp (amounts/USD/rates/utilization); proxy/token are full
    checksum strings. Null-ish fields are omitted (no tokenPrice/tvl when KuCoin
    lookup fails, no myDeposit without a key or with zero balance)."""
    cfg, raw, price = data["cfg"], data["raw"], data["price"]
    d = cfg["decimals"]
    ts_raw, tb_raw = raw.get("totalSupply"), raw.get("totalBorrowed")
    dr_raw, br_raw = raw.get("getDepositRate"), raw.get("getBorrowingRate")
    out = {
        "symbol": cfg["symbol"],
        "proxy": cfg["proxy"],
        "token": cfg["token"],
        "decimals": d,
    }
    # Omit any field whose multicall leg returned None (revert / decode failure).
    # Lets a downstream consumer tell "this pool's read failed" from "this pool is
    # at literally 0".
    if ts_raw is not None:
        out["totalSupply"] = _compact_num(ts_raw / 10**d)
    if tb_raw is not None:
        out["totalBorrowed"] = _compact_num(tb_raw / 10**d)
    if ts_raw is not None and tb_raw is not None:
        ts = ts_raw / 10**d
        util = (tb_raw / 10**d / ts * 100) if ts > 0 else 0.0
        out["utilization"] = _compact_num(util)
    if dr_raw is not None:
        out["depositRate"] = _compact_num(dr_raw / 1e18 * 100)
    if br_raw is not None:
        out["borrowingRate"] = _compact_num(br_raw / 1e18 * 100)
    if price and ts_raw is not None:
        out["tokenPrice"] = _compact_num(price, places=4)
        out["tvl"] = _compact_num(ts_raw / 10**d * price)
    my_bal = raw.get("balanceOf")
    if my_bal is not None and my_bal > 0:
        out["myDeposit"] = _compact_num(my_bal / 10**d, places=6)
    return out


def cmd_pool_info(pool_name: str, as_json: bool = False):
    """Print pool supply / borrow / utilization / rates / TVL for one pool or all.

    Human-facing output (default) is unchanged. With --json: emits a single JSON object
    for a named pool, or a {pool_name: {...}} dict for `all`. JSON shape matches
    degenprime so an agent can consume both chains uniformly. Numbers are floats (no
    decoration), `tokenPrice` / `tvl` are omitted when KuCoin lookup fails, `myDeposit`
    is omitted when no key is configured or the balance is zero."""
    if pool_name == "all":
        if as_json:
            out = {name: _pool_json_shape(_pool_info_data(name)) for name in POOLS}
            print(json.dumps(out, indent=2))
            return
        for name in POOLS:
            cmd_pool_info(name)
            print()
        return

    data = _pool_info_data(pool_name)
    if as_json:
        print(json.dumps(_pool_json_shape(data), indent=2))
        return

    cfg, raw, price = data["cfg"], data["raw"], data["price"]
    p = cfg["proxy"][:12]
    d = cfg["decimals"]
    ts = raw.get("totalSupply") or 0
    tb = raw.get("totalBorrowed") or 0
    print(f"=== {cfg['symbol']} Pool ({p}...) ===")
    print(f"  Total Supply:   {ts / 10**d:>14,.2f} {cfg['symbol']}")
    print(f"  Total Borrowed: {tb / 10**d:>14,.2f} {cfg['symbol']}")
    util = tb / ts * 100 if ts > 0 else 0
    print(f"  Utilization:    {util:>14.2f}%")
    if price:
        print(f"  Token Price:    ${price:>13,.2f}")
        print(f"  TVL:            ${ts / 10**d * price:>13,.2f}")

    # Show the signer's pool deposit when a key is configured; pool-info should
    # also work as a pure read-only command without one. The balanceOf leg is read in
    # the same multicall batch above; we only print it.
    my_bal = raw.get("balanceOf")
    if my_bal is not None and my_bal > 0:
        print(f"  My Deposit:     {my_bal / 10**d:.4f} {cfg['symbol']}")

def cmd_my_positions():
    acct = get_account()
    w3 = get_w3()
    # The Wallet: line MUST always print so the operator can verify the resolved
    # signer address even when every other line is suppressed.
    print(f"Wallet: {acct.address}")

    # Wallet ETH — native gas. Suppress the line when the balance is effectively zero
    # (sub-nanowei dust) so a clean readout doesn't carry a noisy `ETH: 0.000000`.
    eth = w3.eth.get_balance(acct.address) / 1e18
    if eth >= 1e-9:
        print(f"ETH: {eth:.6f}")

    # Wallet PRIME (not a pool token; shown so it's detected/displayed in the wallet view)
    try:
        prime_bal = _prime_token_contract(w3).functions.balanceOf(acct.address).call()
        if prime_bal > 0:
            print(f"  Wallet PRIME: {prime_bal / 10**PRIME_TOKEN['decimals']:.6f}")
    except Exception:
        pass

    # Batch every per-pool read into ONE Multicall3 eth_call: for each pool, the wallet
    # ERC20 balanceOf + the pool balanceOf (the EOA's deposit) + the pool getBorrowed.
    # Previously 3 RPCs per pool × 5 pools = 15 sequential round-trips; now 1 round-trip
    # regardless of pool count. Output order is unchanged (per-pool grouped wallet /
    # deposit / borrow).
    legs = []
    pool_meta = []
    for name, cfg in POOLS.items():
        contract, _, _ = get_pool_contract(name)
        token_cs = Web3.to_checksum_address(cfg["token"])
        token = w3.eth.contract(address=token_cs, abi=ERC20_BALANCE_ABI)
        proxy_cs = Web3.to_checksum_address(cfg["proxy"])
        legs.append((token_cs, bytes.fromhex(token.encode_abi("balanceOf", args=[acct.address])[2:])))
        legs.append((proxy_cs, bytes.fromhex(contract.encode_abi("balanceOf", args=[acct.address])[2:])))
        legs.append((proxy_cs, bytes.fromhex(contract.encode_abi("getBorrowed", args=[acct.address])[2:])))
        pool_meta.append((name, cfg))
    try:
        results = multicall(w3, legs)
    except Exception as e:
        print(f"  pool reads failed via multicall: {type(e).__name__}: {e}")
        results = [(False, b"")] * len(legs)
    for i, (name, cfg) in enumerate(pool_meta):
        wallet_ok, wallet_rd = results[i * 3]
        pool_ok, pool_rd = results[i * 3 + 1]
        borrow_ok, borrow_rd = results[i * 3 + 2]
        try:
            wallet_bal = w3.codec.decode(["uint256"], wallet_rd)[0] if wallet_ok and wallet_rd else 0
            pool_bal = w3.codec.decode(["uint256"], pool_rd)[0] if pool_ok and pool_rd else 0
            borrowed = w3.codec.decode(["uint256"], borrow_rd)[0] if borrow_ok and borrow_rd else 0
        except Exception as e:
            print(f"  {name}: decode failed ({type(e).__name__})")
            continue
        if not wallet_ok:
            print(f"  {name}: wallet balanceOf leg reverted in multicall")
        if not pool_ok:
            print(f"  {name}: pool balanceOf leg reverted in multicall")
        if not borrow_ok:
            print(f"  {name}: getBorrowed leg reverted in multicall")
        if wallet_bal > 0:
            print(f"  Wallet {cfg['symbol']}: {wallet_bal / 10**cfg['decimals']:.4f}")
        if pool_bal > 0:
            print(f"  Pool Deposit {cfg['symbol']}: {pool_bal / 10**cfg['decimals']:.4f}")
        if borrowed > 0:
            print(f"  Borrowed {cfg['symbol']}: {borrowed / 10**cfg['decimals']:.4f}")

    # Prime Account (via getLoanForOwner — the factory has no getAccount())
    try:
        pa = get_prime_account(w3, acct.address)
        if pa:
            print(f"\nPrime Account: {pa}")
            pa_eth = w3.eth.get_balance(Web3.to_checksum_address(pa)) / 1e18
            if pa_eth >= 1e-9:
                print(f"  ETH balance: {pa_eth:.6f}")
        else:
            print("\nNo Prime Account yet. Create with: arbprime create-prime-account --execute")
    except Exception as e:
        print(f"\nPrime Account lookup failed: {e}")

def cmd_deposit(pool_name: str, amount: float, execute: bool = False):
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    amount_wei = to_wei_units(amount, cfg["decimals"])

    if not execute:
        print(f"Preview: Deposit {amount} {cfg['symbol']} into {pool_name.upper()} pool")
        print("Run with --execute to broadcast")
        return

    if cfg["native"]:
        # Native ETH path: depositNativeToken() (payable, no args). deposit(uint256)
        # itself is NOT payable on the eth pool — calling it with value reverts.
        # Gas: the native path wraps ETH→WETH + does the pool accounting +
        # internal rate update, so it needs more than the ERC20 branch's 200k.
        # 500k clears it cleanly.
        dep_tx = contract.functions.depositNativeToken().build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 500000, "chainId": CHAIN_ID, "value": amount_wei,
        })
        receipt = _sign_and_send(w3, acct, dep_tx, "Deposit (native)", timeout=120, fallback_gas=500000)
    else:
        # Approve
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]),
                                abi=json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]'))
        _dep_nonce = w3.eth.get_transaction_count(acct.address)
        app_tx = token.functions.approve(Web3.to_checksum_address(cfg["proxy"]), amount_wei).build_transaction({
            "from": acct.address, "nonce": _dep_nonce,
            "gas": 100000, "chainId": CHAIN_ID,
        })
        _set_gas_price(w3, app_tx)
        signed_app = acct.sign_transaction(app_tx)
        w3.eth.send_raw_transaction(signed_app.raw_transaction)

        # Deposit — nonce N+1 because approve (N) is in-flight
        dep_tx = contract.functions.deposit(amount_wei).build_transaction({
            "from": acct.address, "nonce": _dep_nonce + 1,
            "gas": 400000, "chainId": CHAIN_ID,
        })
        receipt = _sign_and_send(w3, acct, dep_tx, f"Deposit {amount} {cfg['symbol']}", timeout=120, fallback_gas=400000)
    ok = receipt["status"] == 1

def cmd_withdraw(pool_name: str, amount: float, execute: bool = False):
    """Pool-side (LENDER) withdraw — step 1 of a 24h delayed flow.

    Registers a WithdrawalIntent on the pool via createWithdrawalIntent(uint256).
    The pool's own `withdraw(uint256)` reverts: the lender side has the SAME
    delayed-intent flow as the Prime Account collateral side. The intent matures
    24h after registration and is then executable for 48h (24h-72h total window).
    Run `execute-withdrawal-request --pool <p>` after maturity to pull funds.
    """
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    amount_wei = to_wei_units(amount, cfg["decimals"])

    # getBalanceOf is the lender's current deposit balance — sane upper bound for
    # the intent amount. The pool reverts "Amount must be greater than zero" on 0,
    # and reverts on amount > deposit.
    balance = contract.functions.balanceOf(acct.address).call()
    print(f"Create lender withdrawal intent: {amount} {cfg['symbol']} from {pool_name.upper()} pool")
    print(f"  Wallet: {acct.address}")
    print(f"  Current pool deposit: {balance / 10**cfg['decimals']:.6f} {cfg['symbol']}")
    print(f"  Calls createWithdrawalIntent({amount_wei}) — no RedStone payload needed.")
    print("  Delayed flow: becomes executable ~24h later, then has a 48h window (24h-72h total).")
    print(f"  Run `execute-withdrawal-request --pool {pool_name}` after maturity to pull the funds to the wallet.")
    if amount_wei == 0:
        print(f"  ✗ Amount must be greater than zero. Refusing.")
        return
    if amount_wei > balance:
        print(f"  ✗ Requested {amount} {cfg['symbol']} exceeds current pool deposit. Refusing.")
        return

    if not execute:
        print("Run with --execute to broadcast (registers the intent on-chain).")
        return

    tx = contract.functions.createWithdrawalIntent(amount_wei).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 400000, "chainId": CHAIN_ID,
    })
    receipt = _sign_and_send(w3, acct, tx, "Lender withdrawal intent", fallback_gas=400000)
    ok = receipt["status"] == 1

def cmd_withdrawal_requests():
    """Read-only: list pending lender withdrawal intents per pool, with current deposit.
    Uses the oracle-free pool views getUserIntents / getTotalIntentAmount — no RedStone,
    no tx. Intent storage is per-EOA on the pool (NOT the Prime Account)."""
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    any_pending = False
    for pool_name, cfg in POOLS.items():
        contract = w3.eth.contract(address=Web3.to_checksum_address(cfg["proxy"]), abi=POOL_ABI)
        balance = contract.functions.balanceOf(acct.address).call()
        total_intent = contract.functions.getTotalIntentAmount(acct.address).call()
        intents = contract.functions.getUserIntents(acct.address).call()
        if balance == 0 and not intents:
            continue
        dec = cfg["decimals"]
        sym = cfg["symbol"]
        print(f"  {pool_name.upper()} ({sym}): deposit {balance / 10**dec:,.6f}, "
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
        print("  No pending lender withdrawal intents.")

def _encode_pool_withdraw(amount_wei: int, indices: list) -> str:
    """Calldata for the pool's intent-gated executor withdraw(uint256 _amount,
    uint256[] intentIndices) (selector 0x5915d806). Hand-encoded (uint256 head +
    dynamic uint256[] tail) so it never depends on the single-arg withdraw(uint256),
    which does NOT resolve a lender's named intent (see cmd_execute_withdrawal_request).
    Mirrors the DegenPrime pool executor encoding."""
    selector = Web3.keccak(text="withdraw(uint256,uint256[])")[:4].hex()
    head = amount_wei.to_bytes(32, "big").hex()          # _amount
    head += (0x40).to_bytes(32, "big").hex()             # offset to the array (2 head words in)
    tail = len(indices).to_bytes(32, "big").hex()
    tail += b"".join(int(i).to_bytes(32, "big") for i in indices).hex()
    return "0x" + selector + head + tail


def cmd_execute_withdrawal_request(pool_name: str, index: int = None, execute: bool = False):
    """Step 2 of lender pool withdrawal: consume a matured WithdrawalIntent via
    withdraw(uint256 _amount, uint256[] intentIndices) (selector 0x5915d806) — the
    same two-arg intent-gated executor as the DegenPrime pool, hand-encoded via
    _encode_pool_withdraw. NOT the single-arg withdraw(uint256): both selectors exist
    on the pool impl, but only the two-arg form resolves a lender's named intent (the
    single-arg form reverts without reaching the intent lookup).
    Refuses any intent that has not matured (isActionable=false) or has expired.
    --index selects which intent to execute; with no --index, the single actionable
    intent is used (if more than one is actionable, --index is required — one matured
    intent is consumed per `withdraw` call). Not RedStone-gated. An eth_call simulation
    runs before broadcast and refuses to send on revert (simulate-first rule)."""
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    intents = contract.functions.getUserIntents(acct.address).call()
    if not intents:
        print(f"No lender withdrawal intents registered for {pool_name.upper()}.")
        print(f"Register one first: withdraw --pool {pool_name} --amount <n> --execute")
        return

    if index is not None:
        if index < 0 or index >= len(intents):
            print(f"--index {index} out of range (pool has {len(intents)} intent(s)).")
            return
    else:
        actionable = [i for i, it in enumerate(intents) if it[4] and not it[5]]
        if not actionable:
            print("  No matured, non-expired intents to execute. Refusing.")
            return
        if len(actionable) > 1:
            print(f"  Multiple actionable intents {actionable} — pass --index to pick one "
                  f"(one matured intent is consumed per withdraw call).")
            return
        index = actionable[0]

    amt, actionable_at, expires_at, is_pending, is_actionable, is_expired = intents[index]
    print(f"Execute lender withdrawal from {pool_name.upper()} pool")
    print(f"  Wallet: {acct.address}")
    print(f"  [{index}] {amt / 10**cfg['decimals']:,.6f} {cfg['symbol']} — "
          f"{'EXPIRED' if is_expired else 'READY' if is_actionable else 'NOT MATURED'}")
    print(f"       {_fmt_window(actionable_at, expires_at)}")
    if is_expired:
        print(f"  ✗ intent [{index}] has expired — cancel it instead.")
        return
    if not is_actionable:
        print(f"  ✗ intent [{index}] has not matured yet — refusing.")
        return
    print(f"  Will pull {amt / 10**cfg['decimals']:,.6f} {cfg['symbol']} via "
          f"withdraw({amt}, [{index}]) (intent-gated; consumes intent [{index}]).")

    data = _encode_pool_withdraw(amt, [index])

    # Simulate before broadcasting (simulate-first rule). A passing eth_call here means
    # the intent is matured, non-expired, and the named index resolves correctly.
    try:
        w3.eth.call({"from": acct.address, "to": contract.address, "data": data})
    except Exception as e:
        print(f"  ✗ Simulation reverted — refusing to broadcast: {type(e).__name__}: {str(e)[:160]}")
        return

    if not execute:
        print("Run with --execute to broadcast (pulls the funds to the wallet — simulation passed).")
        return

    tx = {
        "from": acct.address, "to": contract.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 600000, "chainId": CHAIN_ID,
        "data": data,
    }
    receipt = _sign_and_send(w3, acct, tx, "Execute lender withdrawal", fallback_gas=600000)
    ok = receipt["status"] == 1

def cmd_cancel_withdrawal_request(pool_name: str, index: int, execute: bool = False):
    """Cancel a pending lender withdrawal intent on the pool via
    cancelWithdrawalIntent(uint256 index). Reverts with `Invalid intent index` if the
    index is out of range or already cleared. Useful before maturity to free up the
    balance for another use."""
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    intents = contract.functions.getUserIntents(acct.address).call()
    if not intents:
        print(f"No lender withdrawal intents registered for {pool_name.upper()}.")
        return
    if index < 0 or index >= len(intents):
        print(f"--index {index} out of range (pool has {len(intents)} intent(s)).")
        return

    amt, actionable_at, expires_at, is_pending, is_actionable, is_expired = intents[index]
    print(f"Cancel lender withdrawal intent on {pool_name.upper()} pool")
    print(f"  Wallet: {acct.address}")
    print(f"  [{index}] {amt / 10**cfg['decimals']:,.6f} {cfg['symbol']} — "
          f"{'EXPIRED' if is_expired else 'READY' if is_actionable else 'maturing'}")
    print(f"       {_fmt_window(actionable_at, expires_at)}")
    print(f"  Calls cancelWithdrawalIntent({index}).")

    if not execute:
        print("Run with --execute to broadcast (cancels the intent on-chain).")
        return

    tx = contract.functions.cancelWithdrawalIntent(index).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 300000, "chainId": CHAIN_ID,
    })
    receipt = _sign_and_send(w3, acct, tx, "Cancel lender withdrawal intent", fallback_gas=300000)
    ok = receipt["status"] == 1

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
        print("Nothing to create. Fund it with: arbprime fund --pool <p> --amount <n> --execute")
        return

    funding = fund_pool is not None and fund_amount is not None
    cfg = POOLS[fund_pool] if funding else None
    if funding and cfg["native"]:
        print("createAndFundLoan is ERC20-only — it cannot wrap native ETH.")
        print("For an ETH-funded account: create-prime-account --execute, then")
        print("  fund --pool eth --amount <n> --execute  (uses depositNativeToken()).")
        return

    factory = get_factory_contract(w3)
    factory_cs = Web3.to_checksum_address(FACTORY_PROXY)

    if not execute:
        print(f"Preview: Create a new Prime Account for {acct.address}")
        if funding:
            symbol = cfg["symbol"]
            amount_wei = to_wei_units(fund_amount, cfg["decimals"])
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
        amount_wei = to_wei_units(fund_amount, cfg["decimals"])
        # createAndFundLoan does token.transferFrom(msg.sender, factory, amount),
        # so approve the factory first.
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]),
                                abi=json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]'))
        _cf_nonce = w3.eth.get_transaction_count(acct.address)
        app_tx = token.functions.approve(factory_cs, amount_wei).build_transaction({
            "from": acct.address, "nonce": _cf_nonce,
            "gas": 100000, "chainId": CHAIN_ID,
        })
        _set_gas_price(w3, app_tx)
        signed_app = acct.sign_transaction(app_tx)
        w3.eth.send_raw_transaction(signed_app.raw_transaction)

        # createAndFundLoan — nonce N+1 because approve (N) is in-flight
        tx = factory.functions.createAndFundLoan(asset_b32(symbol), amount_wei).build_transaction({
            "from": acct.address, "nonce": _cf_nonce + 1,
            "gas": 4000000, "chainId": CHAIN_ID,
        })
    else:
        tx = factory.functions.createLoan().build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 4000000, "chainId": CHAIN_ID,
        })
    label = "Create+fund Prime Account" if funding else "Create Prime Account"
    receipt = _sign_and_send(w3, acct, tx, label, fallback_gas=4000000)
    ok = receipt["status"] == 1
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
    fund(bytes32 asset, amount) on it. Native ETH (eth pool): call the
    payable depositNativeToken() and send ETH as msg.value — the account
    wraps ETH->WETH internally, so no token approve is needed.
    """
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account. Create one first: arbprime create-prime-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    amount_wei = to_wei_units(amount, cfg["decimals"])
    pa_cs = Web3.to_checksum_address(pa)

    if not execute:
        print(f"Preview: Fund {amount} {symbol} into Prime Account {pa}")
        if cfg["native"]:
            print(f"  Native ETH: calls depositNativeToken() with value={amount_wei} wei")
            print("  Wraps ETH->WETH inside the account; no token approval needed.")
        else:
            print(f"  Approves {pa} to spend {amount} {symbol}, then calls fund(bytes32 '{symbol}', {amount_wei})")
        print("  Wallet must hold enough of the asset.")
        print("Run with --execute to broadcast")
        return

    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    if cfg["native"]:
        fund_tx = account.functions.depositNativeToken().build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3000000, "chainId": CHAIN_ID, "value": amount_wei,
        })
        signed = acct.sign_transaction(tx)
    else:
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]),
                                abi=json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]'))
        app_tx = token.functions.approve(pa_cs, amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000, "chainId": CHAIN_ID,
        })
        _set_gas_price(w3, app_tx)
        signed_app = acct.sign_transaction(app_tx)
        w3.eth.send_raw_transaction(signed_app.raw_transaction)

        # Nonce: approve uses get_transaction_count (N). Fund must use N+1 since
        # the approve is in-flight and confirmed nonce hasn't advanced yet.
        _fund_nonce = w3.eth.get_transaction_count(acct.address) + 1
        fund_tx = account.functions.fund(asset_b32(symbol), amount_wei).build_transaction({
            "from": acct.address, "nonce": _fund_nonce,
            "gas": 3000000, "chainId": CHAIN_ID,
        })
        receipt = _sign_and_send(w3, acct, fund_tx, f"Fund {amount} {symbol}", fallback_gas=3000000)
        ok = receipt["status"] == 1
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
    # Multicall: stage A batches getAllOwnedAssets + getDebts (2 -> 1 RPC). Stage B
    # batches one getBalance per owned asset (N -> 1 RPC). Both legs are oracle-free
    # diamond views; no RedStone needed for either.
    pa_cs = account.address
    stage_a_legs = [
        ("getAllOwnedAssets", account.encode_abi("getAllOwnedAssets", args=[])),
        ("getDebts", account.encode_abi("getDebts", args=[])),
    ]
    a_results = multicall(w3, [(pa_cs, bytes.fromhex(d[2:])) for _, d in stage_a_legs])
    owned_raw = w3.codec.decode(["bytes32[]"], a_results[0][1])[0] if a_results[0][0] else []
    debts_raw = w3.codec.decode(["(bytes32,uint256)[]"], a_results[1][1])[0] if a_results[1][0] else []
    owned = [a.rstrip(b"\x00").decode(errors="replace") for a in owned_raw]
    if owned:
        bal_legs = [(pa_cs, bytes.fromhex(account.encode_abi("getBalance", args=[asset_b32(sym)])[2:]))
                    for sym in owned]
        bal_results = multicall(w3, bal_legs)
    else:
        bal_results = []
    supplied = []
    for sym, (ok, rd) in zip(owned, bal_results):
        bal = w3.codec.decode(["uint256"], rd)[0] if ok and rd else 0
        supplied.append({"symbol": sym, "raw": bal, "decimals": _asset_decimals(sym),
                         "balance": f"{bal / 10**_asset_decimals(sym):.6f}"})
    borrowed = []
    for n, v in debts_raw:
        sym = n.rstrip(b"\x00").decode(errors="replace")
        if v > 0:
            borrowed.append({"symbol": sym, "raw": v, "decimals": _asset_decimals(sym),
                             "balance": f"{v / 10**_asset_decimals(sym):.6f}"})
    # Derive the price feeds inline from the already-read assets/debts (mirrors
    # prime_account_price_feeds but skips its two extra eth_calls). ETH first.
    feeds = ["ETH"]
    for sym in owned:
        if sym and sym not in feeds:
            feeds.append(sym)
    for n, _v in debts_raw:
        sym = n.rstrip(b"\x00").decode(errors="replace")
        if sym and sym not in feeds:
            feeds.append(sym)
    out = {"supplied": supplied, "borrowed": borrowed, "w3": w3,
           "total_value_usd": None, "debt_usd": None, "health_ratio": None, "solvent": None}
    # Resolve the account's PRIME tier (oracle-free) and per-asset on-chain debtCoverage,
    # then stamp dc onto every row so _compute_health_pct can run getHealthMeter exactly.
    try:
        tier_code = account.functions.getLeverageTierFullInfo().call()[0]
    except Exception:
        tier_code = 0
    out["tier_code"] = tier_code
    try:
        dc_map = _resolve_debt_coverages(w3, [r["symbol"] for r in supplied + borrowed], tier_code)
        for r in supplied + borrowed:
            r["dc"] = dc_map.get(r["symbol"], 0.0)
    except Exception:
        pass
    try:
        payload = build_redstone_payload(feeds)
        payload_hex = payload.hex()
        # Batch the four RedStone-gated solvency views + getPrices into one multicall.
        # Each leg carries the SAME RedStone payload appended to its calldata; the
        # SolvencyFacet parses the payload from the calldata tail per leg, so replaying
        # the payload across legs is correct (just redundant on the wire — the gateway
        # is still hit once and verifying the same packages multiple times is cheap on
        # the chain). Drops 4-5 sequential eth_calls down to 1.
        price_syms = [s for s in dict.fromkeys(r["symbol"] for r in supplied + borrowed) if s]
        solv_legs = [
            ("getTotalValue", ["uint256"], account.encode_abi("getTotalValue", args=[])),
            ("getDebt", ["uint256"], account.encode_abi("getDebt", args=[])),
            ("getHealthRatio", ["uint256"], account.encode_abi("getHealthRatio", args=[])),
            ("isSolvent", ["bool"], account.encode_abi("isSolvent", args=[])),
        ]
        if price_syms:
            solv_legs.append(("getPrices", ["uint256[]"],
                              account.encode_abi("getPrices",
                                                 args=[[asset_b32(s) for s in price_syms]])))
        solv_results = multicall(w3, [(pa_cs, bytes.fromhex(d[2:]) + bytes.fromhex(payload_hex))
                                       for _, _, d in solv_legs])
        decoded_solv = {}
        for (name, out_types, _d), (ok, rd) in zip(solv_legs, solv_results):
            if not ok or not rd:
                decoded_solv[name] = None
                continue
            try:
                decoded_solv[name] = w3.codec.decode(out_types, rd)[0]
            except Exception:
                decoded_solv[name] = None
        if decoded_solv.get("getTotalValue") is not None:
            out["total_value_usd"] = decoded_solv["getTotalValue"] / 1e18
        if decoded_solv.get("getDebt") is not None:
            out["debt_usd"] = decoded_solv["getDebt"] / 1e18
        if decoded_solv.get("getHealthRatio") is not None:
            ratio = decoded_solv["getHealthRatio"] / 1e18
            # With no/negligible debt the ratio is astronomically large (e.g. 1e59) —
            # meaningless to render. Surface it as None so consumers show "no debt"
            # instead of a junk number.
            out["health_ratio"] = None if ratio > 1000 else ratio
        if decoded_solv.get("isSolvent") is not None:
            out["solvent"] = bool(decoded_solv["isSolvent"])
        # Per-asset USD via getPrices. Missing feeds (anything not in the payload) just
        # fall through as None.
        prices = {}
        if price_syms and decoded_solv.get("getPrices") is not None:
            raw_prices = decoded_solv["getPrices"]
            for i, s in enumerate(price_syms):
                if i < len(raw_prices):
                    prices[s] = raw_prices[i] / 1e8
        for r in supplied + borrowed:
            p = prices.get(r["symbol"])
            r["usd"] = (r["raw"] / 10**r["decimals"] * p) if p is not None else None
    except Exception as e:
        out["solvency_error"] = type(e).__name__
        for r in supplied + borrowed:
            r["usd"] = None
    return out

def _health_meter_pct(assets: list) -> dict:
    """getHealthMeter() exactly as the on-chain HealthMeterFacetProd renders it.

    The frontend health meter is NOT equity*(mult-1); it is a per-asset, debtCoverage-
    weighted formula. For each asset i with USD-valued long balance and borrow, and its
    live debtCoverage dc_i:

        net_i = supplied_usd_i - borrowed_usd_i
        weightedCollateralPlus  = Σ dc_i·net_i        for net_i > 0  (net-long legs)
        weightedCollateralMinus = Σ dc_i·(-net_i)     for net_i < 0  (net-short legs)
        weightedCollateral      = weightedCollateralPlus - weightedCollateralMinus
        weightedBorrowed        = Σ dc_i·borrowed_usd_i
        borrowed                = Σ borrowed_usd_i                    (UNWEIGHTED)

        borrowed == 0                                  -> 100
        weightedCollateral > 0 and
          weightedCollateral + weightedBorrowed > borrowed
            -> (weightedCollateral + weightedBorrowed - borrowed) / weightedCollateral · 100
        else                                           -> 0

    Result clamped to [0, 100]. `assets` is a list of
    {"symbol", "dc", "supplied_usd", "borrowed_usd"}; missing usd legs count as 0.
    For a uniform-dc single-collateral position this reduces to the familiar
    (max_debt - debt)/max_debt·100 with max_debt = equity·dc/(1-dc).
    """
    wc_plus = 0.0
    wc_minus = 0.0
    weighted_borrowed = 0.0
    borrowed = 0.0
    supplied_usd = 0.0
    debt_usd = 0.0
    for a in assets:
        dc = a.get("dc", 0.0) or 0.0
        sup = a.get("supplied_usd", 0.0) or 0.0
        bor = a.get("borrowed_usd", 0.0) or 0.0
        supplied_usd += sup
        debt_usd += bor
        net = sup - bor
        if net > 0:
            wc_plus += dc * net
        elif net < 0:
            wc_minus += dc * (-net)
        weighted_borrowed += dc * bor
        borrowed += bor
    weighted_collateral = wc_plus - wc_minus
    equity = supplied_usd - debt_usd
    if borrowed <= 0:
        health_pct = 100.0
    elif weighted_collateral > 0 and (weighted_collateral + weighted_borrowed) > borrowed:
        health_pct = (weighted_collateral + weighted_borrowed - borrowed) / weighted_collateral * 100.0
        health_pct = max(0.0, min(100.0, health_pct))
    else:
        health_pct = 0.0
    return {"health_pct": round(health_pct, 1), "supplied_usd": round(supplied_usd, 2),
            "debt_usd": round(debt_usd, 2), "equity": round(equity, 2),
            "weighted_collateral": round(weighted_collateral, 2),
            "weighted_borrowed": round(weighted_borrowed, 2)}


_dc_cache = {}


def _resolve_debt_coverages(w3, symbols: list, tier_code: int = 0) -> dict:
    """Per-asset debtCoverage read LIVE on-chain from the TokenManager, keyed by symbol.

    Resolves each symbol to its token address via getAssetAddress(bytes32,true), then reads
    the account's effective coverage: tieredDebtCoverage(tier, token) on Avalanche/Arbitrum
    (the contract's getHealthMeter uses getPrimeLeverageTier() for exactly this), falling
    back to the un-tiered debtCoverage(token). Cached per run keyed by (symbol, tier_code).
    Batched through multicall so N assets cost ~2 eth_calls, not 2N. Symbols that don't
    resolve get dc=0 (they contribute nothing — same as the contract skipping an unpriced
    leg)."""
    want = [s for s in dict.fromkeys(symbols) if s]
    out = {}
    missing = []
    for s in want:
        ck = (s, tier_code)
        if ck in _dc_cache:
            out[s] = _dc_cache[ck]
        else:
            missing.append(s)
    if not missing:
        return out
    tm_abi = json.loads(
        '[{"inputs":[{"name":"_asset","type":"bytes32"},{"name":"_active","type":"bool"}],'
        '"name":"getAssetAddress","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},'
        '{"inputs":[{"name":"a","type":"address"}],"name":"debtCoverage","outputs":[{"type":"uint256"}],'
        '"stateMutability":"view","type":"function"},'
        '{"inputs":[{"name":"t","type":"uint8"},{"name":"a","type":"address"}],"name":"tieredDebtCoverage",'
        '"outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')
    tm = w3.eth.contract(address=Web3.to_checksum_address(TOKEN_MANAGER), abi=tm_abi)
    addr_legs = [(TOKEN_MANAGER, bytes.fromhex(tm.encode_abi("getAssetAddress", args=[asset_b32(s), True])[2:]))
                 for s in missing]
    addr_res = multicall(w3, addr_legs)
    addrs = {}
    for s, (ok, rd) in zip(missing, addr_res):
        try:
            a = w3.codec.decode(["address"], rd)[0] if ok and rd else None
        except Exception:
            a = None
        addrs[s] = a if a and int(a, 16) != 0 else None
    resolvable = [s for s in missing if addrs[s]]
    # Try tiered coverage first (Avalanche/Arbitrum); fall back per-asset to un-tiered.
    tiered_legs = [(TOKEN_MANAGER, bytes.fromhex(
        tm.encode_abi("tieredDebtCoverage", args=[tier_code, Web3.to_checksum_address(addrs[s])])[2:]))
        for s in resolvable]
    untiered_legs = [(TOKEN_MANAGER, bytes.fromhex(
        tm.encode_abi("debtCoverage", args=[Web3.to_checksum_address(addrs[s])])[2:]))
        for s in resolvable]
    tiered_res = multicall(w3, tiered_legs) if tiered_legs else []
    untiered_res = multicall(w3, untiered_legs) if untiered_legs else []
    for i, s in enumerate(resolvable):
        dc = 0.0
        ok_t, rd_t = tiered_res[i]
        if ok_t and rd_t:
            try:
                dc = w3.codec.decode(["uint256"], rd_t)[0] / 1e18
            except Exception:
                dc = 0.0
        if dc <= 0:
            ok_u, rd_u = untiered_res[i]
            if ok_u and rd_u:
                try:
                    dc = w3.codec.decode(["uint256"], rd_u)[0] / 1e18
                except Exception:
                    dc = 0.0
        _dc_cache[(s, tier_code)] = dc
        out[s] = dc
    for s in missing:
        if s not in out:
            _dc_cache[(s, tier_code)] = 0.0
            out[s] = 0.0
    return out


def _compute_health_pct(data: dict, tier_code: int = 0) -> dict:
    """Frontend-exact health (0-100%) for a Prime Account — wraps _health_meter_pct with
    on-chain debtCoverage resolution and the tier label.

    DeltaPrime has *two* health metrics that agents must not confuse:

      1. health_ratio (on-chain, getHealthRatio): 1.0 = liquidation, >1.0 = solvent.
         The raw weighted-collateral / debt ratio from the SolvencyFacet.

      2. health_pct (0-100%, getHealthMeter): the scale the DeltaPrime frontend renders
         and the account-health-monitor cron acts on. 0% = liquidation, 100% = no debt.
         Computed by _health_meter_pct with per-asset dc from tieredDebtCoverage at the
         account's PRIME tier — NOT the old equity*(mult-1) approximation.

    Per-asset USD comes from gather_lending (rows carry `dc` once resolved); if a row has
    no `dc` key yet it is resolved here from `data["w3"]` when present. Returns
    health_pct, supplied_usd, debt_usd, equity, max_debt (display zero-crossing debt),
    tier, or error.
    """
    supplied = data.get("supplied", [])
    borrowed = data.get("borrowed", [])
    tier_labels = {0: "BASIC", 1: "PREMIUM", 2: "_NON_EXISTENT"}
    tier_label = tier_labels.get(tier_code, str(tier_code))
    # Per-symbol long/short USD, merging an asset that is both supplied and borrowed.
    syms = list(dict.fromkeys([r["symbol"] for r in supplied + borrowed if r.get("symbol")]))
    dc_map = {r["symbol"]: r["dc"] for r in supplied + borrowed if r.get("dc") is not None}
    need = [s for s in syms if s not in dc_map]
    if need and data.get("w3") is not None:
        dc_map.update(_resolve_debt_coverages(data["w3"], need, tier_code))
    assets = []
    for s in syms:
        sup = sum(r.get("usd", 0) or 0 for r in supplied if r.get("symbol") == s)
        bor = sum(r.get("usd", 0) or 0 for r in borrowed if r.get("symbol") == s)
        assets.append({"symbol": s, "dc": dc_map.get(s, 0.0), "supplied_usd": sup, "borrowed_usd": bor})
    res = _health_meter_pct(assets)
    equity = res["equity"]
    if equity <= 0.01:
        return {"health_pct": 0.0, "supplied_usd": res["supplied_usd"],
                "debt_usd": res["debt_usd"], "equity": equity,
                "max_debt": 0.0, "tier": tier_label, "error": "equity near zero"}
    # Display-only zero-crossing debt: the unweighted borrow at which health hits 0,
    # equity·dc_eff/(1-dc_eff) for the position's value-weighted collateral dc.
    coll_usd = sum(a["supplied_usd"] for a in assets) or 0.0
    dc_eff = (sum(a["dc"] * a["supplied_usd"] for a in assets) / coll_usd) if coll_usd > 0 else 0.0
    max_debt = equity * dc_eff / (1.0 - dc_eff) if 0 < dc_eff < 1 else 0.0
    return {"health_pct": res["health_pct"], "supplied_usd": res["supplied_usd"],
            "debt_usd": res["debt_usd"], "equity": equity,
            "max_debt": round(max_debt, 2), "tier": tier_label}


def cmd_prime_summary():
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Owner wallet: {acct.address}")
    if not pa:
        print("No Prime Account yet. Create one with: arbprime create-prime-account --execute")
        return

    print(f"Prime Account: {pa}")
    pa_eth = w3.eth.get_balance(pa) / 1e18
    print(f"  Native ETH (gas):   {pa_eth:.6f}")

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

    # Solvency views (SolvencyFacetProdArbitrum) are RedStone-gated: they revert
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
        print(f"  Health ratio (chain): {ratio_str}  (>1.0 = solvent, 1.0 = liquidation)")
        # ─── Equity-based health (0-100%) ───
        # Different from health_ratio! See _compute_health_pct docstring.
        # Get tier from the Prime Account (oracle-free view)
        try:
            tier_info = gather_prime_tier(w3, acct, account)
            tier_code = tier_info.get("tier_code", 0)
        except Exception:
            tier_code = 0
        hp = _compute_health_pct(data, tier_code)
        if "error" not in hp:
            print(f"  Health (0-100%): {hp['health_pct']:.1f}%")
            print(f"    (supplied=${hp['supplied_usd']:.2f}, debt=${hp['debt_usd']:.2f},"
                  f" equity=${hp['equity']:.2f}, max_debt=${hp['max_debt']:.2f}, {hp['tier']})")
            print(f"    0%=liquidation  50%=half borrowing power used  100%=no debt")
        else:
            print(f"  Health (0-100%): N/A ({hp['error']})")
        print(f"  Solvent:            {'yes' if data['solvent'] else 'NO — liquidatable'}")
    else:
        print(f"  Health/solvency:    RedStone fetch/call failed ({data.get('solvency_error', 'error')}); "
              "showing balances only")

def get_account_equity(account_addr: str = None) -> dict:
    """Net equity of a Prime Account: total_value_usd - debt_usd + rewards_usd.

    Read-only (eth_call only): reuses gather_lending for the RedStone-gated
    getTotalValue/getDebt reads. `account_addr` targets a specific Prime Account; when
    None it resolves the one owned by the --as / --owner wallet.

    rewards_usd is 0.0 here: Arbitrum DeltaPrime has no separate unclaimed-reward
    primitive in this tool (no sJOE — that is the Avalanche TraderJoe path; GMX/GLV
    fees compound inside the LP and are already counted by getTotalValue). It stays in
    the dict for cross-tool shape parity.

    Returns {agent, chain, protocol, wallet, prime_account, total_value_usd, debt_usd,
    rewards_usd, net_equity_usd, block, ts, status}. On any read error: status="error",
    an `error` field, and the numeric fields set to None."""
    base = {
        "agent": _SELECTED_AGENT, "chain": "arbitrum", "protocol": "DeltaPrime",
        "wallet": None, "prime_account": None,
        "total_value_usd": None, "debt_usd": None, "rewards_usd": None,
        "net_equity_usd": None, "block": None, "ts": int(time.time()), "status": "ok",
    }
    try:
        w3 = get_w3()
        acct = get_account()
        base["wallet"] = acct.address
        pa = account_addr or get_prime_account(w3, acct.address)
        base["block"] = w3.eth.block_number
        if not pa:
            base["status"] = "no_account"
            return base
        pa = Web3.to_checksum_address(pa)
        base["prime_account"] = pa
        account = w3.eth.contract(address=pa, abi=PRIME_ACCOUNT_ABI)
        lending = gather_lending(w3, account)
        if "solvency_error" in lending or lending.get("total_value_usd") is None:
            base["status"] = "error"
            base["error"] = lending.get("solvency_error", "solvency read failed")
            return base
        total_value = lending["total_value_usd"]
        debt = lending["debt_usd"]
        rewards = 0.0
        base["total_value_usd"] = round(total_value, 2)
        base["debt_usd"] = round(debt, 2)
        base["rewards_usd"] = round(rewards, 2)
        base["net_equity_usd"] = round(total_value - debt + rewards, 2)
        return base
    except Exception as e:
        base["status"] = "error"
        base["error"] = f"{type(e).__name__}: {e}"
        return base

def cmd_equity(as_json: bool = False, account_addr: str = None):
    """Print net equity for the selected wallet's Prime Account (read-only)."""
    eq = get_account_equity(account_addr)
    if as_json:
        print(json.dumps(eq, indent=2))
        return
    print(f"Owner wallet: {eq['wallet']}")
    if eq["status"] == "no_account":
        print("No Prime Account yet. Create one with: arbprime create-prime-account --execute")
        return
    print(f"Prime Account: {eq['prime_account']}")
    if eq["status"] == "error":
        print(f"  Equity: unavailable ({eq.get('error', 'read failed')})")
        return
    tv, debt, rw, net = (eq["total_value_usd"], eq["debt_usd"],
                         eq["rewards_usd"], eq["net_equity_usd"])
    health = (100.0 if debt < 0.01 else round((tv - debt) / tv * 100, 1)) if tv else 0.0
    print(f"  Total value:   ${tv:,.2f}")
    print(f"  Debt:          ${debt:,.2f}")
    print(f"  Rewards:       ${rw:,.2f}  (no separate unclaimed-reward source on Arbitrum DeltaPrime)")
    print(f"  Net equity:    ${net:,.2f}  (total - debt + rewards)")
    print(f"  Equity health: {health:.1f}%  (equity / total value)")

def cmd_borrow(pool_name: str, amount: float, execute: bool = False):
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account. Create one first: arbprime create-prime-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    amount_wei = to_wei_units(amount, cfg["decimals"])
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
        "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Borrow {amount} {symbol}", fallback_gas=4000000)
    ok = receipt["status"] == 1
    return ok

def cmd_repay(pool_name: str, amount: float, execute: bool = False):
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account. Create one first: arbprime create-prime-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    pool, _, _ = get_pool_contract(pool_name)
    # The facet's repay reverts if amount > debt OR amount > in-account balance.
    # Cap to min(requested, debt, in_account) so callers don't need to know either
    # exact figure — pass an overshoot like 9999 and it clips cleanly.
    # ALSO: the contract's _getAvailableBalance() subtracts pending withdrawal intents,
    # so we subtract total_intent from the raw balance to get the true cap.
    requested_wei = to_wei_units(amount, cfg["decimals"])
    debt_wei = pool.functions.getBorrowed(pa_cs).call()
    in_acct_wei = account.functions.getBalance(asset_b32(symbol)).call()
    try:
        total_intent_wei = account.functions.getTotalIntentAmount(asset_b32(symbol)).call()
    except Exception:
        total_intent_wei = 0
    available_wei = in_acct_wei - total_intent_wei if in_acct_wei > total_intent_wei else 0
    if debt_wei == 0:
        print(f"No {symbol} debt to repay on Prime Account {pa}.")
        return
    amount_wei = min(requested_wei, debt_wei, available_wei)
    if amount_wei == 0:
        print(f"Repay {amount} {symbol}: available {symbol} balance is 0 "
              f"(total {in_acct_wei / 10**cfg['decimals']:.6f} minus "
              f"{total_intent_wei / 10**cfg['decimals']:.6f} pending withdrawal intent) — "
              f"swap into {symbol} first or wait for intents to mature.")
        sys.exit(2)
    cap_notes = []
    if amount_wei < requested_wei:
        if available_wei < min(requested_wei, debt_wei):
            cap_notes.append(f"available {symbol} only {available_wei / 10**cfg['decimals']:.6f} "
                             f"(total {in_acct_wei / 10**cfg['decimals']:.6f} minus "
                             f"{total_intent_wei / 10**cfg['decimals']:.6f} pending intent)")
        if debt_wei < requested_wei:
            cap_notes.append(f"debt only {debt_wei / 10**cfg['decimals']:.6f} {symbol}")

    if not execute:
        print(f"Preview: Repay {amount_wei / 10**cfg['decimals']:.6f} {symbol} from Prime Account {pa}")
        if cap_notes:
            print(f"  Capped from requested {amount}: {'; '.join(cap_notes)}")
        print(f"  Calls repay(bytes32 '{symbol}', {amount_wei}) on the Prime Account")
        print(f"  Current debt: {debt_wei / 10**cfg['decimals']:.6f} {symbol} | "
              f"in-account: {in_acct_wei / 10**cfg['decimals']:.6f} {symbol} | "
              f"available: {available_wei / 10**cfg['decimals']:.6f} {symbol}")
        if in_acct_wei < debt_wei:
            shortfall = (debt_wei - in_acct_wei) / 10**cfg['decimals']
            print(f"  Note: in-account < debt by {shortfall:.6f} {symbol} — "
                  f"swap into {symbol} first to close the position fully.")
        if total_intent_wei > 0:
            print(f"  Note: {total_intent_wei / 10**cfg['decimals']:.6f} {symbol} is locked in pending "
                  f"withdrawal intent(s) and not available for repay.")
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
        "gas": 4000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Repay {amount_wei / 10**cfg['decimals']:.6f} {symbol}", fallback_gas=4000000)
    ok = receipt["status"] == 1
    repaid = amount_wei / 10**cfg['decimals']
    if not ok:
        _print_revert_reason(w3, tx, receipt)

def _print_revert_reason(w3, tx, receipt):
    """Try to decode and print the revert reason from a failed tx."""
    try:
        result = w3.eth.call({
            "from": tx["from"], "to": tx["to"], "data": tx["input"],
            "gas": receipt["gasUsed"],
            "maxFeePerGas": tx.get("maxFeePerGas", tx.get("gasPrice", 0)),
            "maxPriorityFeePerGas": tx.get("maxPriorityFeePerGas", 0),
        }, receipt["blockNumber"])
    except Exception as e:
        err = str(e)
        data = err
        if isinstance(e.args, (list, tuple)):
            for arg in e.args:
                if isinstance(arg, str) and arg.startswith("0x"):
                    data = arg
                    break
                elif isinstance(arg, dict) and "data" in arg:
                    data = arg["data"]
                    break
        if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
            sel = data[:10]
            _known_errors = {
                "0x567fe27a": "Unknown error from Prime Account facet",
                "0xf4d678b8": "Execution rejected (may be intent-locked balance check)",
                "0x441a702e": "InsufficientBalance()",
                "0xfd36fde3": "SignerNotAuthorised (RedStone signer mismatch)",
                "0x92ba160c": "RedstoneConsensus()",
                "0xc2c286b7": "SignerNotAuthorised(address)",
                "0x08c379a0": "require() revert: see message below",
            }
            label = _known_errors.get(sel, f"Unknown custom error 0x{sel}")
            print(f"  Revert: {label}")
            if sel == "0x08c379a0" and len(data) >= 138:
                try:
                    from eth_abi import decode
                    msg_bytes = bytes.fromhex(data[10:])
                    decoded = decode(["string"], msg_bytes)
                    print(f'  Require message: "{decoded[0]}"')
                except Exception:
                    pass
        else:
            print(f"  Revert reason: {err[:200]}")


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
    on Arbitrum v6.2 (network=CHAIN_ID=42161). The priceRoute is passed verbatim to
    /transactions."""
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
    # Same simulate-first executor handling as swap-debt (see cmd_swap_debt for full
    # rationale): keep the API executor when the exact tx simulates clean; only fall
    # back to the legacy executor if the unpatched calldata reverts.
    _PARASWAP_FALLBACK_EXECUTOR = "0x000010036C0190E009a000d0fc3541100A07380A"
    feeds = prime_account_price_feeds(account)
    for s in (from_sym, to_sym):
        if s not in feeds:
            feeds.append(s)
    payload = build_redstone_payload(feeds)
    def _sim_paraswap(db):
        base = account.encode_abi("paraSwapV6", args=[full[:4], db])
        try:
            w3.eth.call({"from": acct.address, "to": pa_cs,
                         "data": base + payload.hex(), "gas": 8000000})
            return True, None
        except Exception as e:
            return False, str(e)
    sim_ok, sim_err = _sim_paraswap(data_bytes)
    if sim_ok:
        if _exec is not None and _exec.lower() not in PARASWAP_EXECUTORS:
            print(f"  ✓ Executor {_exec} not in the static whitelist, but the full tx "
                  f"simulates clean — using the API calldata as-is.")
    else:
        print(f"  ✗ Simulation with API executor {_exec} reverted: {sim_err}")
        patched = bytes(12) + bytes.fromhex(_PARASWAP_FALLBACK_EXECUTOR[2:]) + data_bytes[32:]
        sim_ok, err2 = _sim_paraswap(patched)
        if sim_ok:
            print(f"  ⚠ Falling back to legacy executor {_PARASWAP_FALLBACK_EXECUTOR} "
                  f"(simulates clean).")
            data_bytes = patched
            _paraswap_decode_and_check(selector_hex, data_bytes, from_cfg["token"],
                                       to_cfg["token"], amount_in, pa_cs)
        else:
            print(f"  ✗ Legacy-executor fallback also reverted: {err2}")

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

    if not sim_ok:
        print("✗ Refusing to broadcast: simulation reverted for both executor variants.")
        return

    # Rebuild the payload fresh for broadcast (the sim payload may be near the
    # RedStone staleness window by now).
    payload = build_redstone_payload(feeds)
    base_calldata = account.encode_abi("paraSwapV6", args=[full[:4], data_bytes])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Swap {amount} {from_sym} -> {to_sym}", fallback_gas=3000000)
    ok = receipt["status"] == 1
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
        print("Create and fund one first: arbprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    from_cfg, to_cfg = SWAP_ASSETS[from_sym], SWAP_ASSETS[to_sym]
    amount_in = to_wei_units(amount, from_cfg["decimals"])

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
        "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Swap {amount} {from_sym} -> {to_sym}", fallback_gas=3000000)
    ok = receipt["status"] == 1
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

def _calc_swap_debt_amounts(w3, account, from_sym, to_sym, amount):
    """Value-match the new borrow to the old repay using the facet's own RedStone prices.
    Returns (repay_amount, borrow_amount, payload) — repay/borrow in wei.
    Reads the old-debt pool and RedStone prices fresh on each call. Raises ValueError
    if the computed borrow rounds to zero or if there's no old debt."""
    from_cfg, to_cfg = SWAP_ASSETS[from_sym], SWAP_ASSETS[to_sym]
    pa_cs = account.address

    # Current borrowed of the OLD debt asset, read from its pool (the facet caps
    # _repayAmount to exactly this).
    from_pool, _, _ = get_pool_contract(_SYMBOL_TO_POOL[from_sym])
    borrowed = from_pool.functions.getBorrowed(pa_cs).call()
    if borrowed == 0:
        raise ValueError("zero_old_debt")
    repay_amount = min(to_wei_units(amount, from_cfg["decimals"]), borrowed)

    # Value-match the new borrow to the repay using the facet's own RedStone prices.
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
        raise ValueError("zero_borrow")
    return repay_amount, borrow_amount, payload

def _read_account_health(w3, account, payload) -> tuple:
    """Read total_value (USD, 1e18-scaled raw) and total_debt (USD, 1e18-scaled raw) from
    the SolvencyFacet via RedStone-gated eth_calls. Returns (total_value_usd, total_debt_usd)
    as floats; None if any call fails."""
    try:
        tv = account.encode_abi("getTotalValue", args=[]) + payload.hex()
        raw_tv = w3.eth.call({"to": account.address, "data": tv})
        td = account.encode_abi("getDebt", args=[]) + payload.hex()
        raw_td = w3.eth.call({"to": account.address, "data": td})
        return (w3.codec.decode(["uint256"], bytes(raw_tv))[0] / 1e18,
                w3.codec.decode(["uint256"], bytes(raw_td))[0] / 1e18)
    except Exception:
        return None, None

def cmd_swap_debt(from_sym: str, to_sym: str, amount: float, slippage_pct: float = 1.0,
                  execute: bool = False, fallback: bool = False):
    """Refinance debt from --from (existing debt) into --to (new debt).

    Default (one-tx): SwapDebtFacet.swapDebtParaSwap — borrows _borrowAmount of _toAsset,
    ParaSwaps it into _fromAsset, and repays _repayAmount of _fromAsset debt in a single tx.
    (Was broken on-chain 2026-05-30 by a protocol-level Velora/ParaSwap facet bug; the
    DeltaPrime team fixed it — re-verified working via eth_call + live tx 2026-06-04.)

    --fallback (manual 3-tx via YieldYak):
      1. borrow to_sym  — borrow the new debt asset into the account
      2. swap yak       — swap new debt -> old debt via YieldYakSwapFacet.yakSwap
      3. repay from_sym — repay the old debt (capped to min(requested, debt, in-account balance))

    --amount is how much of the OLD (--from) debt to repay, in --from units.
    RedStone-gated on execute for both paths."""
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

    # Reusable amount calc (used by both the one-tx and fallback paths).
    try:
        repay_amount, borrow_amount, payload = _calc_swap_debt_amounts(w3, account, from_sym, to_sym, amount)
    except ValueError as e:
        if str(e) == "zero_old_debt":
            print(f"Prime Account has no {from_sym} debt to refinance.")
        elif str(e) == "zero_borrow":
            print("Computed borrow amount rounds to zero — repay amount too small. Refusing.")
        return

    # --- Shared preview fields ---
    price_from, price_to = _read_prices_usd(w3, account, [from_sym, to_sym], payload)
    repay_usd = price_from * repay_amount / 10**from_cfg["decimals"] / 1e8
    borrow_usd = price_to * borrow_amount / 10**to_cfg["decimals"] / 1e8

    # ─── Fallback path (manual 3-tx via YieldYak) ────────────────────────────
    if fallback:
        # Health check: intermediate state = after borrow, before repay.
        total_value_usd, total_debt_usd = _read_account_health(w3, account, payload)

        print(f"Swap debt (manual • 3-tx fallback via YieldYak) on Prime Account {pa}")
        print(f"  Refinance: {from_sym} debt -> {to_sym} debt")
        print(f"  Step 1: borrow {borrow_amount / 10**to_cfg['decimals']:.6f} {to_sym}  (≈${borrow_usd:,.2f})")
        print(f"  Step 2: swap {to_sym} -> {from_sym} via YieldYak")
        print(f"  Step 3: repay {repay_amount / 10**from_cfg['decimals']:.6f} {from_sym}  (≈${repay_usd:,.2f})")

        # Intermediate health check.
        if total_value_usd is not None and total_debt_usd is not None:
            health_before = total_value_usd / total_debt_usd if total_debt_usd > 0 else float('inf')
            intermediate_debt = total_debt_usd + borrow_usd
            intermediate_health = total_value_usd / intermediate_debt if intermediate_debt > 0 else float('inf')
            print(f"  Current health:   {health_before:.4f}  (total value ${total_value_usd:,.2f} / debt ${total_debt_usd:,.2f})")
            print(f"  Intermediate:     {intermediate_health:.4f}  (after borrow but before repay)")
            if intermediate_health < 1.1:
                print(f"  ✗✗✗ REFUSING: intermediate health {intermediate_health:.2f} < 1.1 —")
                print(f"    account would be liquidatable during the intermediate state.")
                print(f"    Reduce the swap amount or add collateral first.")
                return
            if intermediate_health < 1.5:
                print(f"  ⚠ WARNING: intermediate health {intermediate_health:.2f} < 1.5 —")
                print(f"    dangerously close to liquidation. Proceed with extreme caution.")
        else:
            print(f"  ⚠ Could not read current health — proceeding without intermediate check.")

        # Preview the swap leg.
        amounts, adapters, path = _yak_find_best_path(w3, borrow_amount, to_cfg["token"], from_cfg["token"])
        expected_out = amounts[-1]
        min_out = int(expected_out * (1 - slippage_pct / 100))
        not_whitelisted = [a for a in adapters
                           if not account.functions.isWhitelistedAdapterOptimized(
                               Web3.to_checksum_address(a)).call()]
        print(f"  YieldYak route ({len(adapters)} hop{'s' if len(adapters) != 1 else ''}): "
              f"{' -> '.join(path)}")
        print(f"  Expected {from_sym} out: {expected_out / 10**from_cfg['decimals']:.6f}")
        print(f"  Min out (@{slippage_pct}% slippage): {min_out / 10**from_cfg['decimals']:.6f}")
        if not_whitelisted:
            print(f"  ✗ Non-whitelisted adapter(s): {', '.join(not_whitelisted)}. yakSwap would revert. Refusing.")
            return
        print(f"  All adapters whitelisted.")

        if not execute:
            print("Run with --execute to broadcast the 3-tx fallback sequence.")
            return

        # ─── Execute the 3-tx fallback ───────────────────────────────────────
        print()
        feeds = prime_account_price_feeds(account)
        for s in (from_sym, to_sym):
            if s not in feeds:
                feeds.append(s)
        exec_payload = build_redstone_payload(feeds)

        # Step 1: Borrow to_sym
        print("── Step 1/3: Borrow ──")
        base_borrow = account.encode_abi("borrow", args=[asset_b32(to_sym), borrow_amount])
        tx = {
            "from": acct.address, "to": pa_cs, "data": base_borrow + exec_payload.hex(),
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": CHAIN_ID,
        }
        receipt = _sign_and_send(w3, acct, tx, "Borrow (swap-debt fallback)", fallback_gas=4000000)
        if receipt["status"] != 1:
            print(f"  ✗ Borrow failed — aborting fallback sequence.")
            return


        # Re-check health after borrow (fresh payload).
        fresh_payload = build_redstone_payload(prime_account_price_feeds(account))
        tv, td = _read_account_health(w3, account, fresh_payload)
        if tv is not None and td is not None and td > 0:
            post_borrow_h = tv / td
            print(f"  Health after borrow: {post_borrow_h:.4f}")
            if post_borrow_h < 1.0:
                print(f"  ⚠ Account is now insolvent! Repay or add collateral immediately.")

        # Step 2: Swap to_sym -> from_sym via yak
        print("── Step 2/3: Swap (yak) ──")
        amounts2, adapters2, path2 = _yak_find_best_path(w3, borrow_amount, to_cfg["token"], from_cfg["token"])
        expected_out2 = amounts2[-1]
        min_out2 = int(expected_out2 * (1 - slippage_pct / 100))
        not_whitelisted2 = [a for a in adapters2
                            if not account.functions.isWhitelistedAdapterOptimized(
                                Web3.to_checksum_address(a)).call()]
        if not_whitelisted2:
            print(f"  ✗ Route changed — adapters now non-whitelisted: {', '.join(not_whitelisted2)}. Aborting.")
            print(f"    Steps completed: 1 (borrow). You may need to manually swap & repay.")
            return
        swap_payload2 = build_redstone_payload(prime_account_price_feeds(account))
        base_swap = account.encode_abi("yakSwap", args=[
            borrow_amount, min_out2,
            [Web3.to_checksum_address(p) for p in path2],
            [Web3.to_checksum_address(a) for a in adapters2],
        ])
        tx2 = {
            "from": acct.address, "to": pa_cs, "data": base_swap + swap_payload2.hex(),
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": CHAIN_ID,
        }
        receipt2 = _sign_and_send(w3, acct, tx2, "Swap (swap-debt fallback)", fallback_gas=3000000)
        if receipt2["status"] != 1:
            print(f"  ✗ Swap failed — aborting fallback sequence.")
            print(f"  Steps completed: 1 (borrow). You may need to manually swap & repay.")
            return
        print(f"  ✓ Swapped {to_sym} -> {from_sym}")
        print(f"  Tx: {EXPLORER}/tx/{tx_hash2.hex()}")

        # Step 3: Repay from_sym (capped to min(requested, debt, in-account))
        print("── Step 3/3: Repay ──")
        from_pool, _, _ = get_pool_contract(_SYMBOL_TO_POOL[from_sym])
        debt_wei = from_pool.functions.getBorrowed(pa_cs).call()
        in_acct_wei = account.functions.getBalance(asset_b32(from_sym)).call()
        actual_reply = min(repay_amount, debt_wei, in_acct_wei)
        if actual_reply == 0:
            print(f"  ✗ No {from_sym} available to repay (balance={in_acct_wei/10**from_cfg['decimals']:.6f}, "
                  f"debt={debt_wei/10**from_cfg['decimals']:.6f}). Swap output may have been below debt.")
            print(f"  Steps completed: 1 (borrow), 2 (swap).")
            return
        cap_notes = []
        if actual_reply < repay_amount:
            if in_acct_wei < min(repay_amount, debt_wei):
                cap_notes.append(f"in-account only {in_acct_wei/10**from_cfg['decimals']:.6f} {from_sym}")
            if debt_wei < repay_amount:
                cap_notes.append(f"debt only {debt_wei/10**from_cfg['decimals']:.6f} {from_sym}")
        if cap_notes:
            print(f"  Capped repay to {actual_reply/10**from_cfg['decimals']:.6f} {from_sym} ({'; '.join(cap_notes)})")
        repay_payload3 = build_redstone_payload(prime_account_price_feeds(account))
        base_reply = account.encode_abi("repay", args=[asset_b32(from_sym), actual_reply])
        tx3 = {
            "from": acct.address, "to": pa_cs, "data": base_reply + repay_payload3.hex(),
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": CHAIN_ID,
        }
        receipt3 = _sign_and_send(w3, acct, tx3, f"Repaid (swap-debt fallback)", fallback_gas=4000000)
        ok3 = receipt3["status"] == 1
        if not ok3:
            print(f"  Steps completed: 1 (borrow), 2 (swap). Repay failed — check manually.")
        else:
            print(f"\n✓ All 3 steps completed. {from_sym} debt refinanced to {to_sym}.")
        return

    # ─── One-tx ParaSwap path (original) ────────────────────────────────────
    diff_bps = (abs(repay_usd - borrow_usd) / max(repay_usd, borrow_usd)) * 10000 if max(repay_usd, borrow_usd) else 0

    # ParaSwap calldata for the INTERNAL swap: sell exactly borrow_amount of _toAsset for
    # _fromAsset (facet requires fromAmount == _borrowAmount). srcToken=to, destToken=from.
    price_route = _paraswap_price_route(to_cfg["token"], to_cfg["decimals"],
                                        from_cfg["token"], from_cfg["decimals"], borrow_amount, pa_cs)
    quoted_out = int(price_route["destAmount"])
    tx_built = _paraswap_build_tx(price_route, to_cfg["token"], to_cfg["decimals"],
                                  from_cfg["token"], from_cfg["decimals"], borrow_amount,
                                  slippage_pct, pa_cs)
    full_data = bytes.fromhex(tx_built["data"][2:])
    selector_hex, data_bytes = "0x" + full_data[:4].hex(), full_data[4:]
    _exec, _src, _dest, swap_from_amt, swap_min_out = _paraswap_decode_and_check(
        selector_hex, data_bytes, to_cfg["token"], from_cfg["token"], borrow_amount, pa_cs)

    # Velora/ParaSwap executors rotate per quote and the facet's on-chain executor
    # check was fixed at the protocol level (DeltaPrime team, confirmed by eth_call
    # 2026-06-04) — API-built calldata now passes with its own executor, while the old
    # hard-patch to the legacy executor REVERTS (executor-specific calldata mismatch).
    # So: simulate the exact tx first and keep the API executor when it passes; only
    # fall back to the legacy executor if the unpatched calldata reverts.
    _PARASWAP_FALLBACK_EXECUTOR = "0x000010036C0190E009a000d0fc3541100A07380A"
    def _sim_swap_debt(db):
        base = account.encode_abi("swapDebtParaSwap", args=[
            asset_b32(from_sym), asset_b32(to_sym), repay_amount, borrow_amount,
            full_data[:4], db])
        try:
            w3.eth.call({"from": acct.address, "to": pa_cs,
                         "data": base + payload.hex(), "gas": 8000000})
            return True, None
        except Exception as e:
            return False, str(e)
    sim_ok, sim_err = _sim_swap_debt(data_bytes)
    if sim_ok:
        if _exec is not None and _exec.lower() not in PARASWAP_EXECUTORS:
            print(f"  ✓ Executor {_exec} not in the static whitelist, but the full tx "
                  f"simulates clean — using the API calldata as-is.")
    else:
        print(f"  ✗ Simulation with API executor {_exec} reverted: {sim_err}")
        patched = bytes(12) + bytes.fromhex(_PARASWAP_FALLBACK_EXECUTOR[2:]) + data_bytes[32:]
        sim_ok, err2 = _sim_swap_debt(patched)
        if sim_ok:
            print(f"  ⚠ Falling back to legacy executor {_PARASWAP_FALLBACK_EXECUTOR} "
                  f"(simulates clean).")
            data_bytes = patched
            _paraswap_decode_and_check(selector_hex, data_bytes, to_cfg["token"],
                                       from_cfg["token"], borrow_amount, pa_cs)
        else:
            print(f"  ✗ Legacy-executor fallback also reverted: {err2}")

    from_pool, _, _ = get_pool_contract(_SYMBOL_TO_POOL[from_sym])
    borrowed = from_pool.functions.getBorrowed(pa_cs).call()

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

    if not sim_ok:
        print("✗ Refusing to broadcast: simulation reverted for both executor variants.")
        return

    base_calldata = account.encode_abi("swapDebtParaSwap", args=[
        asset_b32(from_sym), asset_b32(to_sym), repay_amount, borrow_amount,
        full_data[:4], data_bytes,
    ])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Swap debt {from_sym} -> {to_sym}", fallback_gas=4000000)
    ok = receipt["status"] == 1

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
    amount_wei = to_wei_units(amount, cfg["decimals"])
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
        "gas": 1000000, "chainId": CHAIN_ID,
    })
    _set_gas_price(w3, tx)
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Withdrawal intent {'registered' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_cancel_withdrawal(pool_name: str, index: int, execute: bool = False):
    """Cancel a pending withdrawal intent by index. Calls cancelWithdrawalIntent(bytes32
    asset, uint256 index) on the WithdrawalIntentFacet — no RedStone payload needed.
    Preview by default; the preview lists the intent details so you know what you're cancelling."""
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to cancel.")
        return

    symbol = pool_to_asset_symbol(pool_name)
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    intents = account.functions.getUserIntents(asset_b32(symbol)).call()
    if index >= len(intents):
        print(f"Intent index {index} out of range (0–{len(intents)-1}) for {symbol}.")
        return
    amt, actionable_at, expires_at, is_pending, is_actionable, is_expired = intents[index]
    if not is_pending:
        state = "EXPIRED" if is_expired else ("READY" if is_actionable else "inactive")
        print(f"Intent [{index}] is {state} — nothing to cancel (only pending/maturing intents can be cancelled).")
        return

    dec = cfg["decimals"]
    print(f"Cancel withdrawal intent [{index}] on Prime Account {pa}")
    print(f"  Asset: {symbol}  |  Amount: {amt / 10**dec:.6f}")
    print(f"  {_fmt_window(actionable_at, expires_at)}")
    print(f"  Calls cancelWithdrawalIntent(bytes32 '{symbol}', {index}) — no RedStone payload needed.")
    print(f"  The {amt / 10**dec:.6f} {symbol} will be returned to the account's free balance.")

    if not execute:
        print("Run with --execute to broadcast.")
        return

    tx = account.functions.cancelWithdrawalIntent(asset_b32(symbol), index).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 500000, "chainId": CHAIN_ID,
    })
    receipt = _sign_and_send(w3, acct, tx, f"Cancel withdrawal [{index}]", timeout=120, fallback_gas=1000000)
    ok = receipt["status"] == 1


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
        "gas": 3000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "Execute withdrawal", fallback_gas=3000000)
    ok = receipt["status"] == 1

# ─── GMX V2 GM / GM+ LP (GmxV2FacetArbitrum / GmxV2PlusFacetArbitrum) ─────────
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
    """Estimate the GMX V2 execution fee (in wei of ETH) the keeper will require, mirroring
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
    # Floor the gas price at 1 gwei as a safety buffer against keeper gas-price spikes.
    # GMX REFUNDS any excess to the receiver (the Prime Account), so over-reserving is safe
    # but under-reserving stalls the deposit. 1 gwei is ~50× current Arbitrum gas (0.02 gwei)
    # and keeps the upfront requirement low (~$10-20 vs the old 25 gwei floor which needed ~$250).
    # NOTE: an over-reservation still blocks if the EOA's balance is thin — lower --fee-buffer.
    gas_price = max(w3.eth.gas_price, 1 * 10**9)
    fee_wei = int(adjusted * gas_price * buffer_mult)
    return fee_wei, {"adjusted_gas": adjusted, "gas_price": gas_price, "buffer_mult": buffer_mult}

def _gmx_underlying_price_usd(w3, account, payload, symbol: str) -> int:
    """1e8-scaled USD price of an underlying FEED SYMBOL (ETH/BTC/ARB/USDC and the GMX feed
    symbols LINK/UNI/GMX/NEAR/ATOM/SUI/SEI) via the RedStone-gated SolvencyFacet.getPrices.
    (The GM token symbol itself has no SolvencyFacet feed — getPrices reverts 0xec459bc0 on
    it — so GM prices come from the gateway median.)"""
    return _read_prices_usd(w3, account, [symbol], payload)[0]

def _gmx_leg_meta(mkt: dict, leg: str) -> dict:
    """ERC20 metadata ({token, decimals, symbol}) for a GM market's deposit leg. The long
    leg's DEPOSIT token is mkt['long_token'] (WETH for the synthetics; same as the feed for
    real markets); the short leg is USDC (or the single underlying for GM+). Falls back to
    SWAP_ASSETS for any market that predates the long_token field. `leg` is 'long' or 'short'.
    Keys mirror SWAP_ASSETS ({token, decimals}) so callers are interchangeable."""
    if leg == "long":
        t = mkt.get("long_token")
        if t:
            return {"token": t["addr"], "decimals": t["decimals"], "symbol": t["symbol"]}
        return SWAP_ASSETS[mkt["long"]]
    # short leg: USDC for two-sided; the single underlying token for GM+
    short_sym = mkt["short"]
    if short_sym in _GMX_TOKENS:
        t = _GMX_TOKENS[short_sym]
        return {"token": t["addr"], "decimals": t["decimals"], "symbol": t["symbol"]}
    return SWAP_ASSETS[short_sym]

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
    held by the market contract. GMX redeems GM pro-rata across this composition. NOTE: the
    live withdraw path uses the GMX Reader (_gmx_withdrawal_amount_out); this reserve-split is
    a fallback. For synthetics the market collateral is WETH while p_long is the synthetic
    feed price, so this fallback is approximate for those — the Reader path is authoritative.
    Returns (long_frac, short_frac)."""
    erc = json.loads('[{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')
    long_cfg, short_cfg = _gmx_leg_meta(mkt, "long"), _gmx_leg_meta(mkt, "short")
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
    cmd_gmx_positions (print) and cmd_defi (--json).

    Multicall optimisation: stage A batches getAllOwnedAssets + every market's ERC20
    balanceOf in one eth_call (1 + 6 = 7 reads down to 1). Stage B batches the two
    RedStone-gated views per active market in one eth_call per market (2 -> 1), and
    keeps the payloads market-specific (each market has a unique GM feed + underlyings
    set, so a single shared payload won't satisfy SolvencyFacet's feed gating)."""
    pa_cs = account.address
    erc_balanceof_sig = bytes.fromhex("70a08231") + b"\x00" * 12 + bytes.fromhex(pa_cs[2:])
    # Stage A: getAllOwnedAssets + per-market raw GM balanceOf.
    market_items = list(GMX_MARKETS.items())
    stage_a_legs = [(pa_cs, bytes.fromhex(account.encode_abi("getAllOwnedAssets", args=[])[2:]))]
    for _key, mkt in market_items:
        stage_a_legs.append((Web3.to_checksum_address(mkt["gm_token"]), erc_balanceof_sig))
    a_results = multicall(w3, stage_a_legs)
    owned_ok, owned_rd = a_results[0]
    if owned_ok and owned_rd:
        owned = {a.rstrip(b"\x00").decode(errors="replace")
                 for a in w3.codec.decode(["bytes32[]"], owned_rd)[0]}
    else:
        owned = set()
    raw_bals = []
    for (_, _), (ok, rd) in zip(market_items, a_results[1:]):
        raw_bals.append(w3.codec.decode(["uint256"], rd)[0] if ok and rd else 0)

    # Stage B: per active market, batch the two RedStone-gated views in one call.
    positions = []
    for (key, mkt), raw_bal in zip(market_items, raw_bals):
        gm_sym = mkt["gm_feed"]
        if raw_bal == 0 and gm_sym not in owned:
            continue
        gm_cs = Web3.to_checksum_address(mkt["gm_token"])
        pos = {"market": key, "kind": "GM+" if mkt["plus"] else "GM", "gm_feed": gm_sym,
               "gm_token": mkt["gm_token"], "raw": raw_bal, "decimals": GM_TOKEN_DECIMALS,
               "balance": f"{raw_bal / 10**GM_TOKEN_DECIMALS:.6f}",
               "after_fees": None, "perf_pct": None, "gm_price_usd": None, "usd": None}
        feeds = [gm_sym, mkt["long"]] + ([] if mkt["plus"] else [mkt["short"]])
        try:
            payload_hex = build_redstone_payload(feeds).hex()
            perf_fn = "getGmPlusPerformance" if mkt["plus"] else "getGmPerformance"
            b_legs = [
                ("getGmTokenBalanceAfterFees",
                 account.encode_abi("getGmTokenBalanceAfterFees", args=[gm_cs])),
                (perf_fn, account.encode_abi(perf_fn, args=[gm_cs])),
            ]
            b_results = multicall(w3, [(pa_cs, bytes.fromhex(d[2:]) + bytes.fromhex(payload_hex))
                                        for _, d in b_legs])
            if not b_results[0][0] or not b_results[1][0]:
                pos["error"] = "RedStoneViewReverted"
            else:
                after_fees = w3.codec.decode(["uint256"], b_results[0][1])[0]
                perf = w3.codec.decode(["uint256"], b_results[1][1])[0]
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

def cmd_gmx_deposit(market: str, amount: float, is_long: bool | None = None,
                    slippage_pct: float = 1.0, fee_buffer: float = 2.0, execute: bool = False):
    """Open/add a GMX V2 GM (two-sided) or GM+ (single-sided) LP position by depositing an
    in-account underlying. Two-sided markets take --side long|short|auto.
    - long = the volatile leg's DEPOSIT token (ETH/BTC/ARB/LINK/UNI/GMX, or WETH for the
      near/atom/sui/sei synthetics), short = USDC.
    - auto (default for standalone): picks the side whose token has the HIGHEST available
      balance in the Prime Account. This lets you deposit whichever asset you have without
      swapping or converting — GMX handles the internal 50/50 split.
    GM+ markets ignore --side. minGmAmount is set to the fair GM amount (depositUSD / gmPrice)
    minus --slippage, kept within the facet's ±5% isWithinBounds band.
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

    # ── Auto-side resolution ────────────────────────────────────────────────
    # For two-sided GM markets: read available balances of BOTH deposit tokens and pick
    # the one with more USD value, so you can deposit whichever asset you have (USDC, ETH,
    # BTC, ARB, ...) and GMX matches the other side internally. For GM+ (single-sided),
    # auto always picks the long token. NOTE the long-leg DEPOSIT token may differ from the
    # long-leg PRICE FEED: for the synthetics (near/atom/sui/sei) the deposit token is WETH
    # (account symbol "ETH") while the feed is NEAR/ATOM/SUI/SEI. dep_sym is the deposit-token
    # account symbol (for getBalance/asset_b32); dep_feed is the RedStone price-feed symbol.
    long_meta = _gmx_leg_meta(mkt, "long")
    short_meta = _gmx_leg_meta(mkt, "short")
    long_dep_sym, short_dep_sym = long_meta["symbol"], short_meta["symbol"]
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if pa:
        account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
        if is_long is None and not mkt["plus"]:
            long_bal = account.functions.getBalance(asset_b32(long_dep_sym)).call() / 10**long_meta["decimals"]
            short_bal = account.functions.getBalance(asset_b32(short_dep_sym)).call() / 10**short_meta["decimals"]
            # Approximate USD for SIDE SELECTION only (refined by RedStone below): USDC ~= 1:1.
            short_usd, long_usd = short_bal, long_bal
            if short_usd >= long_usd:
                is_long = False
                dep_sym, dep_cfg = short_dep_sym, short_meta
                print(f"  Auto-selected: --side short ({dep_sym}, available ${short_usd:.2f} vs "
                      f"{long_dep_sym} ${long_usd:.2f})")
            else:
                is_long = True
                dep_sym, dep_cfg = long_dep_sym, long_meta
                print(f"  Auto-selected: --side long ({dep_sym}, available ${long_usd:.2f} vs "
                      f"{short_dep_sym} ${short_usd:.2f})")
        elif mkt["plus"] or is_long:
            dep_sym, dep_cfg = long_dep_sym, long_meta
        else:
            dep_sym, dep_cfg = short_dep_sym, short_meta
    else:
        if mkt["plus"] or (is_long if is_long is not None else True):
            dep_sym, dep_cfg = long_dep_sym, long_meta
        else:
            dep_sym, dep_cfg = short_dep_sym, short_meta

    if not pa:
        print("No Prime Account exists for this wallet — nothing to deposit.")
        print("Create and fund one first: arbprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    amount_wei = to_wei_units(amount, dep_cfg["decimals"])
    in_balance = account.functions.getBalance(asset_b32(dep_sym)).call()
    if amount_wei > in_balance:
        print(f"Prime Account holds only {in_balance / 10**dep_cfg['decimals']:.6f} {dep_sym} "
              f"in-account; cannot deposit {amount} {dep_sym}.")
        print("Fund or borrow more of the asset into the account first.")
        return

    # Fair minGmAmount: depositUSD / gmPrice, scaled to GM decimals, minus slippage.
    # `underlyings` are the GM market's COLLATERAL token feeds (the long-leg DEPOSIT token feed
    # + USDC), NOT the synthetic index feed: for the synthetics the GMX market is WETH+USDC-
    # collateralised, so we price the deposited WETH at ETH (the synthetic's own index price is
    # read by GMX internally, not from DeltaPrime's payload). For real markets these coincide.
    underlyings = [long_meta["symbol"]] + ([] if mkt["plus"] else [short_meta["symbol"]])
    underlyings = list(dict.fromkeys(underlyings))  # dedupe (GM+ short == long)
    price_payload = build_redstone_payload(underlyings)
    # The facet runs an inline solvency simulation before minting that prices EVERY debt-registry
    # asset (the full pool set, even at zero balance/debt), each needing 3 unique RedStone signers;
    # a feed missing from the payload reverts the whole deposit with InsufficientNumberOfUniqueSigners.
    # So the write payload must cover the full solvency feed set + the GM feed + the collateral
    # underlyings (deduped). (price_payload above stays underlyings-only; it has no GM feed.)
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
    print(f"  GMX execution fee: {exec_fee / 1e18:.6f} ETH  (msg.value; {fee_d['buffer_mult']}x of "
          f"{fee_d['adjusted_gas']:,} gas @ {fee_d['gas_price'] / 1e9:.2f} gwei). Excess is refunded by GMX.")
    print("  ASYNC: queues a GMX deposit request; a GMX keeper mints the GM tokens in a later")
    print(f"  block. The Prime Account is FROZEN for {mkt['gm_feed']} until the keeper callback fires.")
    print(f"  The EOA must also hold ~{exec_fee / 1e18:.6f} ETH (gas) on top of the execution fee.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # Pre-check: EOA must hold the execution fee + tx gas (~0.0004 ETH floor).
    eoa_balance = w3.eth.get_balance(acct.address)
    tx_gas_cost = int(5_000_000 * _tx_gas_price(w3))
    gas_buf = max(tx_gas_cost, int(0.0004 * 1e18))
    min_needed = exec_fee + gas_buf
    if eoa_balance < min_needed:
        print(f"✗ EOA balance {eoa_balance / 1e18:.6f} ETH is below the minimum needed "
              f"({min_needed / 1e18:.6f} ETH: {exec_fee / 1e18:.6f} exec fee + {gas_buf / 1e18:.6f} tx gas).")
        print("  Transfer more ETH to the EOA before retrying.")
        return

    if mkt["plus"]:
        base_calldata = account.encode_abi(fn, args=[amount_wei, min_gm, exec_fee])
    else:
        base_calldata = account.encode_abi(fn, args=[is_long, amount_wei, min_gm, exec_fee])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data, "value": exec_fee,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"GMX {kind} deposit request", fallback_gas=5000000)
    ok = receipt["status"] == 1
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
    gm_amount = to_wei_units(amount, GM_TOKEN_DECIMALS)
    if gm_bal == 0:
        print(f"Prime Account holds no {mkt['gm_feed']} GM tokens — nothing to withdraw.")
        return
    if gm_amount > gm_bal:
        print(f"Prime Account holds only {gm_bal / 10**GM_TOKEN_DECIMALS:,.6f} GM; "
              f"clamping withdrawal to that (the facet caps to balance anyway).")
        gm_amount = gm_bal

    # Withdraw returns the GM market's COLLATERAL tokens (the long-leg deposit token + USDC),
    # so min-out floors are priced and sized against those — NOT the synthetic index feed. For
    # the synthetics the long collateral is WETH (priced at ETH), so long_cfg/long feed both
    # resolve to the WETH/ETH metadata. For real markets these coincide with mkt["long"].
    long_cfg = _gmx_leg_meta(mkt, "long")
    short_cfg = _gmx_leg_meta(mkt, "short")
    long_feed, short_feed = long_cfg["symbol"], short_cfg["symbol"]
    # Underlyings-only payload for the SolvencyFacet price reads (no GM feed there).
    underlyings = [long_feed] + ([] if mkt["plus"] else [short_feed])
    underlyings = list(dict.fromkeys(underlyings))
    price_payload = build_redstone_payload(underlyings)
    # Write payload: the facet's inline solvency check prices the FULL debt registry
    # (every pool, even at zero balance), each needing 3 RedStone signers — so it must
    # carry prime_account_price_feeds + the GM feed, or it reverts with
    # InsufficientNumberOfUniqueSigners(0,3). (Same fix as cmd_gmx_deposit.)
    _solv_feeds = prime_account_price_feeds(account)
    _extra_feeds = [f for f in ([mkt["gm_feed"]] + underlyings) if f not in _solv_feeds]
    payload = build_redstone_payload(_solv_feeds + _extra_feeds)
    p_long = _gmx_underlying_price_usd(w3, account, price_payload, long_feed)
    p_short = p_long if mkt["plus"] else _gmx_underlying_price_usd(w3, account, price_payload, short_feed)
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
        print(f"  Min {long_cfg['symbol']} out @{slippage_pct}% slippage: {tot_min:,.6f} "
              f"(facet sums minLong {min_long / 10**long_cfg['decimals']:,.6f} + "
              f"minShort {min_short / 10**short_cfg['decimals']:,.6f}, both the single underlying)")
    else:
        print(f"  Expected {long_cfg['symbol']}: {expected_long / 10**long_cfg['decimals']:,.6f}  Expected {short_cfg['symbol']}: {expected_short / 10**short_cfg['decimals']:,.6f}")
        print(f"  Min {long_cfg['symbol']} out @{slippage_pct}% slippage: {min_long / 10**long_cfg['decimals']:,.6f}")
        print(f"  Min {short_cfg['symbol']} out @{slippage_pct}% slippage: {min_short / 10**short_cfg['decimals']:,.6f}")
    print(f"  Facet: {fn}(...)  — isWithinBounds caps the min-out USD within ±5% of the oracle estimate.")
    print(f"  GMX execution fee: {exec_fee / 1e18:.6f} ETH  (msg.value; {fee_d['buffer_mult']}x of "
          f"{fee_d['adjusted_gas']:,} gas @ {fee_d['gas_price'] / 1e9:.2f} gwei). Excess is refunded by GMX.")
    print("  ASYNC: queues a GMX withdrawal request; a GMX keeper returns the underlying(s) in")
    print(f"  a later block. The Prime Account is FROZEN for {mkt['gm_feed']} until the callback fires.")
    print(f"  The EOA must also hold ~{exec_fee / 1e18:.6f} ETH (gas) on top of the execution fee.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return

    # Pre-check: EOA must hold the execution fee + tx gas (~0.0004 ETH floor).
    eoa_balance = w3.eth.get_balance(acct.address)
    tx_gas_cost = int(5_000_000 * _tx_gas_price(w3))
    gas_buf = max(tx_gas_cost, int(0.0004 * 1e18))
    min_needed = exec_fee + gas_buf
    if eoa_balance < min_needed:
        print(f"✗ EOA balance {eoa_balance / 1e18:.6f} ETH is below the minimum needed "
              f"({min_needed / 1e18:.6f} ETH: {exec_fee / 1e18:.6f} exec fee + {gas_buf / 1e18:.6f} tx gas).")
        print("  Transfer more ETH to the EOA before retrying.")
        return

    base_calldata = account.encode_abi(fn, args=[gm_amount, min_long, min_short, exec_fee])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data, "value": exec_fee,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"GMX {kind} withdrawal request", fallback_gas=5000000)
    ok = receipt["status"] == 1
    if ok:
        print("  Request queued — wait for the GMX keeper callback to return the underlying(s).")

# ─── GMX GLV vaults (GlvFacetArbitrum) ───────────────────────────────────────
# A GLV is a vault of GM markets. deposit/withdraw are PAYABLE + ASYNC + RedStone-gated, same
# mechanic as GMX V2 GM, plus an extra `targetMarket` arg (the GM market within the GLV to
# route liquidity into). The GLV token has no SolvencyFacet feed, so its USD price comes from
# the RedStone gateway median under its feed id (same source the facet aggregates from the
# calldata). If the GLV feed can't be resolved, glv-deposit/withdraw fail CLOSED on --execute
# (preview still works) rather than sign a min-out of zero — a signing tool must not accept
# unbounded slippage.

def _glv_price_usd(glv_feed: str):
    """USD price of a GLV token, taken as the median of the RedStone gateway packages for its
    feed id. Returns None if the gateway has no such feed (caller then fails closed)."""
    import statistics
    try:
        gw = _redstone_fetch_packages()
    except Exception:
        return None
    vals = []
    for pkg in gw.get(glv_feed, []):
        for dp in pkg["dataPoints"]:
            if dp["dataFeedId"] == glv_feed:
                vals.append(float(dp["value"]))
    return statistics.median(vals) if vals else None

def _glv_feed_candidates(vault_key: str, vault: dict) -> list:
    """Candidate RedStone feed ids for a GLV token. The exact feed id was NOT verified for
    this port, so we try the most likely names; if none resolve, the caller fails closed.
    TODO: confirm the live GLV RedStone feed id and pin it on the vault config."""
    long_sym = vault["long_token"]["symbol"]  # ETH / BTC
    return [f"GLV_{long_sym}_USDC", f"GLV_W{long_sym}_USDC", vault["glv_token"]]

def gather_glv(w3, account):
    """Read-only GLV vault positions on a Prime Account: per vault, the GLV token balance
    (ERC20 balanceOf) and a best-effort USD value via the RedStone gateway GLV feed (null if
    the feed can't be resolved). Returns a list of position dicts (empty if none). Shared by
    cmd_glv_positions (print) and cmd_defi (--json). Pure reads — no RedStone tx, no signing."""
    pa_cs = account.address
    erc = json.loads('[{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')
    items = list(GLV_VAULTS.items())
    legs = [(Web3.to_checksum_address(v["glv_token"]),
             bytes.fromhex("70a08231") + b"\x00" * 12 + bytes.fromhex(pa_cs[2:])) for _k, v in items]
    try:
        results = multicall(w3, legs)
    except Exception:
        results = [(False, b"")] * len(legs)
    out = []
    for (key, v), (ok, rd) in zip(items, results):
        raw = w3.codec.decode(["uint256"], rd)[0] if ok and rd else 0
        if raw == 0:
            continue
        bal = raw / 10**GLV_TOKEN_DECIMALS
        usd = None
        for feed in _glv_feed_candidates(key, v):
            p = _glv_price_usd(feed)
            if p is not None:
                usd = bal * p
                break
        out.append({"vault": key, "glv_token": v["glv_token"], "raw": raw,
                    "balance": f"{bal:.6f}", "usd": usd})
    return out

def cmd_glv_positions():
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Owner wallet: {acct.address}")
    if not pa:
        print("No Prime Account yet — no GLV positions.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Prime Account: {pa}")
    positions = gather_glv(w3, account)
    if not positions:
        print("  No GLV vault positions.")
        return
    for pos in positions:
        line = f"  [{pos['vault']}] GLV  ({pos['glv_token']})  balance {float(pos['balance']):,.6f}"
        if pos.get("usd") is not None:
            line += f"  -> ~${pos['usd']:,.2f}"
        else:
            line += "  (USD n/a — GLV RedStone feed unresolved)"
        print(line)

def _glv_resolve_target(vault: dict, target_market: str):
    """Resolve the GLV deposit/withdraw targetMarket address. Accepts a GM market key
    (e.g. 'eth-usdc'), a GM feed name (e.g. 'GM_ETH_WETH_USDC'), or a raw 0x address; falls
    back to the vault's default GM market. Returns (checksum_addr, label) or (None, msg)."""
    if not target_market:
        return Web3.to_checksum_address(vault["default_target"]), vault["default_target_name"]
    tm = target_market.strip()
    if tm.lower() in GMX_MARKETS:
        return Web3.to_checksum_address(GMX_MARKETS[tm.lower()]["gm_token"]), GMX_MARKETS[tm.lower()]["gm_feed"]
    for _k, m in GMX_MARKETS.items():
        if m["gm_feed"].upper() == tm.upper():
            return Web3.to_checksum_address(m["gm_token"]), m["gm_feed"]
    if tm.startswith("0x") and len(tm) == 42:
        try:
            return Web3.to_checksum_address(tm), tm
        except Exception:
            pass
    return None, (f"Unknown --target-market '{target_market}'. Use a GM market key "
                  f"({', '.join(k for k, m in GMX_MARKETS.items() if not m['plus'])}), a GM feed "
                  f"name, or a 0x GM token address.")

def cmd_glv_deposit(vault_key: str, amount: float, is_long: bool | None = None,
                    target_market: str = None, slippage_pct: float = 1.0,
                    fee_buffer: float = 2.0, execute: bool = False):
    """Deposit into a GMX GLV vault on the Prime Account. --side long deposits the vault's long
    token (WETH/WBTC), short deposits USDC; GMX routes into --target-market (default the vault's
    primary GM market). PAYABLE + ASYNC + RedStone-gated, like gmx-deposit, with an extra
    targetMarket arg. minGlvAmount is priced off the RedStone gateway GLV feed; if that feed
    can't be resolved the deposit FAILS CLOSED on --execute (no zero-min-out signing)."""
    vault_key = vault_key.lower()
    if vault_key not in GLV_VAULTS:
        print(f"Unknown --vault '{vault_key}'. Choose from: {', '.join(GLV_VAULTS)}")
        return
    if slippage_pct > GMX_MAX_SLIPPAGE_PCT:
        print(f"--slippage {slippage_pct}% exceeds the facet's {GMX_MAX_SLIPPAGE_PCT}% isWithinBounds cap; refusing.")
        return
    vault = GLV_VAULTS[vault_key]
    target_addr, target_label = _glv_resolve_target(vault, target_market)
    if target_addr is None:
        print(target_label)
        return

    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — nothing to deposit.")
        print("Create and fund one first: arbprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    is_long_eff = True if is_long is None else is_long  # default to the long (volatile) leg
    long_meta = {"token": vault["long_token"]["addr"], "decimals": vault["long_token"]["decimals"],
                 "symbol": vault["long_token"]["symbol"]}
    short_meta = _gmx_leg_meta({"short": vault["short"], "plus": False}, "short")
    dep_meta = long_meta if is_long_eff else short_meta
    dep_sym = dep_meta["symbol"]

    amount_wei = to_wei_units(amount, dep_meta["decimals"])
    in_balance = account.functions.getBalance(asset_b32(dep_sym)).call()
    if amount_wei > in_balance:
        print(f"Prime Account holds only {in_balance / 10**dep_meta['decimals']:.6f} {dep_sym} "
              f"in-account; cannot deposit {amount} {dep_sym}.")
        print("Fund or borrow more of the asset into the account first.")
        return

    # Underlyings payload (collateral feeds) for the off-chain deposit-token price read.
    underlyings = list(dict.fromkeys([long_meta["symbol"], short_meta["symbol"]]))
    price_payload = build_redstone_payload(underlyings)
    p_dep = _gmx_underlying_price_usd(w3, account, price_payload, dep_sym)
    deposit_usd = amount_wei / 10**dep_meta["decimals"] * p_dep / 1e8

    # GLV token price (gateway). May be None if the feed id can't be resolved.
    glv_usd = None
    glv_feed_used = None
    for feed in _glv_feed_candidates(vault_key, vault):
        glv_usd = _glv_price_usd(feed)
        if glv_usd is not None:
            glv_feed_used = feed
            break

    exec_fee, fee_d = _estimate_gmx_execution_fee(w3, is_deposit=True, buffer_mult=fee_buffer)
    fn = vault["deposit_fn"]
    leg = f"{'long ' + dep_sym if is_long_eff else 'short ' + dep_sym} leg"
    print(f"GMX GLV deposit into [{vault_key}] (GLV {vault['glv_token']}) on Prime Account {pa}")
    print(f"  Deposit: {amount} {dep_sym} ({leg})  (~${deposit_usd:,.2f})")
    print(f"  Target GM market: {target_label}  ({target_addr})")
    if glv_usd is not None:
        fair_glv = deposit_usd / glv_usd
        min_glv = int(fair_glv * (1 - slippage_pct / 100) * 10**GLV_TOKEN_DECIMALS)
        print(f"  Fair GLV out: {fair_glv:,.6f}  (GLV ${glv_usd:,.6f}, feed {glv_feed_used}); "
              f"minGlvAmount @{slippage_pct}%: {min_glv / 10**GLV_TOKEN_DECIMALS:,.6f}")
    else:
        min_glv = None
        print("  ⚠ GLV price could not be resolved from the RedStone gateway (feed id unverified).")
        print("    Cannot compute a safe minGlvAmount — --execute will REFUSE (no zero-min-out signing).")
    print(f"  Facet: {fn}(isLongToken, tokenAmount, minGlvAmount, targetMarket, executionFee)")
    print(f"  GMX execution fee: {exec_fee / 1e18:.6f} ETH  (msg.value; {fee_d['buffer_mult']}x of "
          f"{fee_d['adjusted_gas']:,} gas @ {fee_d['gas_price'] / 1e9:.2f} gwei). Excess refunded by GMX.")
    print(f"  ASYNC: queues a GLV deposit request; a GMX keeper mints the GLV tokens later. The "
          f"account is FROZEN for the vault until the callback fires. EOA needs ~{exec_fee / 1e18:.6f} ETH gas too.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return
    if min_glv is None:
        print("✗ Refusing to broadcast: GLV min-out is unpriced (would accept unbounded slippage). "
              "Resolve the GLV RedStone feed id first.")
        return

    eoa_balance = w3.eth.get_balance(acct.address)
    tx_gas_cost = int(5_000_000 * _tx_gas_price(w3))
    gas_buf = max(tx_gas_cost, int(0.0004 * 1e18))
    min_needed = exec_fee + gas_buf
    if eoa_balance < min_needed:
        print(f"✗ EOA balance {eoa_balance / 1e18:.6f} ETH is below the minimum needed "
              f"({min_needed / 1e18:.6f} ETH: {exec_fee / 1e18:.6f} exec fee + {gas_buf / 1e18:.6f} tx gas).")
        print("  Transfer more ETH to the EOA before retrying.")
        return

    # Write payload: full solvency feed set + collateral underlyings (deduped). GLV deposit is
    # solvency-gated; the inline check prices the full debt registry, each feed needing 3 signers.
    _solv_feeds = prime_account_price_feeds(account)
    _extra = [f for f in underlyings if f not in _solv_feeds]
    payload = build_redstone_payload(_solv_feeds + _extra)
    base_calldata = account.encode_abi(fn, args=[is_long_eff, amount_wei, min_glv, target_addr, exec_fee])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data, "value": exec_fee,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "GLV deposit request", fallback_gas=5000000)
    ok = receipt["status"] == 1
    if ok:
        print("  Request queued — wait for the GMX keeper callback to mint the GLV tokens.")
    return ok

def cmd_glv_withdraw(vault_key: str, amount: float, target_market: str = None,
                     slippage_pct: float = 1.0, fee_buffer: float = 2.0, execute: bool = False):
    """Withdraw from a GMX GLV vault on the Prime Account by burning --amount GLV tokens. Returns
    the GM market's long token + USDC. PAYABLE + ASYNC + RedStone-gated, with a targetMarket arg
    (default the vault's primary GM market). min long/short token floors are derived from the
    burned GLV's USD value (gateway-priced) split 50/50 then capped at the facet's ±5% band; if
    the GLV feed can't be resolved the withdraw FAILS CLOSED on --execute."""
    vault_key = vault_key.lower()
    if vault_key not in GLV_VAULTS:
        print(f"Unknown --vault '{vault_key}'. Choose from: {', '.join(GLV_VAULTS)}")
        return
    if slippage_pct > GMX_MAX_SLIPPAGE_PCT:
        print(f"--slippage {slippage_pct}% exceeds the facet's {GMX_MAX_SLIPPAGE_PCT}% isWithinBounds cap; refusing.")
        return
    vault = GLV_VAULTS[vault_key]
    target_addr, target_label = _glv_resolve_target(vault, target_market)
    if target_addr is None:
        print(target_label)
        return

    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Prime Account exists for this wallet — no GLV position to withdraw.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    glv_cs = Web3.to_checksum_address(vault["glv_token"])
    erc = json.loads('[{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')
    glv_bal = w3.eth.contract(address=glv_cs, abi=erc).functions.balanceOf(pa_cs).call()
    glv_amount = to_wei_units(amount, GLV_TOKEN_DECIMALS)
    if glv_bal == 0:
        print(f"Prime Account holds no [{vault_key}] GLV tokens — nothing to withdraw.")
        return
    if glv_amount > glv_bal:
        print(f"Prime Account holds only {glv_bal / 10**GLV_TOKEN_DECIMALS:,.6f} GLV; clamping to that.")
        glv_amount = glv_bal

    long_meta = {"token": vault["long_token"]["addr"], "decimals": vault["long_token"]["decimals"],
                 "symbol": vault["long_token"]["symbol"]}
    short_meta = _gmx_leg_meta({"short": vault["short"], "plus": False}, "short")
    long_feed, short_feed = long_meta["symbol"], short_meta["symbol"]
    underlyings = list(dict.fromkeys([long_feed, short_feed]))
    price_payload = build_redstone_payload(underlyings)
    p_long = _gmx_underlying_price_usd(w3, account, price_payload, long_feed)
    p_short = _gmx_underlying_price_usd(w3, account, price_payload, short_feed)

    glv_usd = glv_feed_used = None
    for feed in _glv_feed_candidates(vault_key, vault):
        glv_usd = _glv_price_usd(feed)
        if glv_usd is not None:
            glv_feed_used = feed
            break

    exec_fee, fee_d = _estimate_gmx_execution_fee(w3, is_deposit=False, buffer_mult=fee_buffer)
    fn = vault["withdraw_fn"]
    print(f"GMX GLV withdraw from [{vault_key}] (GLV {vault['glv_token']}) on Prime Account {pa}")
    print(f"  Burn: {glv_amount / 10**GLV_TOKEN_DECIMALS:,.6f} GLV")
    print(f"  Target GM market: {target_label}  ({target_addr})")
    if glv_usd is not None:
        burn_usd = glv_amount / 10**GLV_TOKEN_DECIMALS * glv_usd
        slip = 1 - slippage_pct / 100
        # Conservative 50/50 split (the GLV is GM-backed; the keeper returns long+USDC).
        min_long = int((burn_usd * 0.5) / (p_long / 1e8) * slip * 10**long_meta["decimals"])
        min_short = int((burn_usd * 0.5) / (p_short / 1e8) * slip * 10**short_meta["decimals"])
        print(f"  Burn value ~${burn_usd:,.2f} (GLV ${glv_usd:,.6f}, feed {glv_feed_used}); "
              f"min {long_feed} {min_long / 10**long_meta['decimals']:,.6f} + "
              f"min {short_feed} {min_short / 10**short_meta['decimals']:,.6f} @{slippage_pct}% (50/50 split)")
    else:
        min_long = min_short = None
        print("  ⚠ GLV price could not be resolved from the RedStone gateway (feed id unverified).")
        print("    Cannot compute safe min-out floors — --execute will REFUSE.")
    print(f"  Facet: {fn}(glvAmount, targetMarket, minLongTokenAmount, minShortTokenAmount, executionFee)")
    print(f"  GMX execution fee: {exec_fee / 1e18:.6f} ETH  (msg.value; {fee_d['buffer_mult']}x of "
          f"{fee_d['adjusted_gas']:,} gas @ {fee_d['gas_price'] / 1e9:.2f} gwei). Excess refunded by GMX.")
    print(f"  ASYNC: queues a GLV withdrawal request; a GMX keeper returns the underlying(s) later. "
          f"Account FROZEN for the vault until the callback. EOA needs ~{exec_fee / 1e18:.6f} ETH gas too.")

    if not execute:
        print("Run with --execute to broadcast (appends a fresh RedStone price payload).")
        return
    if min_long is None:
        print("✗ Refusing to broadcast: GLV min-out is unpriced (would accept unbounded slippage). "
              "Resolve the GLV RedStone feed id first.")
        return

    eoa_balance = w3.eth.get_balance(acct.address)
    tx_gas_cost = int(5_000_000 * _tx_gas_price(w3))
    gas_buf = max(tx_gas_cost, int(0.0004 * 1e18))
    min_needed = exec_fee + gas_buf
    if eoa_balance < min_needed:
        print(f"✗ EOA balance {eoa_balance / 1e18:.6f} ETH is below the minimum needed "
              f"({min_needed / 1e18:.6f} ETH: {exec_fee / 1e18:.6f} exec fee + {gas_buf / 1e18:.6f} tx gas).")
        print("  Transfer more ETH to the EOA before retrying.")
        return

    # GLV withdraw is solvency-gated (returns funds to the account, runs remainsSolvent), so a
    # full solvency payload + collateral underlyings is appended (deduped).
    _solv_feeds = prime_account_price_feeds(account)
    _extra = [f for f in underlyings if f not in _solv_feeds]
    payload = build_redstone_payload(_solv_feeds + _extra)
    base_calldata = account.encode_abi(fn, args=[glv_amount, target_addr, min_long, min_short, exec_fee])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data, "value": exec_fee,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "GLV withdrawal request", fallback_gas=5000000)
    ok = receipt["status"] == 1
    if ok:
        print("  Request queued — wait for the GMX keeper callback to return the underlying(s).")
    return ok

# ─── TraderJoe V2 Liquidity Book (TraderJoeV2ArbitrumFacet) ─────────────────
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
    Shared by cmd_lb_positions (print) and cmd_defi (--json).
    STUBBED on Arbitrum: with TJ_LB_PAIRS empty (pairs unverified) there's no pair metadata to
    label/decimal-decode owned bins, so return [] rather than surface raw unmapped addresses."""
    if not TJ_LB_PAIRS:
        return []
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

    # Multicall the per-pair reads: getActiveId + getTokenX + getTokenY + (balanceOf +
    # totalSupply + getBin) per bin. For a typical LP across 5-10 bins this collapses
    # 18-33 sequential RPCs per pair into 1. All legs are oracle-free LBPair views.
    out = []
    for pair_cs, ids in grouped.items():
        ids.sort()
        meta = by_addr.get(pair_cs)
        c = _lb_pair_contract(w3, pair_cs)
        legs = [
            ("getActiveId", ["uint24"], c.encode_abi("getActiveId", args=[])),
            ("getTokenX", ["address"], c.encode_abi("getTokenX", args=[])),
            ("getTokenY", ["address"], c.encode_abi("getTokenY", args=[])),
        ]
        for binid in ids:
            legs.append((f"balanceOf:{binid}", ["uint256"],
                         c.encode_abi("balanceOf", args=[pa_cs, binid])))
            legs.append((f"totalSupply:{binid}", ["uint256"],
                         c.encode_abi("totalSupply", args=[binid])))
            legs.append((f"getBin:{binid}", ["uint128", "uint128"],
                         c.encode_abi("getBin", args=[binid])))
        try:
            results = multicall(w3, [(pair_cs, bytes.fromhex(d[2:])) for _, _, d in legs])
        except Exception as e:
            out.append({"pair": pair_cs, "error": type(e).__name__})
            continue
        decoded = {}
        for (name, out_types, _d), (ok, rd) in zip(legs, results):
            if not ok or not rd:
                decoded[name] = None
                continue
            try:
                raw_out = w3.codec.decode(out_types, rd)
                decoded[name] = raw_out if len(out_types) > 1 else raw_out[0]
            except Exception:
                decoded[name] = None
        if decoded.get("getActiveId") is None or decoded.get("getTokenX") is None or decoded.get("getTokenY") is None:
            out.append({"pair": pair_cs, "error": "PairReadReverted"})
            continue
        active = decoded["getActiveId"]
        tx_addr = Web3.to_checksum_address(decoded["getTokenX"])
        ty_addr = Web3.to_checksum_address(decoded["getTokenY"])
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
            bal = decoded.get(f"balanceOf:{binid}")
            ts = decoded.get(f"totalSupply:{binid}")
            getbin = decoded.get(f"getBin:{binid}")
            if bal is None or ts is None or getbin is None:
                continue
            rx, ry = getbin
            share = (bal / ts) if ts else 0
            sum_x += rx * share / 10**x_cfg["decimals"]
            sum_y += ry * share / 10**y_cfg["decimals"]
        out.append({"pair": pair_cs, "label": label, "active_bin": int(active),
                    "bins": len(ids), "bin_range": [ids[0], ids[-1]],
                    "token_x": {"symbol": x_cfg["symbol"], "amount": sum_x},
                    "token_y": {"symbol": y_cfg["symbol"], "amount": sum_y}})
    return out

def _lb_not_configured() -> bool:
    """LB is stubbed on Arbitrum until the whitelisted pairs are verified. Print the TODO
    notice and return True when there's nothing configured, so the LB commands fail clean."""
    if not TJ_LB_PAIRS:
        print("TraderJoe LB pairs not yet configured for Arbitrum (TODO). The "
              "TraderJoeV2ArbitrumFacet exists, but the whitelisted pair set + token order + "
              "bin steps were not verified for this tool, so LB ops are disabled (a signing "
              "tool must not guess pair addresses).")
        return True
    return False

def cmd_lb_positions():
    if _lb_not_configured():
        return
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
    remainsSolvent). STUBBED on Arbitrum (TJ_LB_PAIRS empty)."""
    if _lb_not_configured():
        return
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
        print("Create and fund one first: arbprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    amount_x_wei = to_wei_units(amount_x, x_cfg["decimals"]) if has_x else 0
    amount_y_wei = to_wei_units(amount_y, y_cfg["decimals"]) if has_y else 0

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
    # Gas scales with bin count: ~150k per bin + 250k overhead + RedStone payload.
    # 31 bins was OOG at 5M (consumed 4.9M); 80 bins (the account cap) needs ~12M.
    bin_count = len(deltas)
    gas = max(5000000, min(12000000, 250000 + bin_count * 160000))
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": gas, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "LB add", fallback_gas=5000000)
    ok = receipt["status"] == 1
    return ok


def cmd_lb_remove(pair_key: str, slippage_pct: float = 1.0, execute: bool = False):
    """Remove ALL of the Prime Account's TraderJoe V2 LB liquidity for a whitelisted pair.
    Reads the owned bin ids for the pair (getOwnedTraderJoeV2Bins) and the account's LB
    balance per bin, then calls removeLiquidityTraderJoeV2 with those ids+amounts.
    amountXMin/amountYMin are slippage floors on the totals withdrawn, derived from the
    account's current share of each bin's reserves. removeLiquidity is NOT solvency-gated,
    so no RedStone payload is appended. Refuses if no Prime Account or no position.
    STUBBED on Arbitrum (TJ_LB_PAIRS empty)."""
    if _lb_not_configured():
        return
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
        "gas": 5000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "LB remove", fallback_gas=5000000)
    ok = receipt["status"] == 1

# ─── PRIME-token leverage tiers (PrimeLeverageFacet) ─────────────────────────

_prime_addr_cache = None

def _prime_token_address(w3) -> str:
    """Resolve the PRIME token address on-chain via TokenManager.getAssetAddress("PRIME",
    true), falling back to the hardcoded PRIME_TOKEN["addr"] if the lookup reverts or returns
    zero. Cached per run. (getTokenAddress does NOT exist on this TokenManager — the getter is
    getAssetAddress(bytes32,bool).)"""
    global _prime_addr_cache
    if _prime_addr_cache is not None:
        return _prime_addr_cache
    fallback = Web3.to_checksum_address(PRIME_TOKEN["addr"])
    try:
        tm_abi = json.loads('[{"inputs":[{"name":"_asset","type":"bytes32"},{"name":"_active","type":"bool"}],'
                            '"name":"getAssetAddress","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"}]')
        tm = w3.eth.contract(address=Web3.to_checksum_address(TOKEN_MANAGER), abi=tm_abi)
        addr = tm.functions.getAssetAddress(asset_b32("PRIME"), True).call()
        _prime_addr_cache = Web3.to_checksum_address(addr) if int(addr, 16) != 0 else fallback
    except Exception:
        _prime_addr_cache = fallback
    return _prime_addr_cache

def _prime_token_contract(w3):
    """Minimal PRIME ERC20 (balanceOf + approve) for the deposit/balance reads. The PRIME
    address is resolved on-chain (TokenManager.getAssetAddress) with a hardcoded fallback."""
    abi = json.loads('[{"constant":true,"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
                     '{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]')
    return w3.eth.contract(address=_prime_token_address(w3), abi=abi)

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
        print("  arbprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    prime = _prime_token_contract(w3)

    tier = account.functions.getLeverageTier().call()
    if tier == PRIME_TIERS["premium"]:
        print(f"Prime Account {pa} is already in PREMIUM tier — nothing to do.")
        return

    deposit_wei = to_wei_units(amount, PRIME_TOKEN["decimals"]) if amount else 0
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
        print(f"  Deposit more PRIME first: arbprime prime-activate --amount <N> --execute")
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
            "gas": 100000, "chainId": CHAIN_ID,
        })
        _set_gas_price(w3, app_tx)
        w3.eth.send_raw_transaction(acct.sign_transaction(app_tx).raw_transaction)
        data = account.encode_abi("depositPrime", args=[deposit_wei]) + payload.hex()
        dep_tx = {
            "from": acct.address, "to": pa_cs, "data": data,
            "nonce": nonce + 1,
            "gas": 3000000, "chainId": CHAIN_ID,
        }
        receipt = _sign_and_send(w3, acct, dep_tx, "depositPrime", fallback_gas=3000000)
        dep_ok = receipt["status"] == 1
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
        "gas": 3000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "PREMIUM tier activate", fallback_gas=3000000)
    ok = receipt["status"] == 1

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
        print("  arbprime create-prime-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    prime = _prime_token_contract(w3)

    eoa_prime = prime.functions.balanceOf(acct.address).call()
    in_acct_prime = account.functions.getBalance(asset_b32(PRIME_TOKEN["symbol"])).call()
    deposit_wei = to_wei_units(amount, PRIME_TOKEN["decimals"])
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
        "gas": 100000, "chainId": CHAIN_ID,
    })
    _set_gas_price(w3, app_tx)
    w3.eth.send_raw_transaction(acct.sign_transaction(app_tx).raw_transaction)
    data = account.encode_abi("depositPrime", args=[deposit_wei]) + payload.hex()
    dep_tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": nonce + 1,
        "gas": 3000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, dep_tx, "depositPrime", fallback_gas=3000000)
    dep_ok = receipt["status"] == 1

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
        "gas": 3000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "PREMIUM tier deactivate", fallback_gas=3000000)
    ok = receipt["status"] == 1

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
    amount_wei = to_wei_units(amount, PRIME_TOKEN["decimals"])
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
        "gas": 3000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "PRIME unstake", fallback_gas=3000000)
    ok = receipt["status"] == 1

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
    amount_wei = to_wei_units(amount, PRIME_TOKEN["decimals"])

    print(f"PRIME repay debt: {amount} PRIME on Prime Account {pa}")
    print(f"  Recorded PRIME debt: {recorded_debt / 10**PRIME_TOKEN['decimals']:,.6f}  "
          "(last snapshot; repay re-snapshots and caps to the true current debt)")
    print(f"  In-account PRIME:  {in_acct_prime / 10**PRIME_TOKEN['decimals']:,.6f}")
    if amount_wei > in_acct_prime:
        print(f"  ✗ Requested {amount} PRIME exceeds in-account PRIME "
              "(facet reverts 'Not enough PRIME to repay the debt'). Refusing.")
        print("  Deposit PRIME into the account first: arbprime prime-activate --amount <N> --execute")
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
        "gas": 3000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "PRIME debt repay", fallback_gas=3000000)
    ok = receipt["status"] == 1

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

def _zap_preflight_gas(w3, gmx_leg_count: int = 1) -> tuple:
    """Estimate total ETH needed for a multi-leg zap (gas + GMX exec fees) and return
    (needed_wei, breakdown_str). If gmx_leg_count > 0, the first leg uses fee_buffer=2x,
    subsequent legs default to 1x (the keeper usually settles fast enough that the second
    deposit won't need the same buffer). Returns (0, empty) on estimation failure."""
    try:
        gas_price = _tx_gas_price(w3)
        # Rough gas estimates per leg type
        FUND_GAS = 300000    # approve + fund (~2 txs worth of gas, but both are sequential)
        BORROW_GAS = 800000
        SWAP_GAS = 1200000
        # Non-GMX tx gas cost
        non_gmx_eth = (FUND_GAS + BORROW_GAS + SWAP_GAS) * gas_price
        # GMX exec fees
        gmx_eth = 0
        for i in range(gmx_leg_count):
            buf = 2.0 if i == 0 else 1.0
            fee, _d = _estimate_gmx_execution_fee(w3, is_deposit=True, buffer_mult=buf)
            gmx_eth += fee
        # Gas for the GMX tx itself (separate from the exec fee)
        gmx_tx_gas = 500000 * gas_price
        total = non_gmx_eth + gmx_eth + gmx_tx_gas
        breakdown = (
            f"  Gas budget: non-GMX legs ~{non_gmx_eth / 1e18:.6f} ETH"
            f"  + GMX exec fees ~{gmx_eth / 1e18:.6f} ETH"
            f"  + GMX tx gas ~{gmx_tx_gas / 1e18:.6f} ETH"
            f"  = ~{total / 1e18:.4f} ETH total"
        )
        return total, breakdown
    except Exception:
        return 0, ""


def cmd_zap(market: str, collateral_pool: str, collateral_amount: float, borrow_amount: float,
            deposit_amount: float = 0, side: str = "auto", swap_to_long: bool = False,
            slippage_pct: float = 1.0, fee_buffer: float = 2.0, execute: bool = False):
    """Leveraged-long zap: fund collateral -> borrow USDC -> [optimal swap] -> GMX GM deposit.
    Composes existing leg commands; each leg is its own tx. Preview prints the ordered plan;
    --execute runs legs in order and stops on first failure (partial-state safe). The GMX
    leg is async (fires the request; the keeper settles later, account frozen until then).

    OPTIMAL PATH (default --side auto):
      ALWAYS picks short (USDC) — because the zap always borrows USDC, USDC is always in
      the account after borrowing. Deposit USDC as the short leg and GMX handles the
      internal conversion to get the right 50/50 long/USDC split. Zero swaps, 3 txs:
      fund -> borrow -> gmx-deposit.
      This works for EVERY collateral type and EVERY two-sided GM market.

    --side long|short overrides auto-detection (rarely needed).
    --swap forces a USDC->long-token swap (only needed with --side long). The swap amount is
    auto-calculated to only convert what's needed for the target leg, not all borrowed.

    PRE-FLIGHT GAS CHECK: estimates total ETH before broadcasting; refuses if short."""
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
    if side not in ("auto", "long", "short"):
        print("--side must be 'auto', 'long' or 'short'.")
        return
    if collateral_amount <= 0 or borrow_amount <= 0:
        print("--collateral-amount and --borrow-amount must be > 0.")
        return
    if slippage_pct > GMX_MAX_SLIPPAGE_PCT:
        print(f"--slippage {slippage_pct}% exceeds the GMX {GMX_MAX_SLIPPAGE_PCT}% cap; the deposit "
              "leg would revert. Lower it.")
        return

    w3 = get_w3()
    # long_sym is the long-leg DEPOSIT token symbol (the WETH/"ETH" deposit token for the
    # synthetics, else the volatile symbol), so the swap + gmx-deposit legs use a real
    # in-account asset and price correctly. short is always USDC.
    long_sym = _gmx_leg_meta(mkt, "long")["symbol"]
    short_sym = mkt["short"]
    collateral_sym = pool_to_asset_symbol(collateral_pool)

    # ── Universal auto-side: always short (USDC) ────────────────────────────────
    # In a leveraged zap we ALWAYS borrow USDC. After borrowing, USDC is always in the
    # account. Depositing as short (USDC) means: no swap, no conversion, 3 txs total.
    # GMX handles the internal 50/50 split — it converts part of the USDC to the long
    # token automatically. This works for EVERY two-sided GM market (eth-usdc, btc-usdc,
    # arb-usdc, ...) regardless of collateral type.
    coll_asset_is_short = collateral_sym == short_sym  # USDC
    if side == "auto":
        side = "short"  # USDC is always available after borrowing — simplest path

    deposit_sym = long_sym if side == "long" else short_sym
    needs_swap = swap_to_long or (side == "long")

    # ── Calculate deposit amount ──────────────────────────────────────────────
    # Total position value = collateral + borrow. For a two-sided GM market the
    # deposit can be either leg: GMX converts internally to match the 50/50 split.
    position_value = collateral_amount + borrow_amount
    if deposit_amount <= 0:
        # Auto-calculate: deposit the full position value in the deposit token's
        # units (USDC for short, the long-leg token for long).
        if deposit_sym == short_sym:
            deposit_amount = position_value  # USDC -> 1:1 with USD
        else:
            # Long leg: need the token amount = position_value / 2 / long_price.
            # Read live price from a RedStone view on an existing account, or fall
            # back to refusing if no price is available.
            try:
                pa = get_prime_account(w3, get_account().address)
                if pa:
                    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
                    payload = build_redstone_payload([long_sym])
                    long_price_raw = _gmx_underlying_price_usd(w3, account, payload, long_sym) / 1e8
                else:
                    long_price_raw = 0
            except Exception:
                long_price_raw = 0
            if long_price_raw > 0:
                deposit_amount = position_value / 2 / long_price_raw
            else:
                # No price available — refuse; agent must provide --deposit-amount explicitly
                print(f"Cannot auto-calculate {long_sym} deposit amount (no on-chain price available).")
                print(f"Provide --deposit-amount <{long_sym.lower()}_tokens> explicitly.")
                return
    if deposit_amount <= 0:
        print("--deposit-amount must be > 0.")
        return

    # ── Calculate swap amount (optimal split) ─────────────────────────────────
    # When swapping USDC->long-token for a long-leg deposit, we only need the long-leg
    # portion (~50% of position value), NOT all borrowed USDC.
    swap_amount = 0
    if needs_swap and deposit_sym == long_sym:
        # Position total = ~$position_value. The long leg needs ~50%.
        # We borrow in USDC; collateral is also USDC. Total USDC available =
        # collateral + borrow. We need half in the long token, half stays in USDC.
        # So swap only (position_value / 2) USDC -> long token.
        swap_amount = position_value / 2
        if swap_amount > borrow_amount:
            swap_amount = borrow_amount
        if swap_amount + collateral_amount > position_value:
            swap_amount = position_value / 2
        # Ensure at least some of the long token to deposit
        if swap_amount < 1:
            print("Position too small for a profitable swap — deposit as short leg instead.")
            return

    # ── Pre-flight gas check ──────────────────────────────────────────────────
    gmx_legs = 1
    total_needed, gas_breakdown = _zap_preflight_gas(w3, gmx_legs)
    if total_needed > 0:
        eoa_balance = w3.eth.get_balance(get_account().address)
        if eoa_balance < total_needed:
            short_by = total_needed - eoa_balance
            print(f"⚠  INSUFFICIENT EOA ETH — zap needs ~{total_needed / 1e18:.4f} ETH total")
            print(f"   EOA has {eoa_balance / 1e18:.4f} ETH, short by {short_by / 1e18:.4f} ETH.")
            print(gas_breakdown)
            print("   Fund the EOA wallet with more ETH before retrying.")
            return

    # ── Build ordered leg plan ────────────────────────────────────────────────
    legs = []
    legs.append((
        f"1. fund {collateral_amount} {collateral_sym} (collateral) into the Prime Account",
        lambda ex: cmd_fund(collateral_pool, collateral_amount, ex),
        False, "ERC20 approves the account then calls fund()."))
    legs.append((
        f"2. borrow {borrow_amount} {short_sym} against the collateral (leverage)",
        lambda ex: cmd_borrow(_SYMBOL_TO_POOL[short_sym], borrow_amount, ex),
        True, "RedStone-gated — needs the account solvent after the draw."))
    if needs_swap and swap_amount > 0:
        legs.append((
            f"3. swap {swap_amount:.2f} {short_sym} -> {long_sym} (YieldYak) — only the long-leg portion",
            lambda ex: cmd_swap(short_sym, long_sym, swap_amount, slippage_pct, "yak", ex),
            True, f"Swaps only {swap_amount:.1f} USDC (not all {borrow_amount}). RedStone-gated."))
    gmx_label_num = '4' if (needs_swap and swap_amount > 0) else '3'
    legs.append((
        f"{gmx_label_num}. gmx-deposit {deposit_amount:.4f} {deposit_sym} "
        f"({'long' if side == 'long' else 'short'} leg) into [{market}] {mkt['gm_feed']}",
        lambda ex: cmd_gmx_deposit(market, deposit_amount, side == "long", slippage_pct, fee_buffer, ex),
        True, "PAYABLE + ASYNC: fires a GMX deposit request; account freezes until keeper settles."))

    # ── Print plan ────────────────────────────────────────────────────────────
    swap_note = f"  |  optimal swap {swap_amount:.1f} {short_sym}->{long_sym}" if needs_swap else ""
    print(f"=== Optimal leveraged zap into [{market}] {mkt['gm_feed']} ===")
    print(f"  Collateral: {collateral_amount} {collateral_sym}  |  "
          f"Borrow: {borrow_amount} {short_sym}  |  Position: ~${position_value:.0f}")
    print(f"  Deposit: {deposit_amount:.4f} {deposit_sym} ({side} leg, GMX handles internal split)"
          f"{swap_note}")
    print(f"  {len(legs)} tx(s). Optimal path: auto-selected side={side}"
          + (f" (match collateral {collateral_sym}). No swap needed." if not needs_swap
             else f" (swapping only the long-leg portion)."))
    print("  Ordered plan:")
    for label, _fn, gated, note in legs:
        print(f"    {label}   [{'RedStone-gated' if gated else 'not gated'}]")
        print(f"        {note}")
    if total_needed > 0:
        print(f"  Pre-flight: {gas_breakdown}")
    print("  Terminal GMX leg is ASYNC — keeper mints later, account frozen until then.")

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
        print(f"\n  ✗ ZAP HALTED at leg {idx}: {label}")
        print(f"    Result: {'tx failed on-chain' if result is False else 'leg refused (see output above)'}.")
        if done:
            print(f"    Legs that DID complete: {len(done)}")
            for d in done:
                print(f"      ✓ {d}")
            print("    The Prime Account is in a PARTIAL state. Review with prime-summary before retrying.")
        else:
            print("    No legs completed; nothing changed on-chain.")
        return

    print(f"\n  ✓ ZAP COMPLETE — all {len(legs)} legs fired.")
    print("    NOTE: the final GMX deposit is ASYNC. Check `arbprime gmx-positions` later.")

def gather_defi() -> dict:
    """Aggregate ALL DeltaPrime (Arbitrum) positions for the selected wallet into one
    DeBank-style dict. Read-only: reuses the gather_* helpers (lending/solvency, GMX V2 LP,
    GLV vaults, TraderJoe V2 LB [stubbed], PRIME tier, plus the EOA's own pool deposits
    surfaced as a Savings group), each of which only does eth_calls. Empty groups are omitted.
    total_usd / health_ratio / solvent come from the RedStone-gated solvency views; per-asset
    USD is best-effort (null where a RedStone feed is missing). Never broadcasts."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    result = {
        "protocol": "DeltaPrime", "url": "https://app.deltaprime.io", "chain": "arbitrum",
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
        # Compute equity-based health (0-100%) from lending data + tier
        result["health_pct"] = _compute_health_pct(lending, tier.get("tier_code", 0)).get("health_pct")
        if lending["supplied"] or lending["borrowed"]:
            result["groups"].append({
                "type": "Lending / Leverage", "health_ratio": lending["health_ratio"],
                "health_pct": result["health_pct"],
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

        glv = gather_glv(w3, account)
        if glv:
            result["groups"].append({"type": "GMX GLV", "items": [
                {"label": p["vault"], "balance": p["balance"], "symbol": "GLV", "usd": p.get("usd")}
                for p in glv]})

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

    # PRIME group: include whenever there is any PRIME stake or in-account balance (the
    # in-account ~200 PRIME with an otherwise empty account is the expected current state).
    if tier["staked"] > 0 or tier["in_account"] > 0:
        result["groups"].append({
            "type": "PRIME", "tier": tier["tier"],
            "staked": tier["staked"], "in_account": tier["in_account"],
        })

    # Savings: the EOA's own pool deposits (the lending / savings side), independent of
    # the Prime Account. One balanceOf(EOA) per pool, batched into a single Multicall3.
    sav_legs, sav_meta = [], []
    for _pname, _pcfg in POOLS.items():
        try:
            _pc, _, _ = get_pool_contract(_pname)
        except Exception:
            continue
        _proxy_cs = Web3.to_checksum_address(_pcfg["proxy"])
        sav_legs.append((_proxy_cs, bytes.fromhex(_pc.encode_abi("balanceOf", args=[acct.address])[2:])))
        sav_meta.append(_pcfg)
    savings = []
    if sav_legs:
        try:
            sav_results = multicall(w3, sav_legs)
        except Exception:
            sav_results = [(False, b"")] * len(sav_legs)
        for _pcfg, (_ok, _rd) in zip(sav_meta, sav_results):
            _bal = w3.codec.decode(["uint256"], _rd)[0] if _ok and _rd else 0
            if _bal > 0:
                savings.append({"symbol": _pcfg["symbol"], "raw": _bal, "decimals": _pcfg["decimals"]})
    if savings:
        sav_prices = {}
        if account is not None:
            try:
                _sav_syms = list(dict.fromkeys(r["symbol"] for r in savings))
                sav_prices = _prices_usd(w3, account, _sav_syms, build_redstone_payload(_sav_syms))
            except Exception:
                sav_prices = {}
        sav_rows, sav_usd_total = [], 0.0
        for r in savings:
            amt = r["raw"] / 10**r["decimals"]
            row = {"symbol": r["symbol"], "balance": f"{amt:.6f}"}
            usd = sav_prices.get(r["symbol"])
            if usd is not None:
                row["usd"] = round(amt * usd, 2)
                sav_usd_total += amt * usd
            sav_rows.append(row)
        result["groups"].append({"type": "Savings", "label": "Savings", "supplied": sav_rows})
        if sav_usd_total:
            result["total_usd"] = (result["total_usd"] or 0) + sav_usd_total

    return result

_DEFI_DECORATIVE_KEYS = {"url"}


def _trim_defi_json(value):
    """Recursively strip noise from `defi --json` output so an LLM consumer doesn't pay
    context for fields that carry no information:
      - drops any dict key whose value is exactly None (RedStone-feed-missing markers,
        unbacked health/solvency reads, missing USD prices, etc.),
      - drops any key whose value is an EMPTY list or EMPTY dict (no "items": [],
        "rewards": []),
      - drops the decorative top-level `url` key (agents don't follow it),
      - PRESERVES numeric 0 and boolean False (those carry meaning: zero balance,
        explicitly-not-solvent, etc.),
      - keeps the top-level structure (chains, account, groups) so a consumer can still
        tell what's missing from what shape the response took.

    Lists are walked recursively too, so per-asset/per-group fields are trimmed
    consistently."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _DEFI_DECORATIVE_KEYS:
                continue
            trimmed = _trim_defi_json(v)
            if trimmed is None:
                continue
            if isinstance(trimmed, (list, dict)) and len(trimmed) == 0:
                continue
            out[k] = trimmed
        return out
    if isinstance(value, list):
        return [_trim_defi_json(v) for v in value]
    return value


def cmd_defi(as_json: bool = True):
    """Aggregate all DeltaPrime positions for the wallet. Default output is the DeBank-style
    JSON (the dashboard consumer). On error, emits {"status":"error", ...} rather than raising,
    so the caller always gets parseable JSON.

    JSON shape contract (v0.2.0): omits null, empty-list, and empty-dict fields; drops the
    decorative `url` key. Numeric zero and boolean false are preserved (they carry real
    information — zero balance, explicit not-solvent, etc.). The top-level structure
    (protocol/chain/wallet/prime_account/groups/status) is preserved so a consumer can
    still tell what's missing from what shape the response took."""
    try:
        data = gather_defi()
    except Exception as e:
        data = {"protocol": "DeltaPrime", "chain": "arbitrum",
                "status": "error", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(_trim_defi_json(data), indent=2))

# ─── PRIME Cross-Chain Bridge (Avalanche ↔ Arbitrum via LayerZero OFT) ─────────

BRIDGE_CHAIN = {
    "avax": {"rpc": "https://api.avax.network/ext/bc/C/rpc", "chain_id": 43114,
             "lz_chain_id": 106, "lz_endpoint": "0x3c2269811836af69497E5F486A85D7316753cf62",
             "prime_token": "0x33C8036E99082B0C395374832FECF70c42C7F298",
             "bridge_target": "0x35643752F4ea0ba70456F0CA1e2778f783206a20",
             "explorer": "https://snowtrace.io/tx", "native": "AVAX"},
    "arb": {"rpc": ARBITRUM_RPC, "chain_id": 42161,
            "lz_chain_id": 110, "lz_endpoint": "0x3c2269811836af69497E5F486A85D7316753cf62",
            "prime_token": "0x3De81CE90f5A27C5E6A5aDb04b54ABA488a6d14E",
            "bridge_target": "0x3De81CE90f5A27C5E6A5aDb04b54ABA488a6d14E",  # PRIME OFT itself
            "explorer": "https://arbiscan.io/tx", "native": "ETH"},
}

BRIDGE_ADAPTER_PARAMS = struct.pack('>H', 1) + struct.pack('>I', 200000).rjust(32, b'\x00')  # v1 + 200k gas

def _bridge_w3(chain_key: str):
    """Get a w3 for the bridge source chain. Avalanche needs POA middleware."""
    cfg = BRIDGE_CHAIN[chain_key]
    w3 = Web3(Web3.HTTPProvider(cfg["rpc"]))
    if chain_key == "avax":
        from web3.middleware import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3

def _bridge_estimate_lz_fee(w3, src_cfg: dict, dst_lz_chain_id: int, amount_wei: int):
    """Query LZ endpoint for the cross-chain message fee."""
    ep = w3.eth.contract(address=Web3.to_checksum_address(src_cfg["lz_endpoint"]),
        abi=json.loads(json.dumps([{
            "inputs": [{"name": "_dstChainId", "type": "uint16"},
                       {"name": "_userApplication", "type": "address"},
                       {"name": "_payload", "type": "bytes"},
                       {"name": "_payInZRO", "type": "bool"},
                       {"name": "_adapterParams", "type": "bytes"}],
            "name": "estimateFees",
            "outputs": [{"name": "", "type": "uint256"}, {"name": "", "type": "uint256"}],
            "stateMutability": "view", "type": "function"}])))
    native, zro = ep.functions.estimateFees(
        dst_lz_chain_id, Web3.to_checksum_address(src_cfg["bridge_target"]),
        _bridge_lz_payload(get_account().address, amount_wei),
        False, BRIDGE_ADAPTER_PARAMS).call()
    return native, zro

def _bridge_lz_payload(to_addr: str, amount_wei: int) -> bytes:
    """OFT payload: bytes32(toAddress) + uint256(amount)."""
    return bytes.fromhex(Web3.to_checksum_address(to_addr)[2:].rjust(64, "0")) + \
           amount_wei.to_bytes(32, 'big')

def cmd_prime_bridge(from_chain: str = "avax", amount: float = None, execute: bool = False):
    """Bridge PRIME between Avalanche and Arbitrum via LayerZero OFT.
    from_chain='avax' bridges Avalanche→Arbitrum (default in arbprime);
    from_chain='arb' bridges Arbitrum→Avalanche."""
    if amount is None:
        print("Usage: arbprime prime-bridge --from avax|arb --amount N [--execute]")
        return
    src_key = from_chain.lower()
    if src_key not in BRIDGE_CHAIN:
        print(f"Unknown source chain '{from_chain}'. Use 'avax' or 'arb'.")
        return
    dst_key = "arb" if src_key == "avax" else "avax"
    src_cfg = BRIDGE_CHAIN[src_key]
    dst_cfg = BRIDGE_CHAIN[dst_key]
    src_chain_id = src_cfg["chain_id"]
    amount_wei = to_wei_units(amount, 18)

    w3 = _bridge_w3(src_key)
    acct = get_account()
    wallet = Web3.to_checksum_address(acct.address)

    print(f"\nPRIME Bridge: {src_key} → {dst_key}")
    print(f"  Amount:    {amount} PRIME")
    print(f"  Source:    {src_cfg['prime_token'][:12]}... (LZ {src_cfg['lz_chain_id']})")
    print(f"  Contract:  {src_cfg['bridge_target'][:12]}...")
    print(f"  Dest:      {dst_cfg['prime_token'][:12]}... (LZ {dst_cfg['lz_chain_id']})")

    # Check balance
    erc20 = w3.eth.contract(address=Web3.to_checksum_address(src_cfg["prime_token"]),
        abi=json.loads(json.dumps([{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
            "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}])))
    bal = erc20.functions.balanceOf(wallet).call()
    print(f"  Balance:   {bal / 1e18:.2f} PRIME")

    # Estimate LZ fee
    try:
        native_fee, _ = _bridge_estimate_lz_fee(w3, src_cfg, dst_cfg["lz_chain_id"], amount_wei)
        print(f"  LZ fee:    {w3.from_wei(native_fee, 'ether')} {src_cfg['native']}")
    except Exception as e:
        print(f"  LZ fee:    estimation failed ({e}) — proceeding anyway")
        native_fee = 0

    if not execute:
        print(f"\n  Steps:")
        print(f"    1. approve bridge target ({src_cfg['bridge_target'][:12]}...) for {amount} PRIME")
        print(f"    2. sendFrom(…) via LZ to chain {dst_cfg['lz_chain_id']}")
        print(f"  Run with --execute to broadcast")
        return

    if bal < amount_wei:
        print(f"  ✗ NOT ENOUGH PRIME! Have {bal/1e18:.2f}, need {amount}")
        return

    bridge_target = Web3.to_checksum_address(src_cfg["bridge_target"])

    # 1. Approve
    allow_c = w3.eth.contract(address=Web3.to_checksum_address(src_cfg["prime_token"]),
        abi=json.loads(json.dumps([{"constant": True, "inputs": [
            {"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
            "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}])))
    current_allowance = allow_c.functions.allowance(wallet, bridge_target).call()
    if current_allowance < amount_wei:
        print(f"  Approving {amount} PRIME for bridge...")
        app_c = w3.eth.contract(address=Web3.to_checksum_address(src_cfg["prime_token"]),
            abi=json.loads(json.dumps([{"constant": False, "inputs": [
                {"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
                "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"}])))
        atx = app_c.functions.approve(bridge_target, amount_wei).build_transaction(
            {"from": wallet, "nonce": w3.eth.get_transaction_count(wallet),
             "gas": 100000, "chainId": src_chain_id})
        _set_gas_price_for(src_chain_id, w3, atx)
        signed = acct.sign_transaction(atx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        _ = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        time.sleep(2)
    else:
        print(f"  Allowance: {current_allowance / 1e18:.2f} PRIME")

    # 2. sendFrom
    refund = wallet
    zero = "0x0000000000000000000000000000000000000000"
    sendfrom_selector = "695ef6bf"  # sendFrom(address,uint16,bytes32,uint256,(address,address,bytes))
    call_params = (refund, zero, BRIDGE_ADAPTER_PARAMS)
    calldata_hex = sendfrom_selector + abi_encode(
        ["address", "uint16", "bytes32", "uint256", "(address,address,bytes)"],
        [wallet, dst_cfg["lz_chain_id"],
         bytes.fromhex(wallet.lower()[2:].rjust(64, "0")), amount_wei, call_params]).hex()

    print(f"  sendFrom() via LayerZero (LZ {src_cfg['lz_chain_id']} → {dst_cfg['lz_chain_id']})...")
    tx = {"from": wallet, "to": bridge_target, "data": bytes.fromhex(calldata_hex),
          "nonce": w3.eth.get_transaction_count(wallet), "gas": 500000,
          "value": native_fee, "chainId": src_chain_id}
    receipt = _sign_and_send(w3, acct, tx, "Bridge", timeout=300, fallback_gas=500000,
                         gas_price_fn=lambda _w, _tx: _set_gas_price_for(src_chain_id, _w, _tx))
    ok = receipt["status"] == 1

def main():
    check_version()
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    # Global wallet selector: --as <agent>, stripped before command dispatch.
    global _SELECTED_AGENT, _CLI_KEY, _OWNER_ADDRESS
    if "--as" in args:
        i = args.index("--as")
        if i + 1 >= len(args):
            print(f"--as requires an agent name. Known: {', '.join(AGENTS)}")
            return
        _SELECTED_AGENT = args[i + 1]
        del args[i:i + 2]
    # Global signing-key override: --key <0xhex>, stripped before command dispatch.
    if "--key" in args:
        i = args.index("--key")
        if i + 1 >= len(args):
            print("--key requires a hex key. Example: --key 0xabc...")
            return
        _CLI_KEY = args[i + 1]
        del args[i:i + 2]
    # Public owner-address selector for read-only commands. Lets monitoring/sim jobs
    # inspect a wallet's Prime Account / positions without resolving or loading a key.
    if "--owner" in args:
        i = args.index("--owner")
        if i + 1 >= len(args):
            print("--owner requires an EVM address. Example: --owner 0xabc...")
            return
        try:
            _OWNER_ADDRESS = Web3.to_checksum_address(args[i + 1])
        except Exception:
            print(f"Invalid --owner address: {args[i + 1]}")
            return
        del args[i:i + 2]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    cmd = args[0]
    if _OWNER_ADDRESS and cmd not in {"defi", "lb-positions", "equity"}:
        print("--owner is only supported for read-only commands: defi, lb-positions, equity")
        return
    if cmd == "pool-info":
        # First positional after `pool-info` is the pool name; --json is an opt-in flag
        # that switches output from human tables to a compact JSON shape (one object for
        # a named pool, dict-of-objects for `all`).
        as_json = "--json" in args
        positional = [a for a in args[1:] if not a.startswith("--")]
        pool = positional[0] if positional else "all"
        if pool != "all" and pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}, all")
            return
        cmd_pool_info(pool, as_json)
    elif cmd == "my-positions":
        cmd_my_positions()
    elif cmd == "deposit":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: arbprime deposit --pool usdc --amount 100 [--execute]")
            return
        cmd_deposit(pool, amount, execute)
    elif cmd == "withdraw":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: arbprime withdraw --pool usdc --amount 100 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_withdraw(pool, amount, execute)
    elif cmd == "withdrawal-requests":
        cmd_withdrawal_requests()
    elif cmd == "execute-withdrawal-request":
        pool, index = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--index" and i + 1 < len(args): index = int(args[i + 1])
        if not pool:
            print("Usage: arbprime execute-withdrawal-request --pool usdc [--index N] [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_execute_withdrawal_request(pool, index, execute)
    elif cmd == "cancel-withdrawal-request":
        pool, index = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--index" and i + 1 < len(args): index = int(args[i + 1])
        if not pool or index is None:
            print("Usage: arbprime cancel-withdrawal-request --pool usdc --index N [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_cancel_withdrawal_request(pool, index, execute)
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
    elif cmd == "equity":
        account_addr = None
        for i, a in enumerate(args):
            if a == "--account" and i + 1 < len(args):
                try:
                    account_addr = Web3.to_checksum_address(args[i + 1])
                except Exception:
                    print(f"Invalid --account address: {args[i + 1]}")
                    return
        cmd_equity("--json" in args, account_addr)
    elif cmd == "fund":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: arbprime fund --pool usdc --amount 100 [--execute]")
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
            print(f"Usage: arbprime {cmd} --pool usdc --amount 100 [--execute]")
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
            print("Usage: arbprime swap --from USDC --to ETH --amount 10 [--via yak|paraswap] [--slippage 0.5] [--execute]")
            return
        cmd_swap(from_sym, to_sym, amount, slippage, via, execute)
    elif cmd == "swap-debt":
        from_sym, to_sym, amount, slippage = None, None, None, 1.0
        execute = "--execute" in args
        fallback = "--fallback" in args
        for i, a in enumerate(args):
            if a == "--from" and i + 1 < len(args): from_sym = args[i + 1]
            if a == "--to" and i + 1 < len(args): to_sym = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
        if not from_sym or not to_sym or amount is None:
            print("Usage: arbprime swap-debt --from ETH --to USDC --amount 100 [--slippage 0.5] [--fallback] [--execute]")
            return
        cmd_swap_debt(from_sym, to_sym, amount, slippage, execute, fallback)
    elif cmd == "withdraw-collateral":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: arbprime withdraw-collateral --pool usdc --amount 100 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_withdraw_collateral(pool, amount, execute)
    elif cmd in ("cancel-withdrawal", "cancel-withdrawal-intent"):
        pool, index = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--index" and i + 1 < len(args): index = int(args[i + 1])
        if not pool or index is None:
            print("Usage: arbprime cancel-withdrawal --pool usdc --index 0 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_cancel_withdrawal(pool, index, execute)
    elif cmd == "withdrawal-intents":
        cmd_withdrawal_intents()
    elif cmd == "execute-withdrawal":
        pool, index = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--index" and i + 1 < len(args): index = int(args[i + 1])
        if not pool:
            print("Usage: arbprime execute-withdrawal --pool usdc [--index N] [--execute]")
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
            print("Usage: arbprime gmx-deposit --market eth-usdc --amount 500 "
                  "[--side auto|long|short] [--slippage 1] [--fee-buffer 2] [--execute]")
            print(f"  markets: {', '.join(GMX_MARKETS)}")
            return
        if side not in ("long", "short", "auto"):
            print("--side must be 'long', 'short', or 'auto'.")
            return
        is_long = None if side == "auto" else (side == "long")
        cmd_gmx_deposit(market, amount, is_long, slippage, fee_buffer, execute)
    elif cmd == "gmx-withdraw":
        market, amount, slippage, fee_buffer = None, None, 1.0, 2.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--market" and i + 1 < len(args): market = args[i + 1].lower()
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--fee-buffer" and i + 1 < len(args): fee_buffer = float(args[i + 1])
        if not market or amount is None:
            print("Usage: arbprime gmx-withdraw --market eth-usdc --amount 5 "
                  "[--slippage 1] [--fee-buffer 2] [--execute]")
            print(f"  markets: {', '.join(GMX_MARKETS)}")
            return
        cmd_gmx_withdraw(market, amount, slippage, fee_buffer, execute)
    elif cmd == "glv-positions":
        cmd_glv_positions()
    elif cmd == "glv-deposit":
        vault, amount, side, target, slippage, fee_buffer = None, None, "long", None, 1.0, 2.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--vault" and i + 1 < len(args): vault = args[i + 1].lower()
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--side" and i + 1 < len(args): side = args[i + 1].lower()
            if a == "--target-market" and i + 1 < len(args): target = args[i + 1]
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--fee-buffer" and i + 1 < len(args): fee_buffer = float(args[i + 1])
        if not vault or amount is None:
            print("Usage: arbprime glv-deposit --vault weth-usdc --amount 500 "
                  "[--side auto|long|short] [--target-market GM_ETH_WETH_USDC] [--slippage 1] [--fee-buffer 2] [--execute]")
            print(f"  vaults: {', '.join(GLV_VAULTS)}")
            return
        if side not in ("long", "short", "auto"):
            print("--side must be 'long', 'short', or 'auto'.")
            return
        is_long = None if side == "auto" else (side == "long")
        cmd_glv_deposit(vault, amount, is_long, target, slippage, fee_buffer, execute)
    elif cmd == "glv-withdraw":
        vault, amount, target, slippage, fee_buffer = None, None, None, 1.0, 2.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--vault" and i + 1 < len(args): vault = args[i + 1].lower()
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--target-market" and i + 1 < len(args): target = args[i + 1]
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--fee-buffer" and i + 1 < len(args): fee_buffer = float(args[i + 1])
        if not vault or amount is None:
            print("Usage: arbprime glv-withdraw --vault weth-usdc --amount 5 "
                  "[--target-market GM_ETH_WETH_USDC] [--slippage 1] [--fee-buffer 2] [--execute]")
            print(f"  vaults: {', '.join(GLV_VAULTS)}")
            return
        cmd_glv_withdraw(vault, amount, target, slippage, fee_buffer, execute)
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
        # LB is stubbed on Arbitrum (TJ_LB_PAIRS empty); cmd_lb_add prints the TODO notice.
        if amount_x <= 0 and amount_y <= 0 and TJ_LB_PAIRS:
            print("Usage: arbprime lb-add --pair PAIR --amount-x N --amount-y M "
                  "[--shape spot|curve|bidask] [--range 5] [--slippage 1] [--id-slippage 5] [--execute]")
            print(f"  pairs: {', '.join(TJ_LB_PAIRS)}")
            return
        cmd_lb_add(pair or "", amount_x, amount_y, shape, rng, slippage, id_slip, execute)
    elif cmd == "lb-remove":
        pair, slippage = None, 1.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pair" and i + 1 < len(args): pair = args[i + 1].lower()
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
        cmd_lb_remove(pair or "", slippage, execute)
    elif cmd == "prime-tier":
        cmd_prime_tier()
    elif cmd == "prime-needed":
        borrow, tier = None, "premium"
        for i, a in enumerate(args):
            if a == "--borrow" and i + 1 < len(args): borrow = float(args[i + 1])
            if a == "--tier" and i + 1 < len(args): tier = args[i + 1].lower()
        if borrow is None:
            print("Usage: arbprime prime-needed --borrow 1000 [--tier premium|basic]")
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
            print("Usage: arbprime prime-deposit --amount 200 [--execute]")
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
            print(f"Usage: arbprime {cmd} --amount 100 [--execute]")
            return
        (cmd_prime_unstake if cmd == "prime-unstake" else cmd_prime_repay)(amount, execute)
    elif cmd == "zap":
        market, collateral, side = None, None, "auto"
        collateral_amount, borrow_amount, deposit_amount = None, None, 0
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
                or borrow_amount is None:
            print("Usage: arbprime zap --market eth-usdc --collateral usdc "
                  "--collateral-amount 100 --borrow-amount 400 "
                  "[--side auto|long|short] [--deposit-amount N] [--swap] "
                  "[--slippage 1] [--fee-buffer 2] [--execute]")
            print(f"  GM markets: {', '.join(k for k, m in GMX_MARKETS.items() if not m['plus'])}")
            print("  Optimal leveraged zap: fund -> borrow -> [optional optimal swap] -> GMX deposit.")
            print("  --side auto (default): picks short (USDC) — zero swap, works for every market.")
            print("  --deposit-amount: optional (auto-calculated from position value if omitted).")
            print("  --swap: force swap USDC->long token (only needed for --side long).")
            return
        cmd_zap(market, collateral, collateral_amount, borrow_amount, deposit_amount,
                side, swap_to_long, slippage, fee_buffer, execute)
    elif cmd == "prime-bridge":
        from_chain = "avax"
        amount = None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--from" and i + 1 < len(args): from_chain = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        cmd_prime_bridge(from_chain, amount, execute)
    elif cmd == "health":
        os.environ.setdefault("PRIMECLI_TOOL", sys.argv[0])
        health_monitor.cli()
    else:
        print(f"Unknown command: {cmd}\n{__doc__}")

if __name__ == "__main__":
    main()
