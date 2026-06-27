"""Offline tests for the two pre-release Aerodrome V3 display-correctness fixes:

  * FIX 1 — `_aero_npm_for_token` ownership-aware resolution. The V2 and V3 Aerodrome
    NPMs are independent ERC-721s with OVERLAPPING tokenId ranges; positions(id) succeeds
    for ANY live id regardless of owner. A tokenId live on BOTH deployments must resolve
    to the deployment the prime account actually owns (held directly, or staked in its
    gauge), not a stranger's struct from the first-probed deployment.
  * FIX 2 — `_aero_match_pool_cfg` matching on (pair, tickSpacing, version) so a V3
    position resolves its OWN baked pool address instead of an earlier same-pair V2 entry
    (weth-euroc-100's V2 pool is dead, ~$15k TVL, stale price).

Pure/offline (per conftest): no network, no RPC, no signing. The on-chain reads
(positions / ownerOf / stakedContains / pool getPool) are stubbed or short-circuited.
"""

from __future__ import annotations

import importlib

import pytest
from web3 import Web3

dp = importlib.import_module("primecli.degenprime")

V2 = Web3.to_checksum_address(dp.AERODROME_NPM_V2)
V3 = Web3.to_checksum_address(dp.AERODROME_NPM_V3)

PA = "0x" + "11" * 20            # the prime account that owns the genuine position
STRANGER = "0x" + "22" * 20      # an unrelated holder (the V2 collision)
V3_GAUGE = "0x" + "33" * 20      # gauge holding the staked V3 NFT on pa's behalf
STRANGER_GAUGE = "0x" + "44" * 20

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
AERO = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"
WETH = "0x4200000000000000000000000000000000000006"
EURC = "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"


# ─────────────────────────── address-keyed w3 stub ───────────────────────────

class _Call:
    def __init__(self, fn, args):
        self._fn, self._args = fn, args

    def call(self):
        return self._fn(*self._args)


class _Functions:
    """contract.functions.NAME(*args).call() dispatches to a handler; a missing handler
    raises AttributeError (mirrors calling an unsupported method on a real contract)."""

    def __init__(self, handlers):
        self._handlers = handlers

    def __getattr__(self, name):
        h = self._handlers.get(name)
        if h is None:
            raise AttributeError(name)
        return lambda *args: _Call(h, args)


class _Contract:
    def __init__(self, handlers):
        self.functions = _Functions(handlers)


class _W3:
    """eth.contract(address, abi) -> the handler dict registered for that address."""

    def __init__(self, by_address):
        self._by = {Web3.to_checksum_address(a): h for a, h in by_address.items()}

        outer = self

        class _Eth:
            def contract(self_inner, address=None, abi=None):
                return _Contract(outer._by.get(Web3.to_checksum_address(address), {}))

        self.eth = _Eth()


def _revert(*_a, **_k):
    raise Exception("execution reverted")


def _pos(token0, token1, tick_spacing=100, tick_lower=-100, tick_upper=100, liq=1000):
    # positions(): nonce, operator, token0, token1, tickSpacing, tickLower, tickUpper,
    #              liquidity, feeGrowth0, feeGrowth1, tokensOwed0, tokensOwed1
    return (0, "0x" + "00" * 20, token0, token1, tick_spacing,
            tick_lower, tick_upper, liq, 0, 0, 0, 0)


GENUINE = _pos(WETH, EURC, tick_spacing=100)     # the position pa really holds (on V3)
FOREIGN = _pos(USDC, AERO, tick_spacing=2000)    # an unrelated stranger position (on V2)


# ─────────────────────────── FIX 1: ownership-aware resolver ─────────────────

def test_collision_resolves_to_v3_when_pa_holds_it_directly():
    # Same tokenId live on BOTH NPMs; pa holds the V3 one directly (unstaked).
    w3 = _W3({
        V2: {"positions": lambda tid: FOREIGN, "ownerOf": lambda tid: STRANGER},
        V3: {"positions": lambda tid: GENUINE, "ownerOf": lambda tid: PA},
        # STRANGER has no stakedContains -> the V2 ownership probe fails closed.
    })
    npm, ver, pos = dp._aero_npm_for_token(w3, 12345, PA)
    assert ver == "v3"
    assert pos == GENUINE


def test_collision_resolves_to_v3_when_pa_staked_in_gauge():
    # pa's V3 position is staked: NPM.ownerOf returns the gauge, and the gauge reports
    # the stake for pa. The V2 collision's gauge does not.
    w3 = _W3({
        V2: {"positions": lambda tid: FOREIGN, "ownerOf": lambda tid: STRANGER_GAUGE},
        V3: {"positions": lambda tid: GENUINE, "ownerOf": lambda tid: V3_GAUGE},
        STRANGER_GAUGE: {"stakedContains": lambda dep, tid: False},
        V3_GAUGE: {"stakedContains": lambda dep, tid: True},
    })
    npm, ver, pos = dp._aero_npm_for_token(w3, 12345, PA)
    assert ver == "v3"
    assert pos == GENUINE


def test_collision_without_pa_keeps_legacy_v2_first():
    # No owner hint -> first live deployment (V2) wins, as before. This is the legacy
    # path the wrapper _aero_resolve_npm uses when no pa is threaded.
    w3 = _W3({
        V2: {"positions": lambda tid: FOREIGN, "ownerOf": lambda tid: STRANGER},
        V3: {"positions": lambda tid: GENUINE, "ownerOf": lambda tid: PA},
    })
    npm, ver, pos = dp._aero_npm_for_token(w3, 12345, None)
    assert ver == "v2"
    assert pos == FOREIGN


def test_single_deployment_v3_resolves_without_owner_calls():
    # Only V3 knows the id (V2 reverts). ownerOf must never be consulted — wire it to
    # blow up so a stray ownership probe would fail the test.
    w3 = _W3({
        V2: {"positions": _revert, "ownerOf": _revert},
        V3: {"positions": lambda tid: GENUINE, "ownerOf": _revert},
    })
    npm, ver, pos = dp._aero_npm_for_token(w3, 999, PA)
    assert ver == "v3"
    assert pos == GENUINE


def test_single_deployment_v2_resolves_without_owner_calls():
    w3 = _W3({
        V2: {"positions": lambda tid: FOREIGN, "ownerOf": _revert},
        V3: {"positions": _revert, "ownerOf": _revert},
    })
    npm, ver, pos = dp._aero_npm_for_token(w3, 777, PA)
    assert ver == "v2"
    assert pos == FOREIGN


def test_unknown_token_on_neither_deployment():
    w3 = _W3({V2: {"positions": _revert}, V3: {"positions": _revert}})
    assert dp._aero_npm_for_token(w3, 1, PA) == (None, None, None)


def test_collision_neither_owned_falls_back_to_v2_first():
    # Defensive: if neither deployment resolves to pa (shouldn't happen for the
    # account's own ids), fall back to the first live deployment rather than dropping it.
    w3 = _W3({
        V2: {"positions": lambda tid: FOREIGN, "ownerOf": lambda tid: STRANGER},
        V3: {"positions": lambda tid: GENUINE, "ownerOf": lambda tid: STRANGER},
    })
    npm, ver, pos = dp._aero_npm_for_token(w3, 12345, PA)
    assert ver == "v2"
    assert pos == FOREIGN


def test_resolve_npm_wrapper_threads_pa():
    w3 = _W3({
        V2: {"positions": lambda tid: FOREIGN, "ownerOf": lambda tid: STRANGER},
        V3: {"positions": lambda tid: GENUINE, "ownerOf": lambda tid: PA},
    })
    ver, pos = dp._aero_resolve_npm(w3, 12345, PA)
    assert ver == "v3" and pos == GENUINE


# ─────────────────────────── FIX 1: _aero_pa_owns_token unit ─────────────────

def test_pa_owns_token_direct_holder():
    w3 = _W3({V3: {"ownerOf": lambda tid: PA}})
    npm = w3.eth.contract(address=V3, abi=dp.AERODROME_NPM_ABI)
    assert dp._aero_pa_owns_token(w3, npm, 1, PA) is True


def test_pa_owns_token_via_gauge():
    w3 = _W3({
        V3: {"ownerOf": lambda tid: V3_GAUGE},
        V3_GAUGE: {"stakedContains": lambda dep, tid: True},
    })
    npm = w3.eth.contract(address=V3, abi=dp.AERODROME_NPM_ABI)
    assert dp._aero_pa_owns_token(w3, npm, 1, PA) is True


def test_pa_owns_token_false_for_stranger():
    w3 = _W3({
        V2: {"ownerOf": lambda tid: STRANGER_GAUGE},
        STRANGER_GAUGE: {"stakedContains": lambda dep, tid: False},
    })
    npm = w3.eth.contract(address=V2, abi=dp.AERODROME_NPM_ABI)
    assert dp._aero_pa_owns_token(w3, npm, 1, PA) is False


def test_pa_owns_token_fails_closed_on_revert():
    w3 = _W3({V2: {"ownerOf": _revert}})
    npm = w3.eth.contract(address=V2, abi=dp.AERODROME_NPM_ABI)
    assert dp._aero_pa_owns_token(w3, npm, 1, PA) is False


# ─────────────────────────── FIX 2: version/tickSpacing-aware cfg match ──────

def test_v3_weth_euroc_resolves_own_baked_pool_not_dead_v2():
    v3 = dp.AERODROME_POOLS["weth-euroc-100-v3"]
    v2 = dp.AERODROME_POOLS["weth-euroc-100"]

    # Legacy pair-only match returns the FIRST hit, which is the dead V2 entry (the bug).
    legacy = dp._aero_match_pool_cfg(WETH, EURC)
    assert legacy is v2
    assert "pool" not in v2  # V2 entry has no baked pool -> would hit the dead V2 pool

    # The fix: matching on pair + tickSpacing + version returns the V3 entry, which bakes
    # the correct live pool address. Range metrics therefore read the right pool. The
    # baked address short-circuits _aero_pool_address (no factory call), so this is offline.
    matched = dp._aero_match_pool_cfg(WETH, EURC, 100, "v3")
    assert matched is v3
    assert matched is not legacy
    assert matched.get("slipstreamVersion") == 1
    assert dp._aero_pool_address(matched) == Web3.to_checksum_address(v3["pool"])


def test_weth_euroc_v2_still_resolves_v2_entry():
    matched = dp._aero_match_pool_cfg(WETH, EURC, 100, "v2")
    assert matched is dp.AERODROME_POOLS["weth-euroc-100"]
    assert matched.get("slipstreamVersion", 0) == 0


def test_virtual_weth_v3_resolves_own_pool():
    v3 = dp.AERODROME_POOLS["virtual-weth-50-v3"]
    matched = dp._aero_match_pool_cfg(v3["token0"], v3["token1"], 50, "v3")
    assert matched is v3
    assert dp._aero_pool_address(matched) == Web3.to_checksum_address(v3["pool"])


def test_virtual_weth_multi_tickspacing_v2_disambiguated():
    # VIRTUAL/WETH exists as V2 at tickSpacing 100 AND 200; the bare pair match would
    # always return the 100 entry. tickSpacing now disambiguates the V2 pools too.
    t0 = dp.AERODROME_POOLS["virtual-weth-100"]["token0"]
    t1 = dp.AERODROME_POOLS["virtual-weth-100"]["token1"]
    assert dp._aero_match_pool_cfg(t0, t1, 100, "v2") is dp.AERODROME_POOLS["virtual-weth-100"]
    assert dp._aero_match_pool_cfg(t0, t1, 200, "v2") is dp.AERODROME_POOLS["virtual-weth-200"]


def test_match_pool_cfg_pair_only_unchanged_for_unique_pair():
    # Legacy 2-arg call still works for callers/tests that don't pass tickSpacing/version.
    cfg = dp.AERODROME_POOLS["aero-cbbtc-200"]
    assert dp._aero_match_pool_cfg(cfg["token0"], cfg["token1"]) is cfg
