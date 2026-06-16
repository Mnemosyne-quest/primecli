#!/usr/bin/env python3
"""DegenPrime Protocol interaction module (Base, chain 8453).

Sister protocol to DeltaPrime on Avalanche, by the same team (DeltaPrimeLabs),
sharing the EIP-2535 Diamond + per-user Smart Loan architecture. Lending pools take
direct EOA deposits/withdrawals. Borrowing and leverage go through a Degen Account:
a per-user SmartLoan (diamond proxy) created via the SmartLoansFactory. The EOA owns
it; borrow/repay/fund run on the Degen Account, which itself talks to the pools.

Usage:
  degenprime pool-info [usdc|weth|cbbtc|aero|brett|kaito|cbdoge|cbxrp|all] [--json]
  degenprime my-positions
  degenprime deposit --pool usdc --amount 100 [--execute]
  degenprime withdraw --pool usdc --amount 100 [--execute]
  degenprime create-account [--execute]
  degenprime create-account --fund-pool usdc --fund-amount 100 [--execute]
  degenprime summary
  degenprime defi --json          (aggregate ALL positions as DeBank-style JSON; read-only)
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
  degenprime aero-add-liquidity --pool weth-usdc-100 --amount-weth 0.05 --amount-usdc 100 [--slippage 1] [--execute]
  degenprime aero-increase-liquidity --pool weth-usdc-100 --token-id N --amount-token0 X --amount-token1 Y [--slippage 1] [--execute]
  degenprime aero-remove-liquidity --token-id N [--token-id M ...] [--execute]   (full close only)
  degenprime aero-collect-fees --token-id N [--execute]
  degenprime aero-rebalance status [--token-id N] [--check] [--history] [--json]
  degenprime aero-rebalance create --token-id N --width-pct W [--mode outside|inside] [--trigger-bps T] [--max-fee-weth F] [--mint-slip-bps 100] [--swap-slip-bps 100] [--execute]
  degenprime aero-rebalance update --token-id N --width-pct W [...same as create] [--execute]
  degenprime aero-rebalance cancel --token-id N [--execute]

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
withdraw-collateral registers a WithdrawalIntent on the Degen Account (createWithdrawalIntent,
which IS RedStone-gated — the create-time solvency check is on-chain). The intent becomes
actionable 24h after creation and stays actionable for a further 48h (expiresAt =
actionableAt + 48h), so the live execute window is 24h-72h after creation; execute-withdrawal
then pulls it to the wallet (executeWithdrawalIntent, RedStone-gated). withdrawal-intents lists
pending intents + per-asset available balance (oracle-free reads). cancel-withdrawal cancels a
pending intent before maturity. (The savings-pool "diamond hands" path differs: see cmd_withdraw
— oracle-free create, and a 24h execute window because the pool re-anchors expiresAt.)

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

aerodrome-positions is read-only: lists each Aerodrome Slipstream (CL) NFT position the
Degen Account owns/stakes, showing token0/token1/tick range/liquidity. Enumerates tokenIds
via getOwnedStakedAerodromeTokenIds, then reads each from the Aerodrome NPM.positions()
view (correct for staked NFTs, which the simplified facet view reports as 0 liquidity).

aero-add-liquidity / aero-remove-liquidity / aero-collect-fees provide write paths to
the Aerodrome Slipstream NonfungiblePositionManager through the Degen Account's
AerodromeFacet wrapper functions. The facet selectors were determined via on-chain
probing (Diamond Loupe) of the smart-loan diamond; function names are inferred from
their parameter layouts and revert signatures.

aero-remove-liquidity fully closes one or more staked positions in a single call via
batchRemoveStakedLiquidityAerodrome(uint256[]) (selector 0x27bed82e): per tokenId it
unstakes from the gauge, removes all liquidity, collects fees, and burns the NFT. There
is no partial/percentage decrease on this path — it always closes 100%. The call is
solvency-gated, so the calldata carries a RedStone payload (verified byte-exact against
the manual close 0x0d65...0a50).

aero-rebalance manages the on-chain auto-rebalancer (AerodromeRebalancerFacet) attached
to a CL position. An order stores a reference price + a range band + a trigger, all in
basis points (1% = 100 bps), and an off-chain executor unwinds->swaps->re-mints the
position (a NEW tokenId) when price drifts past the trigger. Subcommands:
  status  — list active orders (getAllRebalanceOrders, plain read; getRebalanceOrder
            for one --token-id). Resolves the underlying Aerodrome position by trying
            v2 then v3 NPM.positions(). --check adds shouldRebalance (RedStone-gated).
            --history reads the shared RebalanceEventEmitter for this account, chaining
            RebalanceExecuted tokenId->newTokenId. --json emits a machine-readable shape.
  create  — turn the rebalancer ON via createRebalanceOrder(CreateRebalanceOrderParams).
            --width-pct sets a symmetric ±band; --mode outside (default; rebalance after
            price LEAVES the range, lowerTrigger<0/upperTrigger>0) or inside (rebalance
            early, lowerTrigger>0/upperTrigger<0 with |trigger|<|range|); --trigger-bps
            is the trigger magnitude. executionFeeWeth (--max-fee-weth, default 0.001)
            is a per-rebalance fee CEILING (not a deposit), validated > the live
            getAutomationProtocolFee() floor. The write only STORES the order (fee-floor
            check, no solvency), so NO RedStone payload is appended — confirmed by the
            pre-flight eth_call passing clean without one. Preview-then-confirm: prints
            the band/trigger/fee and requires --execute to broadcast.
  update  — re-tune an existing order (updateRebalanceOrder, same args/struct/gating).
  cancel  — turn the rebalancer OFF (cancelRebalanceOrder, bare uint256). The rollback
            primitive; not solvency-gated (no RedStone).
"""

import json, os, sys, time, base64
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
import requests
from eth_account import Account
from eth_keys import keys as eth_keys
from eth_abi import decode as abi_decode
from web3 import Web3

# Health monitoring sub-system
_hm = None
for _mod in ('primecli.health_monitor', 'health_monitor'):
    try:
        _hm = __import__(_mod, fromlist=['cli'])
        break
    except ImportError:
        continue
if _hm is None:
    import importlib
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

# Named-wallet table shared with deltaprime/arbprime. Allows running via
# DEGENPRIME_AGENT=parakletos (or the fallback DELTAPRIME_AGENT) which is
# cleaner than passing raw keys through environment variables.
# Agent resolution also supports --as <agent> CLI flag.
AGENTS = {
    "parakletos":   ("/root/.openclaw/.env",                "PARAKLETOS_EVM_PRIVATE_KEY"),
    "paraklaudios": ("/root/paraklaudios/.credentials.env", "PARAKLAUDIOS_EVM_PRIVATE_KEY"),
    "core1":   ("/root/.openclaw/.env",                "BRUNO_CORE1_PRIVATE_KEY"),
}
_SELECTED_AGENT = None        # set by the --as CLI flag in main()


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
            f"Or set DEGENPRIME_PRIVATE_KEY, or DEGENPRIME_KEY_FILE."
        )
    path, var = AGENTS[agent]
    key = _read_env_var(path, var)
    if not key:
        raise RuntimeError(f"{var} not found in {path} (agent '{agent}').")
    return key


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

# ── Aerodrome Slipstream (CL) ────────────────────────────────────────────────
# Verified on Base: DeployCL-Base.json output from aerodrome-finance/slipstream.
# The NonfungiblePositionManager wraps Uniswap V3-style concentrated liquidity
# positions as ERC-721 NFTs. The Degen Account's AerodromeFacet (diamond facet
# #14 at 0x3c0ddb23) passes calldata through with remainsSolvent checks.
AERODROME_NPM = "0x827922686190790b37229fd06084350E74485b72"

# Selectors extracted from the diamond via DiamondLoupe.facets() on the
# smart-loan diamond beacon. Function names are inferred from parmeter layouts
# and revert signatures (on-chain probing 2026-06-05).
#
# Known facet #14 selectors:
#   6f2845cd  getOwnedStakedAerodromeTokenIds()
#   b6626971  getPositionCompositionSimplified(uint256) -> (address,address,uint256,uint256)
#   121350b3  (view, takes uint256 — detailed position info, unknown return)
#   27bed82e  batchRemoveStakedLiquidityAerodrome(uint256[]) — full close per id
#             (verified byte-exact vs manual close 0x0d65...0a50, 2026-06-14)
#   2c710777  (write, onlyOwner, takes IncreaseLiquidityParams-like tuple — inferred increase)
#   92b5a47e  (write, takes uint256 tokenId, checks position exists — burn/collect)
#   46daca2c  (write, onlyOwnerOrLiquidator — emergency withdrawal)
AERODROME_SEL_MINT = bytes.fromhex("f32f1e56")          # mintAndStakeLiquidityAerodrome
AERODROME_SEL_INCREASE = bytes.fromhex("2c710777")      # inferred: increaseLiquidity
AERODROME_SEL_BURN = bytes.fromhex("27bed82e")           # batchRemoveStakedLiquidityAerodrome
AERODROME_SEL_COLLECT = bytes.fromhex("887e4b7e")        # collectAerodromeFees

# ── Aerodrome auto-rebalancer (AerodromeRebalancerFacet) ──────────────────────
# Two Aerodrome Slipstream deployments coexist on Base; a tokenId belongs to
# exactly one. Resolve which by trying positions(tokenId) on each NPM (the
# non-owning deployment reverts). v2 is the original CL (our cbbtc-200 position
# lives here, whitelisted); v3 is the newer "Gauges V3" deployment.
AERODROME_NPM_V2 = "0x827922686190790b37229fd06084350E74485b72"  # == AERODROME_NPM
AERODROME_NPM_V3 = "0xe1f8cd9AC4e4A65F54f38a5CdAfCA44f6dD68b53"
# Single shared emitter for the 4 order lifecycle/execution events (since the
# 2026-06-16 migration). Filter getLogs by topics=[topic0, padded(primeAccount)].
REBALANCE_EVENT_EMITTER = "0x74a1b3715DD3dcB565c7483551b4C67F8FF3E3dc"
# topic0 (event signature hashes) — precomputed in the feature doc §4.
REBALANCE_TOPIC0 = {
    "RebalanceOrderCreated":  "0x4cb21e6335aff9c3dc194e8ba569d80a836ff7e85c8ac6e00b29c60dcabd203f",
    "RebalanceOrderUpdated":  "0xcf110733c3d79243454a78611f39c969cfad28c677c730f2411cb6b944685a74",
    "RebalanceOrderCanceled": "0xac877909cb8982d2cbc4ba584659922771e99a7d68777b68fe8c46bdaa5144d9",
    "RebalanceExecuted":      "0xc314b9b8c9e55873144ca3fd31788bbb05b0a65949a007bf5c0b875ccfb2dae2",
}

# Whitelisted Aerodrome CL pools exposed as tool keys — the authoritative set of
# ~31 DegenPrime-supported SlipStream pools. Every entry was verified on-chain
# (2026-06-13): token0/token1/tickSpacing read from the pool, decimals/symbol from
# each token, and the pool address cross-checked against the SlipStream factory's
# getPool(token0, token1, tickSpacing) (0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A).
# token0/token1 follow the pool's canonical on-chain ordering; the key is a curated
# label (symbol0 stores WETH as "ETH" to match the lending-pool convention, so WETH
# pairs are keyed "weth-*").
# tickSpacing tiers in use: 1, 50, 100, 200, 2000. gauge_alive=False marks pools
# whose Aerodrome gauge is dead (no AERO emissions; still tradeable/LP-able).
AERODROME_POOLS = {
    "weth-usdc-100":    {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "tickSpacing": 100, "symbol0": "ETH", "symbol1": "USDC",
                         "decimals0": 18, "decimals1": 6, "gauge_alive": True},
    "weth-cbbtc-100":   {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
                         "tickSpacing": 100, "symbol0": "ETH", "symbol1": "cbBTC",
                         "decimals0": 18, "decimals1": 8, "gauge_alive": True},
    "weth-aero-200":    {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
                         "tickSpacing": 200, "symbol0": "ETH", "symbol1": "AERO",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": False},
    "aero-usdc-2000":   {"token0": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "token1": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
                         "tickSpacing": 2000, "symbol0": "USDC", "symbol1": "AERO",
                         "decimals0": 6, "decimals1": 18, "gauge_alive": True},
    "aero-cbbtc-200":   {"token0": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
                         "token1": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
                         "tickSpacing": 200, "symbol0": "AERO", "symbol1": "cbBTC",
                         "decimals0": 18, "decimals1": 8, "gauge_alive": True},
    "avnt-usdc-1":      {"token0": "0x696F9436B67233384889472Cd7cD58A6fB5DF4f1",
                         "token1": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "tickSpacing": 1, "symbol0": "AVNT", "symbol1": "USDC",
                         "decimals0": 18, "decimals1": 6, "gauge_alive": True},
    "avnt-usdc-200":    {"token0": "0x696F9436B67233384889472Cd7cD58A6fB5DF4f1",
                         "token1": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "tickSpacing": 200, "symbol0": "AVNT", "symbol1": "USDC",
                         "decimals0": 18, "decimals1": 6, "gauge_alive": True},
    "cbbtc-lbtc-1":     {"token0": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
                         "token1": "0xecAc9C5F704e954931349Da37F60E39f515c11c1",
                         "tickSpacing": 1, "symbol0": "cbBTC", "symbol1": "LBTC",
                         "decimals0": 8, "decimals1": 8, "gauge_alive": True},
    "cbbtc-cbdoge-100": {"token0": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
                         "token1": "0xcbD06E5A2B0C65597161de254AA074E489dEb510",
                         "tickSpacing": 100, "symbol0": "cbBTC", "symbol1": "cbDOGE",
                         "decimals0": 8, "decimals1": 8, "gauge_alive": False},
    "clanker-weth-200": {"token0": "0x1bc0c42215582d5A085795f4baDbaC3ff36d1Bcb",
                         "token1": "0x4200000000000000000000000000000000000006",
                         "tickSpacing": 200, "symbol0": "CLANKER", "symbol1": "ETH",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "weth-aixbt-200":   {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0x4F9Fd6Be4a90f2620860d680c0d4d5Fb53d1A825",
                         "tickSpacing": 200, "symbol0": "ETH", "symbol1": "AIXBT",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "weth-brett-200":   {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0x532f27101965dd16442E59d40670FaF5eBB142E4",
                         "tickSpacing": 200, "symbol0": "ETH", "symbol1": "BRETT",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "weth-degen-200":   {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
                         "tickSpacing": 200, "symbol0": "ETH", "symbol1": "DEGEN",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "weth-euroc-100":   {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
                         "tickSpacing": 100, "symbol0": "ETH", "symbol1": "EURC",
                         "decimals0": 18, "decimals1": 6, "gauge_alive": False},
    "weth-kaito-100":   {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0x98d0baa52b2D063E780DE12F615f963Fe8537553",
                         "tickSpacing": 100, "symbol0": "ETH", "symbol1": "KAITO",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "weth-spx-200":     {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0x50dA645f148798F68EF2d7dB7C1CB22A6819bb2C",
                         "tickSpacing": 200, "symbol0": "ETH", "symbol1": "SPX",
                         "decimals0": 18, "decimals1": 8, "gauge_alive": True},
    "weth-toshi-200":   {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4",
                         "tickSpacing": 200, "symbol0": "ETH", "symbol1": "TOSHI",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "weth-cbdoge-2000": {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0xcbD06E5A2B0C65597161de254AA074E489dEb510",
                         "tickSpacing": 2000, "symbol0": "ETH", "symbol1": "cbDOGE",
                         "decimals0": 18, "decimals1": 8, "gauge_alive": True},
    "weth-cbltc-200":   {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0xcb17C9Db87B595717C857a08468793f5bAb6445F",
                         "tickSpacing": 200, "symbol0": "ETH", "symbol1": "cbLTC",
                         "decimals0": 18, "decimals1": 8, "gauge_alive": True},
    "weth-cbxrp-2000":  {"token0": "0x4200000000000000000000000000000000000006",
                         "token1": "0xcb585250f852C6c6bf90434AB21A00f02833a4af",
                         "tickSpacing": 2000, "symbol0": "ETH", "symbol1": "cbXRP",
                         "decimals0": 18, "decimals1": 6, "gauge_alive": False},
    "euroc-usdc-1":     {"token0": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
                         "token1": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "tickSpacing": 1, "symbol0": "EURC", "symbol1": "USDC",
                         "decimals0": 6, "decimals1": 6, "gauge_alive": False},
    "euroc-usdc-50":    {"token0": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
                         "token1": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "tickSpacing": 50, "symbol0": "EURC", "symbol1": "USDC",
                         "decimals0": 6, "decimals1": 6, "gauge_alive": True},
    "usdc-cbbtc-100":   {"token0": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "token1": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
                         "tickSpacing": 100, "symbol0": "USDC", "symbol1": "cbBTC",
                         "decimals0": 6, "decimals1": 8, "gauge_alive": True},
    "usdc-usdt-1":      {"token0": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "token1": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
                         "tickSpacing": 1, "symbol0": "USDC", "symbol1": "USDT",
                         "decimals0": 6, "decimals1": 6, "gauge_alive": True},
    "virtual-weth-100": {"token0": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
                         "token1": "0x4200000000000000000000000000000000000006",
                         "tickSpacing": 100, "symbol0": "VIRTUAL", "symbol1": "ETH",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "virtual-weth-200": {"token0": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
                         "token1": "0x4200000000000000000000000000000000000006",
                         "tickSpacing": 200, "symbol0": "VIRTUAL", "symbol1": "ETH",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "zora-usdc-100":    {"token0": "0x1111111111166b7FE7bd91427724B487980aFc69",
                         "token1": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "tickSpacing": 100, "symbol0": "ZORA", "symbol1": "USDC",
                         "decimals0": 18, "decimals1": 6, "gauge_alive": True},
    "cbltc-cbbtc-100":  {"token0": "0xcb17C9Db87B595717C857a08468793f5bAb6445F",
                         "token1": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
                         "tickSpacing": 100, "symbol0": "cbLTC", "symbol1": "cbBTC",
                         "decimals0": 8, "decimals1": 8, "gauge_alive": True},
    "cbxrp-cbbtc-100":  {"token0": "0xcb585250f852C6c6bf90434AB21A00f02833a4af",
                         "token1": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
                         "tickSpacing": 100, "symbol0": "cbXRP", "symbol1": "cbBTC",
                         "decimals0": 6, "decimals1": 8, "gauge_alive": False},
    "weeth-weth-1":     {"token0": "0x04C0599Ae5A44757c0af6F9eC3b93da8976c150A",
                         "token1": "0x4200000000000000000000000000000000000006",
                         "tickSpacing": 1, "symbol0": "weETH", "symbol1": "ETH",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
    "ezeth-weth-1":     {"token0": "0x2416092f143378750bb29b79eD961ab195CcEea5",
                         "token1": "0x4200000000000000000000000000000000000006",
                         "tickSpacing": 1, "symbol0": "ezETH", "symbol1": "ETH",
                         "decimals0": 18, "decimals1": 18, "gauge_alive": True},
}

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
    "0x8faa0000c10015610005ca010ee000d006e0e820",
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
    "USDC", "ETH", "cbBTC", "AERO", "BRETT", "KAITO", "DEGEN",
    "EUROC", "USDT", "LBTC", "cbLTC", "cbDOGE", "cbXRP",
}

_abi_cache = {}
_impl_cache = {}
# Cache for TokenManager symbol/decimals lookups (the in-account view shows non-pool
# collateral too; symbol+decimals reads are pure but cheap to memoise).
_asset_meta_cache = {}

# Process-local Web3 singleton. Avoids reconstructing the HTTPProvider per command —
# cmd_pool_info("all") and gather_defi (when added) make many sequential pool reads.
_W3 = None

def get_w3():
    """Process-local Base RPC client. Base has no POA middleware - it's a standard EVM
    chain; middleware injection is not needed (and would error on Base block headers)."""
    global _W3
    if _W3 is None:
        _W3 = Web3(Web3.HTTPProvider(BASE_RPC))
    return _W3

def _tx_gas_price(w3) -> int:
    """Estimated per-gas cost for balance checks (gas buffer pre-flights). Returns 2x the
    current base fee with a 0.01 gwei floor. NOTE: _tx_gas_price is NOT used for tx building;
    use _set_gas_price() for that (EIP-1559 on Base, with maxFeePerGas + maxPriorityFeePerGas)."""
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
        tx_dict["maxFeePerGas"] = max(int(base * 2), base + prio + 10_000_000)
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
    """Resolve the signing key per the documented precedence:
       1. --key <0xhex> CLI flag
       2. --as <agent> CLI flag
       3. DEGENPRIME_PRIVATE_KEY env var
       4. DEGENPRIME_KEY_FILE env var (path to a file containing the 0x key)
       5. DELTAPRIME_PRIVATE_KEY / DELTAPRIME_KEY_FILE (same key, both chains)
       6. DEGENPRIME_AGENT env var
       7. DELTAPRIME_AGENT env var (fallback)
    Raises with a clear message if none resolve."""
    if _CLI_KEY:
        return _CLI_KEY.strip()
    if _SELECTED_AGENT:
        return _agent_key(_SELECTED_AGENT)
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
    # Named agent via env var
    for ag in ("DEGENPRIME_AGENT", "DELTAPRIME_AGENT"):
        agent = os.environ.get(ag)
        if agent:
            return _agent_key(agent)
    raise RuntimeError(
        "No signing key found. Set DEGENPRIME_PRIVATE_KEY (raw 0x... key) or "
        "DEGENPRIME_KEY_FILE (path to a file with the key), or pass --key <0xhex>. "
        "DELTAPRIME_PRIVATE_KEY / DELTAPRIME_KEY_FILE also work (same key, both chains)."
    )

def get_account() -> Account:
    return Account.from_key(resolve_private_key())

def to_wei_units(amount, decimals):
    """Convert a human amount to integer base units without float drift."""
    return int(Decimal(str(amount)) * (10 ** int(decimals)))

# Basescan's v1 API is deprecated (returns "switch to Etherscan API V2" since 2026),
# and the v2 multichain endpoint (api.etherscan.io/v2/api?chainid=8453) requires an API
# key with no anonymous reads. Rather than depend on an API key for what is a tiny,
# stable ABI surface, we hand-curate the Pool + Factory ABIs below and resolve proxy
# implementations directly via the EIP-1967 storage slot.
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

# Pool ABI - minimum surface for lending pool ops. DegenPrime pools share the
# DeltaPrime Pool implementation: deposit, the intent-gated two-arg withdraw, rate views,
# borrow accounting, and ERC20 receipt-token reads. The matured-intent executor is
# withdraw(uint256 _amount, uint256[] intentIndices) (selector 0x5915d806) — the single-arg
# withdraw(uint256) and instantWithdraw(uint256) do NOT resolve a named lender intent (they
# revert without reaching the intent lookup). Verified on-chain on Base/Avalanche pools
# 2026-06-02. See _encode_pool_withdraw + cmd_execute_pool_withdrawal.
POOL_ABI = json.loads(
    '['
    '{"inputs":[{"name":"_amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[],"name":"depositNativeToken","outputs":[],"stateMutability":"payable","type":"function"},'
    '{"inputs":[{"name":"_amount","type":"uint256"},{"name":"intentIndices","type":"uint256[]"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},'
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

# Multicall3 — deterministic deployment at the same address on every EVM chain
# (Avalanche C-chain and Base both have it). aggregate3(Call3[]) batches read-only
# calls into one eth_call; allowFailure=true per-call so a single revert returns
# success=false for that leg instead of blowing up the whole batch. Used to collapse
# per-pool / per-asset fan-out loops (cmd_pool_info("all"), cmd_summary, my-positions)
# from N RPCs into 1. Same address as DeltaPrime's; verified on chainlist.org and
# tested against the Base RPC.
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
    calldata_bytes) tuple. Returns a list of (success, return_bytes) tuples in input
    order — the caller is responsible for decoding return_bytes against the original
    function's output types and treating success=False as a missing/reverted value. Empty
    input returns []. The whole batch round-trips in one eth_call; gas is paid by the
    simulated caller (zero address by default) so no key is required.

    For RedStone-gated views: append the RedStone payload to each leg's calldata before
    putting it in `calls`. Multicall3 only delegate-calls the target with the bytes you
    provide; the on-chain solvency parser still reads the payload from the calldata tail
    per leg."""
    if not calls:
        return []
    mc = w3.eth.contract(address=Web3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI)
    args = [(Web3.to_checksum_address(t), True, d) for t, d in calls]
    raw = mc.functions.aggregate3(args).call()
    return [(bool(ok), bytes(rd)) for ok, rd in raw]

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
    # getPrices: 1e18-scaled USD prices for the given symbols. RedStone-gated, so a
    # payload is appended for the read. swap-debt uses it to value-match the borrow vs
    # repay leg against the facet's own 5% cap (the facet calls the same view internally).
    {"inputs": [{"name": "symbols", "type": "bytes32[]"}], "name": "getPrices",
     "outputs": [{"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
    # WithdrawalIntentFacet - universal time-locked collateral withdrawal on the Degen
    # Account. On the Account, createWithdrawalIntent IS RedStone-gated (on-chain solvency
    # check at create); cancelWithdrawalIntent is oracle-free; executeWithdrawalIntent is
    # RedStone-gated. IntentInfo's isActionable/isExpired flags make the 24h-72h window
    # (actionable at created+24h, expires at actionableAt+48h) readable on-chain.
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
    # Aerodrome facet (Facet 14 @ 0x3c0ddb23). Selectors extracted via DiamondLoupe
    # on-chain probing 2026-06-05; function names inferred from parameter layouts and
    # revert signatures. All write paths carry remainsSolvent or onlyOwner —
    # RedStone-gated on --execute.
    {"inputs": [], "name": "getOwnedStakedAerodromeTokenIds",
     "outputs": [{"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
    # getPositionCompositionSimplified returns (address token0, address token1,
    # uint256 tickData, uint256 liquidity) — tickData packs tickLower & tickUpper.
    # Return type was verified via raw eth_call decode 2026-06-05.
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "getPositionCompositionSimplified",
     "outputs": [{"name": "token0", "type": "address"},
                  {"name": "token1", "type": "address"},
                  {"name": "tickData", "type": "uint256"},
                  {"name": "liquidity", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    # Write functions — using raw selectors with inferred parameter layouts.
    # These take the same struct types as the Aerodrome NonfungiblePositionManager
    # but are wrapped by the facet for solvency checks and owner validation.
    #
    # mintAerodrome / addLiquidityAerodrome: wraps NPM.mint(MintParams).
    # MintParams = (address token0, address token1, int24 tickSpacing,
    #   int24 tickLower, int24 tickUpper, uint256 amount0Desired,
    #   uint256 amount1Desired, uint256 amount0Min, uint256 amount1Min,
    #   address recipient, uint256 deadline, uint160 sqrtPriceX96).
    # Selector: 0x27bed82e (probed — onlyOwner, accepts MintParams-like encoding).
    {"inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "token0", "type": "address"},
        {"name": "token1", "type": "address"},
        {"name": "tickSpacing", "type": "int24"},
        {"name": "tickLower", "type": "int24"},
        {"name": "tickUpper", "type": "int24"},
        {"name": "amount0Desired", "type": "uint256"},
        {"name": "amount1Desired", "type": "uint256"},
        {"name": "amount0Min", "type": "uint256"},
        {"name": "amount1Min", "type": "uint256"},
        {"name": "recipient", "type": "address"},
        {"name": "deadline", "type": "uint256"},
        {"name": "sqrtPriceX96", "type": "uint160"}
    ]}],
     "name": "mintAerodrome", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    # burnAerodromePosition: wraps NPM.burn(uint256). tokenId must have 0 liquidity
    # and all fees collected.
    # Selector: 0x92b5a47e (probed — takes uint256, checks position exists via NPM.positions).
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "burnAerodromePosition", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    # collectAerodromeFees: also uses selector 0x92b5a47e — same as burn when the
    # facet distinguishes by internal logic. For the tool we expose a dedicated path.
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "collectAerodromeFees", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    # batchRemoveStakedLiquidityAerodrome(uint256[]) — selector 0x27bed82e
    # (== AERODROME_SEL_BURN, verified). Unstakes + closes the listed positions.
    {"inputs":[{"internalType":"uint256[]","name":"tokenIds","type":"uint256[]"}],"name":"batchRemoveStakedLiquidityAerodrome","outputs":[],"stateMutability":"nonpayable","type":"function"},
    # AerodromeRebalancerFacet — on-chain auto-rebalancer attached to a CL position.
    # The two getRebalanceOrder* views are PLAIN storage reads (no oracle data);
    # shouldRebalance is price-consuming -> RedStone-gated (wrap via redstone_view_call).
    # createRebalanceOrder/updateRebalanceOrder take CreateRebalanceOrderParams (the
    # struct WITHOUT referenceSqrtPriceX96/createdOn — those are set by the contract);
    # cancelRebalanceOrder takes a bare tokenId. Selectors confirmed live 2026-06-16
    # (getAllRebalanceOrders 0x8d6c1fef returns a clean array on a real account).
    {"inputs": [], "name": "getAllRebalanceOrders",
     "outputs": [{"name": "", "type": "tuple[]", "components": [
         {"name": "tokenId", "type": "uint256"},
         {"name": "order", "type": "tuple", "components": [
             {"name": "referenceSqrtPriceX96", "type": "uint160"},
             {"name": "lowerRangePercentageBps", "type": "int24"},
             {"name": "upperRangePercentageBps", "type": "int24"},
             {"name": "lowerTriggerPercentageBps", "type": "int24"},
             {"name": "upperTriggerPercentageBps", "type": "int24"},
             {"name": "mintSlippageBps", "type": "uint256"},
             {"name": "swapSlippageBps", "type": "uint256"},
             {"name": "createdOn", "type": "uint256"},
             {"name": "maxExecutionFee", "type": "uint256"}]}]}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "getRebalanceOrder",
     "outputs": [{"name": "", "type": "tuple", "components": [
         {"name": "referenceSqrtPriceX96", "type": "uint160"},
         {"name": "lowerRangePercentageBps", "type": "int24"},
         {"name": "upperRangePercentageBps", "type": "int24"},
         {"name": "lowerTriggerPercentageBps", "type": "int24"},
         {"name": "upperTriggerPercentageBps", "type": "int24"},
         {"name": "mintSlippageBps", "type": "uint256"},
         {"name": "swapSlippageBps", "type": "uint256"},
         {"name": "createdOn", "type": "uint256"},
         {"name": "maxExecutionFee", "type": "uint256"}]}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "shouldRebalance",
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenId", "type": "uint256"},
        {"name": "lowerRangePercentageBps", "type": "int24"},
        {"name": "upperRangePercentageBps", "type": "int24"},
        {"name": "lowerTriggerPercentageBps", "type": "int24"},
        {"name": "upperTriggerPercentageBps", "type": "int24"},
        {"name": "mintSlippageBps", "type": "uint256"},
        {"name": "swapSlippageBps", "type": "uint256"},
        {"name": "executionFeeWeth", "type": "uint256"}]}],
     "name": "createRebalanceOrder", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenId", "type": "uint256"},
        {"name": "lowerRangePercentageBps", "type": "int24"},
        {"name": "upperRangePercentageBps", "type": "int24"},
        {"name": "lowerTriggerPercentageBps", "type": "int24"},
        {"name": "upperTriggerPercentageBps", "type": "int24"},
        {"name": "mintSlippageBps", "type": "uint256"},
        {"name": "swapSlippageBps", "type": "uint256"},
        {"name": "executionFeeWeth", "type": "uint256"}]}],
     "name": "updateRebalanceOrder", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "cancelRebalanceOrder",
     "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

# TokenManager ABI - minimal subset for symbol/decimals lookups + supported tokens
# enumeration. tokenAddressToSymbol returns the bytes32 the diamond uses for the
# in-account view; getSupportedTokensAddresses lists all 32 collateral assets.
TOKEN_MANAGER_ABI = [
    {"inputs": [{"name": "_asset", "type": "bytes32"}], "name": "getPoolAddress",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_address", "type": "address"}], "name": "tokenAddressToSymbol",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_symbol", "type": "bytes32"},
                {"name": "_allowInactive", "type": "bool"}], "name": "getAssetAddress",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getSupportedTokensAddresses",
     "outputs": [{"type": "address[]"}], "stateMutability": "view", "type": "function"},
    # Per-rebalance protocol fee FLOOR (WETH). createRebalanceOrder reverts
    # InsufficientExecutionFee unless executionFeeWeth exceeds this. 0 as of
    # 2026-06-16 but governance-settable, so read it live each run.
    {"inputs": [], "name": "getAutomationProtocolFee",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# Minimal ERC20 ABI - balanceOf + approve + decimals. Used for wallet token reads,
# pool deposits, and TokenManager-discovered non-pool collateral metadata.
ERC20_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},'
    '{"constant":true,"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"}]'
)

# Aerodrome Slipstream NonfungiblePositionManager ABI (Uniswap V3 compatible).
# Source: aerodrome-finance/slipstream INonfungiblePositionManager.sol.
# The facet functions on the Degen Account diamond wrap these NPM calls with
# solvency checks (remainsSolvent) and owner validation.
AERODROME_NPM_ABI = [
    # ── Read ──
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "positions",
     "outputs": [
         {"name": "nonce", "type": "uint96"},
         {"name": "operator", "type": "address"},
         {"name": "token0", "type": "address"},
         {"name": "token1", "type": "address"},
         {"name": "tickSpacing", "type": "int24"},
         {"name": "tickLower", "type": "int24"},
         {"name": "tickUpper", "type": "int24"},
         {"name": "liquidity", "type": "uint128"},
         {"name": "feeGrowthInside0LastX128", "type": "uint256"},
         {"name": "feeGrowthInside1LastX128", "type": "uint256"},
         {"name": "tokensOwed0", "type": "uint128"},
         {"name": "tokensOwed1", "type": "uint128"}
     ], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "ownerOf",
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    # ── Write ──
    {"inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "token0", "type": "address"},
        {"name": "token1", "type": "address"},
        {"name": "tickSpacing", "type": "int24"},
        {"name": "tickLower", "type": "int24"},
        {"name": "tickUpper", "type": "int24"},
        {"name": "amount0Desired", "type": "uint256"},
        {"name": "amount1Desired", "type": "uint256"},
        {"name": "amount0Min", "type": "uint256"},
        {"name": "amount1Min", "type": "uint256"},
        {"name": "recipient", "type": "address"},
        {"name": "deadline", "type": "uint256"},
        {"name": "sqrtPriceX96", "type": "uint160"}
    ]}],
     "name": "mint", "outputs": [{"name": "tokenId", "type": "uint256"},
                                   {"name": "liquidity", "type": "uint128"},
                                   {"name": "amount0", "type": "uint256"},
                                   {"name": "amount1", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenId", "type": "uint256"},
        {"name": "amount0Desired", "type": "uint256"},
        {"name": "amount1Desired", "type": "uint256"},
        {"name": "amount0Min", "type": "uint256"},
        {"name": "amount1Min", "type": "uint256"},
        {"name": "deadline", "type": "uint256"}
    ]}],
     "name": "increaseLiquidity", "outputs": [{"name": "liquidity", "type": "uint128"},
                                                {"name": "amount0", "type": "uint256"},
                                                {"name": "amount1", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenId", "type": "uint256"},
        {"name": "liquidity", "type": "uint128"},
        {"name": "amount0Min", "type": "uint256"},
        {"name": "amount1Min", "type": "uint256"},
        {"name": "deadline", "type": "uint256"}
    ]}],
     "name": "decreaseLiquidity", "outputs": [{"name": "amount0", "type": "uint256"},
                                                {"name": "amount1", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenId", "type": "uint256"},
        {"name": "recipient", "type": "address"},
        {"name": "amount0Max", "type": "uint128"},
        {"name": "amount1Max", "type": "uint128"}
    ]}],
     "name": "collect", "outputs": [{"name": "amount0", "type": "uint256"},
                                      {"name": "amount1", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "burn",
     "outputs": [], "stateMutability": "payable", "type": "function"},
]

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

def fmt_token_amount(raw: int, decimals: int) -> str:
    """Human token amount that never misleadingly rounds a dust balance UP.

    Plain f"{x:,.6f}" turns 9.41e-7 into "0.000001" — visually a larger,
    round number than the true value, which is exactly what over-requested a
    dust balance and reverted a mint after burning gas (2026-06-14). For any
    nonzero value that 6-dp formatting would round to zero or up to its own
    last place, append the raw base units so the real size is unambiguous.
    Normal/large balances keep the familiar grouped 6-dp display."""
    if raw == 0:
        return "0"
    amt = Decimal(raw) / (Decimal(10) ** int(decimals))
    rounded6 = amt.quantize(Decimal("0.000001"))
    # Switch to sci-notation + raw wei only for genuinely small balances that 6-dp
    # formatting would misrepresent: rounds to 0 (looks like nothing), or rounds UP
    # below the 1e-4 dust line (e.g. 9.41e-7 -> 0.000001, the over-request trap).
    # Larger values keep the familiar grouped 6-dp display even if the last place
    # rounds — there a round-up isn't misleading and sci-notation would be noise.
    if rounded6 == 0 or (rounded6 > amt and amt < Decimal("0.0001")):
        return f"{amt:.3e} ({raw} wei)"
    return f"{amt:,.6f}"

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
        addr = tm.functions.getAssetAddress(asset_b32(symbol), True).call()
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


def _compute_health_pct(supplied: list, borrowed: list, w3=None, tier_code: int = 0) -> dict:
    """Frontend-exact health (0-100%) for a Degen Account — wraps _health_meter_pct with
    per-asset on-chain debtCoverage. 0% = liquidation, 100% = no debt.

    DegenPrime renders getHealthMeter (HealthMeterFacetProd) just like DeltaPrime, but Base
    has no PRIME leverage tier, so debtCoverage is the un-tiered TokenManager value (the
    tiered getter reverts and _resolve_debt_coverages falls back to it automatically).

    supplied/borrowed are rows carrying `usd` (and optionally a pre-resolved `dc`); pass `w3`
    so dc can be read live when a row lacks it. Returns health_pct, supplied_usd, debt_usd,
    equity, max_debt (display zero-crossing debt), tier, or error.
    """
    syms = list(dict.fromkeys([r["symbol"] for r in supplied + borrowed if r.get("symbol")]))
    dc_map = {r["symbol"]: r["dc"] for r in supplied + borrowed if r.get("dc") is not None}
    need = [s for s in syms if s not in dc_map]
    if need and w3 is not None:
        dc_map.update(_resolve_debt_coverages(w3, need, tier_code))
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
                "max_debt": 0.0, "tier": "FIXED", "error": "equity near zero"}
    coll_usd = sum(a["supplied_usd"] for a in assets) or 0.0
    dc_eff = (sum(a["dc"] * a["supplied_usd"] for a in assets) / coll_usd) if coll_usd > 0 else 0.0
    max_debt = equity * dc_eff / (1.0 - dc_eff) if 0 < dc_eff < 1 else 0.0
    return {"health_pct": res["health_pct"], "supplied_usd": res["supplied_usd"],
            "debt_usd": res["debt_usd"], "equity": equity,
            "max_debt": round(max_debt, 2), "tier": "FIXED"}


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

def _redstone_data_feed_id(sym: str) -> str:
    """Map a DegenPrime token symbol to its RedStone gateway dataFeedId.

    The Base SolvencyFacet strips the "cb" prefix from dataFeedIds when matching
    against RedStone packages. So for cb-prefixed tokens (cbBTC, cbXRP, cbDOGE),
    the correct gateway dataFeedId is the stripped symbol (BTC, XRP, DOGE).

    Returns the mapped feed ID, or the original symbol if no mapping applies."""
    if sym.startswith("cb") and len(sym) > 2:
        stripped = sym[2:]
        try:
            gw = _redstone_fetch_packages()
            if stripped in gw:
                return stripped
        except Exception:
            pass
    return sym

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
        mapped = _redstone_data_feed_id(sym)
        feed_packages = gateway.get(mapped)
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
    # RedStone v0.9 format: signed metadata (timestamp, version, dataServiceId)
    # Format from Bruno's working tx: threshold byte is the first digit of the timestamp,
    # then the rest of the metadata follows without the initial digit.
    ts_ms = 0
    for sym in symbols:
        mapped = _redstone_data_feed_id(sym)
        feed_packages = gateway.get(mapped)
        if feed_packages:
            ts_ms = feed_packages[0].get("timestampMilliseconds", 0)
            if ts_ms:
                break
    if not ts_ms:
        ts_ms = int(time.time() * 1000)
    ts_str = str(ts_ms)
    # Threshold byte = first digit of timestamp (as ASCII)
    payload += bytes([ord(ts_str[0])])
    # Metadata = rest of timestamp + version + data service ID + null terminator
    signed_metadata = f"{ts_str[1:]}#0.9.0#{REDSTONE_DATA_SERVICE}\0".encode()
    payload += signed_metadata
    # Unsigned metadata size = padding(3) + ts_digit(1) + signed_metadata
    unsigned_meta_size = len(signed_metadata) + 4
    payload += unsigned_meta_size.to_bytes(3, "big")
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
        if sym and sym in REDSTONE_AVAILABLE_FEEDS:
            feeds.append(sym)
    for name, _debt in account.functions.getDebts().call():
        sym = name.rstrip(b"\x00").decode(errors="replace")
        if sym and sym in REDSTONE_AVAILABLE_FEEDS:
            feeds.append(sym)
    return list(dict.fromkeys(feeds))

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

def _pool_info_data(pool_name: str) -> dict:
    """Read every pool-info field for one pool in a SINGLE Multicall3 eth_call:
    totalSupply, totalBorrowed, getDepositRate, getBorrowingRate, and (when a signer is
    configured) the EOA's pool balance. Returns the raw + decoded values plus the
    off-chain KuCoin USD price. Shared by the human-facing print path and the --json
    path. cb-prefixed pool symbols fall back to their bare ticker for the KuCoin probe
    (cbBTC -> BTC, cbDOGE -> DOGE, etc.)."""
    contract, cfg, w3 = get_pool_contract(pool_name)
    proxy_cs = Web3.to_checksum_address(cfg["proxy"])
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
    price_sym = cfg["symbol"].replace("cb", "") if cfg["symbol"].startswith("cb") else cfg["symbol"]
    price = token_price(price_sym)
    return {"name": pool_name, "cfg": cfg, "signer": signer_addr,
            "raw": decoded, "price": price}


def _compact_num(value: float, places: int = 2) -> float:
    """Round to `places` decimal places, defaulting to 2. Compact enough for an LLM
    consumer without lying about the underlying number. Used for amount/USD fields in
    pool-info --json."""
    if value is None:
        return None
    return round(float(value), places)


def _pool_json_shape(data: dict) -> dict:
    """Per-pool JSON object for `pool-info --json`. Same key names as deltaprime's
    _pool_json_shape so an agent can consume both chains uniformly. Numbers are floats
    rounded to 2 dp (amounts/USD/rates/utilization); proxy/token are full checksum
    strings. Null-ish fields are omitted (no tokenPrice/tvl when KuCoin lookup fails,
    no myDeposit without a key or with zero balance)."""
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
    # That lets a downstream consumer tell "this pool's read failed" from "this
    # pool is at literally 0".
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
    """Print pool supply / borrow / utilization / APR / TVL for one pool or all.

    Human-facing output (default) is unchanged. With --json: emits a single JSON object
    for a named pool, or a {pool_name: {...}} dict for `all`. JSON shape matches
    deltaprime so an agent can consume both chains uniformly. Numbers are floats (no
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
    # getDepositRate / getBorrowingRate are 1e18-scaled annualised rates.
    dr_raw, br_raw = raw.get("getDepositRate"), raw.get("getBorrowingRate")
    if dr_raw is not None and br_raw is not None:
        dr = dr_raw / 1e18 * 100
        br = br_raw / 1e18 * 100
        print(f"  Deposit APR:    {dr:>14.2f}%")
        print(f"  Borrow APR:     {br:>14.2f}%")
    if price:
        print(f"  Token Price:    ${price:>13,.2f}")
        print(f"  TVL:            ${ts / 10**d * price:>13,.2f}")

    # Show the signer's pool deposit when a key is configured; the balanceOf leg is
    # read in the same multicall batch above — we just print it here.
    my_bal = raw.get("balanceOf")
    if my_bal is not None and my_bal > 0:
        print(f"  My Deposit:     {my_bal / 10**d:.4f} {cfg['symbol']}")

def cmd_my_positions():
    acct = get_account()
    w3 = get_w3()
    # The Wallet: line MUST always print so the operator can verify the resolved
    # signer address even when every other line is suppressed.
    print(f"Wallet: {acct.address}")

    # Wallet ETH (native Base asset, used for gas). Suppress when below dust so a clean
    # readout doesn't carry a noisy `ETH: 0.000000` line.
    eth = w3.eth.get_balance(acct.address) / 1e18
    if eth >= 1e-9:
        print(f"ETH: {eth:.6f}")

    # Batch every per-pool read into ONE Multicall3 eth_call: for each pool, the wallet
    # ERC20 balanceOf + the pool balanceOf (the EOA's deposit) + the pool getBorrowed.
    # Previously 3 RPCs per pool × 8 pools = 24 sequential round-trips; now 1 round-trip
    # regardless of pool count.
    legs = []
    pool_meta = []
    for name, cfg in POOLS.items():
        contract, _, _ = get_pool_contract(name)
        token_cs = Web3.to_checksum_address(cfg["token"])
        token = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
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

    try:
        # Show pending pool-side withdrawal intents
        import time
        intent_abi = [{"inputs":[{"name":"","type":"address"}],"name":"getUserIntents","outputs":[{"type":"tuple[]","components":[{"name":"amount","type":"uint256"},{"name":"actionableAt","type":"uint256"},{"name":"expiresAt","type":"uint256"},{"name":"isPending","type":"bool"},{"name":"isActionable","type":"bool"},{"name":"isExpired","type":"bool"}]}],"stateMutability":"view","type":"function"}]
        total_abi = [{"inputs":[{"name":"","type":"address"}],"name":"getTotalIntentAmount","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]
        had_pool_intents = False
        for name, cfg in POOLS.items():
            pool_cs = Web3.to_checksum_address(cfg["proxy"])
            try:
                tc = w3.eth.contract(address=pool_cs, abi=total_abi)
                total = tc.functions.getTotalIntentAmount(acct.address).call()
                if total > 0:
                    if not had_pool_intents:
                        print("\nPool Withdrawal Intents:")
                        had_pool_intents = True
                    ic = w3.eth.contract(address=pool_cs, abi=intent_abi)
                    intents = ic.functions.getUserIntents(acct.address).call()
                    dec = cfg["decimals"]
                    sym = cfg["symbol"]
                    print(f"  {name.upper()} pool: {total / 10**dec:.6f} {sym} locked in {len(intents)} intent(s)")
                    for j, intent in enumerate(intents):
                        act = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(intent[1]))
                        exp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(intent[2]))
                        state = "READY" if intent[4] else ("EXPIRED" if intent[5] else "pending")
                        print(f"    [{j}] {intent[0] / 10**dec:.6f} {sym} — {state} (actionable: {act}, expires: {exp})")
            except Exception:
                pass  # pool may not have intent views

        pa = get_prime_account(w3, acct.address)
        if pa:
            print(f"\nDegen Account: {pa}")
            pa_eth = w3.eth.get_balance(Web3.to_checksum_address(pa)) / 1e18
            if pa_eth >= 1e-9:
                print(f"  ETH balance: {pa_eth:.6f}")
        else:
            print("\nNo Degen Account yet. Create with: degenprime create-account --execute")
    except Exception as e:
        print(f"\nDegen Account lookup failed: {e}")

def cmd_deposit(pool_name: str, amount: float, execute: bool = False):
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    amount_wei = to_wei_units(amount, cfg["decimals"])
    print(f"Wallet: {acct.address}")

    if not execute:
        print(f"Preview: Deposit {amount} {cfg['symbol']} into {pool_name.upper()} pool")
        print("Run with --execute to broadcast")
        return

    if cfg["native"]:
        # Native ETH path: pool.deposit(amount) with msg.value == amount (the pool wraps
        # ETH -> WETH internally). Same pattern as DeltaPrime's wavax pool.
        dep_tx = contract.functions.deposit(amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 600000, "chainId": CHAIN_ID, "value": amount_wei,
        })
        receipt = _sign_and_send(w3, acct, dep_tx, f"Deposit {amount} {cfg['symbol']}", timeout=120, fallback_gas=600000)
    else:
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]), abi=ERC20_ABI)
        app_tx = token.functions.approve(Web3.to_checksum_address(cfg["proxy"]), amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000, "chainId": CHAIN_ID,
        })
        _set_gas_price(w3, app_tx)
        signed_app = acct.sign_transaction(app_tx)
        # Wait for approve to be mined before building the deposit tx (nonce race fix)
        app_hash = w3.eth.send_raw_transaction(signed_app.raw_transaction)
        w3.eth.wait_for_transaction_receipt(app_hash, timeout=120)

        dep_tx = contract.functions.deposit(amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 600000, "chainId": CHAIN_ID,
        })
        receipt = _sign_and_send(w3, acct, dep_tx, f"Deposit {amount} {cfg['symbol']}", timeout=120, fallback_gas=600000)
    ok = receipt["status"] == 1

def cmd_withdraw(pool_name: str, amount: float, execute: bool = False):
    """Pool-side (LENDER) withdraw — step 1 of a time-locked intent flow.
    DegenPrime time-locks ALL withdrawals (both pool-side and Degen Account). Step 1
    creates a WithdrawalIntent via createWithdrawalIntent (no RedStone). The pool
    ("diamond hands") becomes actionable 24h after creation and stays actionable for a
    further 24h (the pool re-anchors expiresAt to creation + 48h), so the execute window
    is 24h-48h after creation. Step 2 (execute-pool-withdrawal) consumes it via
    withdraw(uint256 _amount, uint256[] intentIndices) (selector 0x5915d806) — NOT
    instantWithdraw, which does not resolve a named intent.

    Always withdraws the wrapped token (WETH for the weth pool, not native ETH).
    The pool also exposes withdrawNativeToken — future --native flag could opt in."""
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    amount_wei = to_wei_units(amount, cfg["decimals"])
    print(f"Wallet: {acct.address}")

    if not execute:
        print(f"Preview: Request withdrawal of {amount} {cfg['symbol']} from {pool_name.upper()} pool")
        print("  Step 1: createWithdrawalIntent — registers the intent on-chain (no RedStone).")
        print("  Becomes actionable 24h after creation, then stays actionable for a further")
        print("  24h (expires at created+48h). Step 2 is execute-pool-withdrawal in that window.")
        print("  Use withdrawal-intents to track pending intents.")
        print("Run with --execute to broadcast (step 1 only — creates the intent).")
        return

    # Build createWithdrawalIntent(uint256) calldata
    tx = {
        "from": acct.address,
        "to": contract.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 600000,
        
        "chainId": CHAIN_ID,
        "data": "0x" + Web3.keccak(text="createWithdrawalIntent(uint256)")[:4].hex() +
                amount_wei.to_bytes(32, "big").hex(),
    }
    receipt = _sign_and_send(w3, acct, tx, f"Withdrawal intent {amount} {cfg['symbol']} from {pool_name.upper()}", fallback_gas=600000)
    ok = receipt["status"] == 1
    if ok:
        # Read the intent back to show timing
        import time
        intent_abi = [{"inputs":[{"name":"","type":"address"}],"name":"getUserIntents","outputs":[{"type":"tuple[]","components":[{"name":"amount","type":"uint256"},{"name":"actionableAt","type":"uint256"},{"name":"expiresAt","type":"uint256"},{"name":"isPending","type":"bool"},{"name":"isActionable","type":"bool"},{"name":"isExpired","type":"bool"}]}],"stateMutability":"view","type":"function"}]
        ic = w3.eth.contract(address=contract.address, abi=intent_abi)
        intents = ic.functions.getUserIntents(acct.address).call()
        if intents:
            last = intents[-1]
            act = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last[1]))
            exp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last[2]))
            print(f"  Intent index: {len(intents)-1}")
            print(f"  Actionable at: {act}")
            print(f"  Expires at:    {exp}")
            print(f"  After maturity, execute: degenprime execute-pool-withdrawal --pool {pool_name} --index {len(intents)-1} --execute")
            print(f"  Or cancel: degenprime withdraw --pool {pool_name} --amount {amount} --cancel --index {len(intents)-1} --execute")

def _encode_pool_withdraw(amount_wei: int, indices: list) -> str:
    """Calldata for the pool's intent-gated executor withdraw(uint256 _amount,
    uint256[] intentIndices) (selector 0x5915d806). Hand-encoded (uint256 head +
    dynamic uint256[] tail) so it never depends on instantWithdraw / single-arg
    withdraw, neither of which resolves a named lender intent."""
    selector = Web3.keccak(text="withdraw(uint256,uint256[])")[:4].hex()
    head = amount_wei.to_bytes(32, "big").hex()          # _amount
    head += (0x40).to_bytes(32, "big").hex()             # offset to the array (2 head words in)
    tail = len(indices).to_bytes(32, "big").hex()
    tail += b"".join(int(i).to_bytes(32, "big") for i in indices).hex()
    return "0x" + selector + head + tail


def cmd_execute_pool_withdrawal(pool_name: str, index: int, execute: bool = False):
    """Step 2 of pool-side (LENDER) withdrawal: consume a matured WithdrawalIntent via
    withdraw(uint256 _amount, uint256[] intentIndices) (selector 0x5915d806). The intent
    must be past its actionableAt timestamp (24h after creation) and before expiry (a
    further 24h on the pool — created+48h). Not RedStone-gated.

    NOT instantWithdraw: the pool also exposes instantWithdraw(uint256) and single-arg
    withdraw(uint256), but neither resolves a named lender intent — they revert without
    reaching the intent lookup. withdraw(_amount, [index]) reaches the maturity check
    (verified on-chain 2026-06-02). An eth_call simulation runs before broadcast and
    refuses to send on revert."""
    contract, cfg, w3 = get_pool_contract(pool_name)
    acct = get_account()
    print(f"Wallet: {acct.address}")

    # Check intent status first
    import time
    intent_abi = [{"inputs":[{"name":"","type":"address"}],"name":"getUserIntents","outputs":[{"type":"tuple[]","components":[{"name":"amount","type":"uint256"},{"name":"actionableAt","type":"uint256"},{"name":"expiresAt","type":"uint256"},{"name":"isPending","type":"bool"},{"name":"isActionable","type":"bool"},{"name":"isExpired","type":"bool"}]}],"stateMutability":"view","type":"function"}]
    ic = w3.eth.contract(address=contract.address, abi=intent_abi)
    intents = ic.functions.getUserIntents(acct.address).call()
    if index >= len(intents):
        print(f"Intent index {index} not found — only {len(intents)} intent(s) exist.")
        return
    intent = intents[index]
    amount_str = f"{intent[0] / 10**cfg['decimals']:.6f} {cfg['symbol']}"
    act_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(intent[1]))
    exp_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(intent[2]))
    print(f"Intent {index}: {amount_str}, actionable={act_str}, expires={exp_str}")
    if intent[5]:  # isExpired
        print("  ✗ Intent has expired. Use cancel-withdrawal to clear it.")
        return
    if not intent[4]:  # not isActionable
        print(f"  ✗ Not yet actionable. Wait until {act_str}.")
        return

    data = _encode_pool_withdraw(intent[0], [index])

    # Simulate before broadcasting. A passing eth_call here means the intent is matured,
    # non-expired, and the pool has the liquidity.
    try:
        w3.eth.call({"from": acct.address, "to": contract.address, "data": data})
    except Exception as e:
        print(f"  ✗ Simulation reverted — refusing to broadcast: {type(e).__name__}: {str(e)[:160]}")
        return

    if not execute:
        print(f"Preview: Execute withdrawal of {amount_str} via withdraw({intent[0]}, [{index}]) — simulation passed")
        print("Run with --execute to broadcast")
        return

    tx = {
        "from": acct.address,
        "to": contract.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 600000,
        
        "chainId": CHAIN_ID,
        "data": data,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Execute pool withdrawal {amount_str}", fallback_gas=600000)
    ok = receipt["status"] == 1

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
            amount_wei = to_wei_units(fund_amount, cfg["decimals"])
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
        amount_wei = to_wei_units(fund_amount, cfg["decimals"])
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]), abi=ERC20_ABI)
        app_tx = token.functions.approve(factory_cs, amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000, "chainId": CHAIN_ID,
        })
        _set_gas_price(w3, app_tx)
        signed_app = acct.sign_transaction(app_tx)
        # Wait for approve to be mined before calling createAndFundLoan (nonce race fix)
        app_hash = w3.eth.send_raw_transaction(signed_app.raw_transaction)
        w3.eth.wait_for_transaction_receipt(app_hash, timeout=120)

        tx = factory.functions.createAndFundLoan(asset_b32(symbol), amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 4000000, "chainId": CHAIN_ID,
        })
    else:
        tx = factory.functions.createLoan().build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 4000000, "chainId": CHAIN_ID,
        })
    label = "Create+fund Degen Account" if funding else "Create Degen Account"
    receipt = _sign_and_send(w3, acct, tx, label, fallback_gas=4000000)
    ok = receipt["status"] == 1
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
    amount_wei = to_wei_units(amount, cfg["decimals"])
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
        fund_tx = account.functions.depositNativeToken().build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3000000, "chainId": CHAIN_ID, "value": amount_wei,
        })
        receipt = _sign_and_send(w3, acct, fund_tx, f"Fund {amount} {symbol}", fallback_gas=3000000)
    else:
        token = w3.eth.contract(address=Web3.to_checksum_address(cfg["token"]), abi=ERC20_ABI)
        app_tx = token.functions.approve(pa_cs, amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000, "chainId": CHAIN_ID,
        })
        _set_gas_price(w3, app_tx)
        signed_app = acct.sign_transaction(app_tx)
        # Wait for approve to be mined before building the fund tx (nonce race fix)
        app_hash = w3.eth.send_raw_transaction(signed_app.raw_transaction)
        w3.eth.wait_for_transaction_receipt(app_hash, timeout=120)

        fund_tx = account.functions.fund(asset_b32(symbol), amount_wei).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3000000, "chainId": CHAIN_ID,
        })
        receipt = _sign_and_send(w3, acct, fund_tx, f"Fund {amount} {symbol}", fallback_gas=3000000)
    ok = receipt["status"] == 1
    return ok

def _prices_usd(w3, account, symbols: list, payload: bytes) -> dict:
    """Best-effort per-symbol USD price map via the RedStone-gated getPrices view
    (1e18-scaled). Reuses an already-built `payload`; returns {symbol: float}. Symbols
    without a RedStone feed are filtered out before the call - the SolvencyFacet
    reverts on a getPrices request for a symbol whose feed isn't in the payload."""
    syms = [s for s in dict.fromkeys(symbols) if s and s in REDSTONE_AVAILABLE_FEEDS]
    if not syms:
        return {}
    try:
        raw = redstone_view_call(w3, account, "getPrices", payload,
                                 args=[[asset_b32(s) for s in syms]])[0]
        return {s: raw[i] / 1e18 for i, s in enumerate(syms)}
    except Exception:
        return {}

def _gather_pool_deposits(w3, owner: str) -> list:
    """The EOA's 'Diamond Hands' lending-pool balances, read independently of the Degen
    Account via one Multicall3 (one balanceOf per pool). Surfaces even for a wallet with
    no Degen Account. Returns [{symbol, raw, decimals}, ...] for non-zero balances."""
    pool_deposits = []
    dep_legs, dep_meta = [], []
    for _pname, _pcfg in POOLS.items():
        try:
            _pc, _, _ = get_pool_contract(_pname)
        except Exception:
            continue
        _proxy_cs = Web3.to_checksum_address(_pcfg["proxy"])
        dep_legs.append((_proxy_cs, bytes.fromhex(_pc.encode_abi("balanceOf", args=[owner])[2:])))
        dep_meta.append(_pcfg)
    if dep_legs:
        try:
            dep_results = multicall(w3, dep_legs)
        except Exception:
            dep_results = [(False, b"")] * len(dep_legs)
        for _pcfg, (_ok, _rd) in zip(dep_meta, dep_results):
            _bal = w3.codec.decode(["uint256"], _rd)[0] if _ok and _rd else 0
            if _bal > 0:
                pool_deposits.append({"symbol": _pcfg["symbol"], "raw": _bal, "decimals": _pcfg["decimals"]})
    return pool_deposits


def _gather_account_state(w3, account, pool_deposits: list):
    """Read-only collateral / debt / RedStone-gated solvency for an existing Degen Account.
    Shared by `summary` and `defi`. Returns (pa_eth, supplied, borrowed, solvency) where
    supplied/borrowed are [{symbol, raw, decimals}, ...] and solvency carries
    total/debt/ratio/solvent/error/prices. pool_deposits is taken so their feeds get folded
    into the RedStone payload (else getPrices reverts on a deposit-only symbol).

    Multicall: stage A batches getAllOwnedAssets + getDebts (2 -> 1 RPC). Stage B batches
    one getBalance per owned asset (N -> 1 RPC). Stage C batches the four RedStone-gated
    solvency views + getPrices (4-5 -> 1 RPC), each leg carrying the same payload appended."""
    pa_cs = account.address
    pa_eth = w3.eth.get_balance(pa_cs) / 1e18
    stage_a_legs = [
        ("getAllOwnedAssets", ["bytes32[]"], account.encode_abi("getAllOwnedAssets", args=[])),
        ("getDebts", ["(bytes32,uint256)[]"], account.encode_abi("getDebts", args=[])),
    ]
    a_results = multicall(w3, [(pa_cs, bytes.fromhex(d[2:])) for _, _, d in stage_a_legs])
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
        supplied.append({"symbol": sym, "raw": bal, "decimals": _asset_decimals(w3, sym)})
    borrowed = []
    for n, v in debts_raw:
        sym = n.rstrip(b"\x00").decode(errors="replace")
        if v > 0:
            borrowed.append({"symbol": sym, "raw": v, "decimals": _asset_decimals(w3, sym)})
    # Per-asset on-chain debtCoverage (Base: un-tiered) stamped onto every row so the
    # frontend-exact getHealthMeter computation has its dc inputs.
    try:
        dc_map = _resolve_debt_coverages(w3, [r["symbol"] for r in supplied + borrowed])
        for r in supplied + borrowed:
            r["dc"] = dc_map.get(r["symbol"], 0.0)
    except Exception:
        pass

    # Solvency views (SolvencyFacet) are RedStone-gated: they revert (0xe7764c9e)
    # without signed price calldata appended. Fetch a fresh RedStone payload covering
    # every feed the solvency math touches (RedStone-feed symbols only - others come
    # from BaseOracle on-chain) and eth_call the views with it appended. No tx.
    # Each leg in the multicall carries the same payload — redundant on the wire,
    # but correct: the SolvencyFacet parses the payload from the calldata tail per leg.
    solvency = {"total": None, "debt": None, "ratio": None, "solvent": None, "error": None, "prices": {}}
    try:
        feeds = degen_account_price_feeds(account)
        # Pool-deposit assets need their feeds in the payload too, else getPrices reverts
        # on any deposit symbol whose feed the Degen Account doesn't already carry.
        for _dr in pool_deposits:
            if _dr["symbol"] in REDSTONE_AVAILABLE_FEEDS and _dr["symbol"] not in feeds:
                feeds.append(_dr["symbol"])
        payload = build_redstone_payload(feeds)
        payload_hex = payload.hex()
        price_syms = [s for s in dict.fromkeys(r["symbol"] for r in supplied + borrowed + pool_deposits)
                      if s and s in REDSTONE_AVAILABLE_FEEDS]
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
            solvency["total"] = decoded_solv["getTotalValue"] / 1e18
        if decoded_solv.get("getDebt") is not None:
            solvency["debt"] = decoded_solv["getDebt"] / 1e18
        if decoded_solv.get("getHealthRatio") is not None:
            ratio = decoded_solv["getHealthRatio"] / 1e18
            # With negligible debt the ratio is astronomically large (e.g. 1e59) - render
            # that as None and show ">1000" rather than a junk number.
            solvency["ratio"] = None if ratio > 1000 else ratio
        if decoded_solv.get("isSolvent") is not None:
            solvency["solvent"] = bool(decoded_solv["isSolvent"])
        prices = {}
        if price_syms and decoded_solv.get("getPrices") is not None:
            raw_prices = decoded_solv["getPrices"]
            for i, s in enumerate(price_syms):
                if i < len(raw_prices):
                    prices[s] = raw_prices[i] / 1e18
        solvency["prices"] = prices
    except Exception as e:
        solvency["error"] = type(e).__name__
    return pa_eth, supplied, borrowed, solvency


def _aero_gauge_earned(w3, degen_account: 'Web3.eth.Contract', token_id: int) -> float:
    """Query unclaimed AERO rewards from the Aerodrome gauge for a staked LP.

    Returns human-readable AERO amount, or 0.0 if the gauge cannot be read.
    The gauge address is discovered by checking who currently owns the NFT on the
    NPM (the gauge holds the NFT while staked).
    """
    try:
        npm = w3.eth.contract(address=Web3.to_checksum_address(AERODROME_NPM),
                              abi=AERODROME_NPM_ABI)
        gauge_addr = npm.functions.ownerOf(token_id).call()
        if gauge_addr == "0x0000000000000000000000000000000000000000":
            return 0.0
        # Standard Aerodrome gauge earned(address,uint256)
        gauge_abi = json.loads('[{"inputs":[{"name":"","type":"address"},{"name":"","type":"uint256"}],"name":"earned","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
        gauge = w3.eth.contract(address=Web3.to_checksum_address(gauge_addr), abi=gauge_abi)
        earned = gauge.functions.earned(degen_account.address, token_id).call()
        return earned / 10 ** 18  # AERO is 18 decimals
    except Exception:
        return 0.0


def _aero_position_legs(w3, account):
    """Decompose each staked Aerodrome Slipstream position the Degen Account owns into
    its underlying token legs (token0/token1 amounts) using the pool's live sqrtPrice.

    These staked CL NFTs are real collateral counted by getTotalValue, but the NFT
    belongs to the gauge so they never appear in getAllOwnedAssets — hence the wallet
    panel was blind to them. Returns a list of
        {token_id, sym0, sym1, dec0, dec1, amt0, amt1}
    with amt0/amt1 as human token amounts. Read-only; skips any position it can't read
    so one bad NFT never blanks the rest of the DeFi panel."""
    try:
        ids = account.functions.getOwnedStakedAerodromeTokenIds().call()
    except Exception:
        return []
    if not ids:
        return []
    npm = w3.eth.contract(address=Web3.to_checksum_address(AERODROME_NPM),
                          abi=AERODROME_NPM_ABI)
    slot0_abi = json.loads(SLOT0_ABI)
    q96 = Decimal(2) ** 96
    legs = []
    for tid in ids:
        try:
            p = npm.functions.positions(tid).call()
        except Exception:
            continue
        # positions(): nonce, operator, token0, token1, tickSpacing, tickLower,
        #              tickUpper, liquidity, ...
        token0, token1, tick_spacing = p[2], p[3], p[4]
        tick_lower, tick_upper, liq = p[5], p[6], p[7]
        if liq == 0:
            continue
        dec0 = _resolve_token_decimals(w3, token0)
        dec1 = _resolve_token_decimals(w3, token1)
        if dec0 is None or dec1 is None:
            continue
        sym0 = _resolve_token_symbol(w3, token0)
        sym1 = _resolve_token_symbol(w3, token1)
        # Current sqrtPriceX96 from the pool's slot0 (exact); fall back to the
        # geometric mean of the range bounds if the pool read fails.
        sqrt_p = None
        try:
            pool_addr = _aero_pool_address({"token0": token0, "token1": token1,
                                            "tickSpacing": tick_spacing})
            pool_c = w3.eth.contract(address=Web3.to_checksum_address(pool_addr),
                                     abi=slot0_abi)
            sqrt_p = Decimal(pool_c.functions.slot0().call()[0])
        except Exception:
            sqrt_p = None
        sqrt_a = (Decimal("1.0001") ** (Decimal(tick_lower) / 2)) * q96
        sqrt_b = (Decimal("1.0001") ** (Decimal(tick_upper) / 2)) * q96
        if sqrt_p is None or sqrt_p <= 0:
            sqrt_p = (sqrt_a * sqrt_b).sqrt()
        sp = min(max(sqrt_p, sqrt_a), sqrt_b)  # clamp current price into the range
        L = Decimal(liq)
        amt0_wei = L * q96 * (sqrt_b - sp) / (sp * sqrt_b) if sp > 0 else Decimal(0)
        amt1_wei = L * (sp - sqrt_a) / q96
        legs.append({
            "token_id": tid, "sym0": sym0, "sym1": sym1,
            "dec0": dec0, "dec1": dec1,
            "amt0": float(amt0_wei) / 10 ** dec0,
            "amt1": float(amt1_wei) / 10 ** dec1,
        })
    return legs


def _compute_degen_account_health(supplied_rows, borrowed_rows, aero_legs,
                                  get_total_value, w3=None):
    """Compute health_pct with LP gap fallback — shared by gather_defi & cmd_summary.

    supplied_rows/borrowed_rows: lists of dicts with at least {symbol, usd}.
    aero_legs: from _aero_position_legs (already injected into supplied_rows by
    the caller for real legs, but used here for the gap fallback when it's empty).
    get_total_value: on-chain getTotalValue in USD (SolvencyFacet).

    The getTotalValue gap fallback (Issue 3): when aero_legs is empty but
    getTotalValue exceeds priced supplied by >$1, inject a synthetic LP entry.
    This is the shared health computation used by both defi --json and
    summary --json (Issue 4 consistency guarantee)."""
    hp_rows = list(supplied_rows)
    hp_borrowed = list(borrowed_rows)

    # Gap fallback: _aero_position_legs might miss staked NFTs. When aero_legs
    # is empty/None but getTotalValue > priced_supplied, inject the gap.
    if not aero_legs and get_total_value is not None:
        priced_supplied = sum(r.get("usd", 0) or 0 for r in hp_rows)
        gap = get_total_value - priced_supplied
        if gap > 1.0:
            hp_rows.append({"symbol": "LP", "usd": round(gap, 2)})

    return _compute_health_pct(hp_rows, hp_borrowed, w3=w3)


def gather_defi() -> dict:
    """Aggregate ALL DegenPrime positions for the selected wallet into one DeBank-style dict,
    matching the cross-tool shape `deltaprime defi --json` emits. Read-only: reuses the same
    gather helpers as `summary` (lending/solvency via the RedStone-gated views, plus the EOA's
    own pool deposits surfaced as a Savings group). Empty groups are omitted. total_usd /
    health_ratio / solvent come from the RedStone-gated solvency views; per-asset USD is
    best-effort (omitted where a RedStone feed is missing). Never broadcasts."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    result = {
        "protocol": "DegenPrime", "url": "https://degenprime.io", "chain": "base",
        "wallet": acct.address, "prime_account": pa,
        "total_usd": None, "health_ratio": None, "solvent": None,
        "groups": [], "status": "ok",
    }

    pool_deposits = _gather_pool_deposits(w3, acct.address)
    # _gather_account_state folds pool-deposit symbols into getPrices, so this map covers
    # both in-account assets and Diamond-Hands deposits. Empty with no Degen Account.
    prices = {}

    if pa:
        account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
        _pa_eth, supplied, borrowed, solvency = _gather_account_state(w3, account, pool_deposits)
        prices = solvency["prices"]
        result["total_usd"] = solvency["total"]
        result["health_ratio"] = solvency["ratio"]
        result["solvent"] = solvency["solvent"]
        if solvency["error"]:
            result["solvency_error"] = solvency["error"]

        # Staked Aerodrome Slipstream LP positions are real collateral counted by
        # getTotalValue but absent from getAllOwnedAssets (the NFT belongs to the gauge).
        # Decompose each into its underlying token legs so the LP shows up, Net/Supplied
        # match getTotalValue, and the health calc sees the collateral instead of reading
        # negative equity. (Bruno, 2026-06-14 — wallet page was blind to the Aero pool.)
        aero_legs = _aero_position_legs(w3, account)

        # Back-solve a single unpriced collateral symbol from getTotalValue. getPrices
        # only returns RedStone-feed symbols; getTotalValue also values feed-less tokens
        # (e.g. ZORA via the BaseOracle TWAP). With exactly one unpriced symbol across all
        # collateral, its implied price is (getTotalValue - priced collateral) / its total
        # amount, which makes per-row USD sum back to getTotalValue. With zero or several
        # unpriced symbols, skip — those rows stay balance-only.
        price_map = dict(prices)
        coll_amounts = {}  # symbol -> total token amount across in-account + aero legs
        for r in supplied:
            coll_amounts[r["symbol"]] = coll_amounts.get(r["symbol"], 0.0) + r["raw"] / 10**r["decimals"]
        for lg in aero_legs:
            coll_amounts[lg["sym0"]] = coll_amounts.get(lg["sym0"], 0.0) + lg["amt0"]
            coll_amounts[lg["sym1"]] = coll_amounts.get(lg["sym1"], 0.0) + lg["amt1"]
        if result["total_usd"] is not None:
            priced_total = sum(amt * price_map[s] for s, amt in coll_amounts.items() if s in price_map)
            unpriced = {s: amt for s, amt in coll_amounts.items() if s not in price_map and amt > 0}
            if len(unpriced) == 1:
                _s, _amt = next(iter(unpriced.items()))
                _resid = result["total_usd"] - priced_total
                if _resid > 0 and _amt > 0:
                    price_map[_s] = _resid / _amt
            # FIX issue-3: _aero_position_legs may miss staked NFTs when
            # getOwnedStakedAerodromeTokenIds returns empty (e.g. the NFT was staked
            # through a path the Degen Account doesn't track). When aero_legs is empty
            # but getTotalValue exceeds priced collateral by a material amount (>$1),
            # the gap is almost certainly a staked LP position. Inject it as a
            # synthetic LP entry so health_pct sees the collateral instead of reading
            # negative equity, and the wallet panel shows the LP instead of being blank.
            if not aero_legs and not unpriced:
                _gap = result["total_usd"] - priced_total
                if _gap > 1.0:
                    aero_legs = [{
                        "token_id": -1, "sym0": "LP", "sym1": "LP",
                        "dec0": 18, "dec1": 18,
                        "amt0": 0.0, "amt1": 0.0,
                        "_synthetic": True, "_gap_usd": round(_gap, 2),
                    }]
                    # Also inject into price_map for the health rows below
                    price_map["LP"] = 1.0  # gap is already USD

        def _row(r):
            amt = r["raw"] / 10**r["decimals"]
            row = {"symbol": r["symbol"], "balance": f"{amt:.6f}"}
            usd = price_map.get(r["symbol"])
            if usd is not None:
                row["usd"] = round(amt * usd, 2)
            return row

        _rows_supplied = [_row(r) for r in supplied]
        _rows_borrowed = [_row(r) for r in borrowed]

        # Aerodrome LP legs priced via the back-solved map: each leg is a health input
        # row (symbol+usd; dc resolved live by _compute_health_pct) and each position is
        # one display item under an "Aerodrome" group.
        aero_health_rows, aero_items = [], []
        for lg in aero_legs:
            if lg.get("_synthetic"):
                # Fallback: getTotalValue gap injected as a synthetic LP entry.
                # The health row is a single USD-denominated row so the health calc
                # sees the collateral. The display item surfaces the back-solved gap.
                _gap = lg["_gap_usd"]
                aero_health_rows.append({"symbol": "LP", "usd": _gap})
                aero_items.append({
                    "symbol": "Staked LP", "label": "Staked LP (back-solved)",
                    "balance": f"${_gap:,.2f} (estimated from getTotalValue gap)",
                    "token_id": -1, "usd": _gap,
                })
                continue
            u0, u1 = price_map.get(lg["sym0"]), price_map.get(lg["sym1"])
            usd0 = round(lg["amt0"] * u0, 2) if u0 is not None else None
            usd1 = round(lg["amt1"] * u1, 2) if u1 is not None else None
            if usd0 is not None:
                aero_health_rows.append({"symbol": lg["sym0"], "usd": usd0})
            if usd1 is not None:
                aero_health_rows.append({"symbol": lg["sym1"], "usd": usd1})
            item = {"symbol": f"{lg['sym0']}/{lg['sym1']}",
                    "label": f"{lg['sym0']}/{lg['sym1']} LP",
                    "balance": f"{lg['amt0']:.4f} {lg['sym0']} + {lg['amt1']:.4f} {lg['sym1']}",
                    "token_id": lg["token_id"]}
            if usd0 is not None or usd1 is not None:
                item["usd"] = round((usd0 or 0) + (usd1 or 0), 2)
            try:
                _unclaimed = _aero_gauge_earned(w3, account, lg["token_id"])
                if _unclaimed > 0:
                    item["unclaimed_aero"] = round(_unclaimed, 4)
            except Exception:
                pass
            aero_items.append(item)

        # Equity-based health (0-100%) over in-account collateral PLUS the Aerodrome LP
        # legs, so a leveraged LP doesn't read as zero equity.
        _hp = _compute_degen_account_health(
            _rows_supplied + aero_health_rows, _rows_borrowed,
            aero_legs, result["total_usd"], w3=w3)
        result["health_pct"] = _hp.get("health_pct")

        if supplied or borrowed:
            result["groups"].append({
                "type": "Lending / Leverage", "health_ratio": solvency["ratio"],
                "health_pct": result["health_pct"],
                "supplied": _rows_supplied,
                "borrowed": _rows_borrowed,
            })
        if aero_items:
            result["groups"].append({
                "type": "Aerodrome", "label": "Aerodrome", "items": aero_items,
            })

    # Savings: the EOA's own pool deposits ("Diamond Hands"), independent of the Degen
    # Account (so NOT in getTotalValue) — surfaced as their own group and added on top.
    # Priced from the same RedStone read used for the account (no extra RPC).
    if pool_deposits:
        sav_rows, sav_usd_total = [], 0.0
        for r in pool_deposits:
            amt = r["raw"] / 10**r["decimals"]
            row = {"symbol": r["symbol"], "balance": f"{amt:.6f}"}
            usd = prices.get(r["symbol"])
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
    context for fields that carry no information: drops dict keys whose value is exactly
    None, drops keys whose value is an empty list or empty dict, drops the decorative
    top-level `url` key, but PRESERVES numeric 0 and boolean False (zero balance,
    explicitly-not-solvent, etc.) and keeps the top-level structure so a consumer can tell
    what's missing from what shape the response took. Same contract as `deltaprime`'s."""
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
    """Aggregate all DegenPrime positions for the wallet. Default output is the DeBank-style
    JSON (the cross-tool shape the health monitor consumes). On error, emits
    {"status":"error", ...} rather than raising, so the caller always gets parseable JSON."""
    try:
        data = gather_defi()
    except Exception as e:
        data = {"protocol": "DegenPrime", "chain": "base",
                "status": "error", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(_trim_defi_json(data), indent=2))


def cmd_summary(as_json: bool = False):
    """Read-only Degen Account view: in-account collateral, debts, and live
    RedStone-gated solvency (getTotalValue/getDebt/getHealthRatio/isSolvent). Falls
    back to balances-only if the RedStone gateway is unreachable or a view reverts.
    Note: per-asset USD is best-effort - only symbols with a RedStone primary-prod
    feed are priced here. Symbols sourced on-chain from BaseOracle TWAP show as
    balance-only (the SolvencyFacet still values them for the total/debt figures).

    With --json: emits a single JSON object covering wallet, account, native
    balance, per-asset supplied/borrowed with optional USD, poolDeposits (the EOA's
    'Diamond Hands' lending-pool balances, emitted even with no Degen Account),
    total/debt/health-ratio/solvent flags. Null fields, empty lists, and empty dicts are dropped (same
    trim contract as `deltaprime defi --json`). Numeric 0 and boolean false are
    preserved."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not as_json:
        print(f"Wallet: {acct.address}")

    pool_deposits = _gather_pool_deposits(w3, acct.address)

    if not pa:
        # No Degen Account: still surface Diamond Hands deposits (balance-only — the
        # RedStone getPrices view lives on the Degen Account, absent here).
        if as_json:
            out = {"wallet": acct.address, "account": None}
            if pool_deposits:
                out["poolDeposits"] = [{"symbol": r["symbol"], "amount": r["raw"] / 10**r["decimals"]}
                                       for r in pool_deposits]
            print(json.dumps(out, indent=2))
        else:
            print("No Degen Account yet. Create one with: degenprime create-account --execute")
            if pool_deposits:
                print("  Pool Deposits (Diamond Hands):")
                for r in pool_deposits:
                    print(f"    {r['symbol']:<8} {fmt_token_amount(r['raw'], r['decimals'])}")
        return

    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    pa_eth, supplied, borrowed, solvency = _gather_account_state(w3, account, pool_deposits)
    if not as_json:
        print(f"Degen Account: {pa}")
        if pa_eth >= 1e-9:
            print(f"  Native ETH (gas):  {pa_eth:.6f}")

    if as_json:
        def _asset_row(r):
            row = {"symbol": r["symbol"], "amount": r["raw"] / 10**r["decimals"]}
            usd = solvency["prices"].get(r["symbol"])
            if usd is not None:
                row["usd"] = round(row["amount"] * usd, 2)
            return row

        # Equity-based health (0-100%) including Aerodrome LP positions so a
        # leveraged LP doesn't read as zero equity (same calc as gather_defi).
        aero_legs = _aero_position_legs(w3, account)
        prices = solvency["prices"]
        # Resolve unpriced symbols same way as gather_defi does.
        price_map = dict(prices)
        _hp_rows = [_asset_row(r) for r in supplied]
        if aero_legs:
            for lg in aero_legs:
                if lg.get("_synthetic"):
                    _hp_rows.append({"symbol": "LP", "usd": lg["_gap_usd"]})
                    continue
                u0, u1 = price_map.get(lg["sym0"]), price_map.get(lg["sym1"])
                if u0 is not None:
                    _hp_rows.append({"symbol": lg["sym0"], "usd": round(lg["amt0"] * u0, 2)})
                if u1 is not None:
                    _hp_rows.append({"symbol": lg["sym1"], "usd": round(lg["amt1"] * u1, 2)})
        # FIX issue-3 (cmd_summary json path): same getTotalValue gap fallback as
        # gather_defi — when aero_legs is empty but getTotalValue > priced_supplied,
        # inject the gap as a synthetic LP entry so health_pct is consistent.
        _hp_borrowed = [_asset_row(r) for r in borrowed]
        _hp = _compute_degen_account_health(_hp_rows, _hp_borrowed,
                                            aero_legs, solvency["total"], w3=w3)

        out = {
            "wallet": acct.address,
            "account": pa,
            "nativeBalance": pa_eth if pa_eth >= 1e-9 else None,
            "supplied": [_asset_row(r) for r in supplied],
            "borrowed": [_asset_row(r) for r in borrowed],
            "poolDeposits": [_asset_row(r) for r in pool_deposits],
            "totalValueUsd": solvency["total"],
            "debtUsd": solvency["debt"],
            "healthRatio": solvency["ratio"],
            "healthPct": _hp.get("health_pct"),
            "solvent": solvency["solvent"],
            "solvencyError": solvency["error"],
        }
        # Drop None / empty list / empty dict; preserve 0 and False.
        out = {k: v for k, v in out.items()
               if not (v is None or v == [] or v == {})}
        print(json.dumps(out, indent=2))
        return

    print("  Assets:")
    if supplied:
        for r in supplied:
            usd = solvency["prices"].get(r["symbol"])
            usd_str = f"  (~${r['raw'] / 10**r['decimals'] * usd:,.2f})" if usd is not None else ""
            print(f"    {r['symbol']:<8} {fmt_token_amount(r['raw'], r['decimals'])}{usd_str}")
    else:
        print("    (none)")

    print("  Debts:")
    if borrowed:
        for r in borrowed:
            usd = solvency["prices"].get(r["symbol"])
            usd_str = f"  (~${r['raw'] / 10**r['decimals'] * usd:,.2f})" if usd is not None else ""
            print(f"    {r['symbol']:<8} {fmt_token_amount(r['raw'], r['decimals'])}{usd_str}")
    else:
        print("    (none)")

    if pool_deposits:
        print("  Pool Deposits (Diamond Hands):")
        for r in pool_deposits:
            usd = solvency["prices"].get(r["symbol"])
            usd_str = f"  (~${r['raw'] / 10**r['decimals'] * usd:,.2f})" if usd is not None else ""
            print(f"    {r['symbol']:<8} {fmt_token_amount(r['raw'], r['decimals'])}{usd_str}")

    if solvency["error"] is None:
        tv_str = f"${solvency['total']:,.2f}" if solvency["total"] is not None else "n/a"
        debt_str = f"${solvency['debt']:,.2f}" if solvency["debt"] is not None else "n/a"
        print(f"  Total value:        {tv_str}")
        print(f"  Debt:               {debt_str}")
        # Health meter (0-100%) with Aerodrome LP positions included, so a leveraged
        # LP doesn't read as zero equity. 0% = liquidation, 50% = half borrowing power
        # used, 100% = no debt.
        _aero_legs = _aero_position_legs(w3, account)
        _hp_rows = []
        for r in supplied:
            if solvency["prices"].get(r["symbol"]):
                _hp_rows.append({
                    "symbol": r["symbol"], "balance": "0", "dc": r.get("dc", 0.0),
                    "usd": (r["raw"] / 10**r["decimals"]) * solvency["prices"].get(r["symbol"], 0)})
        for lg in _aero_legs:
            if lg.get("_synthetic"):
                _hp_rows.append({"symbol": "LP", "balance": "0", "dc": 0.0,
                                "usd": lg["_gap_usd"]})
                continue
            u0, u1 = solvency["prices"].get(lg["sym0"]), solvency["prices"].get(lg["sym1"])
            if u0 is not None:
                _hp_rows.append({"symbol": lg["sym0"], "balance": "0", "dc": 0.0,
                                "usd": round(lg["amt0"] * u0, 2)})
            if u1 is not None:
                _hp_rows.append({"symbol": lg["sym1"], "balance": "0", "dc": 0.0,
                                "usd": round(lg["amt1"] * u1, 2)})
        _hp_borrowed = []
        for r in borrowed:
            if solvency["prices"].get(r["symbol"]):
                _hp_borrowed.append({
                    "symbol": r["symbol"], "balance": "0", "dc": r.get("dc", 0.0),
                    "usd": (r["raw"] / 10**r["decimals"]) * solvency["prices"].get(r["symbol"], 0)})
        _hp = _compute_degen_account_health(_hp_rows, _hp_borrowed,
                                            _aero_legs, solvency["total"], w3=w3)
        if "error" not in _hp:
            print(f"  Health (0-100%): {_hp['health_pct']:.1f}%")
            print(f"    (supplied=${_hp['supplied_usd']:.2f}, debt=${_hp['debt_usd']:.2f},"
                  f" equity=${_hp['equity']:.2f}, max_debt=${_hp['max_debt']:.2f}, {_hp.get('tier','')})")
            print(f"    0%=liquidation  50%=half borrowing power used  100%=no debt")
        elif solvency["ratio"] is not None:
            r = solvency["ratio"]
            print(f"  Collateral/Debt:   {r:.4f}x  (chain ratio; 1.0 = liquidation)")
        else:
            print(f"  Health:            N/A (RedStone unavailable)")
        # An account with no debt cannot be liquidated. isSolvent() can come back
        # None on a no-debt account (empty multicall leg), which used to render a
        # misleading "NO - liquidatable" despite ratio >1000 and ~$0 debt. Treat
        # negligible debt (or a >1000 ratio, surfaced as ratio=None) as solvent.
        negligible_debt = (solvency["debt"] is None or solvency["debt"] < 0.01
                           or solvency["ratio"] is None)
        is_solvent = bool(solvency["solvent"]) or negligible_debt
        print(f"  Solvent:            {'yes' if is_solvent else 'NO - liquidatable'}")
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
    amount_wei = to_wei_units(amount, cfg["decimals"])
    if not execute:
        print(f"Preview: Borrow {amount} {symbol} into Degen Account {pa}")
        print(f"  Calls borrow(bytes32 '{symbol}', {amount_wei}) on the Degen Account")
        print("  Requires sufficient collateral funded into the account.")
        print("Run with --execute to broadcast")
        return

    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)
    # borrow has remainsSolvent -> needs RedStone price payload appended to calldata.
    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
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
    requested_wei = to_wei_units(amount, cfg["decimals"])
    debt_wei = pool.functions.getBorrowed(pa_cs).call()
    in_acct_wei = account.functions.getBalance(asset_b32(symbol)).call()
    try:
        total_intent_wei = account.functions.getTotalIntentAmount(asset_b32(symbol)).call()
    except Exception:
        total_intent_wei = 0
    available_wei = in_acct_wei - total_intent_wei if in_acct_wei > total_intent_wei else 0
    if debt_wei == 0:
        print(f"No {symbol} debt to repay on Degen Account {pa}.")
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
        print(f"Preview: Repay {amount_wei / 10**cfg['decimals']:.6f} {symbol} from Degen Account {pa}")
        if cap_notes:
            print(f"  Capped from requested {amount}: {'; '.join(cap_notes)}")
        print(f"  Calls repay(bytes32 '{symbol}', {amount_wei}) on the Degen Account")
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
        "gas": 4000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Repay {amount_wei / 10**cfg['decimals']:.6f} {symbol}", fallback_gas=600000)
    ok = receipt["status"] == 1
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


# ─── ParaSwap / Velora route ─────────────────────────────────────────────────
# The Degen Account already holds the funds, so the facet (not the EOA) approves the
# Augustus router and executes. We only build the API calldata with the Degen Account
# as the swapper + receiver, then hand its (selector, data) to paraSwapV6 /
# swapDebtParaSwap.

# Map account-side bytes32 symbols to (token_address, decimals) for the swap and
# swap-debt paths. Pool symbols are pre-baked here; non-pool symbols (memecoin
# collateral) resolve dynamically via _asset_meta at the swap site.
SWAP_ASSETS = {cfg["symbol"]: {"symbol": cfg["symbol"], "token": cfg["token"], "decimals": cfg["decimals"]}
               for cfg in POOLS.values()}

def _swap_asset_meta(w3, symbol: str):
    """Resolve a swap-side symbol to {token, decimals}. Falls back to TokenManager for
    non-pool collateral (memecoins). Returns None if the asset is unknown.
    Lookup is case-insensitive (keys like cbBTC match CBBTC)."""
    if symbol in SWAP_ASSETS:
        return SWAP_ASSETS[symbol]
    # Case-insensitive fallback for mixed-case symbols like cbBTC.
    for key, val in SWAP_ASSETS.items():
        if key.upper() == symbol.upper():
            return val
    addr, dec = _asset_meta(w3, symbol)
    if addr is None:
        return None
    return {"symbol": symbol, "token": addr, "decimals": dec}

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
        "excludeContractMethods": "multiSwap,megaSwap,protectedMultiSwap,protectedMegaSwap,protectedSimpleSwap,simpleSwap,swapExactAmountInOnCurveV1",
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

    from_asset_sym = from_cfg.get("symbol", from_sym)
    to_asset_sym = to_cfg.get("symbol", to_sym)

    amount_in = to_wei_units(amount, from_cfg["decimals"])
    in_balance = account.functions.getBalance(asset_b32(from_asset_sym)).call()
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
    # Simulate-first executor handling (see cmd_swap_debt rationale): keep the API
    # executor when the exact tx simulates clean; only fall back to the legacy
    # executor if the unpatched calldata reverts.
    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
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

    print(f"Swap {amount} {from_asset_sym} -> {to_asset_sym} on Degen Account {pa_cs}  (via ParaSwap/Velora)")
    print(f"  Router method: {price_route['contractMethod']} ({selector_hex})")
    print(f"  Augustus router: {tx_built['to']}")
    print(f"  Expected out: {quoted_out / 10**to_cfg['decimals']:.6f} {to_asset_sym}")
    if min_out is not None:
        print(f"  Min out (@{slippage_pct}% slippage): {min_out / 10**to_cfg['decimals']:.6f} {to_asset_sym}")
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
    receipt = _sign_and_send(w3, acct, tx, f"Swap {amount} {from_asset_sym} -> {to_asset_sym}", fallback_gas=1200000)
    ok = receipt["status"] == 1
    return ok

# ─── Swap debt / refinance (SwapDebtFacet) ───────────────────────────────────
# swapDebtParaSwap borrows _borrowAmount of _toAsset, ParaSwaps it into _fromAsset, and
# repays _repayAmount of the _fromAsset debt - all in one tx. The facet hard-caps the
# USD value difference between the repay and borrow legs at 5% (RedStone-priced) and
# requires the ParaSwap quote's fromAmount to equal _borrowAmount exactly.

_SYMBOL_TO_POOL = {cfg["symbol"]: name for name, cfg in POOLS.items()}

def _read_prices_usd(w3, account, symbols, payload):
    """RedStone-gated getPrices read for `symbols` (1e18-scaled USD), payload appended.
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
    repay_amount = min(to_wei_units(amount, from_cfg["decimals"]), borrowed)

    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
    payload = build_redstone_payload(feeds)
    price_from, price_to = _read_prices_usd(w3, account, [from_sym, to_sym], payload)
    # borrow_amount such that its USD value ≈ repay USD value:
    #   repay_usd  = price_from * repay_amount  / 10**from_dec
    #   borrow_amt = repay_usd * 10**to_dec / price_to
    borrow_amount = (price_from * repay_amount * 10**to_cfg["decimals"]) // (price_to * 10**from_cfg["decimals"])
    if borrow_amount == 0:
        print("Computed borrow amount rounds to zero - repay amount too small. Refusing.")
        return

    repay_usd = price_from * repay_amount / 10**from_cfg["decimals"] / 1e18
    borrow_usd = price_to * borrow_amount / 10**to_cfg["decimals"] / 1e18
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
    # Simulate-first executor handling (protocol-level facet fix confirmed 2026-06-04;
    # Velora rotates executors per quote): keep the API executor when the exact tx
    # simulates clean; only fall back to the legacy executor if it reverts.
    def _sim_swap_debt(db):
        base = account.encode_abi("swapDebtParaSwap", args=[
            asset_b32(from_sym), asset_b32(to_sym), repay_amount, borrow_amount,
            full[:4], db])
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

    if not sim_ok:
        print("✗ Refusing to broadcast: simulation reverted for both executor variants.")
        return

    base_calldata = account.encode_abi("swapDebtParaSwap", args=[
        asset_b32(from_sym), asset_b32(to_sym), repay_amount, borrow_amount,
        full[:4], data_bytes,
    ])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Swap debt {from_sym} -> {to_sym}", fallback_gas=4000000)
    ok = receipt["status"] == 1

# ─── Collateral withdrawal (WithdrawalIntentFacet, Degen Account) ───────────
# Universal 24h time-lock on the Degen Account - NOT just risky assets. On the Account,
# createWithdrawalIntent IS RedStone-gated (on-chain solvency check at create), then
# executeWithdrawalIntent pulls it after maturity (also RedStone-gated). Window (from the
# IntentInfo flags on-chain): actionableAt = createdAt + 24h, expiresAt = actionableAt + 48h.
# So an intent is executable in a 24h-72h window (a 48h actionable span) — actionableAt-
# anchored, unlike the savings pool which re-anchors expiresAt for a 24h window.
# cancelWithdrawalIntent drops a pending intent.

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
    amount_wei = to_wei_units(amount, cfg["decimals"])
    pa_cs = Web3.to_checksum_address(pa)
    account = w3.eth.contract(address=pa_cs, abi=PRIME_ACCOUNT_ABI)

    # getAvailableBalance is oracle-free: in-account minus pending intents.
    available = account.functions.getAvailableBalance(asset_b32(symbol)).call()
    print(f"Create withdrawal intent: {amount} {symbol} from Degen Account {pa}")
    print(f"  Available to withdraw now: {available / 10**cfg['decimals']:.6f} {symbol}")
    if amount_wei > available:
        print(f"  ✗ Requested {amount} {symbol} exceeds available balance. Refusing.")
        return
    print(f"  Calls createWithdrawalIntent(bytes32 '{symbol}', {amount_wei}) — RedStone-gated on Base (known issue: facet rejects standard signers).")
    print("  Universal time-lock: becomes executable ~24h later, then has a 48h window (24h-72h total).")
    print("  Run `execute-withdrawal --pool <p>` after maturity to pull the funds to the wallet.")

    if not execute:
        print("Run with --execute to broadcast (registers the intent on-chain).")
        return

    # createWithdrawalIntent on Base is RedStone-gated (on-chain solvency at create time).
    # The solvency check prices EVERY registered collateral type, not just owned assets.
    # Include all available feeds — same as the DegenPrime UI on mainnet.
    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
    payload = build_redstone_payload(feeds)
    base_calldata = account.encode_abi("createWithdrawalIntent", args=[asset_b32(symbol), amount_wei])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 1000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, "Withdrawal intent", fallback_gas=1000000)
    ok = receipt["status"] == 1

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

    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
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

    # cancelWithdrawalIntent on Base is also RedStone-gated (DegenPrime diamond requires
    # solvency payload on all state-changing facet calls). Include all available feeds.
    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
    payload = build_redstone_payload(feeds)
    base_calldata = account.encode_abi("cancelWithdrawalIntent", args=[asset_b32(symbol), index])
    data = base_calldata + payload.hex()
    tx = {
        "from": acct.address, "to": pa_cs, "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 1000000, "chainId": CHAIN_ID,
    }
    receipt = _sign_and_send(w3, acct, tx, f"Cancel withdrawal intent [{index}]", fallback_gas=1000000)
    ok = receipt["status"] == 1

# ─── Aerodrome (Slipstream CL) ──────────────────────────────────────────────

def cmd_aerodrome_positions():
    """Read-only: list every Aerodrome Slipstream NFT position the Degen Account
    owns/stakes, showing token pair, tick range, and liquidity. Enumerates tokenIds
    via getOwnedStakedAerodromeTokenIds, then reads each from NPM.positions() (the
    simplified facet view reports liquidity=0 + garbage ticks for staked NFTs)."""
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
    print(f"  {len(ids)} Aerodrome NFT position(s):")

    # Read each position straight from NPM.positions(). The account's enumerated
    # tokenIds include STAKED positions whose NFT now belongs to the gauge, and the
    # facet's getPositionCompositionSimplified reports liquidity=0 + garbage ticks
    # for those. NPM.positions() returns the real struct for any holder.
    for tid in ids:
        pos = _aero_read_position(w3, tid)
        if pos is None:
            print(f"    [{tid}] position read failed")
            continue
        token0, token1, tick_lower, tick_upper, liq = pos
        sym0 = _resolve_token_symbol(w3, token0)
        sym1 = _resolve_token_symbol(w3, token1)
        # Human price = token1 per token0 = 1.0001**tick * 10**(dec0 - dec1).
        dec0 = _resolve_token_decimals(w3, token0)
        dec1 = _resolve_token_decimals(w3, token1)
        if dec0 is not None and dec1 is not None:
            scale = 10 ** (dec0 - dec1)
            price_lower = 1.0001 ** tick_lower * scale
            price_upper = 1.0001 ** tick_upper * scale
            print(f"    [{tid}] {sym0}/{sym1}  ticks=[{tick_lower}, {tick_upper}]"
                  f"  liq={liq}  price_range=[{price_lower:.6g}, {price_upper:.6g}] ({sym1}/{sym0})")
        else:
            print(f"    [{tid}] {sym0}/{sym1}  ticks=[{tick_lower}, {tick_upper}]  liq={liq}")
    print("  Manage on Aerodrome UI: https://aerodrome.finance/positions")

def _aero_read_position(w3, token_id: int):
    """Authoritative read of an Aerodrome Slipstream position via NPM.positions().

    getPositionCompositionSimplified is wrong for STAKED positions: once an NFT is
    staked its owner becomes the gauge, and the simplified view returns liquidity=0
    with a tickData word that is not a tick packing at all (it decodes to garbage,
    e.g. [0, -3984902] for the live tokenId 71997868). NPM.positions(tokenId) returns
    the real struct regardless of who holds the NFT — token0/token1, tickLower,
    tickUpper, and liquidity. Returns (token0, token1, tickLower, tickUpper, liquidity)
    or None if the read fails."""
    try:
        npm = w3.eth.contract(address=Web3.to_checksum_address(AERODROME_NPM),
                              abi=AERODROME_NPM_ABI)
        p = npm.functions.positions(token_id).call()
    except Exception:
        return None
    # positions() layout: nonce, operator, token0, token1, tickSpacing,
    #                      tickLower, tickUpper, liquidity, ...
    return (p[2], p[3], p[5], p[6], p[7])

def _int24_from_hi128(val: int) -> int:
    """Extract int24 from the upper 128 bits of a uint256, sign-extending."""
    raw = (val >> 128) & 0xFFFFFF
    if raw & 0x800000:
        return raw - 0x1000000
    return raw

def _int24_from_lo128(val: int) -> int:
    """Extract int24 from the lower 128 bits of a uint256, sign-extending."""
    raw = val & 0xFFFFFF
    if raw & 0x800000:
        return raw - 0x1000000
    return raw

def _resolve_token_symbol(w3, addr: str) -> str:
    """Best-effort token symbol from TokenManager or static pool map."""
    addr_lower = addr.lower()
    # Check static pool map first
    for cfg in POOLS.values():
        if cfg["token"].lower() == addr_lower:
            return cfg["symbol"]
    # Check Aerodrome pool configs
    for cfg in AERODROME_POOLS.values():
        if cfg["token0"].lower() == addr_lower:
            return cfg["symbol0"]
        if cfg["token1"].lower() == addr_lower:
            return cfg["symbol1"]
    # Try TokenManager
    try:
        tm = get_token_manager(w3)
        sym_bytes = tm.functions.tokenAddressToSymbol(Web3.to_checksum_address(addr)).call()
        sym = sym_bytes.rstrip(b"\x00").decode(errors="replace")
        if sym:
            return sym
    except Exception:
        pass
    return addr[:10] + "..."

_ERC20_DECIMALS_ABI = json.dumps([{"inputs": [], "name": "decimals",
    "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"}])

def _resolve_token_decimals(w3, addr: str):
    """Token decimals from the static pool maps, falling back to an on-chain
    decimals() read. Returns the int decimals or None if it can't be determined."""
    addr_lower = addr.lower()
    for cfg in AERODROME_POOLS.values():
        if cfg["token0"].lower() == addr_lower:
            return cfg["decimals0"]
        if cfg["token1"].lower() == addr_lower:
            return cfg["decimals1"]
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(addr),
                            abi=json.loads(_ERC20_DECIMALS_ABI))
        return c.functions.decimals().call()
    except Exception:
        return None

# ─── Aerodrome Write Commands ────────────────────────────────────────────────

# Helper: get Aerodrome CL pool address from the factory's getPool (authoritative).
def _aero_pool_address(pool_cfg: dict) -> str:
    """Resolve the pool address via the factory's getPool()."""
    try:
        w3_local = Web3(Web3.HTTPProvider(BASE_RPC))
        factory = Web3.to_checksum_address("0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A")
        import json
        f_abi = json.loads('[{"inputs":[{"name":"","type":"address"},{"name":"","type":"address"},{"name":"","type":"int24"}],"name":"getPool","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}]')
        factory_c = w3_local.eth.contract(address=factory, abi=f_abi)
        t0 = Web3.to_checksum_address(pool_cfg["token0"])
        t1 = Web3.to_checksum_address(pool_cfg["token1"])
        pool = factory_c.functions.getPool(t0, t1, pool_cfg["tickSpacing"]).call()
        if pool == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool does not exist on Aerodrome")
        return pool
    except Exception as e:
        raise RuntimeError(f"Cannot resolve Aerodrome pool: {e}")

SLOT0_ABI = json.dumps([{"inputs":[],"name":"slot0","outputs":[
    {"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},
    {"internalType":"int24","name":"tick","type":"int24"}],
    "stateMutability":"view","type":"function"}])

# Helper: build the 14-field arg tuple for the Aerodrome mint+stake facet fn
# (selector 0xf32f1e56). Layout verified byte-exact against the successful
# on-chain mint 0x1723377a... (ZORA/USDC, Base, 2026-06-14):
#   token0, token1, tickLower, tickUpper, tickSpacing,
#   amount0Desired(wei), amount1Desired(wei), amount0Min(wei), amount1Min(wei),
#   uint256 const=300, int24 currentTick, 0, 0, 0
# Both amounts are NATIVE/wei units; there is NO recipient or unix-deadline
# field, tickSpacing IS part of the args, and the last tick field is the live
# pool tick (not sqrtPriceX96). The three trailing zero words are
# sqrtPriceX96=0 / borrow-if-needed bools = false (all zero either way).
def _aero_in_account_balance(account, symbol: str) -> int:
    """In-account spendable balance (base units) of `symbol` on the Degen Account.

    The mint+stake facet pulls token0/token1 from the account's own holdings, the
    same balance getBalance(bytes32) reports (verified equal to ERC20.balanceOf on
    the account 2026-06-14). Subtract any pending withdrawal-intent lock so we never
    treat reserved funds as available. Returns 0 if the view reverts."""
    try:
        bal = account.functions.getBalance(asset_b32(symbol)).call()
    except Exception:
        return 0
    try:
        locked = account.functions.getTotalIntentAmount(asset_b32(symbol)).call()
    except Exception:
        locked = 0
    return bal - locked if bal > locked else 0

def _aero_cap_to_balance(account, pool_cfg: dict, amt0_wei: int, amt1_wei: int) -> tuple:
    """Cap each requested amount to what the account actually holds, minus a 1-wei
    margin so display round-up can never push the request past the real balance.
    Returns (amt0_capped, amt1_capped, notes) where notes lists human cap messages."""
    notes = []
    sym0, sym1 = pool_cfg["symbol0"], pool_cfg["symbol1"]
    dec0, dec1 = pool_cfg["decimals0"], pool_cfg["decimals1"]
    for amt, sym, dec, idx in ((amt0_wei, sym0, dec0, 0), (amt1_wei, sym1, dec1, 1)):
        if amt <= 0:
            continue
        avail = _aero_in_account_balance(account, sym)
        safe = avail - 1 if avail > 0 else 0  # 1-wei margin vs rounding overshoot
        if amt > safe:
            capped = safe if safe > 0 else 0
            notes.append((idx, capped,
                          f"Capped {sym} to {fmt_token_amount(capped, dec)} "
                          f"(on-chain balance {fmt_token_amount(avail, dec)})"))
    amt0_out, amt1_out = amt0_wei, amt1_wei
    for idx, capped, _msg in notes:
        if idx == 0:
            amt0_out = capped
        else:
            amt1_out = capped
    return amt0_out, amt1_out, [m for _i, _c, m in notes]

def _aero_simulate_mint(w3, from_addr: str, account_addr: str, calldata: bytes) -> tuple:
    """eth_call-simulate the mint+stake before broadcasting. Returns (ok, info):
    ok=True + would-be tokenId on success, ok=False + revert detail on failure.
    Read-only — never signs or sends."""
    try:
        ret = w3.eth.call({"from": from_addr, "to": account_addr,
                           "data": "0x" + calldata.hex()})
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"
    token_id = int.from_bytes(bytes(ret)[:32], "big") if len(ret) >= 32 else None
    return True, token_id

def _aero_simulate_call(w3, from_addr: str, account_addr: str, calldata: bytes) -> tuple:
    """eth_call-simulate an Aerodrome facet write before broadcasting."""
    try:
        ret = w3.eth.call({"from": from_addr, "to": account_addr,
                           "data": "0x" + calldata.hex()})
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"
    return True, ret

def _aero_mint_params(pool_cfg: dict, amount0_wei: int, amount1_wei: int,
                      tick_lower: int, tick_upper: int,
                      current_tick: int, slippage_pct: float) -> tuple:
    slippage = Decimal(str(slippage_pct)) / Decimal(100)
    amount0_min = int(Decimal(str(amount0_wei)) * (Decimal(1) - slippage))
    amount1_min = int(Decimal(str(amount1_wei)) * (Decimal(1) - slippage))
    return (
        Web3.to_checksum_address(pool_cfg["token0"]),
        Web3.to_checksum_address(pool_cfg["token1"]),
        tick_lower,
        tick_upper,
        pool_cfg["tickSpacing"],
        amount0_wei,          # amount0Desired (wei)
        amount1_wei,          # amount1Desired (wei)
        amount0_min,          # amount0Min (wei)
        amount1_min,          # amount1Min (wei)
        300,                  # word9 constant (frontend passes 300)
        int(current_tick),    # word10: live pool tick
        0, 0, 0,              # word11-13: zero (sqrtPriceX96=0 / bools false)
    )

# Helper: compute tick range around a desired centre price.
def _aero_tick_range(tick_spacing: int, centre_price: float = None,
                     width_pct: float = 2.0, pool_tick: int = None) -> tuple:
    """Return (tickLower, tickUpper) for +/-width_pct around centre.
    Priority: pool_tick > centre_price > full range."""
    import math
    MIN_TICK, MAX_TICK = -887272, 887272

    if pool_tick is not None:
        tick_delta = int(abs(math.log(1.0 + width_pct / 100.0) / math.log(1.0001))) + 1
        raw_lower = pool_tick - tick_delta
        raw_upper = pool_tick + tick_delta
    elif centre_price is not None and centre_price > 0:
        lower_price = centre_price * max(1e-12, 1.0 - width_pct / 100.0)
        upper_price = centre_price * (1.0 + width_pct / 100.0)
        raw_lower = math.log(lower_price) / math.log(1.0001)
        raw_upper = math.log(upper_price) / math.log(1.0001)
    else:
        t_lower = (MIN_TICK // tick_spacing) * tick_spacing
        t_upper = (MAX_TICK // tick_spacing) * tick_spacing
        return (t_lower, t_upper)

    tick_lower = math.floor(raw_lower / tick_spacing) * tick_spacing
    tick_upper = math.ceil(raw_upper / tick_spacing) * tick_spacing
    tick_lower = max(MIN_TICK, min(MAX_TICK, tick_lower))
    tick_upper = max(MIN_TICK, min(MAX_TICK, tick_upper))
    if tick_lower >= tick_upper:
        tick_upper = tick_lower + tick_spacing
    return (tick_lower, tick_upper)


def _aero_fit_amounts_to_range(pool_cfg: dict, amt0_wei: int, amt1_wei: int,
                               tick_lower: int, tick_upper: int,
                               pool_tick: int | None) -> tuple[int, int, list[str]]:
    """Fit desired CL mint amounts to the current price/range ratio.

    Aerodrome's facet derives min amounts from desired amounts. If the caller passes
    "all available balances" and one side is materially above the range ratio, the
    NPM uses only the matching portion but the min for the excess side can trip PSC.
    Fit desired amounts down to the maximum liquidity supported by both sides before
    computing min amounts. Out-of-range mints are allowed to be one-sided.
    """
    if pool_tick is None or amt0_wei <= 0 and amt1_wei <= 0:
        return amt0_wei, amt1_wei, []

    notes = []
    sym0, sym1 = pool_cfg["symbol0"], pool_cfg["symbol1"]
    dec0, dec1 = pool_cfg["decimals0"], pool_cfg["decimals1"]

    with localcontext() as ctx:
        ctx.prec = 80
        q96 = Decimal(2) ** 96

        def sqrt_x96_at(tick: int) -> Decimal:
            return (Decimal("1.0001") ** (Decimal(tick) / Decimal(2))) * q96

        sqrt_a = sqrt_x96_at(tick_lower)
        sqrt_b = sqrt_x96_at(tick_upper)
        sqrt_p = sqrt_x96_at(pool_tick)

        if pool_tick <= tick_lower:
            fitted0, fitted1 = amt0_wei, 0
        elif pool_tick >= tick_upper:
            fitted0, fitted1 = 0, amt1_wei
        else:
            if amt0_wei <= 0 or amt1_wei <= 0:
                notes.append("Current price is inside the range, so minting normally requires both token sides.")
                return amt0_wei, amt1_wei, notes
            l0 = Decimal(amt0_wei) * sqrt_p * sqrt_b / ((sqrt_b - sqrt_p) * q96)
            l1 = Decimal(amt1_wei) * q96 / (sqrt_p - sqrt_a)
            liquidity = min(l0, l1)
            fitted0 = int(liquidity * (sqrt_b - sqrt_p) * q96 / (sqrt_p * sqrt_b))
            fitted1 = int(liquidity * (sqrt_p - sqrt_a) / q96)
            fitted0 = min(max(fitted0, 0), amt0_wei)
            fitted1 = min(max(fitted1, 0), amt1_wei)

    if fitted0 != amt0_wei or fitted1 != amt1_wei:
        notes.append(
            f"Adjusted CL mint to current range ratio: "
            f"{sym0} {fmt_token_amount(fitted0, dec0)} / "
            f"{sym1} {fmt_token_amount(fitted1, dec1)}"
        )
    return fitted0, fitted1, notes


def cmd_aero_add_liquidity(pool_key: str, amount0: float = None,
                           amount1: float = None, slippage_pct: float = 1.0,
                           execute: bool = False, width_pct: float = 2.0):
    """Add concentrated liquidity to an Aerodrome Slipstream pool through the
    Degen Account's AerodromeFacet. Uses in-account token0/token1 balances.

    --pool selects a whitelisted CL pool (e.g. weth-usdc-100).
    --amount-weth / --amount-usdc (or --amount-token0 / --amount-token1) specify
    the desired liquidity amounts in token units. At least one side must be >0.
    --slippage sets the min-amount floor (default 1%).
    --width sets the range ±width% around the current price (default 2%).

    The facet wraps the NPM mint(MintParams) call with remainsSolvent, so
    --execute appends a RedStone signed-price payload."""
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account yet. Create with: degenprime create-account --execute")
        sys.exit(2)
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Degen Account: {pa}")

    if pool_key not in AERODROME_POOLS:
        print(f"Unknown pool '{pool_key}'. Choose from: {', '.join(AERODROME_POOLS)}")
        sys.exit(2)
    pool_cfg = AERODROME_POOLS[pool_key]

    # Convert amounts to wei
    amt0 = to_wei_units(amount0, pool_cfg["decimals0"]) if amount0 else 0
    amt1 = to_wei_units(amount1, pool_cfg["decimals1"]) if amount1 else 0
    if amt0 == 0 and amt1 == 0:
        print("At least one of --amount-<token0> / --amount-<token1> must be > 0")
        sys.exit(2)

    # Auto-cap each side to the account's real in-account balance. The summary
    # display can round a dust balance UP, so a request matching the shown value
    # could exceed the true balance and revert AFTER burning gas (2026-06-14).
    amt0, amt1, cap_notes = _aero_cap_to_balance(account, pool_cfg, amt0, amt1)
    for note in cap_notes:
        print(f"  {note}")
    if amt0 == 0 and amt1 == 0:
        print("  Nothing to deposit after capping to on-chain balance.")
        sys.exit(2)

    # Get pool's on-chain tick from slot0 for accurate range computation
    pool_tick = None
    centre_price = None
    try:
        pool_addr = _aero_pool_address(pool_cfg)
        pool_abi = json.loads(SLOT0_ABI)
        pool_c = w3.eth.contract(address=pool_addr, abi=pool_abi)
        slot0 = pool_c.functions.slot0().call()
        pool_tick = slot0[1]
        print(f"  Pool tick (on-chain): {pool_tick}")
    except Exception:
        # Fallback to KuCoin price
        price_sym = pool_cfg["symbol0"] + "-USDT"
        try:
            r = requests.get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={price_sym}", timeout=3)
            if r.status_code == 200 and r.json().get("code") == "200000":
                centre_price = float(r.json()["data"]["price"])
        except Exception:
            pass

    tick_lower, tick_upper = _aero_tick_range(pool_cfg["tickSpacing"], centre_price, width_pct, pool_tick)
    amt0, amt1, ratio_notes = _aero_fit_amounts_to_range(pool_cfg, amt0, amt1, tick_lower, tick_upper, pool_tick)
    for note in ratio_notes:
        print(f"  {note}")
    if amt0 == 0 and amt1 == 0:
        print("  Nothing to deposit after fitting amounts to the current CL range.")
        sys.exit(2)
    params = _aero_mint_params(pool_cfg, amt0, amt1, tick_lower, tick_upper,
                               pool_tick if pool_tick is not None else (tick_lower + tick_upper) // 2,
                               slippage_pct)

    sym0, sym1 = pool_cfg["symbol0"], pool_cfg["symbol1"]
    print(f"Pool: {sym0}/{sym1} (tickSpacing={pool_cfg['tickSpacing']})")
    if pool_tick is not None:
        print(f"  Tick range: [{tick_lower}, {tick_upper}] (width: +/-{width_pct}%)")
    elif centre_price:
        print(f"  Current {sym0} price: ${centre_price:,.2f}")
        print(f"  Tick range: [{tick_lower}, {tick_upper}] → price [{1.0001**tick_lower:.4f}, {1.0001**tick_upper:.4f}]")
        print(f"  Width: ±{width_pct}%")
    else:
        print(f"  Full-range position (no price data available)")
    print(f"  {sym0}: {fmt_token_amount(amt0, pool_cfg['decimals0'])}  "
          f"{sym1}: {fmt_token_amount(amt1, pool_cfg['decimals1'])}")

    # Build RedStone payload + final mint+stake calldata (selector 0xf32f1e56).
    # 14 flat args; amounts in native wei; tickSpacing + live tick included.
    # Verified byte-exact vs the successful manual mint 0x1723377a.... Built once
    # so the same bytes feed both the pre-flight simulation and the broadcast.
    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
    payload = build_redstone_payload(feeds)
    from eth_abi import encode as abi_encode
    flat_types = ['address', 'address', 'int24', 'int24', 'int24',
                  'uint256', 'uint256', 'uint256', 'uint256',
                  'uint256', 'int24', 'uint256', 'uint256', 'uint256']
    encoded_params = abi_encode(flat_types, list(params))
    mint_calldata = AERODROME_SEL_MINT + encoded_params + payload

    # Pre-flight eth_call simulation — catches reverts (insufficient balance,
    # slippage/PSC, solvency) BEFORE any gas is spent. Gates every broadcast.
    sim_ok, sim_info = _aero_simulate_mint(w3, acct.address, account.address, mint_calldata)
    if not sim_ok:
        print(f"  Simulation reverted — aborting before broadcast: {sim_info}")
        sys.exit(2)
    if sim_info is not None:
        print(f"  Simulation passed — would-be tokenId: {sim_info}")
    else:
        print("  Simulation passed.")

    if not execute:
        print("Preview only. Run with --execute to broadcast.")
        return

    tx = {
        "from": acct.address,
        "to": account.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000,
        "chainId": CHAIN_ID,
        "data": "0x" + mint_calldata.hex(),
    }
    receipt = _sign_and_send(w3, acct, tx, "Add liquidity", timeout=300, fallback_gas=5000000)
    ok = receipt["status"] == 1


def cmd_aero_increase_liquidity(pool_key: str, token_id: int,
                                amount0: float = None, amount1: float = None,
                                slippage_pct: float = 1.0,
                                execute: bool = False):
    """Increase liquidity on an existing staked Aerodrome Slipstream NFT.

    Disabled: selector 0x2c710777 simulates and broadcasts successfully but was
    observed to claim gauge rewards without depositing liquidity. Until the real
    DegenPrime facet selector is verified from source, use remove + add/rebuild.
    """
    print("ABORT: standalone Aerodrome increase-liquidity is not verified on DegenPrime.")
    print("Use the rebuild path (aero-remove-liquidity -> aero-add-liquidity) instead.")
    sys.exit(2)

    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account yet. Create with: degenprime create-account --execute")
        sys.exit(2)
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Degen Account: {pa}")

    if pool_key not in AERODROME_POOLS:
        print(f"Unknown pool '{pool_key}'. Choose from: {', '.join(AERODROME_POOLS)}")
        sys.exit(2)
    pool_cfg = AERODROME_POOLS[pool_key]

    pos = _aero_read_position(w3, token_id)
    if pos is None:
        print(f"Could not read Aerodrome tokenId {token_id}.")
        sys.exit(2)
    pos_t0, pos_t1, tick_lower, tick_upper, liq = pos
    if pos_t0.lower() != pool_cfg["token0"].lower() or pos_t1.lower() != pool_cfg["token1"].lower():
        print(f"TokenId {token_id} is not a {pool_key} position.")
        sys.exit(2)
    if int(liq) <= 0:
        print(f"TokenId {token_id} has no active liquidity.")
        sys.exit(2)

    amt0 = to_wei_units(amount0, pool_cfg["decimals0"]) if amount0 else 0
    amt1 = to_wei_units(amount1, pool_cfg["decimals1"]) if amount1 else 0
    if amt0 == 0 and amt1 == 0:
        print("At least one of --amount-token0 / --amount-token1 must be > 0")
        sys.exit(2)

    amt0, amt1, cap_notes = _aero_cap_to_balance(account, pool_cfg, amt0, amt1)
    for note in cap_notes:
        print(f"  {note}")
    if amt0 == 0 and amt1 == 0:
        print("  Nothing to deposit after capping to on-chain balance.")
        sys.exit(2)

    pool_tick = None
    try:
        pool_addr = _aero_pool_address(pool_cfg)
        pool_c = w3.eth.contract(address=pool_addr, abi=json.loads(SLOT0_ABI))
        slot0 = pool_c.functions.slot0().call()
        pool_tick = int(slot0[1])
        print(f"  Pool tick (on-chain): {pool_tick}")
    except Exception:
        pass

    amt0, amt1, ratio_notes = _aero_fit_amounts_to_range(
        pool_cfg, amt0, amt1, int(tick_lower), int(tick_upper), pool_tick)
    for note in ratio_notes:
        print(f"  {note}")
    if amt0 == 0 and amt1 == 0:
        print("  Nothing to deposit after fitting amounts to the NFT's current range.")
        sys.exit(2)

    slippage = Decimal(str(slippage_pct)) / Decimal(100)
    amount0_min = int(Decimal(str(amt0)) * (Decimal(1) - slippage))
    amount1_min = int(Decimal(str(amt1)) * (Decimal(1) - slippage))

    sym0, sym1 = pool_cfg["symbol0"], pool_cfg["symbol1"]
    print(f"Pool: {sym0}/{sym1} (tickSpacing={pool_cfg['tickSpacing']})")
    print(f"  TokenId: {token_id} ticks=[{tick_lower}, {tick_upper}]")
    print(f"  {sym0}: {fmt_token_amount(amt0, pool_cfg['decimals0'])}  "
          f"{sym1}: {fmt_token_amount(amt1, pool_cfg['decimals1'])}")

    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
    payload = build_redstone_payload(feeds)
    from eth_abi import encode as abi_encode
    encoded_params = abi_encode(
        ['uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256'],
        [int(token_id), int(amt0), int(amt1), amount0_min, amount1_min, 0],
    )
    calldata = AERODROME_SEL_INCREASE + encoded_params + payload

    sim_ok, sim_info = _aero_simulate_call(w3, acct.address, account.address, calldata)
    if not sim_ok:
        print(f"  Simulation reverted — aborting before broadcast: {sim_info}")
        sys.exit(2)
    print("  Simulation passed.")

    if not execute:
        print("Preview only. Run with --execute to broadcast.")
        return

    tx = {
        "from": acct.address,
        "to": account.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000,
        "chainId": CHAIN_ID,
        "data": "0x" + calldata.hex(),
    }
    receipt = _sign_and_send(w3, acct, tx, "Increase liquidity", timeout=300, fallback_gas=5000000)
    if receipt["status"] != 1:
        sys.exit(2)


def cmd_aero_remove_liquidity(token_ids, percentage: float = 100.0,
                               execute: bool = False):
    """Fully close one or more staked Aerodrome Slipstream positions owned by the
    Degen Account, via batchRemoveStakedLiquidityAerodrome(uint256[]) on the
    AerodromeFacet (selector 0x27bed82e). This single call does the FULL unwind per
    tokenId: unstake from the gauge + remove all liquidity + collect fees + burn the
    NFT (the manual reference close 0x0d65... emitted 41 logs doing exactly this).

    There is NO partial/percentage decrease on this path — it always closes 100%.
    The call is remainsSolvent-gated, so the calldata carries a RedStone signed-price
    payload (same construction as the mint+stake path)."""
    if percentage < 100:
        print(f"  Partial removal ({percentage}%) is not supported on this path — "
              f"batchRemoveStakedLiquidityAerodrome fully closes each position "
              f"(unstake + remove + collect + burn). Re-run without --percentage "
              f"(or with --percentage 100) to fully close.")
        return

    if isinstance(token_ids, int):
        token_ids = [token_ids]
    token_ids = [int(t) for t in token_ids]
    if not token_ids:
        print("  No tokenIds supplied.")
        return

    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account yet.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Degen Account: {pa}")

    # Show what each position holds before closing (NPM.positions() is correct for
    # staked NFTs, which the simplified facet view reports as 0 liquidity).
    for tid in token_ids:
        pos = _aero_read_position(w3, tid)
        if pos is None:
            print(f"  Position {tid}: cannot read (may not exist).")
            continue
        token0, token1, tick_lower, tick_upper, current_liq = pos
        sym0 = _resolve_token_symbol(w3, token0)
        sym1 = _resolve_token_symbol(w3, token1)
        print(f"Position {tid}: {sym0}/{sym1}  ticks=[{tick_lower},{tick_upper}]  "
              f"liquidity={current_liq}")

    # Encode batchRemoveStakedLiquidityAerodrome(uint256[] tokenIds) and append the
    # RedStone payload raw. Byte-for-byte layout (selector + uint256[] head + payload)
    # verified against the manual close 0x0d65...0a50.
    from eth_abi import encode as abi_encode
    encoded_ids = abi_encode(['uint256[]'], [token_ids])
    feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
    payload = build_redstone_payload(feeds)
    close_calldata = AERODROME_SEL_BURN + encoded_ids + payload

    # Pre-flight eth_call simulation — refuse to broadcast on revert. The close path
    # IS RedStone-gated, so the simulated calldata already carries the payload.
    try:
        w3.eth.call({"from": acct.address, "to": account.address,
                     "data": "0x" + close_calldata.hex()})
    except Exception as e:
        print(f"  Simulation reverted — aborting before broadcast: {type(e).__name__}: {str(e)[:200]}")
        return
    print("  Simulation passed.")

    if not execute:
        print("Preview only. Run with --execute to broadcast.")
        return

    tx = {
        "from": acct.address,
        "to": account.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 5000000,
        "chainId": CHAIN_ID,
        "data": "0x" + close_calldata.hex(),
    }
    receipt = _sign_and_send(w3, acct, tx, "Close Aerodrome position(s)", timeout=300, fallback_gas=5000000)
    ok = receipt["status"] == 1
    if ok:
        ids_str = ", ".join(str(t) for t in token_ids)
        print(f"  Fully closed (unstaked + removed + collected + burned): {ids_str}")



def cmd_aero_claim_rewards(execute: bool = False):
    """Claim accrued AERO gauge rewards for all staked Aerodrome positions."""
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account yet.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Degen Account: {pa}")

    try:
        ids = account.functions.getOwnedStakedAerodromeTokenIds().call()
    except Exception:
        print("  Cannot read staked token IDs.")
        return
    if not ids:
        print("  No staked Aerodrome positions.")
        return

    npm = w3.eth.contract(address=Web3.to_checksum_address(AERODROME_NPM),
                          abi=AERODROME_NPM_ABI)
    claimed_any = False
    for tid in ids:
        print(f"\nToken {tid}:")
        try:
            p = npm.functions.positions(tid).call()
            sym0 = _resolve_token_symbol(w3, p[2])
            sym1 = _resolve_token_symbol(w3, p[3])
        except Exception:
            sym0, sym1 = "?", "?"
        unclaimed = _aero_gauge_earned(w3, account, tid)
        print(f"  {sym0}/{sym1}: {unclaimed:.4f} AERO unclaimed")
        if unclaimed <= 0:
            print("  No rewards to claim.")
            continue
        try:
            gauge_addr = npm.functions.ownerOf(tid).call()
            print(f"  Gauge: {gauge_addr}")
        except Exception as e:
            print(f"  Cannot get gauge address: {e}")
            continue
        if not execute:
            print("  Preview only. Run with --execute to broadcast.")
            continue
        try:
            gauge_abi = json.loads('[{"inputs":[],"name":"getReward","outputs":[],"stateMutability":"nonpayable","type":"function"}]')
            gc = w3.eth.contract(address=Web3.to_checksum_address(gauge_addr), abi=gauge_abi)
            reward_data = gc.encode_abi("getReward")
            tx = {
                "from": acct.address,
                "to": Web3.to_checksum_address(gauge_addr),
                "nonce": w3.eth.get_transaction_count(acct.address),
                "gas": 500000,
                "chainId": CHAIN_ID,
                "data": reward_data,
            }
            receipt = _sign_and_send(w3, acct, tx, f"Claim gauge rewards (token {tid})",
                                     timeout=300, fallback_gas=500000)
            ok = receipt["status"] == 1
            if ok:
                print(f"  Rewards claimed for token {tid}.")
                claimed_any = True
            else:
                print(f"  Tx reverted for token {tid}.")
        except Exception as e:
            print(f"  Claim failed: {type(e).__name__}: {str(e)[:200]}")
    if claimed_any:
        print("\nDone. Check account balance for AERO.")
    else:
        print("\nNote: gauge getReward() may be callable only from the Degen Account itself.")
        print("In that case, rewards are auto-claimed when the position is closed.")




def cmd_aero_collect_fees(token_id: int, execute: bool = False):
    """Collect accrued swap fees from an Aerodrome Slipstream position.
    Fees accumulate as tokensOwed0/tokensOwed1 on the NPM; collect sends them
    to the Degen Account. After collecting all fees (and removing all liquidity),
    the NFT position can be burned.

    Uses the facet's collect/burn path (selector 0x92b5a47e)."""
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account yet.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Degen Account: {pa}")

    # Read position for display via NPM.positions() — correct for staked NFTs (the
    # simplified facet view reports liquidity=0 once the gauge owns the NFT).
    pos = _aero_read_position(w3, token_id)
    if pos is None:
        print(f"  Cannot read position {token_id}.")
        return
    token0, token1, tick_lower, tick_upper, liq = pos
    sym0 = _resolve_token_symbol(w3, token0)
    sym1 = _resolve_token_symbol(w3, token1)
    print(f"Position {token_id}: {sym0}/{sym1}  liquidity={liq}")

    # Also try to read uncollected fees from the NPM directly
    try:
        npm = w3.eth.contract(address=Web3.to_checksum_address(AERODROME_NPM),
                              abi=AERODROME_NPM_ABI)
        npm_pos = npm.functions.positions(token_id).call()
        owed0 = npm_pos[10]  # tokensOwed0
        owed1 = npm_pos[11]  # tokensOwed1
        if owed0 > 0 or owed1 > 0:
            print(f"  Uncollected fees: {owed0} ({sym0}) + {owed1} ({sym1})")
        else:
            print(f"  No uncollected fees.")
    except Exception:
        print(f"  (Cannot fetch uncollected fees from NPM directly)")

    if not execute:
        print("Preview only. Run with --execute to broadcast.")
        return

    # Build RedStone payload (collect may be solvency-gated)
    try:
        feeds = sorted(REDSTONE_AVAILABLE_FEEDS)
        payload = build_redstone_payload(feeds)
    except Exception:
        payload = b""

    # Encode collect call with the probed selector
    collect_data = account.encode_abi("collectAerodromeFees", args=[token_id])
    collect_params_bytes = bytes.fromhex(collect_data[2:])[4:]
    collect_calldata = AERODROME_SEL_COLLECT + collect_params_bytes
    if payload:
        collect_calldata += payload

    tx = {
        "from": acct.address,
        "to": account.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 3000000,
        "chainId": CHAIN_ID,
        "data": "0x" + collect_calldata.hex(),
    }
    receipt = _sign_and_send(w3, acct, tx, "Collect fees", timeout=300, fallback_gas=3000000)
    ok = receipt["status"] == 1


# ─── Aerodrome Auto-Rebalancer (AerodromeRebalancerFacet) ─────────────────────

def _aero_resolve_npm(w3, token_id: int):
    """Resolve which Aerodrome deployment owns a tokenId by trying positions() on
    each NPM (v2 first — our whitelisted cbbtc-200 lives there — then v3). The
    non-owning deployment reverts. Returns (deployment, position_struct) where
    deployment is 'v2'/'v3' and the struct is the raw positions() tuple, or
    (None, None) if neither deployment knows the tokenId (burned/unknown)."""
    for ver, addr in (("v2", AERODROME_NPM_V2), ("v3", AERODROME_NPM_V3)):
        try:
            npm = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=AERODROME_NPM_ABI)
            return ver, npm.functions.positions(token_id).call()
        except Exception:
            continue
    return None, None


def _bps_band_from_width(width_pct: float) -> tuple:
    """Units bridge: a symmetric ±width_pct LP band as (lowerRangeBps, upperRangeBps).
    1% = 100 bps; lower edge is negative, upper positive. e.g. width_pct=3 ->
    (-300, +300)."""
    band = round(width_pct * 100)
    return (-band, band)


def _trigger_bps(mode: str, range_bps: int, trigger_bps: int) -> tuple:
    """Map a mode + trigger magnitude to (lowerTriggerBps, upperTriggerBps), applying
    the sign rules from the feature doc §3:

      OUTSIDE — rebalance only after price LEAVES the range: lowerTrigger < 0
                (a further drop below the lower edge), upperTrigger > 0.
      INSIDE  — rebalance EARLY while still in range: lowerTrigger > 0,
                upperTrigger < 0, with |trigger| < |range|.

    `range_bps` is the positive band magnitude (upperRangeBps). `trigger_bps` is the
    caller-supplied positive magnitude. Returns the signed pair; raises ValueError on
    an INSIDE trigger that is not strictly inside the band."""
    mag = abs(trigger_bps)
    if mode == "inside":
        if mag >= range_bps:
            raise ValueError(
                f"INSIDE trigger magnitude {mag} bps must be smaller than the range "
                f"band {range_bps} bps (|trigger| < |range|).")
        return (mag, -mag)
    # OUTSIDE (default)
    return (-mag, mag)


def _aero_rebalance_events(account: str, from_block=None, to_block="latest"):
    """Read this account's rebalance lifecycle/execution events from the shared
    RebalanceEventEmitter. Filters server-side by topics=[topic0, padded(account)],
    one getLogs per event type. Decodes the non-indexed args per the doc §4 layout and
    chains RebalanceExecuted tokenId->newTokenId so a logical position can be followed
    across rebalances (the old NFT is burned each time). Returns a list of dicts
    sorted by (blockNumber, logIndex)."""
    w3 = get_w3()
    emitter = Web3.to_checksum_address(REBALANCE_EVENT_EMITTER)
    acct_topic = "0x" + Web3.to_checksum_address(account)[2:].lower().rjust(64, "0")
    latest = w3.eth.block_number
    if to_block == "latest":
        to_block = latest
    if from_block is None:
        # The shared emitter went live with the 2026-06-16 migration; a recent window
        # covers all post-migration history. Public Base RPCs cap getLogs at 50k blocks,
        # so default to ~180k blocks back (≈4 days at 2s/block) and page under the cap.
        from_block = max(0, latest - 180_000)
    # Non-indexed arg types per event (indexed userContract/user/tokenId/executor live
    # in the topics, not the data blob).
    nonindexed = {
        "RebalanceOrderCreated":  ["int24", "int24", "int24", "int24", "uint256", "uint256"],
        "RebalanceOrderUpdated":  ["int24", "int24", "int24", "int24", "uint256"],
        "RebalanceOrderCanceled": ["uint256"],
        "RebalanceExecuted":      ["uint160", "uint160", "uint256", "uint256"],
    }
    CHUNK = 45_000  # under the public-RPC 50k-block getLogs cap
    out = []
    for name, topic0 in REBALANCE_TOPIC0.items():
        logs = []
        start = from_block
        try:
            while start <= to_block:
                end = min(start + CHUNK - 1, to_block)
                logs.extend(w3.eth.get_logs({"address": emitter, "fromBlock": start,
                                             "toBlock": end, "topics": [topic0, acct_topic]}))
                start = end + 1
        except Exception as e:
            print(f"  (getLogs failed for {name}: {type(e).__name__}: {str(e)[:120]})")
            continue
        for lg in logs:
            ev = {"event": name, "block": lg["blockNumber"],
                  "logIndex": lg["logIndex"], "tx": lg["transactionHash"].hex()}
            # topic2 is tokenId (Created/Updated/Canceled) or executor (Executed);
            # for Executed the OLD tokenId is topic3.
            topics = lg["topics"]
            decoded = w3.codec.decode(nonindexed[name], bytes(lg["data"]))
            if name == "RebalanceExecuted":
                ev["executor"] = "0x" + topics[2].hex()[-40:]
                ev["tokenId"] = int(topics[3].hex(), 16)
                ev["oldRefSqrtPriceX96"] = decoded[0]
                ev["currentSqrtPriceX96"] = decoded[1]
                ev["newTokenId"] = decoded[2]
                ev["timestamp"] = decoded[3]
            else:
                ev["tokenId"] = int(topics[3].hex(), 16) if len(topics) > 3 else None
                if name == "RebalanceOrderCreated":
                    ev["rangeBps"] = [decoded[0], decoded[1]]
                    ev["triggerBps"] = [decoded[2], decoded[3]]
                    ev["executionFeeWeth"] = decoded[4]
                    ev["timestamp"] = decoded[5]
                elif name == "RebalanceOrderUpdated":
                    ev["rangeBps"] = [decoded[0], decoded[1]]
                    ev["triggerBps"] = [decoded[2], decoded[3]]
                    ev["timestamp"] = decoded[4]
                elif name == "RebalanceOrderCanceled":
                    ev["timestamp"] = decoded[0]
            out.append(ev)
    out.sort(key=lambda e: (e["block"], e["logIndex"]))
    return out


def _order_to_dict(token_id: int, order: tuple) -> dict:
    """Decode a RebalanceOrder tuple (referenceSqrtPriceX96, lowerRangeBps,
    upperRangeBps, lowerTriggerBps, upperTriggerBps, mintSlippageBps, swapSlippageBps,
    createdOn, maxExecutionFee) into a JSON-friendly dict."""
    return {
        "tokenId": int(token_id),
        "referenceSqrtPriceX96": int(order[0]),
        "rangeBps": [int(order[1]), int(order[2])],
        "triggerBps": [int(order[3]), int(order[4])],
        "mintSlippageBps": int(order[5]),
        "swapSlippageBps": int(order[6]),
        "createdOn": int(order[7]),
        "maxExecutionFeeWei": int(order[8]),
        "maxExecutionFeeWeth": int(order[8]) / 1e18,
    }


def cmd_aero_rebalance_status(token_id: int = None, check: bool = False,
                              history: bool = False, as_json: bool = False):
    """Read-only: list active on-chain rebalance orders on the Degen Account (or one
    order if --token-id). Decodes each order's band/trigger bps, createdOn, and max
    execution fee, and resolves the underlying Aerodrome position (v2 then v3 NPM).
    With --check, also calls shouldRebalance (RedStone-gated). With --history, reads
    the shared emitter for this account's order lifecycle + execution events."""
    w3 = get_w3()
    acct = get_account()
    pa = get_prime_account(w3, acct.address)
    if not pa:
        if as_json:
            print(json.dumps({"account": None, "orders": []}))
        else:
            print(f"Wallet: {acct.address}\nNo Degen Account yet - no rebalance orders.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)

    # Pull orders (plain storage reads, no RedStone).
    try:
        if token_id is not None:
            raw = account.functions.getRebalanceOrder(token_id).call()
            orders = [(token_id, raw)] if int(raw[7]) > 0 else []
        else:
            orders = [(o[0], o[1]) for o in account.functions.getAllRebalanceOrders().call()]
    except Exception as e:
        if as_json:
            print(json.dumps({"account": pa, "error": f"{type(e).__name__}: {str(e)[:200]}"}))
        else:
            print(f"  Rebalance order read failed: {type(e).__name__}: {e}")
        return

    decoded = []
    for tid, order in orders:
        d = _order_to_dict(tid, order)
        ver, pos = _aero_resolve_npm(w3, tid)
        if pos is not None:
            sym0 = _resolve_token_symbol(w3, pos[2])
            sym1 = _resolve_token_symbol(w3, pos[3])
            d["position"] = {"deployment": ver, "token0": sym0, "token1": sym1,
                             "tickLower": int(pos[5]), "tickUpper": int(pos[6]),
                             "liquidity": int(pos[7])}
        else:
            d["position"] = None
        if check:
            try:
                payload = build_redstone_payload(degen_account_price_feeds(account))
                d["shouldRebalance"] = bool(redstone_view_call(
                    w3, account, "shouldRebalance", payload, args=[tid])[0])
            except Exception as e:
                d["shouldRebalance"] = None
                d["shouldRebalanceError"] = f"{type(e).__name__}: {str(e)[:150]}"
        decoded.append(d)

    events = None
    if history:
        events = _aero_rebalance_events(pa)

    if as_json:
        out = {"account": pa, "orders": decoded}
        if events is not None:
            out["events"] = events
        print(json.dumps(out))
        return

    print(f"Wallet: {acct.address}")
    print(f"Degen Account: {pa}")
    if not decoded:
        print("  No active rebalance orders.")
    else:
        print(f"  {len(decoded)} active rebalance order(s):")
        for d in decoded:
            rng = d["rangeBps"]
            trg = d["triggerBps"]
            mode = "inside" if trg[0] > 0 else "outside"
            pos = d["position"]
            pair = f"{pos['token0']}/{pos['token1']} ({pos['deployment']})" if pos else "position not resolved"
            created = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(d["createdOn"])) if d["createdOn"] else "n/a"
            print(f"    [{d['tokenId']}] {pair}")
            print(f"        range: {rng[0]/100:+.2f}% / {rng[1]/100:+.2f}%  "
                  f"trigger: {trg[0]/100:+.2f}% / {trg[1]/100:+.2f}% ({mode})")
            print(f"        mint/swap slippage: {d['mintSlippageBps']/100:.2f}% / {d['swapSlippageBps']/100:.2f}%  "
                  f"max fee: {d['maxExecutionFeeWeth']:.6g} WETH")
            print(f"        created: {created}")
            if check:
                sr = d.get("shouldRebalance")
                if sr is None:
                    print(f"        shouldRebalance: ERROR ({d.get('shouldRebalanceError', '?')})")
                else:
                    print(f"        shouldRebalance: {sr}")
    if events is not None:
        if not events:
            print("  No rebalance history events for this account.")
        else:
            print(f"  History ({len(events)} event(s)):")
            for e in events:
                ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(e["timestamp"])) if e.get("timestamp") else "?"
                if e["event"] == "RebalanceExecuted":
                    print(f"    {ts}  Executed  tokenId {e['tokenId']} -> {e['newTokenId']}  (blk {e['block']})")
                else:
                    label = e["event"].replace("RebalanceOrder", "")
                    print(f"    {ts}  {label}  tokenId {e['tokenId']}  (blk {e['block']})")


def _build_rebalance_order_params(token_id: int, width_pct: float, mode: str,
                                  trigger_bps: int, max_fee_weth: float,
                                  mint_slip_bps: int, swap_slip_bps: int) -> tuple:
    """Build the CreateRebalanceOrderParams tuple from human inputs. Returns
    (params_tuple, preview_dict). Raises ValueError on an out-of-band INSIDE trigger."""
    lower_range, upper_range = _bps_band_from_width(width_pct)
    lower_trig, upper_trig = _trigger_bps(mode, upper_range, trigger_bps)
    fee_wei = int(Decimal(str(max_fee_weth)) * Decimal(10) ** 18)
    params = (token_id, lower_range, upper_range, lower_trig, upper_trig,
              int(mint_slip_bps), int(swap_slip_bps), fee_wei)
    preview = {
        "tokenId": token_id,
        "rangeBps": [lower_range, upper_range],
        "triggerBps": [lower_trig, upper_trig],
        "mode": mode,
        "mintSlippageBps": int(mint_slip_bps),
        "swapSlippageBps": int(swap_slip_bps),
        "maxExecutionFeeWeth": float(max_fee_weth),
        "maxExecutionFeeWei": fee_wei,
    }
    return params, preview


def _aero_rebalance_write(fn_name: str, params: tuple, preview: dict, label: str,
                          execute: bool):
    """Shared create/update path: validate the execution fee against the live
    protocol-fee floor, pre-flight eth_call (abort on revert), print a human preview,
    and broadcast only under --execute. RedStone gating for the WRITE is settled
    empirically: createRebalanceOrder/updateRebalanceOrder only STORE the order
    (fee-floor check, no solvency), so no payload is appended; the pre-flight dry-call
    is the proof (it passes clean without a payload)."""
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account yet.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Degen Account: {pa}")

    # Validate executionFeeWeth > the live protocol-fee floor (governance-settable).
    fee_wei = preview["maxExecutionFeeWei"]
    try:
        tm = get_token_manager(w3)
        floor = tm.functions.getAutomationProtocolFee().call()
    except Exception as e:
        print(f"  Cannot read getAutomationProtocolFee floor: {type(e).__name__}: {str(e)[:150]}")
        return
    if fee_wei <= floor:
        print(f"  executionFeeWeth ({fee_wei} wei = {fee_wei/1e18:.6g} WETH) must EXCEED "
              f"the protocol fee floor ({floor} wei = {floor/1e18:.6g} WETH). "
              f"Raise --max-fee-weth.")
        return

    # Human preview.
    rng, trg = preview["rangeBps"], preview["triggerBps"]
    print(f"  {label}:")
    print(f"    tokenId: {preview['tokenId']}")
    print(f"    range: {rng[0]/100:+.2f}% / {rng[1]/100:+.2f}%")
    print(f"    trigger: {trg[0]/100:+.2f}% / {trg[1]/100:+.2f}% ({preview['mode']})")
    print(f"    mint/swap slippage: {preview['mintSlippageBps']/100:.2f}% / {preview['swapSlippageBps']/100:.2f}%")
    print(f"    max execution fee: {preview['maxExecutionFeeWeth']:.6g} WETH "
          f"(ceiling, not a deposit; protocol floor {floor/1e18:.6g} WETH)")

    # Build calldata: selector + ABI-encoded params, NO RedStone payload (order store).
    calldata = bytes.fromhex(account.encode_abi(fn_name, args=[params])[2:])

    # Pre-flight eth_call — abort on revert.
    try:
        w3.eth.call({"from": acct.address, "to": account.address,
                     "data": "0x" + calldata.hex()})
    except Exception as e:
        print(f"  Simulation reverted — aborting before broadcast: {type(e).__name__}: {str(e)[:200]}")
        return
    print("  Simulation passed.")

    if not execute:
        print("Preview only. Run with --execute to broadcast.")
        return

    tx = {
        "from": acct.address,
        "to": account.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 2000000,
        "chainId": CHAIN_ID,
        "data": "0x" + calldata.hex(),
    }
    receipt = _sign_and_send(w3, acct, tx, label, timeout=300, fallback_gas=2000000)
    if receipt["status"] == 1:
        print(f"  {label} confirmed for tokenId {preview['tokenId']}.")


def cmd_aero_rebalance_create(token_id: int, width_pct: float, mode: str = "outside",
                              trigger_bps: int = 100, max_fee_weth: float = 0.001,
                              mint_slip_bps: int = 100, swap_slip_bps: int = 100,
                              execute: bool = False):
    """Turn on the auto-rebalancer for an existing Aerodrome position. Builds
    CreateRebalanceOrderParams from a symmetric ±width_pct band + a trigger mode, then
    creates the order via createRebalanceOrder."""
    try:
        params, preview = _build_rebalance_order_params(
            token_id, width_pct, mode, trigger_bps, max_fee_weth, mint_slip_bps, swap_slip_bps)
    except ValueError as e:
        print(f"  {e}")
        return
    _aero_rebalance_write("createRebalanceOrder", params, preview,
                          "Create rebalance order", execute)


def cmd_aero_rebalance_update(token_id: int, width_pct: float, mode: str = "outside",
                              trigger_bps: int = 100, max_fee_weth: float = 0.001,
                              mint_slip_bps: int = 100, swap_slip_bps: int = 100,
                              execute: bool = False):
    """Re-tune an existing rebalance order's bands/trigger/fee via updateRebalanceOrder.
    Same args/struct as create."""
    try:
        params, preview = _build_rebalance_order_params(
            token_id, width_pct, mode, trigger_bps, max_fee_weth, mint_slip_bps, swap_slip_bps)
    except ValueError as e:
        print(f"  {e}")
        return
    _aero_rebalance_write("updateRebalanceOrder", params, preview,
                          "Update rebalance order", execute)


def cmd_aero_rebalance_cancel(token_id: int, execute: bool = False):
    """Turn off the auto-rebalancer for a position via cancelRebalanceOrder (bare
    uint256 tokenId). This is the rollback primitive — cancelling reverts a position
    to off-chain monitoring. Not solvency-gated, so no RedStone payload (the pre-flight
    dry-call confirms it passes clean without one)."""
    w3 = get_w3()
    acct = get_account()
    print(f"Wallet: {acct.address}")
    pa = get_prime_account(w3, acct.address)
    if not pa:
        print("No Degen Account yet.")
        return
    account = w3.eth.contract(address=Web3.to_checksum_address(pa), abi=PRIME_ACCOUNT_ABI)
    print(f"Degen Account: {pa}")
    print(f"  Cancel rebalance order for tokenId {token_id}")

    calldata = bytes.fromhex(account.encode_abi("cancelRebalanceOrder", args=[token_id])[2:])
    try:
        w3.eth.call({"from": acct.address, "to": account.address,
                     "data": "0x" + calldata.hex()})
    except Exception as e:
        print(f"  Simulation reverted — aborting before broadcast: {type(e).__name__}: {str(e)[:200]}")
        return
    print("  Simulation passed.")

    if not execute:
        print("Preview only. Run with --execute to broadcast.")
        return

    tx = {
        "from": acct.address,
        "to": account.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 1500000,
        "chainId": CHAIN_ID,
        "data": "0x" + calldata.hex(),
    }
    receipt = _sign_and_send(w3, acct, tx, "Cancel rebalance order", timeout=300, fallback_gas=1500000)
    if receipt["status"] == 1:
        print(f"  Rebalance order canceled for tokenId {token_id}.")


def main():
    check_version()
    try:
        _dispatch()
    except RuntimeError as e:
        print(f"degenprime: {e}", file=sys.stderr)
        sys.exit(1)

def _dispatch():
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    # Global signing-key override: --key <0xhex>, stripped before command dispatch.
    global _SELECTED_AGENT, _CLI_KEY
    if "--key" in args:
        i = args.index("--key")
        if i + 1 >= len(args):
            print("--key requires a hex key. Example: --key 0xabc...")
            return
        _CLI_KEY = args[i + 1]
        del args[i:i + 2]
    if "--as" in args:
        i = args.index("--as")
        if i + 1 >= len(args):
            print("--as requires an agent name. Example: --as parakletos")
            return
        _SELECTED_AGENT = args[i + 1]
        del args[i:i + 2]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    cmd = args[0]
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
        cmd_summary(as_json="--json" in args)
    elif cmd == "defi":
        cmd_defi("--json" in args)
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
    elif cmd == "execute-pool-withdrawal":
        pool, index = None, None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool = args[i + 1]
            if a == "--index" and i + 1 < len(args): index = int(args[i + 1])
        if not pool or index is None:
            print("Usage: degenprime execute-pool-withdrawal --pool usdc --index N [--execute]")
            return
        if pool not in POOLS:
            print(f"Unknown pool '{pool}'. Choose from: {', '.join(POOLS)}")
            return
        cmd_execute_pool_withdrawal(pool, index, execute)
    elif cmd == "aerodrome-positions":
        cmd_aerodrome_positions()
    elif cmd == "aero-add-liquidity":
        pool_key = None
        amt0, amt1 = None, None
        slippage = 1.0
        width = 2.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool_key = args[i + 1]
            if a == "--amount-weth" and i + 1 < len(args): amt0 = float(args[i + 1])
            if a == "--amount-usdc" and i + 1 < len(args): amt1 = float(args[i + 1])
            if a == "--amount-token0" and i + 1 < len(args): amt0 = float(args[i + 1])
            if a == "--amount-token1" and i + 1 < len(args): amt1 = float(args[i + 1])
            if a == "--amount-aero" and i + 1 < len(args): amt0 = float(args[i + 1])
            if a == "--amount-cbbtc" and i + 1 < len(args): amt1 = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
            if a == "--width" and i + 1 < len(args): width = float(args[i + 1])
        if not pool_key or (amt0 is None and amt1 is None):
            print("Usage: degenprime aero-add-liquidity --pool weth-usdc-100 --amount-weth 0.05 --amount-usdc 100 [--slippage 1] [--width 2] [--execute]")
            return
        cmd_aero_add_liquidity(pool_key, amt0, amt1, slippage, execute, width)
    elif cmd == "aero-increase-liquidity":
        pool_key = None
        token_id = None
        amt0, amt1 = None, None
        slippage = 1.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--pool" and i + 1 < len(args): pool_key = args[i + 1]
            if a == "--token-id" and i + 1 < len(args): token_id = int(args[i + 1])
            if a == "--amount-token0" and i + 1 < len(args): amt0 = float(args[i + 1])
            if a == "--amount-token1" and i + 1 < len(args): amt1 = float(args[i + 1])
            if a == "--amount-weth" and i + 1 < len(args): amt0 = float(args[i + 1])
            if a == "--amount-aero" and i + 1 < len(args): amt0 = float(args[i + 1])
            if a == "--amount-cbbtc" and i + 1 < len(args): amt1 = float(args[i + 1])
            if a == "--amount-usdc" and i + 1 < len(args): amt1 = float(args[i + 1])
            if a == "--slippage" and i + 1 < len(args): slippage = float(args[i + 1])
        if not pool_key or token_id is None or (amt0 is None and amt1 is None):
            print("Usage: degenprime aero-increase-liquidity --pool weth-usdc-100 --token-id N --amount-token0 X --amount-token1 Y [--slippage 1] [--execute]")
            return
        cmd_aero_increase_liquidity(pool_key, token_id, amt0, amt1, slippage, execute)
    elif cmd == "aero-remove-liquidity":
        token_ids = []
        percentage = 100.0
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--token-id" and i + 1 < len(args): token_ids.append(int(args[i + 1]))
            if a == "--percentage" and i + 1 < len(args): percentage = float(args[i + 1])
        if not token_ids:
            print("Usage: degenprime aero-remove-liquidity --token-id N [--token-id M ...] [--execute]")
            print("  Fully closes (unstake + remove + collect + burn) each staked position. Full close only.")
            return
        cmd_aero_remove_liquidity(token_ids, percentage, execute)
    elif cmd == "aero-collect-fees":
        token_id = None
        execute = "--execute" in args
        for i, a in enumerate(args):
            if a == "--token-id" and i + 1 < len(args): token_id = int(args[i + 1])
        if token_id is None:
            print("Usage: degenprime aero-collect-fees --token-id N [--execute]")
            return
        cmd_aero_collect_fees(token_id, execute)
    elif cmd == "aero-claim-rewards":
        execute = "--execute" in args
        cmd_aero_claim_rewards(execute)
    elif cmd == "aero-rebalance":
        sub = args[1] if len(args) > 1 and not args[1].startswith("--") else None
        execute = "--execute" in args
        token_id = None
        width_pct = None
        mode = "outside"
        trigger_bps = 100
        max_fee_weth = 0.001
        mint_slip_bps = 100
        swap_slip_bps = 100
        as_json = "--json" in args
        check = "--check" in args
        history = "--history" in args
        for i, a in enumerate(args):
            if a == "--token-id" and i + 1 < len(args): token_id = int(args[i + 1])
            if a == "--width-pct" and i + 1 < len(args): width_pct = float(args[i + 1])
            if a == "--mode" and i + 1 < len(args): mode = args[i + 1]
            if a == "--trigger-bps" and i + 1 < len(args): trigger_bps = int(args[i + 1])
            if a == "--max-fee-weth" and i + 1 < len(args): max_fee_weth = float(args[i + 1])
            if a == "--mint-slip-bps" and i + 1 < len(args): mint_slip_bps = int(args[i + 1])
            if a == "--swap-slip-bps" and i + 1 < len(args): swap_slip_bps = int(args[i + 1])
        if mode not in ("outside", "inside"):
            print("--mode must be 'outside' or 'inside'.")
            return
        if sub in (None, "status"):
            cmd_aero_rebalance_status(token_id, check, history, as_json)
        elif sub in ("create", "update"):
            if token_id is None or width_pct is None:
                print(f"Usage: degenprime aero-rebalance {sub} --token-id N --width-pct W "
                      f"[--mode outside|inside] [--trigger-bps T] [--max-fee-weth F] "
                      f"[--mint-slip-bps 100] [--swap-slip-bps 100] [--execute]")
                return
            fn = cmd_aero_rebalance_create if sub == "create" else cmd_aero_rebalance_update
            fn(token_id, width_pct, mode, trigger_bps, max_fee_weth,
               mint_slip_bps, swap_slip_bps, execute)
        elif sub == "cancel":
            if token_id is None:
                print("Usage: degenprime aero-rebalance cancel --token-id N [--execute]")
                return
            cmd_aero_rebalance_cancel(token_id, execute)
        else:
            print(f"Unknown aero-rebalance subcommand '{sub}'. "
                  f"Choose from: status, create, update, cancel.")
    elif cmd == "health":
        os.environ.setdefault("PRIMECLI_TOOL", sys.argv[0])
        if health_monitor:
            health_monitor.cli()
        else:
            print("health_monitor module not available")
    else:
        print(f"Unknown command: {cmd}\n{__doc__}")

if __name__ == "__main__":
    main()
