"""Tests for the on-chain hard borrow-utilisation cap:
get_max_pool_utilisation_for_borrowing() + the maxPoolUtilisation field emitted in
pool-info --json, across all three sibling modules (degenprime / deltaprime / arbprime).

Root cause these lock in (2026-07-18): the borrow-sizing/capacity layer assumed a
hardcoded 0.88/0.90/0.95 max pool utilisation, none matching the REAL on-chain cap
(getMaxPoolUtilisationForBorrowing() = 0.925 on every DegenPrime/DeltaPrime/ArbPrime pool).
Sizing a borrow above the real cap reverts on-chain with MaxPoolUtilisationBreached(). The
tool now reads the real value; these tests pin the reader + JSON wiring + safe fallback.

All offline: the getter's only network touch is w3.eth.call, which we inject as a mock.
The selector + checksum are computed against the real Web3 class (static, no connection).
"""

from __future__ import annotations

import importlib

import pytest

MODULES = ["primecli.degenprime", "primecli.deltaprime", "primecli.arbprime"]


class _MockEth:
    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc

    def call(self, tx):
        # The helper must build a well-formed call: a `to` address and 4-byte `data`.
        assert "to" in tx and "data" in tx
        assert len(tx["data"]) == 4  # keccak(sig)[:4]
        if self._exc is not None:
            raise self._exc
        return self._result


class _MockW3:
    def __init__(self, result=None, exc=None):
        self.eth = _MockEth(result, exc)


def _u256(value_1e18: int) -> bytes:
    return int(value_1e18).to_bytes(32, "big")


@pytest.mark.parametrize("modname", MODULES)
def test_fallback_constant_is_925(modname):
    m = importlib.import_module(modname)
    assert m.MAX_POOL_UTIL_FALLBACK == 0.925


@pytest.mark.parametrize("modname", MODULES)
def test_getter_reads_live_fraction(modname):
    m = importlib.import_module(modname)
    # 0.925 * 1e18 -> the real on-chain value on every pool today
    w3 = _MockW3(result=_u256(925 * 10**15))
    got = m.get_max_pool_utilisation_for_borrowing("0x" + "ab" * 20, w3=w3)
    assert got == pytest.approx(0.925)


@pytest.mark.parametrize("modname", MODULES)
def test_getter_reads_a_different_live_value(modname):
    # Not hardcoded to 0.925 — a pool returning 0.90 is read as 0.90 (proves it's a real read).
    m = importlib.import_module(modname)
    w3 = _MockW3(result=_u256(90 * 10**16))  # 0.90
    got = m.get_max_pool_utilisation_for_borrowing("0x" + "cd" * 20, w3=w3)
    assert got == pytest.approx(0.90)


@pytest.mark.parametrize("modname", MODULES)
def test_getter_falls_back_on_revert(modname):
    m = importlib.import_module(modname)
    w3 = _MockW3(exc=RuntimeError("execution reverted: no such function"))
    got = m.get_max_pool_utilisation_for_borrowing("0x" + "ef" * 20, w3=w3)
    assert got == m.MAX_POOL_UTIL_FALLBACK == 0.925


@pytest.mark.parametrize("modname", MODULES)
def test_getter_falls_back_on_implausible_value(modname):
    # A garbage read (e.g. > 1.0, or 0) must NOT be trusted as a utilisation ceiling —
    # sizing a borrow to a bogus 2.0 would defeat the whole point.
    m = importlib.import_module(modname)
    for bogus in (2 * 10**18, 0):
        w3 = _MockW3(result=_u256(bogus))
        got = m.get_max_pool_utilisation_for_borrowing("0x" + "11" * 20, w3=w3)
        assert got == 0.925


@pytest.mark.parametrize("modname", MODULES)
def test_pool_json_shape_emits_max_pool_utilisation(modname):
    """_pool_json_shape (pure) emits maxPoolUtilisation (a FRACTION) when the multicall leg
    decoded a value, and omits it when the leg reverted (raw value None)."""
    m = importlib.import_module(modname)
    cfg = {"symbol": "USDC", "proxy": "0x" + "22" * 20, "token": "0x" + "33" * 20,
           "decimals": 6}
    base_raw = {"totalSupply": 1_000_000 * 10**6, "totalBorrowed": 500_000 * 10**6,
                "getDepositRate": 5 * 10**16, "getBorrowingRate": 8 * 10**16,
                "balanceOf": None}

    # Present -> emitted as a fraction (0.925), NOT percent like `utilization`.
    raw = dict(base_raw, maxPoolUtil=925 * 10**15)
    out = m._pool_json_shape({"cfg": cfg, "raw": raw, "price": 1.0})
    assert out["maxPoolUtilisation"] == pytest.approx(0.925)
    assert out["utilization"] == pytest.approx(50.0)  # percent scale unchanged

    # Absent (leg reverted / decode failed) -> field omitted, so a consumer can tell
    # "unread" from a real value and fall back to MAX_POOL_UTIL_FALLBACK itself.
    raw_missing = dict(base_raw, maxPoolUtil=None)
    out2 = m._pool_json_shape({"cfg": cfg, "raw": raw_missing, "price": 1.0})
    assert "maxPoolUtilisation" not in out2
