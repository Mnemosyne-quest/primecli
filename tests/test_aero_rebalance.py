"""Unit tests for the DegenPrime Aerodrome auto-rebalancer helpers.

Pure/offline (per conftest): no network, no RPC, no signing. Covers the units
bridge (width% <-> band bps), the OUTSIDE/INSIDE trigger sign rules from the
feature doc s3, the CreateRebalanceOrderParams build, the ABI function selectors
(must match the on-chain selectors confirmed live 2026-06-16), and the precomputed
event topic0 hashes (must equal keccak of the canonical event signatures).
"""

from __future__ import annotations

import pytest
from eth_utils import keccak

import primecli.degenprime as d


# ── width% <-> band bps ──────────────────────────────────────────────────────

@pytest.mark.parametrize("width,expected", [
    (1.0, (-100, 100)),
    (3.0, (-300, 300)),
    (10.0, (-1000, 1000)),
    (0.5, (-50, 50)),
    (2.5, (-250, 250)),
])
def test_bps_band_from_width(width, expected):
    assert d._bps_band_from_width(width) == expected


# ── trigger sign rules (doc s3) ──────────────────────────────────────────────

def test_trigger_outside_is_lower_neg_upper_pos():
    # OUTSIDE: rebalance after price leaves the range -> lowerTrigger<0, upperTrigger>0.
    assert d._trigger_bps("outside", 300, 100) == (-100, 100)


def test_trigger_inside_is_lower_pos_upper_neg():
    # INSIDE: rebalance early, still in range -> lowerTrigger>0, upperTrigger<0.
    assert d._trigger_bps("inside", 300, 100) == (100, -100)


def test_trigger_outside_ignores_sign_of_input():
    # The magnitude is what matters; sign is applied by the mode.
    assert d._trigger_bps("outside", 300, -100) == (-100, 100)


def test_trigger_inside_must_be_strictly_inside_band():
    # |trigger| must be < |range| for INSIDE, else it's not actually inside.
    with pytest.raises(ValueError):
        d._trigger_bps("inside", 100, 100)   # equal -> invalid
    with pytest.raises(ValueError):
        d._trigger_bps("inside", 100, 150)   # outside -> invalid


# ── CreateRebalanceOrderParams build ─────────────────────────────────────────

def test_build_rebalance_order_params_outside():
    params, preview = d._build_rebalance_order_params(
        token_id=42, width_pct=4.0, mode="outside", trigger_bps=100,
        max_fee_weth=0.001, mint_slip_bps=100, swap_slip_bps=100)
    # tuple order: (tokenId, lowerRange, upperRange, lowerTrig, upperTrig,
    #               mintSlip, swapSlip, feeWei)
    assert params == (42, -400, 400, -100, 100, 100, 100, 1_000_000_000_000_000)
    assert preview["rangeBps"] == [-400, 400]
    assert preview["triggerBps"] == [-100, 100]
    assert preview["mode"] == "outside"
    assert preview["maxExecutionFeeWei"] == 1_000_000_000_000_000


def test_build_rebalance_order_params_inside():
    params, preview = d._build_rebalance_order_params(
        token_id=7, width_pct=5.0, mode="inside", trigger_bps=200,
        max_fee_weth=0.002, mint_slip_bps=150, swap_slip_bps=50)
    assert params == (7, -500, 500, 200, -200, 150, 50, 2_000_000_000_000_000)
    assert preview["triggerBps"] == [200, -200]


def test_build_rebalance_order_params_rejects_bad_inside():
    with pytest.raises(ValueError):
        d._build_rebalance_order_params(
            token_id=1, width_pct=3.0, mode="inside", trigger_bps=400,
            max_fee_weth=0.001, mint_slip_bps=100, swap_slip_bps=100)


# ── ABI selectors must match the live on-chain selectors ─────────────────────

@pytest.mark.parametrize("fn,args,selector", [
    ("getAllRebalanceOrders", [], "0x8d6c1fef"),
    ("getRebalanceOrder", [1], "0x4f6a4629"),
    ("shouldRebalance", [1], "0x619c2245"),
    ("cancelRebalanceOrder", [1], "0x098b060b"),
])
def test_view_and_cancel_selectors(fn, args, selector):
    # encode_abi off a contract bound to the ABI; first 4 bytes are the selector.
    from web3 import Web3
    c = Web3().eth.contract(abi=d.PRIME_ACCOUNT_ABI)
    assert c.encode_abi(fn, args=args)[:10] == selector


@pytest.mark.parametrize("fn,selector", [
    ("createRebalanceOrder", "0x569719c4"),
    ("updateRebalanceOrder", "0x83b63144"),
])
def test_write_struct_selectors(fn, selector):
    from web3 import Web3
    c = Web3().eth.contract(abi=d.PRIME_ACCOUNT_ABI)
    params = (1, -300, 300, -100, 100, 100, 100, 1_000_000_000_000_000)
    assert c.encode_abi(fn, args=[params])[:10] == selector


# ── event topic0 hashes must equal keccak of the canonical signatures ────────

@pytest.mark.parametrize("name,sig", [
    ("RebalanceOrderCreated",
     "RebalanceOrderCreated(address,address,uint256,int24,int24,int24,int24,uint256,uint256)"),
    ("RebalanceOrderUpdated",
     "RebalanceOrderUpdated(address,address,uint256,int24,int24,int24,int24,uint256)"),
    ("RebalanceOrderCanceled",
     "RebalanceOrderCanceled(address,address,uint256,uint256)"),
    ("RebalanceExecuted",
     "RebalanceExecuted(address,address,uint256,uint160,uint160,uint256,uint256)"),
])
def test_event_topic0_hashes(name, sig):
    assert d.REBALANCE_TOPIC0[name] == "0x" + keccak(text=sig).hex()
