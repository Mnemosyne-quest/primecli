#!/usr/bin/env python3
"""
Bridge PRIME tokens between Avalanche and Arbitrum via DeltaPrime's LayerZero bridge.

Forward (Ava→Arb):  sendFrom() on Bridge contract (0x35643752F4ea...6a20)
Reverse  (Arb→Ava): sendFrom() on PRIME token contract (0x3De81CE9...14E)

Usage:
  python3 prime-bridge.py quote --from avalanche --to arbitrum --amount 5
  python3 prime-bridge.py bridge --from avalanche --to arbitrum --amount 5 --execute
"""

import os, sys, json, time, struct
from eth_abi import encode as abi_encode
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

WALLET = "0x0218f5b006FD43181018F584Ed4Be13c356b3428"
PRIVATE_KEY = os.environ.get("PARAKLETOS_EVM_PRIVATE_KEY") or os.environ.get("DELTAPRIME_PRIVATE_KEY")

CHAINS = {
    "avalanche": {
        "rpc": "https://api.avax.network/ext/bc/C/rpc",
        "chain_id": 43114,
        "lz_chain_id": 106,
        "prime_token": "0x33C8036E99082B0C395374832FECF70c42C7F298",
        "bridge_contract": "0x35643752F4ea0ba70456F0CA1e2778f783206a20",
        "lz_endpoint": "0x3c2269811836af69497E5F486A85D7316753cf62",
        "explorer": "https://snowtrace.io/tx",
    },
    "arbitrum": {
        "rpc": "https://arb1.arbitrum.io/rpc",
        "chain_id": 42161,
        "lz_chain_id": 110,
        "prime_token": "0x3De81CE90f5A27C5E6A5aDb04b54ABA488a6d14E",
        "bridge_contract": "0x3De81CE90f5A27C5E6A5aDb04b54ABA488a6d14E",  # PRIME token itself
        "lz_endpoint": "0x3c2269811836af69497E5F486A85D7316753cf62",
        "explorer": "https://arbiscan.io/tx",
    },
}

# Standard LZ v1 adapter params: uint16 version (1) + uint256 gas (200000)
ADAPTER_PARAMS_V1 = struct.pack('>H', 1) + struct.pack('>I', 200000).rjust(32, b'\x00')

ESTIMATE_FEES_ABI = json.dumps([{
    "inputs": [
        {"name": "_dstChainId", "type": "uint16"},
        {"name": "_userApplication", "type": "address"},
        {"name": "_payload", "type": "bytes"},
        {"name": "_payInZRO", "type": "bool"},
        {"name": "_adapterParams", "type": "bytes"},
    ],
    "name": "estimateFees",
    "outputs": [{"name": "", "type": "uint256"}, {"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
}])


def get_w3(chain):
    cfg = CHAINS[chain]
    w3 = Web3(Web3.HTTPProvider(cfg["rpc"]))
    if chain == "avalanche":
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_chain_name(w3):
    for name, cfg in CHAINS.items():
        if cfg["chain_id"] == w3.eth.chain_id:
            return name
    return None


def wallet_bytes32():
    return bytes.fromhex(WALLET.lower()[2:].rjust(64, "0"))


def build_lz_payload(amount_wei):
    """OFT payload: bytes32(toAddress) + uint256(amount)"""
    return wallet_bytes32() + amount_wei.to_bytes(32, 'big')


def build_sendfrom_data(dst_lz_chain_id, amount_wei, adapter_params):
    """sendFrom(address, uint16, bytes32, uint256, (address,address,bytes))"""
    refund = WALLET
    zero = "0x0000000000000000000000000000000000000000"
    call_params = (refund, zero, adapter_params)
    return "695ef6bf" + abi_encode(
        ["address", "uint16", "bytes32", "uint256", "(address,address,bytes)"],
        [WALLET, dst_lz_chain_id, wallet_bytes32(), amount_wei, call_params],
    ).hex()


def estimate_lz_fee(w3, dst_lz_chain_id, ua_address, payload, adapter_params):
    """Get LZ message fee from the endpoint."""
    ep = w3.eth.contract(
        address=Web3.to_checksum_address(
            CHAINS[get_chain_name(w3)]["lz_endpoint"]
        ),
        abi=json.loads(ESTIMATE_FEES_ABI),
    )
    native, zro = ep.functions.estimateFees(
        dst_lz_chain_id,
        Web3.to_checksum_address(ua_address),
        payload,
        False,
        adapter_params,
    ).call()
    return native, zro


def get_tx_gas_params(w3):
    """Return (maxFeePerGas, maxPriorityFeePerGas) for the chain."""
    base = w3.eth.gas_price
    prio = w3.eth.max_priority_fee
    if get_chain_name(w3) == "avalanche":
        avax_prio = max(prio, 25 * 10**9)
        return base + avax_prio, avax_prio
    else:
        return base + prio + 10**9, prio


def sign_and_send(w3, tx_dict):
    account = w3.eth.account.from_key(PRIVATE_KEY)
    tx_dict["chainId"] = w3.eth.chain_id
    if "nonce" not in tx_dict:
        tx_dict["nonce"] = w3.eth.get_transaction_count(WALLET)
    max_fee, max_prio = get_tx_gas_params(w3)
    tx_dict["maxFeePerGas"] = max_fee
    tx_dict["maxPriorityFeePerGas"] = max_prio
    tx_dict.pop("gasPrice", None)
    signed = account.sign_transaction(tx_dict)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    ok = receipt["status"] == 1
    print(f"  Status: {'OK' if ok else 'FAIL'}, gas: {receipt.gasUsed}")
    return receipt


def cmd_quote(from_chain, to_chain, amount):
    cfg_from = CHAINS[from_chain]
    cfg_to = CHAINS[to_chain]
    w3 = get_w3(from_chain)
    amount_wei = int(amount * 10**18)

    print(f"\nPRIME Bridge: {from_chain} -> {to_chain}")
    print(f"  Amount:    {amount} PRIME")
    print(f"  Source:    {cfg_from['prime_token'][:12]}... (LZ {cfg_from['lz_chain_id']})")
    print(f"  Contract:  {cfg_from['bridge_contract'][:12]}...")
    print(f"  Dest:      {cfg_to['prime_token'][:12]}... (LZ {cfg_to['lz_chain_id']})")

    payload = build_lz_payload(amount_wei)
    native_fee, _ = estimate_lz_fee(
        w3, cfg_to["lz_chain_id"],
        cfg_from["bridge_contract"],
        payload,
        ADAPTER_PARAMS_V1,
    )
    print(f"  LZ fee:    {w3.from_wei(native_fee, 'ether')} native")

    # Check balance
    erc20 = w3.eth.contract(
        address=Web3.to_checksum_address(cfg_from["prime_token"]),
        abi=json.dumps([{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
                          "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                          "type": "function"}]),
    )
    bal = erc20.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
    print(f"  Balance:   {bal / 1e18:.2f} PRIME")

    print(f"\n  Steps:")
    print(f"    1. approve bridge contract ({cfg_from['bridge_contract'][:12]}...) for {amount} PRIME")
    print(f"    2. sendFrom(…) via LZ to chain {cfg_to['lz_chain_id']}")
    print(f"    3. Pays {w3.from_wei(native_fee, 'ether')} native as msg.value")
    print(f"  Run with --execute to broadcast")


def cmd_bridge(from_chain, to_chain, amount, execute=False):
    cfg_from = CHAINS[from_chain]
    cfg_to = CHAINS[to_chain]
    w3 = get_w3(from_chain)
    wallet = Web3.to_checksum_address(WALLET)
    amount_wei = int(amount * 10**18)

    print(f"\nBridging {amount} PRIME ({from_chain} -> {to_chain})...")

    # Check balance
    erc20 = w3.eth.contract(
        address=Web3.to_checksum_address(cfg_from["prime_token"]),
        abi=json.dumps([{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
                          "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                          "type": "function"}]),
    )
    bal = erc20.functions.balanceOf(wallet).call()
    print(f"  PRIME balance: {bal / 1e18:.2f}")
    if bal < amount_wei:
        print(f"  NOT ENOUGH PRIME! Have {bal/1e18:.2f}, need {amount}")
        return

    # 1. Approve
    bridge_contract = Web3.to_checksum_address(cfg_from["bridge_contract"])
    allowance_abi = json.dumps([{"constant": True, "inputs": [
        {"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
        "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}])
    allow_c = w3.eth.contract(address=Web3.to_checksum_address(cfg_from["prime_token"]),
                              abi=json.loads(allowance_abi))
    current_allowance = allow_c.functions.allowance(wallet, bridge_contract).call()
    if current_allowance < amount_wei:
        print(f"  Approving {amount} PRIME for bridge contract...")
        if not execute:
            print(f"    (preview, run with --execute)")
            print(f"\n  Run with --execute to broadcast")
            return
        approve_abi = json.dumps([{"constant": False, "inputs": [
            {"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
            "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"}])
        app_c = w3.eth.contract(address=Web3.to_checksum_address(cfg_from["prime_token"]),
                                abi=json.loads(approve_abi))
        atx = app_c.functions.approve(bridge_contract, amount_wei).build_transaction({"from": wallet})
        sign_and_send(w3, atx)
        time.sleep(2)
    else:
        print(f"  Allowance: {current_allowance / 1e18:.2f} PRIME")

    # 2. Estimate LZ fee
    payload = build_lz_payload(amount_wei)
    native_fee, _ = estimate_lz_fee(
        w3, cfg_to["lz_chain_id"],
        cfg_from["bridge_contract"],
        payload,
        ADAPTER_PARAMS_V1,
    )
    print(f"  LZ fee: {w3.from_wei(native_fee, 'ether')} native")

    # 3. sendFrom
    print(f"  sendFrom() via LayerZero (chain {cfg_from['lz_chain_id']} -> {cfg_to['lz_chain_id']})...")
    if not execute:
        print(f"    (preview, run with --execute)")
        return

    calldata_hex = build_sendfrom_data(cfg_to["lz_chain_id"], amount_wei, ADAPTER_PARAMS_V1)
    tx = {
        "from": wallet,
        "to": bridge_contract,
        "data": bytes.fromhex(calldata_hex),
        "nonce": w3.eth.get_transaction_count(wallet),
        "gas": 500000,
        "value": native_fee,
    }
    receipt = sign_and_send(w3, tx)
    if receipt.status:
        print(f"\n  Bridge submitted! LZ message in transit to {to_chain}.")
        print(f"  Explorer: {cfg_from['explorer']}/{receipt.transactionHash.hex()}")
    else:
        print(f"\n  Bridge FAILED!")
        print(f"  Explorer: {cfg_from['explorer']}/{receipt.transactionHash.hex()}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    args = {}
    for i in range(2, len(sys.argv)):
        if sys.argv[i].startswith("--"):
            key = sys.argv[i].lstrip("-")
            if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
                args[key] = sys.argv[i + 1]
                i += 1
            else:
                args[key] = True

    if cmd == "quote":
        cmd_quote(
            args.get("from", "avalanche"),
            args.get("to", "arbitrum"),
            float(args.get("amount", 5)),
        )
    elif cmd == "bridge":
        execute = "--execute" in [a for a in sys.argv] or args.pop("execute", False)
        # Re-parse execute directly
        execute = any(a == "--execute" or a == "-x" for a in sys.argv)
        cmd_bridge(
            args.get("from", "avalanche"),
            args.get("to", "arbitrum"),
            float(args.get("amount", 5)),
            execute,
        )
    else:
        print(f"Unknown: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
