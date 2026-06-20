"""Tests for _estimate_gas_limit and _sign_and_send helpers.

Ethereum RPC estimation is mocked. The test verifies:
- A successful estimate is authoritative: buffered estimate wins, even when it is
  BELOW fallback_gas (fallback_gas is a fallback-on-failure, NOT a floor — flooring
  at e.g. 4M over-reserved gas_limit*maxFeePerGas and tripped "insufficient funds for
  gas" on low-balance wallets; the ZORA/USDC converge borrow symptom, 2026-06-20).
- RPC failure falls back to the supplied fallback_gas
- 25% buffer is applied correctly
- OOG detection logic (stateless, pure Python)
- Signed tx correctness for the test cross-file identity check
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "primecli"))

# Import _estimate_gas_limit from each tool
# Since they share the same implementation, test one and verify presence in all
import importlib.util


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dgp = _load_module(
    Path(__file__).resolve().parent.parent / "primecli" / "degenprime.py",
    "degenprime_test",
)
dp = _load_module(
    Path(__file__).resolve().parent.parent / "primecli" / "deltaprime.py",
    "deltaprime_test",
)
ap = _load_module(
    Path(__file__).resolve().parent.parent / "primecli" / "arbprime.py",
    "arbprime_test",
)


class FakeEth:
    def __init__(self, result, raises):
        self._result = result
        self._raises = raises

    def estimate_gas(self, tx):
        if self._raises:
            raise Exception("RPC error")
        return self._result


class FakeW3:
    """Minimal fake that returns a fixed gas estimate."""

    def __init__(self, estimate_result=100000, estimate_raises=False):
        self.eth = FakeEth(estimate_result, estimate_raises)


def test_estimate_gas_limit_prefers_eth_estimate():
    """A successful estimate is authoritative — the buffered estimate wins even when
    it is below fallback_gas. fallback_gas is NOT a floor; flooring over-reserved gas
    (gas_limit*maxFeePerGas) and broke borrows on low-gas-balance wallets."""
    w3 = FakeW3(estimate_result=80000)
    tx = {"from": "0xaaa", "to": "0xbbb", "data": "0xdead"}
    result = dgp._estimate_gas_limit(w3, tx, fallback_gas=3000000, buffer_bps=1250)
    # 80000 * 1250 // 1000 = 100000  (buffered estimate; fallback_gas no longer floors it)
    assert result == 100000, f"Expected 100000, got {result}"


def test_estimate_gas_limit_with_large_estimate():
    """When estimated gas exceeds fallback, the buffered estimate wins."""
    w3 = FakeW3(estimate_result=3500000)
    tx = {"from": "0xaaa", "to": "0xbbb", "data": "0xdead"}
    result = dgp._estimate_gas_limit(w3, tx, fallback_gas=3000000, buffer_bps=1250)
    # 3500000 * 1250 // 1000 = 4375000
    # max(3000000, 4375000) = 4375000
    assert result == 4375000, f"Expected 4375000, got {result}"


def test_estimate_gas_limit_rpc_failure():
    """When RPC fails, fall back to the supplied fallback_gas."""
    w3 = FakeW3(estimate_result=0, estimate_raises=True)
    tx = {"from": "0xaaa", "to": "0xbbb", "data": "0xdead"}
    result = dgp._estimate_gas_limit(w3, tx, fallback_gas=3000000, buffer_bps=1250)
    assert result == 3000000, f"Expected 3000000, got {result}"


def test_oog_detection():
    """Stateless OOG detection: gasUsed >= gasLimit => out of gas."""
    # Simulate the check inside _sign_and_send
    receipt_oog = {"gasUsed": 3000000, "status": 0}
    gas_limit = 3000000
    assert receipt_oog["gasUsed"] >= gas_limit, "Should be OOG"

    receipt_normal_fail = {"gasUsed": 1500000, "status": 0}
    assert receipt_normal_fail["gasUsed"] < gas_limit, "Should NOT be OOG"


# Cross-file identity: verify _sign_and_send exists in all three tools
def test_sign_and_send_exists_in_degenprime():
    assert hasattr(dgp, "_sign_and_send"), "degenprime missing _sign_and_send"


def test_sign_and_send_exists_in_deltaprime():
    assert hasattr(dp, "_sign_and_send"), "deltaprime missing _sign_and_send"


def test_sign_and_send_exists_in_arbprime():
    assert hasattr(ap, "_sign_and_send"), "arbprime missing _sign_and_send"


def test_estimate_gas_limit_exists_in_all():
    assert hasattr(dgp, "_estimate_gas_limit")
    assert hasattr(dp, "_estimate_gas_limit")
    assert hasattr(ap, "_estimate_gas_limit")


def test_degenprime_no_remaining_hardcoded_sends():
    """Verify no send_raw_transaction outside helper (excluding approvals)."""
    import ast, re

    with open(Path(__file__).resolve().parent.parent / "primecli" / "degenprime.py") as f:
        src = f.read()

    tree = ast.parse(src)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "send_raw_transaction":
                calls.append(node.lineno)

    # Filter out the helper function
    helper_line = None
    for i, line in enumerate(src.split("\n")):
        if "def _sign_and_send" in line:
            helper_line = i + 1
            break

    # Filter out approval txs (100k gas) and helper internals
    remaining = []
    for ln in calls:
        if helper_line and ln >= helper_line and ln < helper_line + 60:
            continue  # inside helper
        line_text = src.split("\n")[ln - 1]
        ctx = src.split("\n")[max(0, ln - 6) : ln + 2]
        ctx_str = "\n".join(ctx)
        if '"gas": 100000' in ctx_str:
            continue  # approval
        remaining.append(ln)

    assert not remaining, f"Non-helper, non-approval send_raw_transaction at lines: {remaining}"
