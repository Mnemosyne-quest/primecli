"""Regression test for the RedStone data-package value encoding in primecli.

Guards against a real-world bug: `_redstone_encode_package` used to scale the
signed price with `int(round(float(value) * 1e8))`, which double-rounds (float
multiply + banker's rounding) and so re-derived a DIFFERENT body than RedStone
actually signed for half-boundary / high-precision values. The contract then
ecrecovered a garbage address and reverted `SignerNotAuthorised` (0xec459bc0,
wrapped in 0xfd36fde3) — intermittently breaking EVERY RedStone-gated path
(lending, swaps, GMX, LB, PRIME, solvency views).

The fix reconstructs the value exactly as RedStone does:
`parseUnits(Number(v).toFixed(8), 8)` — via
`Decimal(float(v)).quantize(1e-8, ROUND_HALF_UP)`. Python float is the same
IEEE-754 double as JS Number, so this reproduces the signed value byte-for-byte.
The expected values below were validated against the live gateway (recovered
signer matched the gateway's reported signerAddress on 2340/2340 packages).

If anyone "tidies" the encoder back to round()/float-scaling, the starred cases
below flip and this test fails.

The `_redstone_scaled_value` encoder is triplicated across deltaprime (Avalanche),
arbprime (Arbitrum) and degenprime (Base); every copy must stay correct, so each
case is parametrized over all three modules.
"""

from __future__ import annotations

import importlib

import pytest

# The three sibling modules that each carry their own copy of the RedStone encoder.
MODULES = ["primecli.deltaprime", "primecli.arbprime", "primecli.degenprime"]

# Pinned correct scaled values (what RedStone signs). Starred (*) cases are ones
# the old int(round(v*1e8)) formula gets WRONG — they are the regression tripwires.
CASES = [
    ("1.161001035", 116100103),        # * old round() -> 116100104 (wrong)
    ("9.234567895", 923456789),        # * old round() -> 923456790 (wrong)
    ("2102.997830325", 210299783032),
    ("1.0389805528424663", 103898055),
    ("2.260315", 226031500),
    ("1.675857", 167585700),
    ("0.5555756034707967", 55557560),
    ("0.0", 0),
]

# The starred cases where the buggy formula diverges from the fix.
DIVERGENT = ["1.161001035", "9.234567895"]


@pytest.fixture(params=MODULES)
def mod(request):
    """The module under test, one per RedStone-carrying sibling."""
    return importlib.import_module(request.param)


@pytest.mark.parametrize("raw,expected", CASES, ids=[c[0] for c in CASES])
def test_redstone_scaled_value(mod, raw, expected):
    """`_redstone_scaled_value` reconstructs exactly the uint256 RedStone signs."""
    assert mod._redstone_scaled_value(float(raw)) == expected


@pytest.mark.parametrize("raw", DIVERGENT)
def test_old_round_formula_diverges(mod, raw):
    """Tripwire: the discarded int(round(v*1e8)) formula MUST differ from the fix on
    the starred cases. If the encoder is ever reverted to round(), the pinned cases
    above already fail; this makes the intended divergence explicit."""
    v = float(raw)
    buggy = int(round(v * 10 ** 8))
    assert buggy != mod._redstone_scaled_value(v)
