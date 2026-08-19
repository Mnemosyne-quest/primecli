"""Offline tests for the send/move additions and broadcast retry hardening.

Covers:
  * `_send_raw_with_nonce_retry` bump-and-replace on "replacement transaction
    underpriced" (2026-08-19: deposit/fund loops on the local RPC proxy) —
    SAME nonce, fees raised ~50%, bounded retries.
  * the legacy "nonce too low" stale-read path still refetches the nonce.
  * `_resolve_asset` pool-name / symbol resolution for `send` / `move`.

No RPC is made: a stub w3 fails N times then accepts.
"""

from __future__ import annotations

import pytest

from eth_account import Account

from primecli import degenprime as d


class _FlakyEth:
    def __init__(self, w3, error_text):
        self.w3 = w3
        self.error_text = error_text
        self.nonce = 7

    def send_raw_transaction(self, raw):
        self.w3.calls += 1
        if self.w3.calls <= self.w3.failures:
            raise Exception(self.error_text)
        return b"\xab" * 32

    def get_transaction_count(self, addr):
        return self.nonce

    @property
    def gas_price(self):
        return 10 ** 9


class _FlakyW3:
    def __init__(self, failures, error_text):
        self.failures = failures
        self.calls = 0
        self.eth = _FlakyEth(self, error_text)


@pytest.fixture
def acct():
    return Account.create()


@pytest.fixture
def eip1559_tx(acct):
    return {
        "from": acct.address,
        "to": "0x" + "22" * 20,
        "nonce": 5,
        "gas": 21000,
        "maxFeePerGas": 1_000_000_000,
        "maxPriorityFeePerGas": 100_000_000,
        "chainId": 8453,
        "value": 12345,
    }


def test_underpriced_bumps_fees_and_keeps_nonce(acct, eip1559_tx, monkeypatch):
    """The stale-pending-tx rejection must bump fees on the SAME nonce (replacement)."""
    monkeypatch.setattr("primecli.degenprime.time.sleep", lambda s: None)
    w3 = _FlakyW3(failures=1, error_text="replacement transaction underpriced")
    h = d._send_raw_with_nonce_retry(w3, acct, eip1559_tx, "test")
    assert h == b"\xab" * 32
    assert eip1559_tx["nonce"] == 5, "replacement must keep the same nonce"
    assert eip1559_tx["maxFeePerGas"] == 1_500_000_000, "maxFeePerGas must bump 1.5x"
    assert eip1559_tx["maxPriorityFeePerGas"] >= 100_000_000
    assert w3.calls == 2


def test_underpriced_exhausts_attempts_then_raises(acct, eip1559_tx, monkeypatch):
    monkeypatch.setattr("primecli.degenprime.time.sleep", lambda s: None)
    w3 = _FlakyW3(failures=99, error_text="replacement transaction underpriced")
    with pytest.raises(Exception, match="underpriced"):
        d._send_raw_with_nonce_retry(w3, acct, eip1559_tx, "test", max_attempts=2)
    assert w3.calls == 2, "must not retry past max_attempts"


def test_nonce_too_low_refetches_nonce(acct, eip1559_tx, monkeypatch):
    """The stale-nonce race must refetch the nonce and retry (existing behaviour)."""
    monkeypatch.setattr("primecli.degenprime.time.sleep", lambda s: None)
    w3 = _FlakyW3(failures=1, error_text="nonce too low: next nonce 6, tx nonce 5")
    h = d._send_raw_with_nonce_retry(w3, acct, eip1559_tx, "test")
    assert h == b"\xab" * 32
    assert eip1559_tx["nonce"] == 7, "must refetch the nonce for the retry"
    assert eip1559_tx["maxFeePerGas"] == 1_000_000_000, "nonce path must not bump fees"


def test_unrelated_error_propagates(acct, eip1559_tx, monkeypatch):
    monkeypatch.setattr("primecli.degenprime.time.sleep", lambda s: None)
    w3 = _FlakyW3(failures=1, error_text="insufficient funds for gas * price")
    with pytest.raises(Exception, match="insufficient funds"):
        d._send_raw_with_nonce_retry(w3, acct, eip1559_tx, "test")


def test_legacy_gasprice_bump(acct, monkeypatch):
    """Legacy (gasPrice) txs must bump gasPrice, not the EIP-1559 fields."""
    monkeypatch.setattr("primecli.degenprime.time.sleep", lambda s: None)
    w3 = _FlakyW3(failures=1, error_text="replacement transaction underpriced")
    tx = {
        "from": acct.address,
        "to": "0x" + "22" * 20,
        "nonce": 5,
        "gas": 21000,
        "gasPrice": 2_000_000_000,
        "chainId": 8453,
        "value": 1,
    }
    h = d._send_raw_with_nonce_retry(w3, acct, tx, "test")
    assert h == b"\xab" * 32
    assert tx["gasPrice"] == 3_000_000_000
    assert "maxFeePerGas" not in tx


def test_resolve_asset_pool_and_symbol():
    assert d._resolve_asset("usdc")[0] == "usdc"
    assert d._resolve_asset("USDC")[0] == "usdc"
    assert d._resolve_asset("weth")[0] == "weth"
    assert d._resolve_asset("eth")[0] == "weth", "native ETH resolves via the weth pool"
    assert d._resolve_asset("ETH")[0] == "weth"
    assert d._resolve_asset("aero")[0] == "aero"
    assert d._resolve_asset("AERO")[0] == "aero"
    assert d._resolve_asset("cbBTC")[0] == "cbbtc"


def test_resolve_asset_unknown_raises():
    with pytest.raises(RuntimeError, match="Unknown asset"):
        d._resolve_asset("pepe")
