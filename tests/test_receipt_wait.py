"""Unit tests for _wait_for_receipt_stable (v0.14.3, 2026-08-12).

The helper retries TRANSIENT provider errors on the receipt WAIT (the tx is
already broadcast; re-waiting is idempotent). A revert is a status-0 receipt,
NOT an exception — it must pass through on the first wait. A provider that
stays broken must propagate the last error after the bounded retries.
"""

from primecli.deltaprime import _wait_for_receipt_stable
from primecli.degenprime import _wait_for_receipt_stable as _dg


class _RaisingW3:
    """Fake w3 whose receipt wait raises N times, then returns a receipt."""

    def __init__(self, raises: int, receipt=None):
        self.raises = raises
        self.calls = 0
        self.receipt = receipt or {"status": 1}
        self.eth = _EthStub(self)

    def _wait(self, tx_hash, timeout=180):
        self.calls += 1
        if self.calls <= self.raises:
            raise ConnectionError("transient reset")
        return self.receipt


class _EthStub:
    def __init__(self, owner):
        self.owner = owner

    def wait_for_transaction_receipt(self, tx_hash, timeout=180):
        return self.owner._wait(tx_hash, timeout=timeout)


def test_returns_immediately_when_first_wait_succeeds():
    w3 = _RaisingW3(raises=0, receipt={"status": 1})
    rc = _wait_for_receipt_stable(w3, "0xabc", 180, "t", attempts=3)
    assert rc == {"status": 1}
    assert w3.calls == 1


def test_retries_transient_errors_then_succeeds():
    w3 = _RaisingW3(raises=2, receipt={"status": 1})
    rc = _wait_for_receipt_stable(w3, "0xabc", 180, "t", attempts=3)
    assert rc == {"status": 1}
    assert w3.calls == 3  # two failures + one success


def test_status_zero_revert_is_not_retried():
    # A landed-but-reverted tx returns a status-0 receipt — NOT an exception.
    w3 = _RaisingW3(raises=0, receipt={"status": 0})
    rc = _wait_for_receipt_stable(w3, "0xabc", 180, "t", attempts=3)
    assert rc == {"status": 0}
    assert w3.calls == 1


def test_persistent_failure_propagates_last_error():
    w3 = _RaisingW3(raises=99)
    try:
        _wait_for_receipt_stable(w3, "0xabc", 180, "t", attempts=3)
        assert False, "expected the last error to propagate"
    except ConnectionError:
        pass
    assert w3.calls == 3  # bounded retries, no infinite loop


def test_degenprime_copy_behaves_identically():
    w3 = _RaisingW3(raises=1, receipt={"status": 1})
    rc = _dg(w3, "0xabc", 180, "t", attempts=2)
    assert rc == {"status": 1}
    assert w3.calls == 2
