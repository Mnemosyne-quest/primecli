"""Exactness tests for `to_wei_units` across all three sibling modules.

`to_wei_units` converts a human amount to integer base units via Decimal, so it
must NOT drift like a float multiply would (e.g. 0.000001 at 6 decimals has to
land on exactly 1, not 0 or 2). The helper is triplicated, so every copy is
checked.
"""

from __future__ import annotations

import importlib

import pytest

MODULES = ["primecli.deltaprime", "primecli.arbprime", "primecli.degenprime"]

# (amount, decimals, expected_base_units)
CASES = [
    (1234.5678, 18, 1234567800000000000000),
    ("0.1", 18, 10 ** 17),
    (1, 6, 10 ** 6),
    (0.000001, 6, 1),
    (1000000.000001, 6, 1000000000001),
    ("123456.789012345678", 18, 123456789012345678000000),  # large 18-dec case
    (0, 18, 0),
]


@pytest.fixture(params=MODULES)
def mod(request):
    return importlib.import_module(request.param)


@pytest.mark.parametrize(
    "amount,decimals,expected",
    CASES,
    ids=[f"{a}@{d}" for a, d, _ in CASES],
)
def test_to_wei_units(mod, amount, decimals, expected):
    assert mod.to_wei_units(amount, decimals) == expected
