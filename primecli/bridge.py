#!/usr/bin/env python3
"""bridge - cross-chain bridge for the primecli wallets (Avalanche / Base / Arbitrum).

Move native or ERC-20 funds between chains for any wallet primecli already knows
(parakletos, paraklaudios, core1) via the same `--as <agent>` interface the
protocol commands use. Routing goes through the LiFi aggregator (li.quest) — the
same endpoint and tx shape as a same-chain swap, just with toChain != fromChain.

    bridge --as <agent> --from <chain> --to <chain> --token <SYM> --amount <decimal>
           [--to-address <addr>] [--to-token <SYM>] [--slippage <pct>] [--execute]

Chains: avalanche (43114) | base (8453) | arbitrum (42161).

SAFETY
  * Dry-run by DEFAULT. Only --execute broadcasts (exactly like the protocol commands).
  * Self-bridge only (v1): the destination is the SIGNER'S OWN address. Passing a
    --to-address that differs from the signer is REFUSED.
  * Destination token defaults to the destination chain's native gas token
    (gas-top-up default, e.g. AVAX -> ETH when bridging to Base). Override with
    --to-token if you want the same asset on the other side.
  * Slippage cap default 1.0%. If the quote's toAmountMin implies worse than the
    cap, the bridge is REFUSED.

Routing keys (set if LiFi rate-limits you):
  LIFI_API_KEY  -> sent as x-lifi-api-key header on quote/status calls.
"""

import argparse
import os
import sys
import time
from decimal import Decimal

import requests
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from primecli._wallets import AGENTS, _agent_key

LIFI_QUOTE_URL = "https://li.quest/v1/quote"
LIFI_STATUS_URL = "https://li.quest/v1/status"

# Native-token sentinel LiFi expects for "the chain's gas token".
NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

# Chain config — the single source of truth for this command. Mirrors the shape
# of paraklaudios/config/wallet.json but kept inline (bridge spans all three
# chains, where each protocol module owns just one). RPCs are overridable via
# BRIDGE_<CHAIN>_RPC so a flaky public endpoint can be swapped without an edit.
CHAINS = {
    "avalanche": {
        "chain_id": 43114,
        "rpc": os.environ.get("BRIDGE_AVALANCHE_RPC", "https://api.avax.network/ext/bc/C/rpc"),
        "explorer": "https://snowtrace.io",
        "poa": True,
        "native": "AVAX",
        "tokens": {
            "AVAX":  {"address": None, "decimals": 18},
            "WAVAX": {"address": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7", "decimals": 18},
            "USDC":  {"address": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", "decimals": 6},
            "USDT":  {"address": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7", "decimals": 6},
        },
    },
    "base": {
        "chain_id": 8453,
        "rpc": os.environ.get("BRIDGE_BASE_RPC", "https://mainnet.base.org"),
        "explorer": "https://basescan.org",
        "poa": False,
        "native": "ETH",
        "tokens": {
            "ETH":   {"address": None, "decimals": 18},
            "WETH":  {"address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "USDC":  {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "USDbC": {"address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": 6},
        },
    },
    "arbitrum": {
        "chain_id": 42161,
        "rpc": os.environ.get("BRIDGE_ARBITRUM_RPC", "https://arb1.arbitrum.io/rpc"),
        "explorer": "https://arbiscan.io",
        "poa": False,
        "native": "ETH",
        "tokens": {
            "ETH":  {"address": None, "decimals": 18},
            "WETH": {"address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18},
            "USDC": {"address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
            "USDT": {"address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "decimals": 6},
            "ARB":  {"address": "0x912CE59144191C1204E64559FE8253a0e49E6548", "decimals": 18},
        },
    },
}


def _hexint(v, default=0):
    """LiFi returns ints sometimes as 0x-hex strings, sometimes as decimals.
    Parse robustly (mirrors walletctl's tolerance)."""
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v)
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


def resolve_token(chain, symbol):
    """Return {symbol, address|None, decimals, native, lifi} for a symbol on a chain."""
    cfg = CHAINS[chain]
    sym = symbol.upper()
    if sym not in cfg["tokens"]:
        known = ", ".join(cfg["tokens"])
        raise SystemExit(
            f"Unknown token '{symbol}' on {chain}. Known: {known}. "
            f"(Add it to CHAINS in bridge.py if you need another.)"
        )
    t = cfg["tokens"][sym]
    native = t["address"] is None
    addr = None if native else Web3.to_checksum_address(t["address"])
    return {
        "symbol": sym,
        "address": addr,
        "decimals": t["decimals"],
        "native": native,
        "lifi": NATIVE_SENTINEL if native else addr,
    }


def w3_for(chain):
    cfg = CHAINS[chain]
    w3 = Web3(Web3.HTTPProvider(cfg["rpc"], request_kwargs={"timeout": 20}))
    if cfg["poa"]:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def _lifi_headers():
    key = os.environ.get("LIFI_API_KEY")
    return {"x-lifi-api-key": key} if key else {}


def lifi_quote(from_chain, to_chain, from_tok, to_tok, raw_amount, address, slippage):
    params = {
        "fromChain": CHAINS[from_chain]["chain_id"],
        "toChain": CHAINS[to_chain]["chain_id"],
        "fromToken": from_tok["lifi"],
        "toToken": to_tok["lifi"],
        "fromAmount": str(raw_amount),
        "fromAddress": address,
        "toAddress": address,  # self-bridge: destination is the signer's own EOA
        "slippage": slippage,
    }
    r = requests.get(LIFI_QUOTE_URL, params=params, headers=_lifi_headers(), timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"LiFi quote failed ({r.status_code}): {r.text[:400]}")
    return r.json()


def build_preview(quote, from_chain, to_chain, from_tok, to_tok, amount, slippage):
    """Turn a LiFi quote into a structured preview dict (no I/O, unit-testable)."""
    est = quote["estimate"]
    to_amount = Decimal(str(est["toAmount"])) / (Decimal(10) ** to_tok["decimals"])
    to_amount_min = Decimal(str(est["toAmountMin"])) / (Decimal(10) ** to_tok["decimals"])
    expected = to_amount if to_amount > 0 else Decimal(1)
    implied_slippage = float((to_amount - to_amount_min) / expected) if to_amount > 0 else 0.0
    fee_costs = est.get("feeCosts") or []
    gas_costs = est.get("gasCosts") or []
    fee_usd = sum(float(f.get("amountUSD", 0) or 0) for f in fee_costs)
    gas_usd = sum(float(g.get("amountUSD", 0) or 0) for g in gas_costs)
    tool = quote.get("tool") or quote.get("toolDetails", {}).get("name") or est.get("tool") or "?"
    return {
        "from_chain": from_chain,
        "to_chain": to_chain,
        "from_token": from_tok["symbol"],
        "to_token": to_tok["symbol"],
        "amount": amount,
        "to_amount": to_amount,
        "to_amount_min": to_amount_min,
        "implied_slippage": implied_slippage,
        "slippage_cap": slippage,
        "fee_usd": fee_usd,
        "gas_usd": gas_usd,
        "tool": tool,
    }


def print_preview(p, execute):
    mode = "EXECUTE" if execute else "DRY-RUN"
    print(f"=== BRIDGE ({mode}) ===")
    print(f"  from:         {p['amount']} {p['from_token']} on {p['from_chain']}")
    print(f"  to (est):     {p['to_amount']} {p['to_token']} on {p['to_chain']}")
    print(f"  to (min):     {p['to_amount_min']} {p['to_token']}  "
          f"(implied slippage {p['implied_slippage']*100:.3f}%, cap {p['slippage_cap']*100:.2f}%)")
    print(f"  route tool:   {p['tool']}")
    print(f"  protocol fee: ${p['fee_usd']:.4f}")
    print(f"  source gas:   ${p['gas_usd']:.4f}")


def enforce_slippage(preview):
    """Refuse if the quote's implied slippage is worse than the cap."""
    if preview["implied_slippage"] > preview["slippage_cap"] + 1e-9:
        raise SystemExit(
            f"REFUSED: quote implies {preview['implied_slippage']*100:.3f}% slippage, "
            f"over the {preview['slippage_cap']*100:.2f}% cap. "
            f"Raise --slippage only if you understand the price impact."
        )


def _send_tx(w3, acct, tx_req, chain_id):
    """Sign and broadcast a LiFi transactionRequest on the source chain."""
    tx = {
        "chainId": chain_id,
        "from": acct.address,
        "to": Web3.to_checksum_address(tx_req["to"]),
        "data": tx_req["data"],
        "value": _hexint(tx_req.get("value", 0)),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": _hexint(tx_req["gasLimit"], 0),
    }
    if "maxFeePerGas" in tx_req and "maxPriorityFeePerGas" in tx_req:
        tx["maxFeePerGas"] = _hexint(tx_req["maxFeePerGas"])
        tx["maxPriorityFeePerGas"] = _hexint(tx_req["maxPriorityFeePerGas"])
        tx["type"] = 2
    elif "gasPrice" in tx_req:
        tx["gasPrice"] = _hexint(tx_req["gasPrice"])
    signed = acct.sign_transaction(tx)
    return w3.eth.send_raw_transaction(signed.raw_transaction)


def poll_status(tx_hash, from_chain, to_chain, timeout=300, interval=15):
    """Poll LiFi /v1/status until DONE/FAILED or timeout. Best-effort."""
    params = {
        "txHash": tx_hash,
        "fromChain": CHAINS[from_chain]["chain_id"],
        "toChain": CHAINS[to_chain]["chain_id"],
    }
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = requests.get(LIFI_STATUS_URL, params=params, headers=_lifi_headers(), timeout=20)
            if r.status_code == 200:
                data = r.json()
                status = data.get("status")
                substatus = data.get("substatus")
                if status != last:
                    print(f"  status: {status}" + (f" ({substatus})" if substatus else ""))
                    last = status
                if status in ("DONE", "FAILED", "REFUNDED"):
                    return data
        except requests.RequestException:
            pass
        time.sleep(interval)
    print("  status: still pending after timeout — check the LiFi status URL above.")
    return None


def run(args):
    if args.from_chain == args.to_chain:
        raise SystemExit(
            f"--from and --to are both '{args.from_chain}'. For same-chain swaps use the "
            f"swap command, not bridge."
        )
    if args.agent not in AGENTS:
        raise SystemExit(f"Unknown agent '{args.agent}'. Known: {', '.join(AGENTS)}.")

    acct = Account.from_key(_agent_key(args.agent))

    # Self-bridge enforcement: destination must be the signer's own address.
    if args.to_address is not None:
        if Web3.to_checksum_address(args.to_address) != acct.address:
            raise SystemExit(
                f"REFUSED: --to-address {args.to_address} differs from the signer "
                f"({acct.address}). v1 is self-bridge only — funds can only go to the "
                f"signer's own EOA on the destination chain."
            )

    from_tok = resolve_token(args.from_chain, args.token)
    to_sym = args.to_token if args.to_token else CHAINS[args.to_chain]["native"]
    to_tok = resolve_token(args.to_chain, to_sym)

    slippage = args.slippage / 100.0  # CLI is a percent; LiFi wants a fraction
    amount = Decimal(str(args.amount))
    raw_amount = int(amount * (Decimal(10) ** from_tok["decimals"]))

    print(f"signer:       {acct.address}  (--as {args.agent})")
    quote = lifi_quote(
        args.from_chain, args.to_chain, from_tok, to_tok, raw_amount, acct.address, slippage
    )
    preview = build_preview(
        quote, args.from_chain, args.to_chain, from_tok, to_tok, amount, slippage
    )
    print_preview(preview, args.execute)
    enforce_slippage(preview)

    if not args.execute:
        print("\nDry-run only. Re-run with --execute to broadcast on the source chain.")
        return

    w3 = w3_for(args.from_chain)
    chain_id = CHAINS[args.from_chain]["chain_id"]
    tx_req = quote["transactionRequest"]
    spender = Web3.to_checksum_address(tx_req["to"])

    # ERC-20 source token needs an allowance to the LiFi router/diamond first.
    if not from_tok["native"]:
        token = w3.eth.contract(address=from_tok["address"], abi=ERC20_ABI)
        allowance = token.functions.allowance(acct.address, spender).call()
        if allowance < raw_amount:
            print(f"\nApproving {from_tok['symbol']} for {spender} ...")
            fees = w3.eth.fee_history(1, "latest")
            base_fee = fees["baseFeePerGas"][-1]
            priority = w3.to_wei(2, "gwei") if args.from_chain == "avalanche" else w3.to_wei(1.5, "gwei")
            approve_tx = token.functions.approve(spender, raw_amount).build_transaction({
                "chainId": chain_id,
                "from": acct.address,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "maxFeePerGas": base_fee * 2 + priority,
                "maxPriorityFeePerGas": priority,
                "type": 2,
            })
            signed = acct.sign_transaction(approve_tx)
            ah = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  approve tx: {ah.hex()}")
            w3.eth.wait_for_transaction_receipt(ah, timeout=180)

    tx_hash = _send_tx(w3, acct, tx_req, chain_id)
    hx = tx_hash.hex()
    if not hx.startswith("0x"):
        hx = "0x" + hx
    explorer = CHAINS[args.from_chain]["explorer"]
    status_url = (
        f"{LIFI_STATUS_URL}?txHash={hx}"
        f"&fromChain={CHAINS[args.from_chain]['chain_id']}"
        f"&toChain={CHAINS[args.to_chain]['chain_id']}"
    )
    print(f"\nBroadcast on {args.from_chain}: {hx}")
    print(f"Source explorer: {explorer}/tx/{hx}")
    print(f"LiFi status:     {status_url}")

    if args.poll:
        print("\nPolling LiFi for cross-chain completion ...")
        final = poll_status(hx, args.from_chain, args.to_chain)
        if final:
            print(f"Final status: {final.get('status')} "
                  f"({final.get('substatus', '')})".rstrip(" ()"))


def build_parser():
    p = argparse.ArgumentParser(
        prog="bridge",
        description="Cross-chain bridge for primecli wallets (Avalanche / Base / Arbitrum) via LiFi.",
    )
    p.add_argument("--as", dest="agent", required=True,
                   help="Wallet to sign as: " + ", ".join(AGENTS))
    p.add_argument("--from", dest="from_chain", required=True, choices=list(CHAINS),
                   help="Source chain.")
    p.add_argument("--to", dest="to_chain", required=True, choices=list(CHAINS),
                   help="Destination chain.")
    p.add_argument("--token", required=True,
                   help="Token symbol on the SOURCE chain (e.g. AVAX, ETH, USDC).")
    p.add_argument("--amount", required=True,
                   help="Amount of --token to bridge (decimal).")
    p.add_argument("--to-token", default=None,
                   help="Token to receive on the destination chain. "
                        "Default: the destination chain's native gas token (gas top-up).")
    p.add_argument("--to-address", default=None,
                   help="Destination address. Must equal the signer (self-bridge only); "
                        "omit to default to the signer's own EOA.")
    p.add_argument("--slippage", type=float, default=1.0,
                   help="Max slippage percent (default 1.0). Quotes worse than this are refused.")
    p.add_argument("--poll", action="store_true",
                   help="After --execute, poll LiFi status until the bridge completes or times out.")
    p.add_argument("--execute", action="store_true",
                   help="Broadcast the bridge tx. Without this flag the command is a dry-run.")
    # Accepted for parity with the other primecli commands; the actual suppression
    # is handled by check_version() reading sys.argv directly.
    p.add_argument("--no-version-check", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv=None):
    try:
        from primecli import check_version
    except ImportError:
        def check_version(*a, **kw):
            pass
    check_version()
    args = build_parser().parse_args(argv)
    # argparse stores --amount as a string; validate it is a positive number early.
    try:
        if Decimal(str(args.amount)) <= 0:
            raise SystemExit("--amount must be positive.")
    except (ArithmeticError, ValueError):
        raise SystemExit(f"--amount '{args.amount}' is not a valid number.")
    run(args)


if __name__ == "__main__":
    main()
