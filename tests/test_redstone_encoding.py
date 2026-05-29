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

Run: python3 tests/test_redstone_encoding.py
"""

from __future__ import annotations

import sys

from primecli import deltaprime as dp

# ──────────────────────────────────────────────────────────────────────────────
# Test harness

PASSED = 0
FAILED = 0
FAIL_NAMES: list[str] = []


def assert_eq(name: str, got, expected) -> None:
    global PASSED, FAILED
    if got == expected:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        FAIL_NAMES.append(name)
        print(f"  FAIL  {name}")
        print(f"        expected: {expected!r}")
        print(f"        got:      {got!r}")


def assert_true(name: str, cond: bool) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        FAIL_NAMES.append(name)
        print(f"  FAIL  {name}")


# ──────────────────────────────────────────────────────────────────────────────
# Pinned correct scaled values (what RedStone signs). Starred (*) cases are ones
# the old int(round(v*1e8)) formula gets WRONG — they are the regression
# tripwires.

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

# The starred cases where the buggy formula diverges (drives the assertions below).
DIVERGENT = ["1.161001035", "9.234567895"]

print("RedStone value-encoding regression")

for raw, expected in CASES:
    assert_eq(f"_redstone_scaled_value({raw})", dp._redstone_scaled_value(float(raw)), expected)

# Tripwire: the discarded round() formula MUST differ from the fix on the starred
# cases. (If the encoder is ever reverted to round(), the pinned assertions above
# already fail; this makes the intent explicit and documents the exact divergence.)
for raw in DIVERGENT:
    v = float(raw)
    buggy = int(round(v * 10 ** 8))
    assert_true(
        f"old round() diverges from the fix on {raw} (got {buggy}, fix {dp._redstone_scaled_value(v)})",
        buggy != dp._redstone_scaled_value(v),
    )

# ──────────────────────────────────────────────────────────────────────────────
# Summary

print()
print("===================================")
print(f"Tests run:    {PASSED + FAILED}")
print(f"Tests failed: {FAILED}")
if FAILED > 0:
    print("Failing tests:")
    for n in FAIL_NAMES:
        print(f"  - {n}")
    sys.exit(1)
print("All RedStone value-encoding tests passed.")
sys.exit(0)
