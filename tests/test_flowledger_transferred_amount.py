"""Regression guard for _flowledger.transferred_amount — the receipt-truth parser
behind the fund/withdraw flow logging fix (2026-06-23).

Background: `fund(asset, amount)` can pull LESS than `amount` when the EOA is short
(a leverage-open funds mostly from borrow, so the wallet only holds dust). The old
logger recorded the requested `amount` as contribution, inflating the PnL basis and
corrupting every downstream since-open PnL / ROI / effective-APR. The fix logs the
ACTUAL on-chain Transfer(EOA -> account) amount instead. These tests pin that parser.
"""

from __future__ import annotations

from primecli import _flowledger as fl

# Real lowercase addresses from the incident (Base).
TOKEN = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # USDC (6 decimals)
EOA = "0x0218f5b006fd43181018f584ed4be13c356b3428"
ACCT = "0x150619b111e21f0eac2232ff63f5f0027a47d331"
OTHER = "0x00000000000000000000000000000000deadbeef"

TRANSFER = fl._ERC20_TRANSFER_TOPIC


def _topic_addr(addr: str) -> str:
    """32-byte left-padded topic encoding of an address."""
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def _transfer_log(token: str, frm: str, to: str, raw: int) -> dict:
    return {
        "address": token,
        "topics": [TRANSFER, _topic_addr(frm), _topic_addr(to)],
        "data": "0x" + format(raw, "064x"),
    }


def _receipt(logs: list[dict]) -> dict:
    return {"logs": logs}


def test_real_transfer_returns_actual_amount():
    # 199.9 USDC genuinely funded -> parser returns 199.9, not whatever was requested.
    rcpt = _receipt([_transfer_log(TOKEN, EOA, ACCT, 199_900_000)])
    assert fl.transferred_amount(rcpt, TOKEN, EOA, ACCT, 6) == 199.9


def test_partial_pull_returns_dust_not_requested():
    # The ZORA phantom: fund(132 USDC) but only 0.08425 actually moved.
    rcpt = _receipt([_transfer_log(TOKEN, EOA, ACCT, 84_250)])
    assert fl.transferred_amount(rcpt, TOKEN, EOA, ACCT, 6) == 84_250 / 1e6


def test_zero_value_transfer_returns_zero_not_none():
    # A 0-value Transfer is still a Transfer: the fund pulled nothing -> 0.0, not None.
    rcpt = _receipt([_transfer_log(TOKEN, EOA, ACCT, 0)])
    assert fl.transferred_amount(rcpt, TOKEN, EOA, ACCT, 6) == 0.0


def test_no_matching_transfer_returns_none():
    # No Transfer to the account at all -> None, so the caller falls back to the request.
    rcpt = _receipt([_transfer_log(TOKEN, EOA, OTHER, 100_000_000)])
    assert fl.transferred_amount(rcpt, TOKEN, EOA, ACCT, 6) is None


def test_wrong_token_ignored():
    other_token = "0x4200000000000000000000000000000000000006"  # WETH
    rcpt = _receipt([_transfer_log(other_token, EOA, ACCT, 5_000_000)])
    assert fl.transferred_amount(rcpt, TOKEN, EOA, ACCT, 6) is None


def test_sums_multiple_matching_transfers():
    rcpt = _receipt([
        _transfer_log(TOKEN, EOA, ACCT, 10_000_000),
        _transfer_log(TOKEN, EOA, ACCT, 25_000_000),
    ])
    assert fl.transferred_amount(rcpt, TOKEN, EOA, ACCT, 6) == 35.0


def test_direction_matters_for_withdraw():
    # Withdraw is account -> EOA. A fund-direction parse must not pick it up.
    rcpt = _receipt([_transfer_log(TOKEN, ACCT, EOA, 50_000_000)])
    assert fl.transferred_amount(rcpt, TOKEN, EOA, ACCT, 6) is None
    assert fl.transferred_amount(rcpt, TOKEN, ACCT, EOA, 6) == 50.0


def test_checksum_and_hexbytes_inputs_normalise():
    class _HB(bytes):
        def hex(self):
            return super().hex()

    raw_topics = [
        _HB(bytes.fromhex(TRANSFER.removeprefix("0x"))),
        _HB(bytes.fromhex(_topic_addr(EOA).removeprefix("0x"))),
        _HB(bytes.fromhex(_topic_addr(ACCT).removeprefix("0x"))),
    ]
    rcpt = {"logs": [{
        "address": _HB(bytes.fromhex(TOKEN.removeprefix("0x"))),
        "topics": raw_topics,
        "data": _HB((75_000_000).to_bytes(32, "big")),
    }]}
    # Pass checksum-style mixed-case addresses to confirm normalisation.
    assert fl.transferred_amount(rcpt, TOKEN.upper(), EOA, ACCT, 6) == 75.0
