"""Offline tests for the degenprime additions:

  * `_swap_with_usdc_fallback` — direct swap, else a two-hop via USDC for
    pool-token pairs with no direct route (e.g. AERO<->cbBTC). Engages only on
    --execute; sizes hop 2 from the realised USDC balance delta.
  * `_aero_range_metrics` — live CL range math (in/out of range, % through, ±width,
    distance to each edge) from the pool tick.
  * `_aero_match_pool_cfg` — match a position's token pair to a pool config.

Pure/offline: cmd_swap, the in-account balance read, and the pool-tick read are
all stubbed. No network, no signing.
"""

from __future__ import annotations

import importlib

import pytest

dp = importlib.import_module("primecli.degenprime")


# ─────────────────────────── cmd_swap / balance stubs ───────────────────────────

class _SwapRecorder:
    """Stub for cmd_swap: pops a return value per call, records the call args."""

    def __init__(self, returns):
        self._returns = list(returns)
        self.calls = []

    def __call__(self, from_sym, to_sym, amount, slippage_pct=1.0, execute=False):
        self.calls.append({
            "from": from_sym, "to": to_sym, "amount": amount,
            "slippage": slippage_pct, "execute": execute,
        })
        return self._returns.pop(0) if self._returns else None


def _patch_swap(monkeypatch, returns):
    rec = _SwapRecorder(returns)
    monkeypatch.setattr(dp, "cmd_swap", rec)
    return rec


def _patch_balance(monkeypatch, usdc_wei_sequence):
    """Stub _aero_in_account_balance: returns successive USDC balances (wei)."""
    seq = list(usdc_wei_sequence)
    monkeypatch.setattr(dp, "_aero_in_account_balance", lambda account, sym: seq.pop(0))


ACCT = object()  # opaque account handle (the stubs ignore it)


# ─────────────────────────── _swap_with_usdc_fallback ───────────────────────────

def test_direct_swap_success_no_fallback(monkeypatch):
    rec = _patch_swap(monkeypatch, [True])
    ok = dp._swap_with_usdc_fallback(ACCT, "AERO", "cbBTC", 100.0, 1.0, execute=True)
    assert ok is True
    assert len(rec.calls) == 1  # no fallback attempted
    assert rec.calls[0]["from"] == "AERO" and rec.calls[0]["to"] == "cbBTC"


def test_preview_does_not_trigger_fallback(monkeypatch):
    # cmd_swap returns None in preview; the fallback must NOT engage on execute=False.
    rec = _patch_swap(monkeypatch, [None])
    ok = dp._swap_with_usdc_fallback(ACCT, "AERO", "cbBTC", 100.0, 1.0, execute=False)
    assert not ok
    assert len(rec.calls) == 1
    assert rec.calls[0]["execute"] is False


def test_two_hop_fallback_sizes_hop2_from_delta(monkeypatch):
    # Direct AERO->cbBTC fails; hop1 AERO->USDC succeeds producing 142 USDC;
    # hop2 USDC->cbBTC spends 99% of the *delta*.
    rec = _patch_swap(monkeypatch, [None, True, True])  # direct fail, hop1 ok, hop2 ok
    _patch_balance(monkeypatch, [10_000_000, 152_000_000])  # before 10 USDC, after 152
    ok = dp._swap_with_usdc_fallback(ACCT, "AERO", "cbBTC", 120.0, 1.0, execute=True)
    assert ok is True
    assert len(rec.calls) == 3
    assert (rec.calls[1]["from"], rec.calls[1]["to"]) == ("AERO", "USDC")
    assert (rec.calls[2]["from"], rec.calls[2]["to"]) == ("USDC", "cbBTC")
    # delta = 152 - 10 = 142 USDC; hop2 spends 99%
    assert rec.calls[2]["amount"] == pytest.approx(142.0 * 0.99, rel=1e-9)


def test_usdc_leg_has_no_fallback(monkeypatch):
    # When a leg is already USDC there is no intermediary to fall back to.
    rec = _patch_swap(monkeypatch, [None])  # direct fails
    ok = dp._swap_with_usdc_fallback(ACCT, "USDC", "cbBTC", 100.0, 1.0, execute=True)
    assert not ok
    assert len(rec.calls) == 1  # only the direct attempt


def test_hop1_failure_aborts(monkeypatch):
    rec = _patch_swap(monkeypatch, [None, None])  # direct fail, hop1 fail
    ok = dp._swap_with_usdc_fallback(ACCT, "AERO", "cbBTC", 100.0, 1.0, execute=True)
    assert ok is False
    assert len(rec.calls) == 2  # direct + hop1, no hop2


def test_tiny_usdc_delta_aborts_before_hop2(monkeypatch):
    # hop1 succeeds but produces < $1 of USDC delta -> don't attempt hop2.
    rec = _patch_swap(monkeypatch, [None, True])  # direct fail, hop1 ok
    _patch_balance(monkeypatch, [5_000_000, 5_500_000])  # delta 0.5 USDC
    ok = dp._swap_with_usdc_fallback(ACCT, "AERO", "cbBTC", 100.0, 1.0, execute=True)
    assert ok is False
    assert len(rec.calls) == 2  # no hop2


# ─────────────────────────── _aero_range_metrics ───────────────────────────

class _Slot0Fn:
    def __init__(self, tick):
        self._tick = tick

    def slot0(self):
        outer = self

        class _Call:
            def call(self_inner):
                return (0, outer._tick)  # (sqrtPriceX96 unused, tick)
        return _Call()


class _StubContract:
    def __init__(self, tick):
        self.functions = _Slot0Fn(tick)


class _StubW3:
    def __init__(self, tick):
        self._tick = tick

        class _Eth:
            def contract(_self, address=None, abi=None):
                return _StubContract(self._tick)
        self.eth = _Eth()


def _patch_pool_addr(monkeypatch):
    # Return a valid checksummable address; the real factory call is bypassed.
    monkeypatch.setattr(dp, "_aero_pool_address",
                        lambda cfg: "0x4200000000000000000000000000000000000006")


def test_range_metrics_in_range(monkeypatch):
    _patch_pool_addr(monkeypatch)
    w3 = _StubW3(tick=-347978)  # current tick inside [-349800, -346800]
    m = dp._aero_range_metrics(w3, {"dummy": 1}, -349800, -346800)
    assert m is not None
    assert m["current_tick"] == -347978
    assert m["in_range"] is True
    assert m["pct_through_range"] == pytest.approx(60.7, abs=0.1)
    assert m["width_pct"] == pytest.approx(16.2, abs=0.2)
    assert m["dist_to_lower_pct"] == pytest.approx(19.98, abs=0.2)
    assert m["dist_to_upper_pct"] == pytest.approx(12.5, abs=0.2)


def test_range_metrics_out_of_range_below(monkeypatch):
    _patch_pool_addr(monkeypatch)
    w3 = _StubW3(tick=-350000)  # below the lower bound
    m = dp._aero_range_metrics(w3, {"dummy": 1}, -349800, -346800)
    assert m["in_range"] is False
    assert m["pct_through_range"] < 0  # price has fallen out the bottom


def test_range_metrics_degenerate_span_returns_none(monkeypatch):
    _patch_pool_addr(monkeypatch)
    w3 = _StubW3(tick=-347000)
    assert dp._aero_range_metrics(w3, {"dummy": 1}, -347000, -347000) is None


def test_range_metrics_tick_read_failure_returns_none(monkeypatch):
    def _boom(cfg):
        raise RuntimeError("pool not found")
    monkeypatch.setattr(dp, "_aero_pool_address", _boom)
    assert dp._aero_range_metrics(_StubW3(0), {"dummy": 1}, -10, 10) is None


# ─────────────────────────── _aero_match_pool_cfg ───────────────────────────

def test_match_pool_cfg_known_pair():
    cfg = dp.AERODROME_POOLS["aero-cbbtc-200"]
    matched = dp._aero_match_pool_cfg(cfg["token0"], cfg["token1"])
    assert matched is not None
    assert {matched["symbol0"].upper(), matched["symbol1"].upper()} == {"AERO", "CBBTC"}


def test_match_pool_cfg_order_insensitive():
    cfg = dp.AERODROME_POOLS["aero-cbbtc-200"]
    # swap the order of the two tokens — still matches
    assert dp._aero_match_pool_cfg(cfg["token1"], cfg["token0"]) is not None


def test_match_pool_cfg_unknown_pair_returns_none():
    assert dp._aero_match_pool_cfg(
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
    ) is None
