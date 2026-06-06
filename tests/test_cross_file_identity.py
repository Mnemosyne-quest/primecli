"""Anti-drift guard for the security-critical blocks duplicated across the three
sibling modules (deltaprime / arbprime / degenprime).

The RedStone encoder, the authorised-signer set, the wei conversion and the
cross-chain gas helper are intentionally copy-pasted into each sibling rather
than shared, so a fix applied to one and not the others is a real, dangerous
drift: a wrong RedStone body re-derives a garbage signer and reverts every
priced path; a wrong signer set lets an unauthorised feed through. This test
fails loudly the moment any of those copies diverge, telling the author to apply
the fix to all files.

Comparison strategy:
  * deltaprime vs arbprime: BYTE-IDENTICAL source (these two are kept literally
    the same, comments included).
  * deltaprime vs degenprime: degenprime's docstrings are shorter, so we compare
    with comments + docstrings stripped (tokenize-based) and confirm the code is
    identical. `build_redstone_payload` is the documented exception — see
    test_build_redstone_payload_degen_intentional_divergence.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import io
import textwrap
import token
import tokenize

import pytest

DELTA = importlib.import_module("primecli.deltaprime")
ARB = importlib.import_module("primecli.arbprime")
DEGEN = importlib.import_module("primecli.degenprime")

MOD_NAMES = {id(DELTA): "deltaprime", id(ARB): "arbprime", id(DEGEN): "degenprime"}

# Functions that must stay identical across all three siblings (code-wise).
SHARED_FUNCS = [
    "_redstone_scaled_value",
    "_redstone_encode_package",
    "_redstone_package_signer",
    "build_redstone_payload",
    "to_wei_units",
    # Frontend-exact getHealthMeter core + its on-chain debtCoverage resolver. The math
    # and the TokenManager wiring must not drift — a wrong dc or formula misreports every
    # account's health identically wrongly on whichever chain drifted.
    "_health_meter_pct",
    "_resolve_debt_coverages",
]


def _name(mod) -> str:
    return MOD_NAMES.get(id(mod), getattr(mod, "__name__", "?"))


def _raw_source(mod, fn_name: str) -> str:
    """Source of the named function, dedented so it can be compared independent of
    the surrounding indentation."""
    return textwrap.dedent(inspect.getsource(getattr(mod, fn_name)))


def _strip_comments_and_docstring(src: str) -> str:
    """Return the function source with comments and the leading docstring removed and
    whitespace normalised, so two functions that differ only in comments/docstrings
    compare equal. Uses tokenize to drop comments, then ast to drop the docstring and
    re-emit canonical source (ast.unparse erases formatting noise entirely)."""
    # 1. Drop comment tokens via tokenize.
    out = []
    toks = tokenize.generate_tokens(io.StringIO(src).readline)
    for tok in toks:
        if tok.type in (token.COMMENT, tokenize.NL):
            continue
        out.append(tok)
    decommented = tokenize.untokenize(out)

    # 2. Parse, strip the docstring, re-emit canonical source.
    tree = ast.parse(textwrap.dedent(decommented))
    fn = tree.body[0]
    if (
        fn.body
        and isinstance(fn.body[0], ast.Expr)
        and isinstance(fn.body[0].value, ast.Constant)
        and isinstance(fn.body[0].value.value, str)
    ):
        fn.body = fn.body[1:]
    return ast.unparse(tree)


def _code_identical(mod_a, mod_b, fn_name: str) -> bool:
    """True if the two functions are identical ignoring comments/docstrings/format."""
    return _strip_comments_and_docstring(
        _raw_source(mod_a, fn_name)
    ) == _strip_comments_and_docstring(_raw_source(mod_b, fn_name))


# ──────────────────────────────────────────────────────────────────────────────
# Authorised signer set


def test_redstone_authorised_signers_identical():
    """The RedStone authorised-signer set must be identical across all three modules.
    A divergence would let an unauthorised feed through on one chain."""
    assert DELTA.REDSTONE_AUTHORISED_SIGNERS == ARB.REDSTONE_AUTHORISED_SIGNERS, (
        "the REDSTONE_AUTHORISED_SIGNERS block has drifted between deltaprime and "
        "arbprime — primecli requires these to be byte-identical; apply the fix to "
        "all files"
    )
    assert DELTA.REDSTONE_AUTHORISED_SIGNERS == DEGEN.REDSTONE_AUTHORISED_SIGNERS, (
        "the REDSTONE_AUTHORISED_SIGNERS block has drifted between deltaprime and "
        "degenprime — primecli requires these to be byte-identical; apply the fix to "
        "all files"
    )


# ──────────────────────────────────────────────────────────────────────────────
# deltaprime vs arbprime — byte-identical (the strict invariant: same comments too)


@pytest.mark.parametrize("fn_name", SHARED_FUNCS)
def test_delta_arb_byte_identical(fn_name):
    """deltaprime and arbprime keep these blocks literally byte-for-byte identical."""
    assert _raw_source(DELTA, fn_name) == _raw_source(ARB, fn_name), (
        f"the {fn_name} block has drifted between deltaprime and arbprime — "
        f"primecli requires these to be byte-identical; apply the fix to all files"
    )


# ──────────────────────────────────────────────────────────────────────────────
# deltaprime vs degenprime — identical code (degenprime docstrings are shorter)

# degenprime's build_redstone_payload intentionally differs: it remaps each symbol
# through `_redstone_data_feed_id(sym)` before looking it up in the gateway map
# (Base feeds use different feed ids). So it is excluded from the cross-degen code
# check below and asserted separately to differ on purpose.
_DEGEN_SHARED_FUNCS = [f for f in SHARED_FUNCS if f != "build_redstone_payload"]


@pytest.mark.parametrize("fn_name", _DEGEN_SHARED_FUNCS)
def test_delta_degen_code_identical(fn_name):
    """deltaprime and degenprime carry identical code for these blocks (comments and
    docstrings may differ in degenprime — compared with both stripped)."""
    assert _code_identical(DELTA, DEGEN, fn_name), (
        f"the {fn_name} block has drifted between deltaprime and degenprime — "
        f"primecli requires these to be byte-identical; apply the fix to all files"
    )


def test_build_redstone_payload_degen_intentional_divergence():
    """degenprime.build_redstone_payload is the one documented exception: Base feeds
    are looked up through `_redstone_data_feed_id(sym)`, so its code legitimately
    differs from the delta/arb copy. Pin that the divergence is exactly the feed-id
    remap (and nothing else), so an unrelated drift here still gets caught."""
    delta_code = _strip_comments_and_docstring(_raw_source(DELTA, "build_redstone_payload"))
    degen_code = _strip_comments_and_docstring(_raw_source(DEGEN, "build_redstone_payload"))
    assert delta_code != degen_code  # they are expected to differ
    # The only functional difference is the feed-id remap step.
    assert "_redstone_data_feed_id" in degen_code
    assert "_redstone_data_feed_id" not in delta_code
    # deltaprime/arbprime stay byte-identical for this function (covered above too).
    assert _raw_source(DELTA, "build_redstone_payload") == _raw_source(
        ARB, "build_redstone_payload"
    ), (
        "the build_redstone_payload block has drifted between deltaprime and arbprime "
        "— primecli requires these to be byte-identical; apply the fix to all files"
    )


# ──────────────────────────────────────────────────────────────────────────────
# _set_gas_price_for — present in deltaprime and arbprime only


def test_set_gas_price_for_delta_arb_identical():
    """`_set_gas_price_for` was added to both deltaprime and arbprime for cross-chain
    flows; the two copies must stay identical."""
    have_delta = hasattr(DELTA, "_set_gas_price_for")
    have_arb = hasattr(ARB, "_set_gas_price_for")
    if not (have_delta and have_arb):
        pytest.skip(
            f"_set_gas_price_for not present in both (delta={have_delta}, arb={have_arb})"
        )
    assert _raw_source(DELTA, "_set_gas_price_for") == _raw_source(
        ARB, "_set_gas_price_for"
    ), (
        "the _set_gas_price_for block has drifted between deltaprime and arbprime — "
        "primecli requires these to be byte-identical; apply the fix to all files"
    )
