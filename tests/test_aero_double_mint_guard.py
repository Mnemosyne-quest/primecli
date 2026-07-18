"""Tests for the aero-add-liquidity --use-all-available double-mint guard
(_aero_open_positions_for_pool), degenprime only.

Root cause this locks in (2026-07-18, hit live on core1): --use-all-available ALWAYS mints
a fresh Aerodrome NFT. When an OPEN position already exists on the same pool it silently
creates a SECOND, duplicate position (only one of which the auto-rebalancer is armed on).
The guard detects an existing open position (matching token pair + tickSpacing, liquidity>0)
so the user-facing command can refuse and point at aero-increase-liquidity --token-id N.

All offline: the account's tokenId enumeration and per-token position read are mocked; no
live RPC.
"""

from __future__ import annotations

import importlib

import pytest

dp = importlib.import_module("primecli.degenprime")

POOL = "aero-cbbtc-200"
_CFG = dp.AERODROME_POOLS[POOL]
AERO, CBBTC, TS = _CFG["token0"], _CFG["token1"], _CFG["tickSpacing"]


class _Call:
    def __init__(self, ids, exc=None):
        self._ids, self._exc = ids, exc

    def call(self):
        if self._exc is not None:
            raise self._exc
        return self._ids


class _Funcs:
    def __init__(self, ids, exc=None):
        self._ids, self._exc = ids, exc

    def getOwnedStakedAerodromeTokenIds(self):
        return _Call(self._ids, self._exc)


class _Account:
    def __init__(self, ids, exc=None):
        self.functions = _Funcs(ids, exc)


def _pos(token0, token1, tick_spacing, liq):
    # positions() struct layout: [_, _, token0, token1, tickSpacing, tickLower, tickUpper, liq, ...]
    return [0, 0, token0, token1, tick_spacing, -100, 100, liq]


@pytest.fixture
def patch_npm(monkeypatch):
    def _install(pos_map):
        monkeypatch.setattr(dp, "_aero_npm_for_token",
                            lambda w3, tid, pa: (None, "v3", pos_map.get(tid)))
    return _install


def test_detects_open_matching_position(patch_npm):
    patch_npm({73237998: _pos(AERO, CBBTC, TS, 12345)})
    got = dp._aero_open_positions_for_pool(None, _Account([73237998]), "0xPA", POOL)
    assert got == [73237998]


def test_ignores_zero_liquidity_husk(patch_npm):
    # A closed/burned position (liquidity 0) must NOT trip the guard — it isn't a real
    # open position and blocking on it would wrongly refuse a legitimate fresh open.
    patch_npm({99: _pos(AERO, CBBTC, TS, 0)})
    assert dp._aero_open_positions_for_pool(None, _Account([99]), "0xPA", POOL) == []


def test_ignores_different_pool_same_pair(patch_npm):
    # Same token pair but a DIFFERENT tickSpacing is a different pool — must not match.
    patch_npm({5: _pos(AERO, CBBTC, 100, 555)})
    assert dp._aero_open_positions_for_pool(None, _Account([5]), "0xPA", POOL) == []


def test_ignores_unrelated_pair(patch_npm):
    weth = "0x4200000000000000000000000000000000000006"
    patch_npm({7: _pos(weth, CBBTC, TS, 999)})
    assert dp._aero_open_positions_for_pool(None, _Account([7]), "0xPA", POOL) == []


def test_no_positions_returns_empty(patch_npm):
    patch_npm({})
    assert dp._aero_open_positions_for_pool(None, _Account([]), "0xPA", POOL) == []


def test_enumeration_failure_is_fail_open(patch_npm):
    # Fail-open: a flaky enumeration must never BLOCK a legitimate first open.
    patch_npm({})
    acct = _Account([], exc=RuntimeError("rpc down"))
    assert dp._aero_open_positions_for_pool(None, acct, "0xPA", POOL) == []


def test_unknown_pool_key_returns_empty(patch_npm):
    patch_npm({1: _pos(AERO, CBBTC, TS, 100)})
    assert dp._aero_open_positions_for_pool(None, _Account([1]), "0xPA", "no-such-pool") == []


def test_matches_regardless_of_token_order(patch_npm):
    # positions() may report the pair in either order; the match is order-insensitive.
    patch_npm({42: _pos(CBBTC, AERO, TS, 4242)})
    assert dp._aero_open_positions_for_pool(None, _Account([42]), "0xPA", POOL) == [42]
