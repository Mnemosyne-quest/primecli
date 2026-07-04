"""Tests for _aero_decode_minted_token_id: reads the AUTHORITATIVE minted tokenId
from a real mint transaction's receipt logs, instead of trusting
_aero_simulate_mint's pre-broadcast guess (which can be stale by the time the
real tx lands, since Aerodrome's tokenId counter is shared across every user of
the protocol, not just this account -- confirmed 2026-07-03, off by +1 twice in
one session).

All offline: builds synthetic receipts shaped like the real web3.py structure
(AttributeDict-like, HexBytes-like topics) rather than making live RPC calls.
"""

from __future__ import annotations

import importlib

dp = importlib.import_module("primecli.degenprime")

NPM_V3 = dp.AERODROME_NPM_V3
NPM_V2 = dp.AERODROME_NPM_V2
TRANSFER_TOPIC = "0x" + dp.Web3.keccak(text="Transfer(address,address,uint256)").hex().removeprefix("0x")
ZERO_ADDR_TOPIC = "0x" + "0" * 64
DEGEN_ACCOUNT = "0x3A04E3B0B5Ab5d21c5a0E4De8953654591F53Cb6"
GAUGE = "0x" + "1" * 40


class _HexBytesLike:
    """Mimics web3.py's HexBytes: .hex() returns the value WITHOUT a '0x' prefix
    in some web3.py versions -- the exact quirk that caused the original bug."""

    def __init__(self, value_no_prefix: str):
        self._value = value_no_prefix

    def hex(self) -> str:
        return self._value


def _topic(addr_or_hex: str) -> _HexBytesLike:
    """Build a topic HexBytes-like value, padded to 32 bytes, no '0x' prefix."""
    h = addr_or_hex.removeprefix("0x").lower()
    return _HexBytesLike(h.zfill(64))


def _mint_log(token_id: int, to_addr: str = DEGEN_ACCOUNT, address: str = NPM_V3) -> dict:
    return {
        "address": dp.Web3.to_checksum_address(address),
        "topics": [
            _topic(TRANSFER_TOPIC),
            _topic("0x" + "0" * 40),  # from = zero address (mint)
            _topic(to_addr),
            _topic(hex(token_id)),
        ],
    }


def _stake_log(token_id: int, from_addr: str = DEGEN_ACCOUNT, to_addr: str = GAUGE) -> dict:
    return {
        "address": dp.Web3.to_checksum_address(NPM_V3),
        "topics": [
            _topic(TRANSFER_TOPIC),
            _topic(from_addr),
            _topic(to_addr),
            _topic(hex(token_id)),
        ],
    }


def test_decodes_mint_then_stake_receipt():
    """Real receipts contain the mint Transfer(0x0->account) followed by a stake
    Transfer(account->gauge) for the SAME tokenId -- must pick the mint one."""
    receipt = {"logs": [_mint_log(2037736), _stake_log(2037736)]}
    assert dp._aero_decode_minted_token_id(receipt) == 2037736


def test_decodes_v2_pool_mint():
    """A V2 (legacy, non-Slipstream) pool mint emits its Transfer from
    AERODROME_NPM_V2, a different contract than V3. Checking only V3 silently
    missed every V2 mint (confirmed live 2026-07-04, AERO/cbBTC rebuild) --
    always fell through to 'could not decode tokenId from receipt logs'."""
    receipt = {"logs": [_mint_log(72732676, address=NPM_V2)]}
    assert dp._aero_decode_minted_token_id(receipt) == 72732676


def test_ignores_unrelated_transfers_on_other_contracts():
    """A Transfer log from a different contract (e.g. the WETH/EURC token
    transfers that happen in the same tx) must not be mistaken for the mint."""
    other_token = "0x4200000000000000000000000000000000000006"
    receipt = {
        "logs": [
            _mint_log(2040290, address=other_token),  # wrong contract, same shape
            _mint_log(2040290),  # the real one
        ],
    }
    assert dp._aero_decode_minted_token_id(receipt) == 2040290


def test_ignores_non_mint_transfers_on_the_npm():
    """A staking Transfer (account->gauge, NOT from the zero address) on the NPM
    itself must not be mistaken for a mint."""
    receipt = {"logs": [_stake_log(999999)]}
    assert dp._aero_decode_minted_token_id(receipt) is None


def test_returns_none_on_no_logs():
    assert dp._aero_decode_minted_token_id({"logs": []}) is None


def test_returns_none_on_missing_logs_key():
    assert dp._aero_decode_minted_token_id({}) is None


def test_handles_plain_string_topics_with_0x_prefix():
    """Some receipt sources (or a plain dict built by hand) may already carry
    '0x'-prefixed plain strings instead of HexBytes -- normalization must accept
    either form."""
    receipt = {
        "logs": [
            {
                "address": dp.Web3.to_checksum_address(NPM_V3),
                "topics": [
                    TRANSFER_TOPIC,
                    ZERO_ADDR_TOPIC,
                    "0x" + DEGEN_ACCOUNT[2:].lower().zfill(64),
                    "0x" + hex(2037736)[2:].zfill(64),
                ],
            }
        ]
    }
    assert dp._aero_decode_minted_token_id(receipt) == 2037736
