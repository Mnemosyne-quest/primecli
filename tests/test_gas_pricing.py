"""Offline tests for `_set_gas_price_for(chain_id, w3, tx)` in deltaprime and arbprime.

This helper sets gas fields for cross-chain flows (like prime-bridge). All three
supported chains (Arbitrum 42161, Base 8453, Avalanche 43114 post-Etna) speak
EIP-1559, so the helper now tries EIP-1559 first regardless of chain id:
  * `eth.max_priority_fee` works → maxFeePerGas + maxPriorityFeePerGas,
    and NO legacy gasPrice.
  * `eth.max_priority_fee` raises (legacy-only chain/RPC) → legacy gasPrice with
    a 1 gwei floor, and NO EIP-1559 fields.
  * EIP-1559 fields already present on the tx → left untouched (stale gasPrice dropped).

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
        self._max_priority_fee = max_priority_fee

    @property
    def max_priority_fee(self):
        if isinstance(self._max_priority_fee, Exception):
            raise self._max_priority_fee
        return self._max_priority_fee


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
# Avalanche (43114) — EIP-1559 post-Etna (ACP-125); legacy gasPrice only as fallback


def test_avalanche_uses_eip1559_when_priority_fee_available(mod):
    w3 = _StubW3(gas_price=30 * GWEI, max_priority_fee=1 * GWEI)
    tx = {"gasPrice": 1}  # stale value dropped, not added-to
    mod._set_gas_price_for(43114, w3, tx)
    assert "gasPrice" not in tx
    # max(base*2, base + prio + 1gwei) = max(60, 32) = 60 gwei
    assert tx["maxFeePerGas"] == 60 * GWEI
    assert tx["maxPriorityFeePerGas"] == 1 * GWEI


def test_legacy_fallback_applies_1_gwei_floor(mod):
    # max_priority_fee unsupported (raises) → legacy gasPrice path with the 1 gwei
    # floor: gas_price*2 (0.02 gwei, realistic post-Etna base) < 1 gwei → floor wins
    w3 = _StubW3(gas_price=GWEI // 100, max_priority_fee=ValueError("no eip-1559"))
    tx = {"gasPrice": 1}  # stale value replaced, not added-to
    mod._set_gas_price_for(43114, w3, tx)
    assert tx["gasPrice"] == 1 * GWEI
    assert "maxFeePerGas" not in tx
    assert "maxPriorityFeePerGas" not in tx


def test_legacy_fallback_doubles_above_floor(mod):
    # max_priority_fee unsupported → legacy path: gas_price*2 (60 gwei) > 1 gwei floor
    w3 = _StubW3(gas_price=30 * GWEI, max_priority_fee=ValueError("no eip-1559"))
    tx = {}
    mod._set_gas_price_for(43114, w3, tx)
    assert tx["gasPrice"] == 60 * GWEI
    assert "maxFeePerGas" not in tx
    assert "maxPriorityFeePerGas" not in tx


def test_preset_eip1559_fields_left_untouched(mod):
    # build_transaction already set the fee fields → helper must not override them,
    # but must still drop a stale legacy gasPrice
    w3 = _StubW3(gas_price=30 * GWEI, max_priority_fee=1 * GWEI)
    tx = {"maxFeePerGas": 5 * GWEI, "maxPriorityFeePerGas": 2 * GWEI, "gasPrice": 1}
    mod._set_gas_price_for(43114, w3, tx)
    assert tx["maxFeePerGas"] == 5 * GWEI
    assert tx["maxPriorityFeePerGas"] == 2 * GWEI
    assert "gasPrice" not in tx
