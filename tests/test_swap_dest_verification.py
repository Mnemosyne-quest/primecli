"""Offline tests for cmd_swap's C1 dest-leg verification (2026-09-07 core1).

A ParaSwap route can land status-1 while delivering a DIFFERENT asset than
requested (the AERO->cbBTC route settled as USDC — the wrong-dest delivery that
started the core1 deadlock). The receipt proves the tx landed, not WHAT landed:
cmd_swap must re-read the in-account dest balance and require an increase vs
the pre-broadcast read, failing loudly (return False) on a dest that never
arrives. Everything is mocked — nothing signs or broadcasts.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

dp = importlib.import_module("primecli.degenprime")

PA = "0x" + "11" * 20
EOA = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20


def _route():
    return {
        "price_route": {"contractMethod": "swapExactTokensForTokens",
                        "srcUSD": "100", "destUSD": "99"},
        "tx_built": {"to": "0x" + "44" * 20},
        "full": b"\x12" * 4,
        "selector_hex": "0x12345678",
        "data_bytes": b"\x12" * 68,
        "quoted_out": 99_000_000,
        "min_out": 98_000_000,
        "sim_ok": True,
        "slippage_pct_used": 1.0,
    }


def _wire(monkeypatch, balances, w3):
    monkeypatch.setattr(dp, "get_w3", lambda: w3)
    monkeypatch.setattr(dp, "get_account", lambda: SimpleNamespace(address=EOA))
    monkeypatch.setattr(dp, "get_prime_account", lambda _w3, _a: PA)
    monkeypatch.setattr(dp, "_swap_asset_meta",
                        lambda _w3, s: {"symbol": s, "token": TOKEN, "decimals": 6})
    monkeypatch.setattr(dp, "_paraswap_swap_with_escalation", lambda *_a, **_k: _route())
    monkeypatch.setattr(dp, "build_redstone_payload", lambda _f: b"")

    def bal(account, sym):
        return balances.get(sym, 0)

    monkeypatch.setattr(dp, "_aero_in_account_balance", bal)
    # cmd_swap's pre-broadcast dest read now uses the STRICT reader (returns
    # None when the view is unreadable so the swap fails closed) — patch it
    # with the same balance table.
    monkeypatch.setattr(dp, "_aero_in_account_balance_strict", bal)


def _mock_w3():
    w3 = MagicMock()
    # The in-account balance pre-check reads account.getBalance — return a big
    # number so the swap request is always "in balance".
    (w3.eth.contract.return_value.functions.getBalance.return_value
     .call.return_value) = 1_000_000_000
    return w3


def test_swap_dest_arrived_returns_true(monkeypatch):
    balances = {"USDC": 500_000_000, "ETH": 0}
    w3 = _mock_w3()
    _wire(monkeypatch, balances, w3)

    def sign(*_a, **_k):
        balances["ETH"] = 99_000_000
        return {"status": 1, "gasUsed": 100000}

    monkeypatch.setattr(dp, "_sign_and_send", sign)
    monkeypatch.setattr(dp.time, "sleep", lambda *_a, **_k: None)
    ok = dp.cmd_swap("USDC", "ETH", 100.0, 1.0, execute=True)
    assert ok is True


def test_swap_wrong_delivery_returns_false(monkeypatch):
    # The tx lands status-1 but the dest balance never increases (the route
    # delivered a different asset / settled elsewhere) — cmd_swap must FAIL,
    # never report OK to the caller.
    balances = {"USDC": 500_000_000, "ETH": 0}
    w3 = _mock_w3()
    _wire(monkeypatch, balances, w3)
    monkeypatch.setattr(dp, "_sign_and_send",
                        lambda *_a, **_k: {"status": 1, "gasUsed": 100000})
    monkeypatch.setattr(dp.time, "sleep", lambda *_a, **_k: None)
    ok = dp.cmd_swap("USDC", "ETH", 100.0, 1.0, execute=True)
    assert ok is False


def test_swap_preview_not_verified(monkeypatch):
    # Preview (execute=False) returns before any broadcast — the dest check is
    # broadcast-only and must not alter preview semantics (returns None).
    balances = {"USDC": 500_000_000, "ETH": 0}
    w3 = _mock_w3()
    _wire(monkeypatch, balances, w3)
    ok = dp.cmd_swap("USDC", "ETH", 100.0, 1.0, execute=False)
    assert ok is None


def test_swap_fail_closed_on_unreadable_pre_read(monkeypatch):
    # The strict pre-read returns None when the in-account view is unreadable
    # (flaky proxy / indexer lag — the exact conditions of the core1 wrong-
    # delivery). The swap must fail CLOSED: no broadcast, return False.
    # A lenient pre-read that defaulted to 0 would degrade the whole check to
    # "dest balance > 0" and pass on any pre-existing dest balance.
    balances = {"USDC": 500_000_000, "ETH": 0}
    w3 = _mock_w3()
    _wire(monkeypatch, balances, w3)
    monkeypatch.setattr(dp, "_aero_in_account_balance_strict",
                        lambda _a, _s: None)
    sent = []
    monkeypatch.setattr(dp, "_sign_and_send",
                        lambda *a, **k: sent.append(1) or {"status": 1})
    ok = dp.cmd_swap("USDC", "ETH", 100.0, 1.0, execute=True)
    assert ok is False
    assert sent == [], "a fail-closed pre-read must never broadcast"
