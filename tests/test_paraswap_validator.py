"""Offline tests for the ParaSwap calldata validator in deltaprime.

`_paraswap_decode_and_check` mirrors the on-chain ParaSwapFacet
decodeParaSwapData + validateSwapParameters on the calldata the ParaSwap API
returns, so a bad build fails loud locally instead of reverting on-chain. It is a
pure byte-decoder — no RPC, no web3 provider needed — so we synthesise minimal
swapExactAmountIn (0xe3ead59e) calldata and assert each guard.

Calldata layout the validator decodes (after the 4-byte selector is stripped,
i.e. `data_bytes`):
  word 0      [0:32]    executor address (right-aligned in the 32-byte word)
  words 1-7   [32:256]  abi(address src, address dest, uint256 fromAmount,
                        uint256 toAmount, uint256 quotedAmount, bytes32 metadata,
                        address beneficiary)
  word 8      [256:288] partnerAndFee  (partner = bits >>96, feeBps = low 14 bits)

NOTE on the "non-whitelisted executor" case: the validator does NOT raise on an
unknown executor — it prints a warning and returns the decoded executor. The
actual swap path (`_swap_via_paraswap` / `cmd_swap_debt`) is what reacts, by
patching the executor to a known fallback. So the test below asserts the real
contract of this function: a warning is emitted and the executor is returned,
not that it raises.
"""

from __future__ import annotations

import importlib

import pytest
from eth_abi import encode as abi_encode

dp = importlib.import_module("primecli.deltaprime")
Web3 = dp.Web3

SELECTOR = "0xe3ead59e"  # swapExactAmountIn — the fully field-decoded variant

# A whitelisted executor (lower-cased members live in PARASWAP_EXECUTORS).
WHITELISTED_EXECUTOR = "0xdef171fe48cf0115b1d80b88dc8eab59176fee57"
NON_WHITELISTED_EXECUTOR = "0x" + "ab" * 20

SRC = Web3.to_checksum_address("0x" + "11" * 20)
DEST = Web3.to_checksum_address("0x" + "22" * 20)
WRONG_DEST = Web3.to_checksum_address("0x" + "44" * 20)
PRIME_ACCOUNT = Web3.to_checksum_address("0x" + "33" * 20)
FROM_AMOUNT = 1000
TO_AMOUNT = 990


def build_calldata(
    *,
    executor: str = WHITELISTED_EXECUTOR,
    src: str = SRC,
    dest: str = DEST,
    from_amount: int = FROM_AMOUNT,
    to_amount: int = TO_AMOUNT,
    beneficiary: str | None = None,
    partner_and_fee: int = 0,
) -> bytes:
    """Build a minimal, correctly-shaped swapExactAmountIn `data_bytes` body."""
    if beneficiary is None:
        beneficiary = PRIME_ACCOUNT
    executor_word = bytes(12) + bytes.fromhex(executor[2:])
    middle = abi_encode(
        ["address", "address", "uint256", "uint256", "uint256", "bytes32", "address"],
        [src, dest, from_amount, to_amount, 0, b"\x00" * 32, beneficiary],
    )
    paf_word = partner_and_fee.to_bytes(32, "big")
    return executor_word + middle + paf_word


def check(data_bytes: bytes):
    return dp._paraswap_decode_and_check(
        SELECTOR, data_bytes, SRC, DEST, FROM_AMOUNT, PRIME_ACCOUNT
    )


# ──────────────────────────────────────────────────────────────────────────────
# (a) valid-shaped calldata passes


def test_valid_calldata_passes():
    executor, src, dest, from_amt, to_amt = check(build_calldata())
    assert executor.lower() == WHITELISTED_EXECUTOR.lower()
    assert Web3.to_checksum_address(src) == SRC
    assert Web3.to_checksum_address(dest) == DEST
    assert from_amt == FROM_AMOUNT
    assert to_amt == TO_AMOUNT


# ──────────────────────────────────────────────────────────────────────────────
# (b) non-whitelisted executor — WARNS, does not reject (see module docstring)


def test_non_whitelisted_executor_warns_but_does_not_raise(capsys):
    executor, *_ = check(build_calldata(executor=NON_WHITELISTED_EXECUTOR))
    assert executor.lower() == NON_WHITELISTED_EXECUTOR.lower()
    captured = capsys.readouterr()
    assert "not in the KNOWN whitelist" in captured.out


# ──────────────────────────────────────────────────────────────────────────────
# (c) non-zero partner / fee rejected


def test_nonzero_fee_rejected():
    with pytest.raises(RuntimeError, match="non-zero partner/fee"):
        check(build_calldata(partner_and_fee=5))  # fee_bps = 5


def test_nonzero_partner_rejected():
    partner_and_fee = 0xDEAD << 96  # partner in the high bits, fee_bps == 0
    with pytest.raises(RuntimeError, match="non-zero partner/fee"):
        check(build_calldata(partner_and_fee=partner_and_fee))


# ──────────────────────────────────────────────────────────────────────────────
# (d) wrong destToken rejected


def test_wrong_dest_token_rejected():
    with pytest.raises(RuntimeError, match="src/dest token mismatch"):
        check(build_calldata(dest=WRONG_DEST))


# ──────────────────────────────────────────────────────────────────────────────
# (e) fromAmount mismatch rejected


def test_from_amount_mismatch_rejected():
    with pytest.raises(RuntimeError, match="fromAmount"):
        check(build_calldata(from_amount=FROM_AMOUNT - 1))


# ──────────────────────────────────────────────────────────────────────────────
# Extra guards that are cheap to pin offline


def test_unsupported_selector_rejected():
    with pytest.raises(RuntimeError, match="which the facet does not"):
        dp._paraswap_decode_and_check(
            "0xdeadbeef", build_calldata(), SRC, DEST, FROM_AMOUNT, PRIME_ACCOUNT
        )


def test_short_calldata_rejected():
    with pytest.raises(RuntimeError, match="too short"):
        check(b"\x00" * 100)


def test_beneficiary_other_than_zero_or_account_rejected():
    stranger = Web3.to_checksum_address("0x" + "55" * 20)
    with pytest.raises(RuntimeError, match="beneficiary"):
        check(build_calldata(beneficiary=stranger))


def test_zero_beneficiary_allowed():
    """A zero beneficiary is explicitly allowed (alongside the Prime Account)."""
    zero = Web3.to_checksum_address("0x" + "00" * 20)
    executor, *_ = check(build_calldata(beneficiary=zero))
    assert executor.lower() == WHITELISTED_EXECUTOR.lower()
