"""Offline tests for the `--reserve SYMBOL:FRACTION` reserve seam on
`aero-add-liquidity --use-all-available`.

The reserve holds back a FRACTION of a specific asset's inventoried loose balance
from the sweep-and-swap, leaving that portion loose + untouched in the account (no
swap, no mint). Used by the defisims AERO reward-hold policy, but generic over any
symbol/fraction.

Two load-bearing guarantees are locked here:
  * Backward compatibility: with no reserve the deploy set is byte-identical (the
    SAME object is returned) — this path serves every position including core1.
  * The `reserve` parameter exists on `cmd_aero_add_liquidity` — the exact seam the
    defisims `reserve_backend_available()` capability probe inspects.

Pure/offline: no network, no signing — just the parse + subtract helpers.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

import importlib

dp = importlib.import_module("primecli.degenprime")

WEI = 10 ** 18


# ─────────────────────────── _aero_parse_reserve_specs ───────────────────────────

def test_parse_single_spec():
    assert dp._aero_parse_reserve_specs(["AERO:0.5"]) == {"AERO": 0.5}


def test_parse_is_case_insensitive_on_symbol():
    assert dp._aero_parse_reserve_specs(["aero:0.5"]) == {"AERO": 0.5}


def test_parse_multiple_specs():
    assert dp._aero_parse_reserve_specs(["AERO:0.5", "weth:0.25"]) == {
        "AERO": 0.5, "WETH": 0.25}


def test_parse_empty_and_none():
    assert dp._aero_parse_reserve_specs([]) == {}
    assert dp._aero_parse_reserve_specs(None) == {}


def test_parse_boundary_fractions_are_valid():
    # [0,1] inclusive: 0 (hold nothing) and 1 (hold all) are both legal.
    assert dp._aero_parse_reserve_specs(["AERO:0"]) == {"AERO": 0.0}
    assert dp._aero_parse_reserve_specs(["AERO:1"]) == {"AERO": 1.0}


@pytest.mark.parametrize("bad", [
    "AERO:2.0",     # > 1
    "AERO:-0.1",    # < 0
    "AERO:abc",     # non-numeric
    "AERO:nan",     # NaN
    "AERO",         # no colon
    ":0.5",         # no symbol
])
def test_parse_rejects_bad_specs(bad):
    with pytest.raises(ValueError):
        dp._aero_parse_reserve_specs([bad])


# ─────────────────────────── _aero_apply_reserve ───────────────────────────

def test_apply_none_is_strict_noop_same_object():
    v = {"AERO": 100 * WEI, "WETH": 2 * WEI}
    assert dp._aero_apply_reserve(v, None) is v          # identical object
    assert dp._aero_apply_reserve(v, {}) is v            # empty dict too


def test_apply_half_subtracts_exactly_via_integer_math():
    v = {"AERO": 100 * WEI}
    out = dp._aero_apply_reserve(v, {"AERO": 0.5})
    assert out == {"AERO": 50 * WEI}
    # original untouched (helper returns a new dict when reserve is active)
    assert v == {"AERO": 100 * WEI}


def test_apply_leaves_non_reserved_assets_untouched():
    v = {"AERO": 100 * WEI, "WETH": 2 * WEI, "USDC": 500 * 10 ** 6}
    out = dp._aero_apply_reserve(v, {"AERO": 0.5})
    assert out["AERO"] == 50 * WEI
    assert out["WETH"] == 2 * WEI
    assert out["USDC"] == 500 * 10 ** 6


def test_apply_is_case_insensitive_on_held_symbol():
    v = {"aero": 100 * WEI}          # account labels it lowercase
    out = dp._aero_apply_reserve(v, {"AERO": 0.5})
    assert out["aero"] == 50 * WEI


def test_apply_full_reserve_drops_asset_from_deploy_set():
    v = {"AERO": 5 * WEI, "WETH": 1 * WEI}
    out = dp._aero_apply_reserve(v, {"AERO": 1.0})
    assert "AERO" not in out         # fully held loose, nothing to deploy
    assert out["WETH"] == 1 * WEI


def test_apply_reserve_symbol_absent_is_content_noop():
    v = {"WETH": 1 * WEI}
    out = dp._aero_apply_reserve(v, {"AERO": 0.5})
    assert out == v                  # equal content (new dict, no matching asset)


def test_apply_matches_the_parser_output_end_to_end():
    reserve = dp._aero_parse_reserve_specs(["AERO:0.5"])
    v = {"AERO": 80 * WEI, "USDC": 100 * 10 ** 6}
    out = dp._aero_apply_reserve(v, reserve)
    assert out["AERO"] == 40 * WEI and out["USDC"] == 100 * 10 ** 6


def test_apply_fraction_uses_decimal_precision():
    # 1/3 hold on a non-round balance: kept = bal - int(bal * 0.333333...).
    bal = 123456789 * WEI + 7
    out = dp._aero_apply_reserve({"AERO": bal}, {"AERO": 0.333333})
    expected_reserved = int(Decimal(bal) * Decimal("0.333333"))
    assert out["AERO"] == bal - expected_reserved


# ─────────────────────────── capability-probe contract ───────────────────────────

def test_cmd_aero_add_liquidity_exposes_reserve_param():
    """defisims `reserve_backend_available()` gates enforcement on exactly this:
    `"reserve" in inspect.signature(cmd_aero_add_liquidity).parameters`. Locking it
    here means a refactor that drops/renames the param fails a test instead of
    silently disabling the reward-hold enforcement in the live automation."""
    params = inspect.signature(dp.cmd_aero_add_liquidity).parameters
    assert "reserve" in params
