"""Offline tests for the ParaSwap re-quote loop in degenprime.

`_paraswap_requote_until_clean` is the resilience fix for transient SwapFailed()
reverts: Velora's /prices is non-deterministic per call, so a route whose executor
is dead-but-whitelisted (or an RFQ/maker leg) usually clears on a fresh quote. The
loop re-quotes up to `_PARASWAP_REQUOTE_ATTEMPTS` times and returns the FIRST route
that simulates clean for the caller's facet method.

Pure/offline: the three ParaSwap HTTP helpers (`_paraswap_price_route`,
`_paraswap_build_tx`, `_paraswap_decode_and_check`) and the simulate callback are
all stubbed. No network, no signing, no RPC.
"""

from __future__ import annotations

import importlib

import pytest

dp = importlib.import_module("primecli.degenprime")

SRC = "0x" + "11" * 20
DEST = "0x" + "22" * 20
PA = "0x" + "33" * 20
AMOUNT = 1000


def _patch_quote_chain(monkeypatch, *, n_routes):
    """Stub the price-route / build-tx / decode chain so each loop iteration produces a
    distinct, identifiable route. Route k carries destAmount=1000+k and selector word
    bytes that encode k, so we can assert WHICH route the loop returned."""
    calls = {"price": 0, "build": 0, "decode": 0}

    def fake_price(src_token, src_dec, dest_token, dest_dec, amount_in_wei, user_addr):
        calls["price"] += 1
        k = calls["price"]
        return {"destAmount": str(1000 + k), "contractMethod": "swapExactAmountIn",
                "_k": k}

    def fake_build(price_route, *a, **kw):
        calls["build"] += 1
        k = price_route["_k"]
        # 4-byte selector + a body byte that records k, so data_bytes differs per route.
        data_hex = "0xe3ead59e" + f"{k:064x}"
        return {"data": data_hex, "to": "0xaugustus"}

    def fake_decode(selector_hex, data_bytes, src_token, dest_token, expected_from, pa_cs):
        calls["decode"] += 1
        k = int.from_bytes(data_bytes, "big")  # body == k
        # (executor, src, dest, from_amt, to_amt)
        return ("0xexec", src_token, dest_token, expected_from, 990 + k)

    monkeypatch.setattr(dp, "_paraswap_price_route", fake_price)
    monkeypatch.setattr(dp, "_paraswap_build_tx", fake_build)
    monkeypatch.setattr(dp, "_paraswap_decode_and_check", fake_decode)
    return calls


def _run(sim_fn):
    return dp._paraswap_requote_until_clean(
        SRC, 18, DEST, 6, AMOUNT, 1.0, PA, sim_fn)


def test_first_route_clean_no_requote(monkeypatch):
    """Happy path: the first quote simulates clean -> exactly one quote, returned as-is."""
    calls = _patch_quote_chain(monkeypatch, n_routes=5)
    sim_calls = []

    def sim_ok(selector4, db):
        sim_calls.append(db)
        return True, None

    route = _run(sim_ok)
    assert route["sim_ok"] is True
    assert calls["price"] == 1  # no re-quote
    assert len(sim_calls) == 1
    assert route["quoted_out"] == 1001  # route k=1
    assert route["min_out"] == 991


def test_bad_then_good_requotes_and_picks_good(monkeypatch):
    """The first two routes revert in simulation; the third clears. The loop must
    re-quote and return the THIRD route (its artifacts), not the first."""
    calls = _patch_quote_chain(monkeypatch, n_routes=5)
    attempts = {"n": 0}

    def sim_bad_then_good(selector4, db):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return False, "SwapFailed()"
        return True, None

    route = _run(sim_bad_then_good)
    assert route["sim_ok"] is True
    assert calls["price"] == 3  # re-quoted twice before the clean route
    assert route["quoted_out"] == 1003  # route k=3 won
    assert route["min_out"] == 993
    # data_bytes belongs to the winning (3rd) route.
    assert int.from_bytes(route["data_bytes"], "big") == 3


def test_all_bad_returns_last_with_sim_ok_false(monkeypatch):
    """If every quote reverts, the loop exhausts its attempts and returns the LAST
    route's artifacts with sim_ok=False so the caller can preview and refuse."""
    calls = _patch_quote_chain(monkeypatch, n_routes=10)

    def sim_always_bad(selector4, db):
        return False, "SwapFailed()"

    route = _run(sim_always_bad)
    assert route["sim_ok"] is False
    assert route["last_err"] == "SwapFailed()"
    assert calls["price"] == dp._PARASWAP_REQUOTE_ATTEMPTS  # tried the full budget
    # Returned artifacts are from the final attempt.
    assert int.from_bytes(route["data_bytes"], "big") == dp._PARASWAP_REQUOTE_ATTEMPTS


def test_executors_pruned_to_verified_good():
    """The static whitelist is pruned to the single verified-good v6.2 executor; the
    dead legacy executors (and the useless fallback constant) are gone."""
    assert dp.PARASWAP_EXECUTORS == {"0x8faa0000c10015610005ca010ee000d006e0e820"}
    assert not hasattr(dp, "_PARASWAP_FALLBACK_EXECUTOR")


def test_price_route_excludes_rfq_dexs(monkeypatch):
    """`_paraswap_price_route` must send excludeDEXS dropping the RFQ/maker sources so
    they never reach the build/simulate stage."""
    captured = {}

    class _Resp:
        @staticmethod
        def json():
            return {"priceRoute": {"destAmount": "1"}}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(dp.requests, "get", fake_get)
    dp._paraswap_price_route(SRC, 18, DEST, 6, AMOUNT, PA)
    excluded = captured["params"]["excludeDEXS"]
    assert "AugustusRFQ" in excluded
    assert "ParaSwapLimitOrders" in excluded
