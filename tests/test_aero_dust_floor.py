"""Regression tests for _aero_resweep_dust_floor (2026-08-13 p2 incident).

The reserve carve-out leaves a sub-$5 remainder; sweeping it makes ParaSwap
refuse ("max impact reached") and aborts the whole increase/mint.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from primecli.degenprime import _aero_resweep_dust_floor  # noqa: E402


def test_dust_remainder_after_reserve_is_dropped():
    # AERO total $12.13, reserve 0.999968 -> remaining 0.000947 AERO (~$0.0004)
    valuable = {"AERO": 947 * 10 ** 6}          # remaining wei after the reserve
    inventory = {"AERO": [29.584966 * 10 ** 18, 18, 12.13]}  # total wei, dec, usd
    out = _aero_resweep_dust_floor(valuable, inventory, {"VIRTUAL", "ETH"})
    assert "AERO" not in out


def test_priced_nonpool_remainder_kept():
    # AERO total $120, reserve 0.5 -> remaining $60 >= $5 -> kept
    valuable = {"AERO": 50 * 10 ** 18}
    inventory = {"AERO": [100 * 10 ** 18, 18, 120.0]}
    out = _aero_resweep_dust_floor(valuable, inventory, {"VIRTUAL", "ETH"})
    assert "AERO" in out


def test_pool_legs_always_kept_even_dust():
    # A pool leg with a tiny remainder must stay (no swap needed, goes into LP)
    valuable = {"VIRTUAL": 1 * 10 ** 18}
    inventory = {"VIRTUAL": [100 * 10 ** 18, 18, 58.0]}
    out = _aero_resweep_dust_floor(valuable, inventory, {"VIRTUAL", "ETH"})
    assert "VIRTUAL" in out


def test_unpriced_asset_kept_when_no_inventory_row():
    valuable = {"WEIRD": 123}
    out = _aero_resweep_dust_floor(valuable, {}, {"VIRTUAL", "ETH"})
    assert "WEIRD" in out
