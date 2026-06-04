"""Offline tests for `_set_gas_price_for(chain_id, w3, tx)` in deltaprime and arbprime.

This helper sets gas fields for an explicit chain id (used by cross-chain flows
like prime-bridge), so it must pick the right fee model per chain:
  * Arbitrum (42161) / Base (8453): EIP-1559 — maxFeePerGas + maxPriorityFeePerGas,
    and NO legacy gasPrice.
  * Avalanche (43114): legacy gasPrice with a 25 gwei floor, and NO EIP-1559 fields.

No RPC is made: we feed a stub w3 whose `eth.gas_price` / `eth.max_priority_fee`
return canned values. The helper is duplicated in both modules, so both are tested.
"""

from __future__ import annotations

import importlib

import pytest

GWEI = 10 ** 9

MODULES = ["primecli.deltaprime", "primecli.arbprime"]


class _StubEth:
    def __init__(self, gas_price, max_priority_fee):
        self.gas_price = gas_price
        self.max_priority_fee = max_priority_fee


class _StubW3:
    """Minimal web3 stand-in exposing only what `_set_gas_price_for` reads."""

    def __init__(self, gas_price, max_priority_fee):
        self.eth = _StubEth(gas_price, max_priority_fee)


@pytest.fixture(params=MODULES)
def mod(request):
    m = importlib.import_module(request.param)
    if not hasattr(m, "_set_gas_price_for"):
        pytest.skip(f"_set_gas_price_for not present in {request.param}")
    return m


# ──────────────────────────────────────────────────────────────────────────────
# Arbitrum (42161) — EIP-1559


def test_arbitrum_sets_eip1559_no_gasprice(mod):
    w3 = _StubW3(gas_price=20 * GWEI, max_priority_fee=1 * GWEI)
    tx = {"gasPrice": 999}  # any stale gasPrice must be dropped
    mod._set_gas_price_for(42161, w3, tx)

    assert "gasPrice" not in tx
    assert "maxPriorityFeePerGas" in tx and "maxFeePerGas" in tx
    assert tx["maxPriorityFeePerGas"] == 1 * GWEI
    # max(base*2, base + prio + 1gwei) = max(40, 22) = 40 gwei
    assert tx["maxFeePerGas"] == 40 * GWEI


def test_arbitrum_floor_branch_when_base_tiny(mod):
    # base*2 (2 gwei) < base + prio + 1gwei (1 + 5 + 1 = 7 gwei) → takes the latter
    w3 = _StubW3(gas_price=1 * GWEI, max_priority_fee=5 * GWEI)
    tx = {}
    mod._set_gas_price_for(42161, w3, tx)
    assert tx["maxFeePerGas"] == 7 * GWEI
    assert tx["maxPriorityFeePerGas"] == 5 * GWEI
    assert "gasPrice" not in tx


# ──────────────────────────────────────────────────────────────────────────────
# Base (8453) — EIP-1559 (the helper handles it on the same branch as Arbitrum)


def test_base_sets_eip1559_no_gasprice(mod):
    w3 = _StubW3(gas_price=20 * GWEI, max_priority_fee=1 * GWEI)
    tx = {}
    mod._set_gas_price_for(8453, w3, tx)
    assert "gasPrice" not in tx
    assert tx["maxFeePerGas"] == 40 * GWEI
    assert tx["maxPriorityFeePerGas"] == 1 * GWEI


# ──────────────────────────────────────────────────────────────────────────────
# Avalanche (43114) — legacy gasPrice with 25 gwei floor


def test_avalanche_sets_legacy_gasprice_no_eip1559(mod):
    # gas_price*2 (60 gwei) > 25 gwei floor → uses doubled value
    w3 = _StubW3(gas_price=30 * GWEI, max_priority_fee=1 * GWEI)
    tx = {}
    mod._set_gas_price_for(43114, w3, tx)
    assert tx["gasPrice"] == 60 * GWEI
    assert "maxFeePerGas" not in tx
    assert "maxPriorityFeePerGas" not in tx


def test_avalanche_applies_25_gwei_floor(mod):
    # gas_price*2 (10 gwei) < 25 gwei floor → floor wins
    w3 = _StubW3(gas_price=5 * GWEI, max_priority_fee=1 * GWEI)
    tx = {"gasPrice": 1}  # stale value replaced, not added-to
    mod._set_gas_price_for(43114, w3, tx)
    assert tx["gasPrice"] == 25 * GWEI
    assert "maxFeePerGas" not in tx
    assert "maxPriorityFeePerGas" not in tx
