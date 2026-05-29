#!/usr/bin/env python3
"""DegenPrime Protocol interaction module (Base, chain 8453).

Sister protocol to DeltaPrime on Avalanche, by the same team (DeltaPrimeLabs),
sharing the EIP-2535 Diamond + per-user Smart Loan architecture. Lending pools take
direct EOA deposits/withdrawals. Borrowing and leverage go through a Degen Account:
a per-user SmartLoan (diamond proxy) created via the SmartLoansFactory. The EOA owns
it; borrow/repay/fund run on the Degen Account, which itself talks to the pools.

Usage:
  degenprime pool-info [usdc|weth|cbbtc|aero|brett|kaito|cbdoge|cbxrp|all]
  degenprime my-positions
  degenprime deposit --pool usdc --amount 100 [--execute]
  degenprime withdraw --pool usdc --amount 100 [--execute]
  degenprime create-account [--execute]
  degenprime create-account --fund-pool usdc --fund-amount 100 [--execute]
  degenprime summary
  degenprime fund --pool usdc --amount 100 [--execute]
  degenprime borrow --pool usdc --amount 100 [--execute]
  degenprime repay --pool usdc --amount 100 [--execute]
  degenprime swap --from USDC --to ETH --amount 10 [--slippage 0.5] [--execute]
  degenprime swap-debt --from ETH --to USDC --amount 100 [--slippage 0.5] [--execute]
  degenprime withdraw-collateral --pool usdc --amount 100 [--execute]
  degenprime withdrawal-intents
  degenprime execute-withdrawal --pool usdc [--index N] [--execute]
  degenprime cancel-withdrawal --pool usdc --index N [--execute]
  degenprime aerodrome-positions

Configuration (env vars):
  DEGENPRIME_PRIVATE_KEY  Raw 0x... private key for the signer. Falls back to
                          DELTAPRIME_PRIVATE_KEY if unset (same EVM key works on
                          both chains).
  DEGENPRIME_KEY_FILE     Path to a file containing the 0x key (alternative to
                          the env var). Falls back to DELTAPRIME_KEY_FILE.
  DEGENPRIME_RPC          Base RPC URL (defaults to base.publicnode.com).
  --key <0xhex>           One-off CLI override (takes precedence over env vars).

summary reports live solvency (health ratio, total value, debt, solvent flag) from the
Diamond's SolvencyFacet, read via eth_call with a RedStone price payload appended (falls
back to balances-only if the gateway is unreachable).

Collateral withdrawal is universally time-locked on DegenPrime (NOT just risky assets):
withdraw-collateral registers a WithdrawalIntent (createWithdrawalIntent, no RedStone).
The intent becomes executable ~24h later for a 48h window (24h-72h total);
execute-withdrawal then pulls it to the wallet (executeWithdrawalIntent, RedStone-gated).
withdrawal-intents lists pending intents + per-asset available balance (oracle-free reads).
cancel-withdrawal cancels a pending intent before maturity.

Leverage flow: create-account -> fund (collateral) -> borrow -> repay -> withdraw.
fund moves collateral from the wallet into the Degen Account; borrow needs a funded
account. ERC20 assets approve the account then call fund(); native ETH (weth pool)
uses the payable depositNativeToken(). create-account --fund-* does both in one tx
via createAndFundLoan() (ERC20 only).

swap trades one in-account asset for another on the Degen Account via ParaSwap v6:
the ParaSwap API on Base (network=8453) builds the swap calldata (/prices price route
-> /transactions tx data). The facet takes paraSwapV6(bytes4 selector, bytes data) -
we split the API calldata into its 4-byte selector + remaining bytes and pass them
through. Only the two router methods the facet decodes are accepted: swapExactAmountIn
(0xe3ead59e) and swapExactAmountInOnUniswapV3 (0x876a02f6). The facet enforces a hard
5% slippage cap (RedStone-priced) regardless of --slippage. Carries remainsSolvent, so
--execute appends a RedStone signed-price payload to the calldata. Asset names are the
bytes32 symbols, not the wrapped-token names (e.g. ETH not WETH).

swap-debt refinances debt from one asset into another in a single tx via
swapDebtParaSwap(_fromAsset, _toAsset, _repayAmount, _borrowAmount, selector, data):
it borrows --amount of the NEW debt asset (--to), ParaSwaps it into the OLD debt
asset (--from), and repays the old debt. --from is the existing debt being
refinanced; --to is the new debt taken on. RedStone-gated on execute.

aerodrome-positions is read-only: lists the Aerodrome NFT tokenIds the Degen Account
owns/has staked via the diamond's getOwnedStakedAerodromeTokenIds view. Write paths
(add/remove/stake liquidity) are deferred to v2 - signatures vary by Aerodrome version
and need on-chain probing per market.
"""

import json, os, sys, time, base64
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import requests
from eth_account import Account
from eth_keys import keys as eth_keys
from eth_abi import decode as abi_decode
from web3 import Web3

# Default Base RPC. mainnet.base.org rate-limits hard (429 within a few calls); the
# publicnode endpoint is fronted by a load balancer with much higher anonymous limits
# and has been the most reliable free option for this tool's traffic pattern (lots of
# small reads in quick succession for `pool-info all` / `my-positions` / `summary`).
# Override with the DEGENPRIME_RPC env var for paid Alchemy/QuickNode/Infura endpoints.
BASE_RPC = os.environ.get("DEGENPRIME_RPC", "https://base.publicnode.com")
EXPLORER = "https://basescan.org"
CHAIN_ID = 8453
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# ── Signing key resolution ──────────────────────────────────────────────────
# The Degen Account is derived on-chain from the wallet owner (getLoansForOwner),
# so each user automatically operates on their own Degen Account — no per-user
# addresses are hardcoded.
#
# Key resolution order (first hit wins; see resolve_private_key):
#   1. --key <0xhex> CLI flag         -> raw 0x... key (one-off escape hatch)
#   2. DEGENPRIME_PRIVATE_KEY env var -> raw 0x... key (primary path)
#   3. DEGENPRIME_KEY_FILE env var    -> path to a file containing the 0x key
#   4. DELTAPRIME_PRIVATE_KEY / DELTAPRIME_KEY_FILE — fallback, since the same
#      EVM key works on both chains.
#
# The CLI key (#1) is set by main() before the command runs; the env vars are
# read lazily so read-only commands that don't sign never need a key at all.
_CLI_KEY = None  # set by the --key CLI flag in main()

# Core protocol addresses (verified on Base 2026-05-29).
FACTORY_PROXY = "0x5A6a0e2702cF4603a098C3Df01f3F0DF56115456"  # SmartLoansFactory TUP
# Diamond beacon. Every Degen Account is a per-user proxy that delegates here, so the
# facet ABIs (borrow/repay/fund + view fns) are reachable at any deployed account.
SMART_LOAN_DIAMOND = "0x85c2BAA28C1d7A07bFC5C5c9903FFf4c39ae5151"
# On-chain registry of active pools + collateral assets. getPoolAddress(bytes32 asset)
# and tokenAddressToSymbol(address) are the source of truth for the 8 pools + 32
# supported collateral tokens.
TOKEN_MANAGER = "0x97e74e0A3D2713D87E3fBf6d18F869042F0d0116"
# Base native wrapped (used by the weth pool's native ETH path).
WETH = "0x4200000000000000000000000000000000000006"

# ParaSwap v6 / Velora aggregator on Base. The Degen Account's ParaSwapFacet.paraSwapV6
# and SwapDebtFacet.swapDebtParaSwap call this Augustus router with API-built calldata.
# The router address is shared across chains (v6 unified). The facet only decodes two
# router methods, so the API route must resolve to one of these selectors:
#   swapExactAmountIn          0xe3ead59e  (generic executor route)
#   swapExactAmountInOnUniV3   0x876a02f6  (Uniswap-V3 direct route)
PARASWAP_API = "https://apiv5.paraswap.io"
PARASWAP_AUGUSTUS = "0x6A000F20005980200259B80c5102003040001068"
PARASWAP_SUPPORTED_SELECTORS = {"0xe3ead59e", "0x876a02f6"}
# Executors the facet whitelists. Lowercased. Starting set mirrors DeltaPrime's - the
# v6 router is shared, so the same executors are plausible candidates. Real txs will
# reveal missing ones with InvalidExecutor reverts; add as they show up.
PARASWAP_EXECUTORS = {
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57",
    "0x6a000f20005980200259b80c5102003040001068",
    "0x000010036c0190e009a000d0fc3541100a07380a",
    "0x00c600b30fb0400701010f4b080409018b9006e0",
    "0xa0f408a000017007015e0f00320e470d00090a5b",
}

# RedStone on-demand oracle config for DegenPrime on Base. Verified identical to
# DeltaPrime - same data service, same 5 authorised signers, same 3-of-5 threshold,
# same marker bytes, same gateways. The Degen Account's solvency math reads signed
# prices appended to the tx calldata, same wrapping as on Avalanche.
REDSTONE_DATA_SERVICE = "redstone-primary-prod"
REDSTONE_SIGNERS_THRESHOLD = 3
REDSTONE_MARKER = bytes.fromhex("000002ed57011e0000")
REDSTONE_GATEWAYS = [
    "https://oracle-gateway-1.a.redstone.finance",
    "https://oracle-gateway-2.a.redstone.finance",
]
REDSTONE_VALUE_DECIMALS = 8
# Stored lower-case because _redstone_package_signer returns checksummed addresses and
# the filter compares signer.lower() in this set.
REDSTONE_AUTHORISED_SIGNERS = {
    "0x8bb8f32df04c8b654987daaed53d6b6091e3b774",
    "0xdeb22f54738d54976c4c0fe5ce6d408e40d88499",
    "0x51ce04be4b3e32572c4ec9135221d0691ba7d202",
    "0xdd682daec5a90dd295d14da4b0bec9281017b5be",
    "0x9c5ae89c4af6aa32ce58588dbaf90d18a855b6de",
}

# Active lending pools on Base, verified live (totalSupply > 0, all wired in
# TokenManager 2026-05-29). The proxy is the user-facing Pool TUP; token is the
# underlying ERC20; symbol is the on-chain bytes32 the Diamond uses; native flags
# the pool whose underlying is wrapped ETH (uses depositNativeToken() for the
# fund() path on the Degen Account side).
POOLS = {
    "usdc":   {"proxy": "0x2Fc7641F6A569d0e678C473B95C2Fc56A88aDF75", "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "symbol": "USDC",   "decimals": 6,  "native": False},
    "weth":   {"proxy": "0x81b0b59C7967479EC5Ce55cF6588bf314C3E4852", "token": "0x4200000000000000000000000000000000000006", "symbol": "ETH",    "decimals": 18, "native": True},
    "cbbtc":  {"proxy": "0xCA8C954073054551B99EDee4e1F20c3d08778329", "token": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "symbol": "cbBTC",  "decimals": 8,  "native": False},
    "aero":   {"proxy": "0x4524D39Ca5b32527E7AF6c288Ad3E2871B9f343B", "token": "0x940181a94A35A4569E4529A3CDfB74e38FD98631", "symbol": "AERO",   "decimals": 18, "native": False},
    "brett":  {"proxy": "0x6c307F792FfDA3f63D467416C9AEdfeE2DD27ECF", "token": "0x532f27101965dd16442E59d40670FaF5eBB142E4", "symbol": "BRETT",  "decimals": 18, "native": False},
    "kaito":  {"proxy": "0x293E41F1405Dde427B41c0074dee0aC55D064825", "token": "0x98d0baa52b2D063E780DE12F615f963Fe8537553", "symbol": "KAITO",  "decimals": 18, "native": False},
    "cbdoge": {"proxy": "0xAf61B10BDB78e31fdbC5Da4e57d60e32aFe468B9", "token": "0xcbD06E5A2B0C65597161de254AA074E489dEb510", "symbol": "cbDOGE", "decimals": 8,  "native": False},
    "cbxrp":  {"proxy": "0x056076e717332403Bc23B2D4F6D87683ceF582B9", "token": "0xcb585250f852C6c6bf90434AB21A00f02833a4af", "symbol": "cbXRP",  "decimals": 6,  "native": False},
}

# RedStone primary-prod feed availability on Base. The SolvencyFacet sources missing
# symbols from BaseOracle TWAP internally, so we filter the payload to only the symbols
# the gateway actually has feeds for. Probed against the gateway 2026-05-29.
REDSTONE_AVAILABLE_FEEDS = {
    "USDC", "ETH", "cbBTC", "AERO", "BRETT", "KAITO", "DEGEN", "MOG",
    "weETH", "EUROC", "USDT", "LBTC", "ezETH",
}

_abi_cache = {}
_impl_cache = {}
# Cache for TokenManager symbol/decimals lookups (the in-account view shows non-pool
# collateral too; symbol+decimals reads are pure but cheap to memoise).
_asset_meta_cache = {}

def get_w3():
    """Base RPC client. Base has no POA middleware - it's a standard EVM chain;
    middleware injection is not needed (and would error on the Base block headers)."""
    return Web3(Web3.HTTPProvider(BASE_RPC))

def _tx_gas_price(w3) -> int:
    """Gas price for broadcasts: 2x the current network price with a 1 gwei floor.
    Base's base fee is ~0.001 gwei, so an unfloored tx can strand if the base fee
    ticks up after submission. The bump guarantees timely inclusion and gives
    headroom to REPLACE a stranded same-nonce tx. Cost is negligible on Base."""
    return max(int(w3.eth.gas_price * 2), 10**9)

def resolve_private_key():
    """Resolve the signing key per the documented precedence:
       1. --key <0xhex> CLI flag
       2. DEGENPRIME_PRIVATE_KEY env var
       3. DEGENPRIME_KEY_FILE env var (path to a file containing the 0x key)
       4. DELTAPRIME_PRIVATE_KEY / DELTAPRIME_KEY_FILE (same key, both chains)
    Raises with a clear message if none of the four are set."""
    if _CLI_KEY:
        return _CLI_KEY.strip()
    for env_var in ("DEGENPRIME_PRIVATE_KEY", "DELTAPRIME_PRIVATE_KEY"):
        raw = os.environ.get(env_var)
        if raw:
            return raw.strip()
    for path_var in ("DEGENPRIME_KEY_FILE", "DELTAPRIME_KEY_FILE"):
        key_file = os.environ.get(path_var)
        if key_file:
            try:
                return Path(key_file).read_text().strip()
            except FileNotFoundError:
                raise RuntimeError(f"{path_var} points at {key_file} but the file does not exist.")
    raise RuntimeError(
        "No signing key found. Set DEGENPRIME_PRIVATE_KEY (raw 0x... key) or "
        "DEGENPRIME_KEY_FILE (path to a file with the key), or pass --key <0xhex>. "
        "DELTAPRIME_PRIVATE_KEY / DELTAPRIME_KEY_FILE also work (same key, both chains)."
    )

def get_account() -> Account:
    return Account.from_key(resolve_private_key())

# Basescan's v1 API is deprecated (returns "switch to Etherscan API V2" since 2026),
# and the v2 multichain endpoint (api.etherscan.io/v2/api?chainid=8453) requires an API
# key with no anonymous reads. Rather than depend on an API key for what is a tiny,
# stable ABI surface, we hand-curate the Pool + Factory ABIs below and resolve proxy
# implementations directly via the EIP-1967 storage slot.
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

# Pool ABI - minimum surface for lending pool ops. DegenPrime pools share the
# DeltaPrime Pool implementation: deposit/withdraw/instantWithdraw, rate views,
# borrow accounting, and ERC20 receipt-token reads. Function names are verified
# against Harvest's DegenPrime strategies (which call these directly) and the
# DeltaPrimeLabs/deltaprime-contracts Pool.sol source.
POOL_ABI = json.loads(
    '['
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[],"name":"depositNativeToken","outputs":[],"stateMutability":"payable","type":"function"},'
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"withdrawNativeToken","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"instantWithdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[],"name":"totalSupply","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"totalBorrowed","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"getDepositRate","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"getBorrowingRate","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"tokenAddress","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"getBorrowed","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}'
    ']'
)

# SmartLoansFactory ABI - minimum surface. Same shape as DeltaPrime's SmartLoansFactory.
FACTORY_ABI = json.loads(
    '['
    '{"inputs":[],"name":"createLoan","outputs":[{"type":"address"}],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_fundedAsset","type":"bytes32"},{"name":"_amount","type":"uint256"}],"name":"createAndFundLoan","outputs":[{"type":"address"}],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"_user","type":"address"}],"name":"getLoansForOwner","outputs":[{"type":"address[]"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"_loan","type":"address"}],"name":"getOwnerOfLoan","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"getLoansLength","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"_from","type":"uint256"},{"name":"_count","type":"uint256"}],"name":"getLoans","outputs":[{"type":"address[]"}],"stateMutability":"view","type":"function"}'
    ']'
)

def get_pool_impl(pool_proxy: str) -> str:
    """Resolve a TUP's implementation via the EIP-1967 storage slot. Caches per-proxy."""
    p = pool_proxy.lower()
    if p not in _impl_cache:
        w3 = get_w3()
        raw = w3.eth.get_storage_at(Web3.to_checksum_address(pool_proxy), int(EIP1967_IMPL_SLOT, 16))
        impl = "0x" + raw.hex()[-40:]
        _impl_cache[p] = impl if int(impl, 16) != 0 else pool_proxy
    return _impl_cache[p]

def get_pool_contract(pool_name: str):
    cfg = POOLS[pool_name]
    proxy = Web3.to_checksum_address(cfg["proxy"])
    w3 = get_w3()
    return w3.eth.contract(address=proxy, abi=POOL_ABI), cfg, w3

# Minimal Degen Account ABI: only the facet functions this tool calls. The diamond
# beacon's own ABI is beacon-management only, so the borrow/repay/fund and view
# selectors live in facets - we hand-pick the verified signatures here rather than
# enumerate facet contracts at runtime. Selectors probed against a live Degen Account
# 2026-05-29 (every entry below returned EXISTS_REVERT/REVERT, never MISSING).
PRIME_ACCOUNT_ABI = [
    # Core: AssetsOperations + SmartLoanWrappedNativeToken facets
    {"inputs": [{"name": "_asset", "type": "bytes32"}, {"name": "_amount", "type": "uint256"}],
     "name": "borrow", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}, {"name": "_amount", "type": "uint256"}],
     "name": "repay", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "_fundedAsset", "type": "bytes32"}, {"name": "_amount", "type": "uint256"}],
     "name": "fund", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "depositNativeToken", "outputs": [],
     "stateMutability": "payable", "type": "function"},
    # SmartLoanView facet
    {"inputs": [], "name": "getAllOwnedAssets", "outputs": [{"type": "bytes32[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_asset", "type": "bytes32"}], "name": "getBalance",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getDebts",
     "outputs": [{"components": [{"name": "name", "type": "bytes32"}, {"name": "debt", "type": "uint256"}], "type": "tuple[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getHealthMeter", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "owner", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    # ParaSwapFacet + SwapDebtFacet - both RedStone-gated (remainsSolvent). selector+data
    # are the ParaSwap Augustus calldata, split into its 4-byte method selector and the
    # remaining ABI-encoded args.
    {"inputs": [{"name": "selector", "type": "bytes4"}, {"name": "data", "type": "bytes"}],
     "name": "paraSwapV6", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_fromAsset", "type": "bytes32"}, {"name": "_toAsset", "type": "bytes32"},
                {"name": "_repayAmount", "type": "uint256"}, {"name": "_borrowAmount", "type": "uint256"},
                {"name": "selector", "type": "bytes4"}, {"name": "data", "type": "bytes"}],
     "name": "swapDebtParaSwap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    # SolvencyFacet views - RedStone-gated. getTotalValue/getDebt are 1e18-scaled USD;
    # getHealthRatio is 1e18-scaled (1e18 == liquidation line, so the human ratio is the
    # raw value / 1e18). All revert with 0xe7764c9e on a bare eth_call - a signed
    # RedStone price payload must be appended to the calldata.
    {"inputs": [], "name": "getHealthRatio", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getTotalValue", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getDebt", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "isSolvent", "outputs": [{"type": "bool"}],
     "stateMutability": "view", "type": "function"},
    # getPrices: 1e8-scaled USD prices for the given symbols. RedStone-gated, so a
    # payload is appended for the read. swap-debt uses it to value-match the borrow vs
    # repay leg against the facet's own 5% cap (the facet calls the same view internally).
    {"inputs": [{"name": "symbols", "type": "bytes32[]"}], "name": "getPrices",
     "outputs": [{"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
    # WithdrawalIntentFacet - universal time-locked collateral withdrawal on DegenPrime.
    # createWithdrawalIntent / cancelWithdrawalIntent are oracle-free; executeWithdrawalIntent
    # is RedStone-gated. IntentInfo's isActionable/isExpired flags make the 24h-72h window
    # readable on-chain.
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
    # Aerodrome read-only - the diamond exposes a list view of owned staked Aerodrome
    # tokenIds. Write paths (add/remove/stake liquidity) deferred to v2; the exact
    # composition view signature varies and needs runtime probing.
    {"inputs": [], "name": "getOwnedStakedAerodromeTokenIds",
     "outputs": [{"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
]

# TokenManager ABI - minimal subset for symbol/decimals lookups + supported tokens
# enumeration. tokenAddressToSymbol returns the bytes32 the diamond uses for the
# in-account view; getSupportedTokensAddresses lists all 32 collateral assets.
TOKEN_MANAGER_ABI = [
    {"inputs": [{"name": "_asset", "type": "bytes32"}], "name": "getPoolAddress",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_address", "type": "address"}], "name": "tokenAddressToSymbol",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_symbol", "type": "bytes32"}], "name": "getAssetAddress",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getSupportedTokensAddresses",
     "outputs": [{"type": "address[]"}], "stateMutability": "view", "type": "function"},
]

# Minimal ERC20 ABI - balanceOf + approve + decimals. Used for wallet token reads,
# pool deposits, and TokenManager-discovered non-pool collateral metadata.
ERC20_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},'
    '{"constant":true,"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"}]'
)

def get_factory_contract(w3):
    """SmartLoansFactory - hand-curated ABI (same shape as DeltaPrime's factory)."""
    return w3.eth.contract(address=Web3.to_checksum_address(FACTORY_PROXY), abi=FACTORY_ABI)

def get_token_manager(w3):
    return w3.eth.contract(address=Web3.to_checksum_address(TOKEN_MANAGER), abi=TOKEN_MANAGER_ABI)

def get_prime_account(w3, owner: str) -> str:
    """Owner -> Degen Account address. Returns None if none exists.

    DegenPrime's factory exposes `getLoansForOwner(address) returns address[]` (plural,
    array - different from DeltaPrime's singular `getLoanForOwner` that returns one
    address). Empty array means the owner hasn't minted a Degen Account; we collapse
    that to None. In practice the factory only mints ONE loan per owner (createLoan
    checks ownersToLoans first), so we return the first element when present."""
    loans = get_factory_contract(w3).functions.getLoansForOwner(Web3.to_checksum_address(owner)).call()
    return loans[0] if loans else None

def asset_b32(symbol: str) -> bytes:
    return symbol.encode().ljust(32, b"\x00")

def pool_to_asset_symbol(pool_name: str) -> str:
    """Pool key -> on-chain bytes32 asset symbol (the contracts use 'ETH', not 'WETH')."""
    return POOLS[pool_name]["symbol"]

def token_price(symbol: str) -> float:
    """Spot price from KuCoin, best-effort. Returns 0.0 on any failure; callers treat 0
    as 'no price available' and skip the USD display."""
    try:
        r = requests.get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}-USDT", timeout=3)
        if r.status_code == 200 and r.json().get("code") == "200000":
            return float(r.json()["data"]["price"])
    except Exception:
        pass
    return 0.0

def _asset_meta(w3, symbol: str):
    """Resolve a bytes32 symbol to (token_address, decimals) via the TokenManager.
    Used for in-account assets that aren't lending pool symbols (memecoin collateral
    like AIXBT, TOSHI, etc.). Cached - reads are pure but the TokenManager call is one
    eth_call + an ERC20.decimals() per asset."""
    if symbol in _asset_meta_cache:
        return _asset_meta_cache[symbol]
    # Pool symbols hit the static map - no on-chain read needed.
    for cfg in POOLS.values():
        if cfg["symbol"] == symbol:
            _asset_meta_cache[symbol] = (cfg["token"], cfg["decimals"])
            return _asset_meta_cache[symbol]
    try:
        tm = get_token_manager(w3)
        addr = tm.functions.getAssetAddress(asset_b32(symbol)).call()
        if int(addr, 16) == 0:
            _asset_meta_cache[symbol] = (None, 18)
            return _asset_meta_cache[symbol]
        tok = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
        dec = tok.functions.decimals().call()
        _asset_meta_cache[symbol] = (addr, dec)
        return _asset_meta_cache[symbol]
    except Exception:
        _asset_meta_cache[symbol] = (None, 18)
        return _asset_meta_cache[symbol]

# ─── RedStone on-demand price wrapping ───────────────────────────────────────
# DegenPrime uses RedStone's on-demand model identically to DeltaPrime: signed price
# packages are fetched off-chain and APPENDED to the function calldata (after the
# normal ABI-encoded args). The solvency math parses them from the calldata tail,
# verifies the signatures, and aggregates by median. Without the payload these calls
# revert with 0xe7764c9e.
#
# Payload layout (matches @redstone-finance/evm-connector). Each signed data package:
#     for each data point: symbol(bytes32) ++ value(uint256, scaled 1e8, big-endian)
#     trailer: timestamp_ms(6) ++ dataPointValueByteSize(4)=32 ++ dataPointsCount(3)
#     signature(65): r ++ s ++ v
# After all packages: dataPackagesCount(2) ++ unsignedMetadataSize(3)=0 ++ marker(9).
#
# The value MUST be reconstructed exactly as RedStone signed it:
# parseUnits(Number(value).toFixed(8), 8). Decimal(float(value)).quantize(1e-8,
# ROUND_HALF_UP) reproduces toFixed(8) byte-for-byte; the old int(round(value*1e8))
# path double-rounds and produces a body the contract ecrecovers wrong -> garbage
# signer -> SignerNotAuthorised. Verified on DeltaPrime; we keep the same fix here.

# Per-run cache for the RedStone gateway response - so a single summary call hits the
# gateway once instead of per-feed-symbol. Cleared implicitly when the process exits.
_redstone_gateway_cache = None

def _redstone_fetch_packages(use_cache: bool = True) -> dict:
    """Fetch the latest signed price packages from the RedStone gateway. Returns the
    per-feed map: {feedSymbol: [package, ...]} with one package per signer. The per-run
    cache lets repeat callers reuse the same snapshot - important for summary, where the
    same payload backs getTotalValue / getDebt / getHealthRatio / isSolvent / getPrices."""
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
    .toFixed(8), 8). See module docstring for why this is the only correct path."""
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
    returning extra/standby signers and surfaces a clear error rather than an on-chain
    revert."""
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
    payload += len(packages).to_bytes(2, "big")
    payload += (0).to_bytes(3, "big")
    payload += REDSTONE_MARKER
    return payload

def degen_account_price_feeds(account) -> list:
    """RedStone feed symbols a solvency check on this account needs: ETH (Base's native
    base-asset reference, the BaseOracle anchor), every owned asset that has a RedStone
    feed, and every debt-registry asset that has one. Symbols without a RedStone feed
    are skipped - the SolvencyFacet sources them on-chain from BaseOracle TWAP and the
    payload doesn't need to cover them. Deduped, ETH first."""
    feeds = ["ETH"]
    for a in account.functions.getAllOwnedAssets().call():
        sym = a.rstrip(b"\x00").decode(errors="replace")
        if sym and sym in REDSTONE_AVAILABLE_FEEDS and sym not in feeds:
            feeds.append(sym)
    for name, _debt in account.functions.getDebts().call():
        sym = name.rstrip(b"\x00").decode(errors="replace")
        if sym and sym in REDSTONE_AVAILABLE_FEEDS and sym not in feeds:
            feeds.append(sym)
    return feeds

def redstone_view_call(w3, account, fn_name: str, payload: bytes, args: list = None):
    """Read-only call of a RedStone-gated view on the Degen Account. The signed price
    payload is appended to the ABI-encoded calldata (same wrapping as a write tx), then
    eth_call'd and the result decoded against the function's ABI. Used for the solvency
    views (getHealthRatio/getTotalValue/getDebt/isSolvent, no args), which revert with
    0xe7764c9e on a bare call. `payload` is reused across calls so the gateway is hit
    once."""
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
    # getDepositRate / getBorrowingRate are 1e18-scaled annualised rates.
    try:
        dr = contract.functions.getDepositRate().call() / 1e18 * 100
        br = contract.functions.getBorrowingRate().call() / 1e18 * 100
        print(f"  Deposit APR:    {dr:>14.2f}%")
        print(f"  Borrow APR:     {br:>14.2f}%")
    except Exception:
        pass
    # KuCoin doesn't trade cbBTC/cbDOGE/cbXRP directly - the cb-prefixed variants are
    # Coinbase wrapped versions; fall back to the underlying ticker for the price probe.
    price_sym = cfg["symbol"].replace("cb", "") if cfg["symbol"].startswith("cb") else cfg["symbol"]
    price = token_price(price_sym)
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

    # Wallet ETH (native Base asset, used for gas).
    eth = w3.eth.get_balance(acct.address) / 1e18
    print(f"ETH: {eth:.6f}")

    for name, cfg in POOLS.items():
        try:
            contract, _, _ = get_pool_contract(name)
            token_addr = Web3.to_checksum_address(cfg["token"])
            token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
            bal = token.functions.balanceOf(acct.address).call()
            if bal > 0:
                print(f"  Wallet {cfg['symbol']}: {bal / 10**cfg['decimals']:.4f}")

            pool_bal = contract.functions.balanceOf(acct.address).call()
            if pool_bal > 0:
                print(f"  Pool Deposit {cfg['symbol']}: {pool_bal / 10**cfg['decimals']:.4f}")

            borrowed = contract.functions.getBorrowed(acct.address).call()
            if borrowed > 0:
                print(f"  Borrowed {cfg['symbol']}: {borrowed / 10**cfg['decimals']:.4f}")

        except Exception as e:
            print(f"  {name}: {e}")

    try:
        pa = get_prime_account(w3, acct.address)
        if pa:
            print(f"\nDegen Account: {pa}")
            pa_eth = w3.eth.get_balance(Web3.to_checksum_address(pa)) / 1e18
            print(f"  ETH balance: {pa_eth:.6f}")
        else:
            print("\nNo Degen Account yet. Create with: degenprime create-account --execute")
    except Exception as e:
        print(f"\nDegen Account lookup failed: {e}")

def cmd_deposit(pool_name: str, amount: float, execute: bool = False):
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    amount_wei = int(amount * 10**cfg["decimals"])
    print(f"Wallet: {acct.address}")

    if not execute:
        print(f"Preview: Deposit {amount} {cfg['symbol']} into {pool_name.upper()} pool")
        print("Run with --execute to broadcast")
        return

    if cfg["native"]:
        # Native ETH path: pool.deposit(amount) with msg.value == amount (the pool wraps
        # ETH -> WETH internally). Same pattern as DeltaPrime's wavax pool.
        tx = contract.functions.deposit(amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 300000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID, "value": amount_wei,
        })
        signed = acct.sign_transaction(tx)
    else:
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]), abi=ERC20_ABI)
        app_tx = token.functions.approve(Web3.to_checksum_address(cfg["proxy"]), amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
        signed_app = acct.sign_transaction(app_tx)
        w3.eth.send_raw_transaction(signed_app.raw_transaction)

        dep_tx = contract.functions.deposit(amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 300000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
        })
        signed = acct.sign_transaction(dep_tx)

    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Deposit {amount} {cfg['symbol']} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

def cmd_withdraw(pool_name: str, amount: float, execute: bool = False):
    """Pool-side (LENDER) withdraw. Instant, no time-lock - this is the savings-pool
    side. The Degen Account collateral withdraw flow is separate (withdraw-collateral
    -> 24h delay -> execute-withdrawal).

    Always calls the pool's `withdraw()` (returns wrapped tokens to the wallet, even
    for the WETH pool). The pool also exposes `withdrawNativeToken(uint256)` that
    would unwrap to native ETH directly; a future --native flag could opt into it."""
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    amount_wei = int(amount * 10**cfg["decimals"])
    print(f"Wallet: {acct.address}")

    if not execute:
        print(f"Preview: Withdraw {amount} {cfg['symbol']} from {pool_name.upper()} pool")
        print("  Instant withdrawal from the lending pool (no time-lock).")
        print("Run with --execute to broadcast")
        return

    tx = contract.functions.withdraw(amount_wei).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 300000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Withdraw {amount} {cfg['symbol']} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

# ─── Degen Account commands ──────────────────────────────────────────────────

def _asset_decimals(w3, symbol: str) -> int:
    """bytes32 asset symbol -> decimals. Pool symbols are static; non-pool collateral
    (memecoins, LSTs, etc.) resolves via the TokenManager + ERC20.decimals(). Fall back
    to 18 if even that fails."""
    _addr, dec = _asset_meta(w3, symbol)
    return dec

def cmd_create_account(execute: bool = False, fund_pool: str = None, fund_amount: float = None):
    """Create a Degen Account. With fund_pool/fund_amount, create and fund in one
    tx via SmartLoansFactory.createAndFundLoan(bytes32 asset, amount) - ERC20 only,
    and the factory pulls the asset via transferFrom so it needs a prior approve to
    the factory. Without fund args, plain createLoan() makes an empty account."""
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    existing = get_prime_account(w3, acct.address)
    if existing:
        print(f"Degen Account already exists: {existing}")
        print("Nothing to create. Fund it with: degenprime fund --pool <p> --amount <n> --execute")
        return

    funding = fund_pool is not None and fund_amount is not None
    cfg = POOLS[fund_pool] if funding else None
    if funding and cfg["native"]:
        print("createAndFundLoan is ERC20-only - it cannot wrap native ETH.")
        print("For an ETH-funded account: create-account --execute, then")
        print("  fund --pool weth --amount <n> --execute  (uses depositNativeToken()).")
        return

    factory = get_factory_contract(w3)
    factory_cs = Web3.to_checksum_address(FACTORY_PROXY)

    if not execute:
        print(f"Preview: Create a new Degen Account for {acct.address}")
        if funding:
            symbol = cfg["symbol"]
            amount_wei = int(fund_amount * 10**cfg["decimals"])
            print(f"  Factory: {FACTORY_PROXY} (SmartLoansFactory.createAndFundLoan())")
            print(f"  Approves the factory to spend {fund_amount} {symbol}, then")
            print(f"  calls createAndFundLoan(bytes32 '{symbol}', {amount_wei}) - creates + funds in one go.")
            print("  Wallet must hold enough of the asset.")
        else:
            print(f"  Factory: {FACTORY_PROXY} (SmartLoansFactory.createLoan())")
            print("  Creates an empty account; fund it afterwards before borrowing.")
        print("Run with --execute to broadcast")
        return

    if funding:
        symbol = cfg["symbol"]
        amount_wei = int(fund_amount * 10**cfg["decimals"])
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]), abi=ERC20_ABI)
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
    label = "Create+fund Degen Account" if funding else "Create Degen Account"
    print(f"{'✓' if ok else '✗'} {label} {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")
    if ok:
        # getLoansForOwner can lag a beat behind the receipt; poll briefly so we
        # print the new account address instead of None right after creation.
        pa = None
        for _ in range(6):
            pa = get_prime_account(w3, acct.address)
            if pa:
                break
            time.sleep(2)
        if pa:
            print(f"  Degen Account: {pa}")
        else:
            print("  Degen Account: created - getLoansForOwner not propagated yet, run 'my-positions' shortly.")

def cmd_fund(pool_name: str, amount: float, execute: bool = False):
    """Fund collateral from the EOA wallet into its Degen Account.

    ERC20 assets: approve the Degen Account to spend the token, then call
    fund(bytes32 asset, amount) on it. Native ETH (weth pool): call the
    payable depositNativeToken() and send ETH as msg.value - the account
    wraps ETH->WETH internally, so no token approve is needed.
    """
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account. Create one first: degenprime create-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    amount_wei = int(amount * 10**cfg["decimals"])
    pa_cs = Web3.to_checksum_address(pa)

    if not execute:
        print(f"Preview: Fund {amount} {symbol} into Degen Account {pa}")
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
        tx = account.functions.depositNativeToken().build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID, "value": amount_wei,
        })
        signed = acct.sign_transaction(tx)
    else:
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]), abi=ERC20_ABI)
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
    """Best-effort per-symbol USD price map via the RedStone-gated getPrices view
    (1e8-scaled). Reuses an already-built `payload`; returns {symbol: float}. Symbols
    without a RedStone feed are filtered out before the call - the SolvencyFacet
    reverts on a getPrices request for a symbol whose feed isn't in the payload."""
    syms = [s for s in dict.fromkeys(symbols) if s and s in REDSTONE_AVAILABLE_FEEDS]
    if not syms:
        return {}
    try:
        raw = redstone_view_call(w3, account, "getPrices", payload,
                                 args=[[asset_b32(s) for s in syms]])[0]
        return {s: raw[i] / 1e8 for i, s in enumerate(syms)}
    except Exception:
        return {}

def cmd_summary():
    """Read-only Degen Account view: in-account collateral, debts, and live
    RedStone-gated solvency (getTotalValue/getDebt/getHealthRatio/isSolvent). Falls
    back to balances-only if the RedStone gateway is unreachable or a view reverts.
    Note: per-asset USD is best-effort - only symbols with a RedStone primary-prod
    feed are priced here. Symbols sourced on-chain from BaseOracle TWAP show as
    balance-only (the SolvencyFacet still values them for the total/debt figures)."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Wallet: {acct.address}")
    if not pa:
        print("No Degen Account yet. Create one with: degenprime create-account --execute")
        return

    print(f"Degen Account: {pa}")
    pa_eth = w3.eth.get_balance(pa) / 1e18
    print(f"  Native ETH (gas):  {pa_eth:.6f}")

    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    owned = [a.rstrip(b"\x00").decode(errors="replace") for a in account.functions.getAllOwnedAssets().call()]
    supplied = []
    for sym in owned:
        bal = account.functions.getBalance(asset_b32(sym)).call()
        supplied.append({"symbol": sym, "raw": bal, "decimals": _asset_decimals(w3, sym)})
    borrowed = []
    for n, v in account.functions.getDebts().call():
        sym = n.rstrip(b"\x00").decode(errors="replace")
        if v > 0:
            borrowed.append({"symbol": sym, "raw": v, "decimals": _asset_decimals(w3, sym)})

    # Solvency views (SolvencyFacet) are RedStone-gated: they revert (0xe7764c9e)
    # without signed price calldata appended. Fetch a fresh RedStone payload covering
    # every feed the solvency math touches (RedStone-feed symbols only - others come
    # from BaseOracle on-chain) and eth_call the views with it appended. No tx.
    solvency = {"total": None, "debt": None, "ratio": None, "solvent": None, "error": None, "prices": {}}
    try:
        feeds = degen_account_price_feeds(account)
        payload = build_redstone_payload(feeds)
        solvency["total"] = redstone_view_call(w3, account, "getTotalValue", payload)[0] / 1e18
        solvency["debt"] = redstone_view_call(w3, account, "getDebt", payload)[0] / 1e18
        ratio = redstone_view_call(w3, account, "getHealthRatio", payload)[0] / 1e18
        # With negligible debt the ratio is astronomically large (e.g. 1e59) - render
        # that as None and show ">1000" rather than a junk number.
        solvency["ratio"] = None if ratio > 1000 else ratio
        solvency["solvent"] = bool(redstone_view_call(w3, account, "isSolvent", payload)[0])
        solvency["prices"] = _prices_usd(w3, account, [r["symbol"] for r in supplied + borrowed], payload)
    except Exception as e:
        solvency["error"] = type(e).__name__

    print("  Assets:")
    if supplied:
        for r in supplied:
            usd = solvency["prices"].get(r["symbol"])
            usd_str = f"  (~${r['raw'] / 10**r['decimals'] * usd:,.2f})" if usd is not None else ""
            print(f"    {r['symbol']:<8} {r['raw'] / 10**r['decimals']:,.6f}{usd_str}")
    else:
        print("    (none)")

    print("  Debts:")
    if borrowed:
        for r in borrowed:
            usd = solvency["prices"].get(r["symbol"])
            usd_str = f"  (~${r['raw'] / 10**r['decimals'] * usd:,.2f})" if usd is not None else ""
            print(f"    {r['symbol']:<8} {r['raw'] / 10**r['decimals']:,.6f}{usd_str}")
    else:
        print("    (none)")

    if solvency["error"] is None:
        print(f"  Total value:        ${solvency['total']:,.2f}")
        print(f"  Debt:               ${solvency['debt']:,.2f}")
        ratio_str = ">1000.00 (negligible debt)" if solvency["ratio"] is None else f"{solvency['ratio']:.4f}"
        print(f"  Health ratio:       {ratio_str}  (>1.0 = solvent)")
        print(f"  Solvent:            {'yes' if solvency['solvent'] else 'NO - liquidatable'}")
    else:
        print(f"  Health/solvency:    RedStone fetch/call failed ({solvency['error']}); showing balances only")

def cmd_borrow(pool_name: str, amount: float, execute: bool = False):
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account. Create one first: degenprime create-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    amount_wei = int(amount * 10**cfg["decimals"])
    if not execute:
        print(f"Preview: Borrow {amount} {symbol} into Degen Account {pa}")
        print(f"  Calls borrow(bytes32 '{symbol}', {amount_wei}) on the Degen Account")
        print("  Requires sufficient collateral funded into the account.")
        print("Run with --execute to broadcast")
        return

    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    # borrow has remainsSolvent -> needs RedStone price payload appended to calldata.
    feeds = degen_account_price_feeds(account)
    if symbol in REDSTONE_AVAILABLE_FEEDS and symbol not in feeds:
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
    """Repay from the Degen Account's in-account balance.

    On the DeltaPrime/DegenPrime shared facet, repay's internal path runs
    `_isSolvent()` over proxyDelegateCalldata, so the tx must carry a RedStone price
    payload appended to the calldata even though the function signature isn't directly
    `remainsSolvent`-modified. Append it (mirrors the DeltaPrime repay fix).

    The facet's repay reverts if amount > debt OR amount > in-account balance, so we
    cap to min(requested, debt, in_account) for clean handling of overshoots."""
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account. Create one first: degenprime create-account --execute")
        return

    symbol = pool_to_asset_symbol(pool_name)
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    pool, _, _ = get_pool_contract(pool_name)
    requested_wei = int(amount * 10**cfg["decimals"])
    debt_wei = pool.functions.getBorrowed(pa_cs).call()
    in_acct_wei = account.functions.getBalance(asset_b32(symbol)).call()
    if debt_wei == 0:
        print(f"No {symbol} debt to repay on Degen Account {pa}.")
        return
    amount_wei = min(requested_wei, debt_wei, in_acct_wei)
    if amount_wei == 0:
        print(f"Repay {amount} {symbol}: in-account {symbol} balance is 0 - "
              f"swap into {symbol} first (e.g. degenprime swap --to {symbol} --amount N --execute).")
        return
    cap_notes = []
    if amount_wei < requested_wei:
        if in_acct_wei < min(requested_wei, debt_wei):
            cap_notes.append(f"in-account {symbol} only {in_acct_wei / 10**cfg['decimals']:.6f}")
        if debt_wei < requested_wei:
            cap_notes.append(f"debt only {debt_wei / 10**cfg['decimals']:.6f} {symbol}")

    if not execute:
        print(f"Preview: Repay {amount_wei / 10**cfg['decimals']:.6f} {symbol} from Degen Account {pa}")
        if cap_notes:
            print(f"  Capped from requested {amount}: {'; '.join(cap_notes)}")
        print(f"  Calls repay(bytes32 '{symbol}', {amount_wei}) on the Degen Account")
        print(f"  Current debt: {debt_wei / 10**cfg['decimals']:.6f} {symbol} | "
              f"in-account: {in_acct_wei / 10**cfg['decimals']:.6f} {symbol}")
        if in_acct_wei < debt_wei:
            shortfall = (debt_wei - in_acct_wei) / 10**cfg['decimals']
            print(f"  Note: in-account < debt by {shortfall:.6f} {symbol} - "
                  f"swap into {symbol} first to close the position fully.")
        print("Run with --execute to broadcast")
        return

    if cap_notes:
        print(f"  Capped requested {amount} {symbol} to {amount_wei / 10**cfg['decimals']:.6f} "
              f"({'; '.join(cap_notes)}).")
    # repay's internal _isSolvent uses proxyDelegateCalldata -> needs RedStone payload
    feeds = degen_account_price_feeds(account)
    if symbol not in feeds and symbol in REDSTONE_AVAILABLE_FEEDS:
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

# ─── ParaSwap / Velora route ─────────────────────────────────────────────────
# The Degen Account already holds the funds, so the facet (not the EOA) approves the
# Augustus router and executes. We only build the API calldata with the Degen Account
# as the swapper + receiver, then hand its (selector, data) to paraSwapV6 /
# swapDebtParaSwap.

# Map account-side bytes32 symbols to (token_address, decimals) for the swap and
# swap-debt paths. Pool symbols are pre-baked here; non-pool symbols (memecoin
# collateral) resolve dynamically via _asset_meta at the swap site.
SWAP_ASSETS = {cfg["symbol"]: {"token": cfg["token"], "decimals": cfg["decimals"]}
               for cfg in POOLS.values()}

def _swap_asset_meta(w3, symbol: str):
    """Resolve a swap-side symbol to {token, decimals}. Falls back to TokenManager for
    non-pool collateral (memecoins). Returns None if the asset is unknown."""
    if symbol in SWAP_ASSETS:
        return SWAP_ASSETS[symbol]
    addr, dec = _asset_meta(w3, symbol)
    if addr is None:
        return None
    return {"token": addr, "decimals": dec}

def _paraswap_price_route(src_token, src_dec, dest_token, dest_dec, amount_in_wei, user_addr):
    """ParaSwap /prices on Base (network=8453, v6.2). Returns the priceRoute dict for a
    SELL of amount_in_wei src->dest. The priceRoute is passed verbatim to /transactions.
    excludeContractMethods is hard-coded to keep ParaSwap from picking a router method
    the facet can't decode (multiSwap/megaSwap/protected* etc.)."""
    params = {
        "srcToken": src_token, "srcDecimals": src_dec,
        "destToken": dest_token, "destDecimals": dest_dec,
        "amount": str(amount_in_wei), "side": "SELL",
        "network": CHAIN_ID, "version": "6.2", "userAddress": user_addr,
        "excludeContractMethods": "multiSwap,megaSwap,protectedMultiSwap,protectedMegaSwap,protectedSimpleSwap,simpleSwap",
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
    """ParaSwap /transactions on Base. Builds the Augustus calldata with the Degen
    Account as userAddress + receiver. partner='paraswap' makes the encoded
    partnerAndFee resolve to partner=0/fee=0, which the facet requires."""
    body = {
        "srcToken": src_token, "srcDecimals": src_dec,
        "destToken": dest_token, "destDecimals": dest_dec,
        "srcAmount": str(amount_in_wei),
        "slippage": int(round(slippage_pct * 100)),  # bps
        "priceRoute": price_route,
        "userAddress": user_addr,
        "receiver": user_addr,
        "partner": "paraswap",
    }
    # ignoreChecks: the swapper is the Degen Account (a contract that holds no funds at
    # build time and hasn't approved Augustus yet - the facet does that mid-tx), so the
    # API's balance/allowance pre-checks would reject an otherwise valid build.
    r = requests.post(f"{PARASWAP_API}/transactions/{CHAIN_ID}?ignoreChecks=true&ignoreGasEstimate=true",
                      json=body, headers={"Accept": "application/json"}, timeout=20)
    d = r.json()
    if "data" not in d:
        raise RuntimeError(f"ParaSwap /transactions returned no calldata: {d.get('error', d)}")
    return d

def _paraswap_decode_and_check(selector_hex, data_bytes, src_token, dest_token, expected_from, pa_cs):
    """Mirror the facet's decodeParaSwapData + validateSwapParameters on the built
    calldata so a preview fails loud here rather than reverting on-chain. Returns the
    decoded (executor, src, dest, fromAmount, toAmount) for display."""
    if selector_hex not in PARASWAP_SUPPORTED_SELECTORS:
        raise RuntimeError(f"ParaSwap returned method {selector_hex}, which the facet does not "
                           f"decode (supported: {', '.join(sorted(PARASWAP_SUPPORTED_SELECTORS))}). "
                           "Refusing.")
    if len(data_bytes) < 288:
        raise RuntimeError(f"ParaSwap calldata body too short ({len(data_bytes)} bytes, need >=288).")

    if selector_hex == "0xe3ead59e":
        executor = "0x" + data_bytes[:32][-20:].hex()
        src, dest, from_amt, to_amt, _quoted, _meta, beneficiary = abi_decode(
            ["address", "address", "uint256", "uint256", "uint256", "bytes32", "address"],
            data_bytes[32:256])
        partner_and_fee = int.from_bytes(data_bytes[256:288], "big")
        partner = (partner_and_fee >> 96) & ((1 << 160) - 1)
        fee_bps = partner_and_fee & 0x3FFF
        if executor.lower() not in PARASWAP_EXECUTORS:
            print(f"  ⚠ ParaSwap executor {executor} not in the KNOWN whitelist - the on-chain facet")
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
                               f"is neither zero nor the Degen Account. Refusing.")
        if from_amt != expected_from:
            raise RuntimeError(f"ParaSwap fromAmount {from_amt} != expected {expected_from}. Refusing.")
        return executor, src, dest, from_amt, to_amt
    # UniV3 variant: selector + length sanity only.
    return None, src_token, dest_token, expected_from, None

# Executor fallback - mirrors DeltaPrime's swap-debt path. If the ParaSwap API returns a
# new executor not on the whitelist, patch in this one (the canonical legacy executor
# whose calldata format is compatible with the current API output).
_PARASWAP_FALLBACK_EXECUTOR = "0x000010036C0190E009a000d0fc3541100A07380A"

def cmd_swap(from_sym: str, to_sym: str, amount: float, slippage_pct: float = 1.0,
             execute: bool = False):
    """Swap one in-account asset for another via the Degen Account on ParaSwap v6.
    Sells the account's in-account balance of --from for --to. Carries remainsSolvent,
    so the --execute path appends a RedStone signed-price payload to the calldata."""
    from_sym, to_sym = from_sym.upper(), to_sym.upper()
    if from_sym == to_sym:
        print("--from and --to must differ.")
        return

    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account exists for this wallet - nothing to swap.")
        print("Create and fund one first: degenprime create-account --execute")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    from_cfg = _swap_asset_meta(w3, from_sym)
    to_cfg = _swap_asset_meta(w3, to_sym)
    if from_cfg is None:
        print(f"Unknown --from asset '{from_sym}'. Pool symbols: {', '.join(SWAP_ASSETS)}; "
              "or any TokenManager-listed collateral symbol.")
        return
    if to_cfg is None:
        print(f"Unknown --to asset '{to_sym}'. Pool symbols: {', '.join(SWAP_ASSETS)}; "
              "or any TokenManager-listed collateral symbol.")
        return

    amount_in = int(amount * 10**from_cfg["decimals"])
    in_balance = account.functions.getBalance(asset_b32(from_sym)).call()
    if amount_in > in_balance:
        print(f"Degen Account holds only {in_balance / 10**from_cfg['decimals']:.6f} {from_sym} "
              f"in-account; cannot swap {amount} {from_sym}.")
        print("Fund or borrow more of the asset into the account first.")
        return

    price_route = _paraswap_price_route(from_cfg["token"], from_cfg["decimals"],
                                        to_cfg["token"], to_cfg["decimals"], amount_in, pa_cs)
    quoted_out = int(price_route["destAmount"])
    tx_built = _paraswap_build_tx(price_route, from_cfg["token"], from_cfg["decimals"],
                                  to_cfg["token"], to_cfg["decimals"], amount_in,
                                  slippage_pct, pa_cs)
    full = bytes.fromhex(tx_built["data"][2:])
    selector_hex, data_bytes = "0x" + full[:4].hex(), full[4:]
    _exec, _src, _dest, _from_amt, min_out = _paraswap_decode_and_check(
        selector_hex, data_bytes, from_cfg["token"], to_cfg["token"], amount_in, pa_cs)
    if _exec is not None and _exec.lower() not in PARASWAP_EXECUTORS:
        fallback_bytes = bytes(12) + bytes.fromhex(_PARASWAP_FALLBACK_EXECUTOR[2:])
        data_bytes = fallback_bytes + data_bytes[32:]
        print(f"  ⚠ Executor {_exec} not whitelisted; patching to {_PARASWAP_FALLBACK_EXECUTOR}")
        _paraswap_decode_and_check(selector_hex, data_bytes, from_cfg["token"], to_cfg["token"],
                                   amount_in, pa_cs)

    print(f"Swap {amount} {from_sym} -> {to_sym} on Degen Account {pa_cs}  (via ParaSwap/Velora)")
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

    feeds = degen_account_price_feeds(account)
    for s in (from_sym, to_sym):
        if s in REDSTONE_AVAILABLE_FEEDS and s not in feeds:
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

# ─── Swap debt / refinance (SwapDebtFacet) ───────────────────────────────────
# swapDebtParaSwap borrows _borrowAmount of _toAsset, ParaSwaps it into _fromAsset, and
# repays _repayAmount of the _fromAsset debt - all in one tx. The facet hard-caps the
# USD value difference between the repay and borrow legs at 5% (RedStone-priced) and
# requires the ParaSwap quote's fromAmount to equal _borrowAmount exactly.

_SYMBOL_TO_POOL = {cfg["symbol"]: name for name, cfg in POOLS.items()}

def _read_prices_usd(w3, account, symbols, payload):
    """RedStone-gated getPrices read for `symbols` (1e8-scaled USD), payload appended.
    Used by swap-debt to value-match the borrow vs repay leg against the facet's own
    5% cap (same numbers the contract sees). Symbols must all have RedStone feeds."""
    data = account.encode_abi("getPrices", args=[[asset_b32(s) for s in symbols]]) + payload.hex()
    raw = w3.eth.call({"to": account.address, "data": data})
    return w3.codec.decode(["uint256[]"], bytes(raw))[0]

def cmd_swap_debt(from_sym: str, to_sym: str, amount: float, slippage_pct: float = 1.0,
                  execute: bool = False):
    """Refinance debt from --from (existing debt) into --to (new debt) via
    swapDebtParaSwap. --amount is how much of the OLD (--from) debt to repay, in --from
    units. We value-match the new borrow to the repay using the facet's own RedStone
    prices, build the ParaSwap calldata for the internal --to -> --from swap, and
    preview the 5% USD-diff cap. RedStone-gated on execute. Both assets must be
    DegenPrime POOL assets (the swap-debt path touches pool getBorrowed)."""
    from_sym, to_sym = from_sym.upper(), to_sym.upper()
    if from_sym not in SWAP_ASSETS:
        print(f"Unknown --from (old debt) asset '{from_sym}'. Must be a pool asset: {', '.join(SWAP_ASSETS)}")
        return
    if to_sym not in SWAP_ASSETS:
        print(f"Unknown --to (new debt) asset '{to_sym}'. Must be a pool asset: {', '.join(SWAP_ASSETS)}")
        return
    if from_sym == to_sym:
        print("--from and --to must differ.")
        return
    if from_sym not in REDSTONE_AVAILABLE_FEEDS or to_sym not in REDSTONE_AVAILABLE_FEEDS:
        print(f"swap-debt requires both assets to have a RedStone primary-prod feed "
              f"(for the value-match price read). Available: {sorted(REDSTONE_AVAILABLE_FEEDS & {cfg['symbol'] for cfg in POOLS.values()})}")
        return

    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account exists for this wallet - no debt to swap.")
        return
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    from_cfg, to_cfg = SWAP_ASSETS[from_sym], SWAP_ASSETS[to_sym]

    # Current borrowed of the OLD debt asset, read from its pool.
    from_pool, _, _ = get_pool_contract(_SYMBOL_TO_POOL[from_sym])
    borrowed = from_pool.functions.getBorrowed(pa_cs).call()
    if borrowed == 0:
        print(f"Degen Account has no {from_sym} debt to refinance.")
        return
    repay_amount = min(int(amount * 10**from_cfg["decimals"]), borrowed)

    feeds = degen_account_price_feeds(account)
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
        print("Computed borrow amount rounds to zero - repay amount too small. Refusing.")
        return

    repay_usd = price_from * repay_amount / 10**from_cfg["decimals"] / 1e8
    borrow_usd = price_to * borrow_amount / 10**to_cfg["decimals"] / 1e8
    diff_bps = (abs(repay_usd - borrow_usd) / max(repay_usd, borrow_usd)) * 10000 if max(repay_usd, borrow_usd) else 0

    price_route = _paraswap_price_route(to_cfg["token"], to_cfg["decimals"],
                                        from_cfg["token"], from_cfg["decimals"], borrow_amount, pa_cs)
    quoted_out = int(price_route["destAmount"])
    tx_built = _paraswap_build_tx(price_route, to_cfg["token"], to_cfg["decimals"],
                                  from_cfg["token"], from_cfg["decimals"], borrow_amount,
                                  slippage_pct, pa_cs)
    full = bytes.fromhex(tx_built["data"][2:])
    selector_hex, data_bytes = "0x" + full[:4].hex(), full[4:]
    _exec, _src, _dest, _swap_from_amt, swap_min_out = _paraswap_decode_and_check(
        selector_hex, data_bytes, to_cfg["token"], from_cfg["token"], borrow_amount, pa_cs)
    if _exec.lower() not in PARASWAP_EXECUTORS:
        fallback_bytes = bytes(12) + bytes.fromhex(_PARASWAP_FALLBACK_EXECUTOR[2:])
        data_bytes = fallback_bytes + data_bytes[32:]
        print(f"  ⚠ Executor {_exec} not whitelisted; patching to {_PARASWAP_FALLBACK_EXECUTOR}")
        _paraswap_decode_and_check(selector_hex, data_bytes, to_cfg["token"], from_cfg["token"],
                                   borrow_amount, pa_cs)

    print(f"Swap debt on Degen Account {pa}")
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
              f"min(swap output, {repay_amount / 10**from_cfg['decimals']:.6f}, debt) - any shortfall "
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
# Universal 24h time-lock on DegenPrime - NOT just risky assets. createWithdrawalIntent
# registers an intent (no RedStone), then executeWithdrawalIntent pulls it after
# maturity (RedStone-gated). Window (from the IntentInfo flags on-chain):
# actionableAt = createdAt + 24h, expiresAt = actionableAt + 48h. So an intent is
# executable in a 24h-72h window. cancelWithdrawalIntent drops a pending intent.

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
    """Step 1 of collateral withdrawal: register a WithdrawalIntent via
    createWithdrawalIntent(bytes32 asset, uint256 amount). Does NOT need a RedStone
    payload (the solvency check is deferred to the execute step). After ~24h the intent
    becomes executable for a 48h window (see execute-withdrawal). Preview by default."""
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account exists for this wallet - nothing to withdraw.")
        return

    symbol = pool_to_asset_symbol(pool_name)
    amount_wei = int(amount * 10**cfg["decimals"])
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    # getAvailableBalance is oracle-free: in-account minus pending intents.
    available = account.functions.getAvailableBalance(asset_b32(symbol)).call()
    print(f"Create withdrawal intent: {amount} {symbol} from Degen Account {pa}")
    print(f"  Available to withdraw now: {available / 10**cfg['decimals']:.6f} {symbol}")
    if amount_wei > available:
        print(f"  ✗ Requested {amount} {symbol} exceeds available balance. Refusing.")
        return
    print(f"  Calls createWithdrawalIntent(bytes32 '{symbol}', {amount_wei}) - no RedStone payload needed.")
    print("  Universal time-lock: becomes executable ~24h later, then has a 48h window (24h-72h total).")
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
    available balance. Uses oracle-free WithdrawalIntentFacet views - no RedStone."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    print(f"Wallet: {acct.address}")
    if not pa:
        print("No Degen Account yet - no withdrawal intents.")
        return

    print(f"Degen Account: {pa}")
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    owned = account.functions.getAllOwnedAssets().call()
    if not owned:
        print("  Account holds no assets - nothing to withdraw.")
        return

    any_pending = False
    for a in owned:
        sym = a.rstrip(b"\x00").decode(errors="replace")
        dec = _asset_decimals(w3, sym)
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
            print(f"    [{idx}] {amt / 10**dec:,.6f} {sym} - {state}")
            print(f"         {_fmt_window(actionable_at, expires_at)}")
    if not any_pending:
        print("  No pending withdrawal intents.")

def cmd_execute_withdrawal(pool_name: str, index: int = None, execute: bool = False):
    """Step 2 of collateral withdrawal: executeWithdrawalIntent(bytes32 asset,
    uint256[] indices) pulls matured intent(s) to the EOA. RedStone-gated, so
    --execute appends a fresh RedStone price payload. Refuses any intent not yet
    matured (isActionable=false) or expired. --index selects one intent; default
    executes all currently-actionable intents (indices passed strictly increasing,
    as the contract requires)."""
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account exists for this wallet - nothing to execute.")
        return

    symbol = pool_to_asset_symbol(pool_name)
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    intents = account.functions.getUserIntents(asset_b32(symbol)).call()
    if not intents:
        print(f"No withdrawal intents registered for {symbol}.")
        print("Register one first: withdraw-collateral --pool <p> --amount <n> --execute")
        return

    if index is not None:
        if index < 0 or index >= len(intents):
            print(f"--index {index} out of range (asset has {len(intents)} intent(s)).")
            return
        candidates = [index]
    else:
        candidates = [i for i, it in enumerate(intents) if it[4]]  # isActionable

    print(f"Execute withdrawal of {symbol} from Degen Account {pa}")
    ready = []
    for i in candidates:
        amt, actionable_at, expires_at, _is_pending, is_actionable, is_expired = intents[i]
        print(f"  [{i}] {amt / 10**cfg['decimals']:,.6f} {symbol} - "
              f"{'EXPIRED' if is_expired else 'READY' if is_actionable else 'NOT MATURED'}")
        print(f"       {_fmt_window(actionable_at, expires_at)}")
        if is_expired:
            print(f"       ✗ intent [{i}] has expired - cannot execute (cancel/clear it instead).")
        elif not is_actionable:
            print(f"       ✗ intent [{i}] has not matured yet - refusing.")
        else:
            ready.append(i)

    if not ready:
        print("  No matured, non-expired intents to execute. Refusing.")
        return
    ready.sort()
    print(f"  Will execute indices {ready} via executeWithdrawalIntent(bytes32 '{symbol}', {ready}).")
    print("  Carries remainsSolvent - appends a fresh RedStone payload.")

    if not execute:
        print("Run with --execute to broadcast (pulls the funds to the wallet).")
        return

    feeds = degen_account_price_feeds(account)
    if symbol in REDSTONE_AVAILABLE_FEEDS and symbol not in feeds:
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

def cmd_cancel_withdrawal(pool_name: str, index: int, execute: bool = False):
    """Cancel a pending withdrawal intent before it matures (or before it's executed).
    Calls cancelWithdrawalIntent(bytes32 asset, uint256 intentIndex) - oracle-free,
    no RedStone payload. Useful when changing your mind about a queued withdrawal,
    or when freeing up the locked amount for swap-debt / repay first."""
    cfg = POOLS[pool_name]
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account exists for this wallet - nothing to cancel.")
        return

    symbol = pool_to_asset_symbol(pool_name)
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    intents = account.functions.getUserIntents(asset_b32(symbol)).call()
    if not intents:
        print(f"No withdrawal intents registered for {symbol}.")
        return
    if index < 0 or index >= len(intents):
        print(f"--index {index} out of range (asset has {len(intents)} intent(s)).")
        return

    amt, actionable_at, expires_at, _is_pending, is_actionable, is_expired = intents[index]
    state = "EXPIRED" if is_expired else "READY" if is_actionable else "maturing"
    print(f"Cancel withdrawal intent [{index}] for {symbol} on Degen Account {pa}")
    print(f"  Amount: {amt / 10**cfg['decimals']:,.6f} {symbol}  ({state})")
    print(f"  {_fmt_window(actionable_at, expires_at)}")
    print(f"  Calls cancelWithdrawalIntent(bytes32 '{symbol}', {index}) - no RedStone payload needed.")

    if not execute:
        print("Run with --execute to broadcast.")
        return

    tx = account.functions.cancelWithdrawalIntent(asset_b32(symbol), index).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 1000000, "gasPrice": _tx_gas_price(w3), "chainId": CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt["status"] == 1
    print(f"{'✓' if ok else '✗'} Cancel withdrawal intent [{index}] {'confirmed' if ok else 'failed'}")
    print(f"  Tx: {EXPLORER}/tx/{tx_hash.hex()}")

# ─── Aerodrome (read-only for v1) ────────────────────────────────────────────

def cmd_aerodrome_positions():
    """Read-only: list the Aerodrome NFT tokenIds the Degen Account owns/has staked,
    via the diamond's getOwnedStakedAerodromeTokenIds view. Write paths (add/remove/
    stake liquidity) are deferred to v2 - the on-chain signatures vary by Aerodrome
    version and need per-market probing before broadcasting. Position composition
    (per-token amounts) needs the getPositionCompositionSimplified return shape, which
    we don't decode in v1; just listing IDs keeps this safe and useful."""
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account yet - no Aerodrome positions.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Degen Account: {pa}")
    try:
        ids = account.functions.getOwnedStakedAerodromeTokenIds().call()
    except Exception as e:
        print(f"  Aerodrome read failed: {type(e).__name__}: {e}")
        return
    if not ids:
        print("  No Aerodrome positions (owned/staked tokenIds).")
        return
    print(f"  {len(ids)} Aerodrome NFT tokenId(s):")
    for tid in ids:
        print(f"    [{tid}]  https://aerodrome.finance/positions  (manage on Aerodrome UI)")
    print("  v1 lists tokenIds only. Composition + write paths deferred to v2.")

def main():
    try:
        _dispatch()
    except RuntimeError as e:
        print(f"degenprime: {e}", file=sys.stderr)
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
            print("Usage: degenprime deposit --pool usdc --amount 100 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_deposit(pool, amount, execute)
    elif cmd == "withdraw":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: degenprime withdraw --pool usdc --amount 100 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_withdraw(pool, amount, execute)
    elif cmd in ("create-account", "create-degen-account"):
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
        cmd_create_account("--execute" in args, fund_pool, fund_amount)
    elif cmd == "summary":
        cmd_summary()
    elif cmd == "fund":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: degenprime fund --pool usdc --amount 100 [--execute]")
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
            print(f"Usage: degenprime {cmd} --pool usdc --amount 100 [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        (cmd_borrow if cmd == "borrow" else cmd_repay)(pool, amount, execute)
    elif cmd == "swap":
        from_sym, to_sym, amount, slippage = None, None, None, 1.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--from" and i + 1 < len(args): from_sym = args[i + 1]
            if a == "--to" and i + 1 < len(args): to_sym = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
        if not from_sym or not to_sym or amount is None:
            print("Usage: degenprime swap --from USDC --to ETH --amount 10 [--slippage 0.5] [--execute]")
            return
        cmd_swap(from_sym, to_sym, amount, slippage, execute)
    elif cmd == "swap-debt":
        from_sym, to_sym, amount, slippage = None, None, None, 1.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--from" and i + 1 < len(args): from_sym = args[i + 1]
            if a == "--to" and i + 1 < len(args): to_sym = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
        if not from_sym or not to_sym or amount is None:
            print("Usage: degenprime swap-debt --from ETH --to USDC --amount 100 [--slippage 0.5] [--execute]")
            return
        cmd_swap_debt(from_sym, to_sym, amount, slippage, execute)
    elif cmd == "withdraw-collateral":
        pool, amount = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--amount" and i + 1 < len(args): amount = float(args[i + 1])
        if not pool or amount is None:
            print("Usage: degenprime withdraw-collateral --pool usdc --amount 100 [--execute]")
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
            print("Usage: degenprime execute-withdrawal --pool usdc [--index N] [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_execute_withdrawal(pool, index, execute)
    elif cmd == "cancel-withdrawal":
        pool, index = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--index" and i + 1 < len(args): index = int(args[i + 1])
        if not pool or index is None:
            print("Usage: degenprime cancel-withdrawal --pool usdc --index N [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_cancel_withdrawal(pool, index, execute)
    elif cmd == "aerodrome-positions":
        cmd_aerodrome_positions()
    else:
        print(f"Unknown command: {cmd}\n{__doc__}")

if __name__ == "__main__":
    main()
