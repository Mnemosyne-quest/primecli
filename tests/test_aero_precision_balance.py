"""Offline tests for the shared Aerodrome precision-balancing helper
`_aero_precision_balance` and its two call sites.

Context (live incident 2026-07-20, core1 AERO/cbBTC): a `degenprime aero-rebuild`
left ~$74.56 of loose AERO+cbBTC undeployed, and a manual `aero-increase-liquidity`
sweep-in couldn't fully deploy it either — the increase path only capped the two
pool-token balances to the smaller side, stranding the excess on the larger side,
because it had no pool-token-to-pool-token balancing (only the fresh-mint path did).

This locks:
  * The precision loop, extracted verbatim from the mint path, still converges a
    one-sided balance to the tick-range ratio (increase-mode, fixed band) and still
    recomputes the range each pass in mint-mode (width_pct set).
  * A `reserve` naming one of the pool's OWN legs is held out of both the k0/k1 target
    and the swap cap, so balancing never swaps away a reserved pool token.
  * Both commands wire the helper in correctly: the mint path threads width_pct +
    reserve; the increase path runs it after the non-pool sweep, execute-only, on the
    NFT's fixed band with reserve threaded.

Pure/offline (per conftest): every chain/network/signing dependency is mocked. The
swap dependency is mocked to a pure in-memory 1:1 transfer, so nothing signs or
broadcasts even though the helper is driven with execute=True.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

dp = importlib.import_module("primecli.degenprime")

# sqrtPriceX96 for tick 0 → price 1.0 (2**96). The helper reads slot0()[0] for the
# price and slot0()[1] for the tick; a fixed tick 0 keeps price at 1.0 across passes
# while the mocked swaps move the *balances* toward the range ratio.
SQRT_PRICE_X96_TICK0 = 2 ** 96


def _pool_cfg():
    """Synthetic 6/6-decimal pool so 1:1 swaps are wei-exact (no float round-trip
    loss at ~1e9 wei). Decimals don't affect the k0/k1 ratio math, only the human
    swap size + USD dust gate."""
    return {
        "symbol0": "TKA", "symbol1": "TKB",
        "decimals0": 6, "decimals1": 6, "tickSpacing": 10,
        "token0": "0x" + "a" * 40, "token1": "0x" + "b" * 40,
    }


def _mock_w3(tick=0, sqrt_price=SQRT_PRICE_X96_TICK0):
    w3 = MagicMock()
    (w3.eth.contract.return_value.functions.slot0.return_value
     .call.return_value) = [sqrt_price, tick, 0, 0, 0, 0, 0]
    return w3


def _wire_balances(monkeypatch, bals, dec=6, swaps=None):
    """Point _aero_in_account_balance + _swap_with_usdc_fallback at an in-memory
    balance dict. The swap is a pure 1:1 (price 1.0, equal decimals) transfer that
    mutates the dict, so the helper converges without any real chain call."""
    swaps = [] if swaps is None else swaps

    def _bal(account, symbol):
        return int(bals.get(symbol, 0))

    def _swap(account, from_sym, to_sym, amount_human, slippage_pct, execute=False):
        swaps.append((from_sym, to_sym, amount_human))
        wei = int(round(amount_human * 10 ** dec))
        bals[from_sym] = bals.get(from_sym, 0) - wei
        bals[to_sym] = bals.get(to_sym, 0) + wei
        return True

    monkeypatch.setattr(dp, "_aero_in_account_balance", _bal)
    monkeypatch.setattr(dp, "_swap_with_usdc_fallback", _swap)
    monkeypatch.setattr(dp, "_aero_pool_address", lambda cfg: "0x" + "c" * 40)
    return swaps


# ─────────────────────── helper: convergence (increase-mode) ───────────────────────

def test_increase_mode_converges_one_sided_to_balanced(monkeypatch):
    """width_pct=None (increase-liquidity on an existing NFT): a fully one-sided
    balance (1000 TKA / 0 TKB) converges toward the ~1:1 ratio of a symmetric
    in-range band by selling the over-weighted leg — the exact deploy the old
    cap-to-smaller-side path could not do."""
    bals = {"TKA": 1_000 * 10 ** 6, "TKB": 0}
    swaps = _wire_balances(monkeypatch, bals)
    w3 = _mock_w3(tick=0)

    tl, tu, pt = dp._aero_precision_balance(
        w3, MagicMock(), _pool_cfg(), -10, 10, 0,
        slippage_pct=1.0, execute=True, width_pct=None, reserve=None,
    )

    # Sold the over-weighted leg (TKA) for the empty leg (TKB), within the pass cap.
    assert 1 <= len(swaps) <= 3
    assert swaps[0][0] == "TKA" and swaps[0][1] == "TKB"
    # Converged from one-sided to two-sided (~500/500 at a symmetric band, price 1.0).
    assert bals["TKA"] > 4 * 10 ** 8
    assert bals["TKB"] > 4 * 10 ** 8
    # Fixed band is returned unchanged (increase-mode never recentres).
    assert (tl, tu) == (-10, 10)


def test_no_reserve_is_strict_noop_no_extra_reads(monkeypatch):
    """reserve=None/empty: the reserve snapshot branch is skipped entirely —
    _aero_apply_reserve is never called and no entry-balance reads happen, so the
    mint path stays byte-for-byte with the pre-extraction inline loop."""
    apply_calls = []
    monkeypatch.setattr(dp, "_aero_apply_reserve",
                        lambda v, r: apply_calls.append(r) or v)
    bals = {"TKA": 1_000 * 10 ** 6, "TKB": 0}
    _wire_balances(monkeypatch, bals)
    w3 = _mock_w3(tick=0)

    dp._aero_precision_balance(
        w3, MagicMock(), _pool_cfg(), -10, 10, 0,
        slippage_pct=1.0, execute=True, width_pct=None, reserve=None,
    )
    assert apply_calls == []


# ─────────────────────── helper: pool-leg reserve ───────────────────────

def test_pool_leg_reserve_is_held_out_of_balancing(monkeypatch, capsys):
    """When `reserve` names one of the pool's OWN legs, that fraction of the entry
    balance is off-limits: it is excluded from the k0/k1 target and the swap cap, so
    balancing can never swap it away.

    NOTE: dormant today (a live reserve only ever names a non-pool reward token), so
    this exercises the branch defensively rather than against a real pool config."""
    bals = {"TKA": 1_000 * 10 ** 6, "TKB": 0}
    swaps = _wire_balances(monkeypatch, bals)
    w3 = _mock_w3(tick=0)

    dp._aero_precision_balance(
        w3, MagicMock(), _pool_cfg(), -10, 10, 0,
        slippage_pct=1.0, execute=True, width_pct=None,
        reserve={"TKA": 0.5},  # hold back 50% of the 1000 TKA entry balance
    )

    RESERVED = 500 * 10 ** 6  # 50% of the 1000-TKA entry balance
    # The reserved half is NEVER swapped away — balance stays at or above it every pass.
    assert bals["TKA"] >= RESERVED
    # It still balanced the sweepable (non-reserved) half — TKB got bought.
    assert bals["TKB"] > 0
    # And it only ever spent out of the sweepable half (sold <= 500 TKA).
    assert (1_000 * 10 ** 6 - bals["TKA"]) <= RESERVED
    # The hold is announced.
    out = capsys.readouterr().out
    assert "Reserve: holding" in out and "TKA" in out


def test_pool_leg_reserve_full_hold_blocks_all_balancing(monkeypatch):
    """A 100% reserve on a leg makes the whole leg off-limits; with the other leg
    empty there is nothing sweepable, so balancing does nothing (no swap)."""
    bals = {"TKA": 1_000 * 10 ** 6, "TKB": 0}
    swaps = _wire_balances(monkeypatch, bals)
    w3 = _mock_w3(tick=0)

    dp._aero_precision_balance(
        w3, MagicMock(), _pool_cfg(), -10, 10, 0,
        slippage_pct=1.0, execute=True, width_pct=None, reserve={"TKA": 1.0},
    )
    assert swaps == []
    assert bals == {"TKA": 1_000 * 10 ** 6, "TKB": 0}


# ─────────────────────── helper: mint-mode (width_pct set) ───────────────────────

def test_mint_mode_recomputes_range_each_pass(monkeypatch):
    """width_pct not None (fresh-mint / rebuild path): the band is recomputed from
    width_pct around the freshly-read pool tick each pass via _aero_tick_range, and
    the balance still converges. Proves the extracted mint-mode branch is intact."""
    range_calls = []
    real_tick_range = dp._aero_tick_range

    def spy_tick_range(*a, **k):
        range_calls.append((a, k))
        return real_tick_range(*a, **k)

    monkeypatch.setattr(dp, "_aero_tick_range", spy_tick_range)
    bals = {"TKA": 1_000 * 10 ** 6, "TKB": 0}
    swaps = _wire_balances(monkeypatch, bals)
    w3 = _mock_w3(tick=0)

    dp._aero_precision_balance(
        w3, MagicMock(), _pool_cfg(), 0, 0, 0,
        slippage_pct=1.0, execute=True, width_pct=2.0, reserve=None,
    )
    # Range was recomputed at least once from width_pct (mint-mode only).
    assert range_calls
    assert range_calls[0][0][2] == 2.0  # width_pct positional passed through
    # Still converged.
    assert 1 <= len(swaps) <= 3
    assert bals["TKA"] > 4 * 10 ** 8 and bals["TKB"] > 4 * 10 ** 8


# ─────────────────────── command wiring: increase-liquidity ───────────────────────

def _wire_increase_command(monkeypatch, order, balance_calls):
    """Minimal mock rig for cmd_aero_increase_liquidity: everything past the helper
    call site is stubbed to a clean success so the test can assert the wiring."""
    pool_key = "weth-usdc-100"
    pool_cfg = dp.AERODROME_POOLS[pool_key]
    t0, t1 = pool_cfg["token0"], pool_cfg["token1"]

    monkeypatch.setattr(dp, "get_w3", lambda: _mock_w3(tick=0))
    monkeypatch.setattr(dp, "get_account", lambda: MagicMock(address="0x" + "1" * 40))
    monkeypatch.setattr(dp, "get_prime_account", lambda w3, addr: "0x" + "2" * 40)
    monkeypatch.setattr(dp.Web3, "to_checksum_address", staticmethod(lambda a: a))
    monkeypatch.setattr(dp, "_aero_read_position",
                        lambda w3, tid, pa: (t0, t1, -100, 100, 1))

    def spy_sweep(*a, **k):
        order.append("sweep")

    def spy_balance(*a, **k):
        order.append("balance")
        balance_calls.append((a, k))
        return (a[3], a[4], None)  # ticks unchanged (fixed-band increase mode)

    monkeypatch.setattr(dp, "_aero_rebuild_sweep", spy_sweep)
    monkeypatch.setattr(dp, "_aero_precision_balance", spy_balance)
    monkeypatch.setattr(dp, "_aero_in_account_balance", lambda account, sym: 10 ** 30)
    monkeypatch.setattr(dp, "_aero_cap_to_balance",
                        lambda account, cfg, a0, a1: (a0, a1, []))
    monkeypatch.setattr(dp, "_aero_fit_amounts_to_range",
                        lambda cfg, a0, a1, tl, tu, pt: (a0, a1, []))
    monkeypatch.setattr(dp, "_aero_simulate_call", lambda *a, **k: (True, None))
    monkeypatch.setattr(dp, "build_redstone_payload", lambda feeds: b"")
    monkeypatch.setattr(dp, "_sign_and_send", lambda *a, **k: {"status": 1})
    import eth_abi
    monkeypatch.setattr(eth_abi, "encode", lambda *a, **k: b"")
    return pool_key


def test_increase_liquidity_runs_balancing_after_sweep_execute_only(monkeypatch):
    order, balance_calls = [], []
    pool_key = _wire_increase_command(monkeypatch, order, balance_calls)

    reserve = {"AERO": 0.5}
    dp.cmd_aero_increase_liquidity(
        pool_key, token_id=999, amount0=1, amount1=1,
        slippage_pct=1.0, execute=True, reserve=reserve,
    )

    # Balancing runs AFTER the non-pool sweep.
    assert order == ["sweep", "balance"]
    assert len(balance_calls) == 1
    a, k = balance_calls[0]
    # The EXISTING NFT's fixed band (tick_lower=-100, tick_upper=100), not a fresh one.
    assert a[3] == -100 and a[4] == 100
    assert k["width_pct"] is None          # fixed-band increase mode
    assert k["reserve"] == reserve         # reserve threaded through


def test_increase_liquidity_skips_balancing_in_preview(monkeypatch):
    order, balance_calls = [], []
    pool_key = _wire_increase_command(monkeypatch, order, balance_calls)

    dp.cmd_aero_increase_liquidity(
        pool_key, token_id=999, amount0=1, amount1=1,
        slippage_pct=1.0, execute=False, reserve=None,
    )
    # Preview must not swap pool tokens — balancing is execute-only, like the mint.
    assert balance_calls == []
    assert "balance" not in order


# ─────────────────────── command wiring: add-liquidity (mint path) ───────────────────────

def test_add_liquidity_all_available_delegates_with_width_and_reserve(monkeypatch):
    """The extraction did not change the mint path: it still delegates the precision
    balancing to _aero_precision_balance, threading width_pct (so the band tracks the
    moving tick) and reserve."""
    pool_key = "weth-usdc-100"
    balance_calls = []

    monkeypatch.setattr(dp, "get_w3", lambda: _mock_w3(tick=0))
    monkeypatch.setattr(dp, "get_account", lambda: MagicMock(address="0x" + "1" * 40))
    monkeypatch.setattr(dp, "get_prime_account", lambda w3, addr: "0x" + "2" * 40)
    monkeypatch.setattr(dp.Web3, "to_checksum_address", staticmethod(lambda a: a))
    monkeypatch.setattr(dp, "_aero_use_all_available",
                        lambda *a, **k: (10 ** 18, 10 ** 6, -100, 100, 0))
    monkeypatch.setattr(dp, "_aero_fit_amounts_to_range",
                        lambda cfg, a0, a1, tl, tu, pt: (a0, a1, []))
    monkeypatch.setattr(dp, "_aero_in_account_balance", lambda account, sym: 10 ** 18)

    def spy_balance(*a, **k):
        balance_calls.append((a, k))
        return (a[3], a[4], a[5])  # ticks/tick unchanged

    monkeypatch.setattr(dp, "_aero_precision_balance", spy_balance)
    monkeypatch.setattr(dp, "_aero_mint_params", lambda *a, **k: tuple(range(14)))
    monkeypatch.setattr(dp, "build_redstone_payload", lambda feeds: b"")
    monkeypatch.setattr(dp, "_aero_simulate_mint", lambda *a, **k: (True, 123))
    monkeypatch.setattr(dp, "_sign_and_send", lambda *a, **k: {"status": 1})
    monkeypatch.setattr(dp, "_aero_decode_minted_token_id", lambda receipt: 123)
    import eth_abi
    monkeypatch.setattr(eth_abi, "encode", lambda *a, **k: b"")

    dp._cmd_aero_add_liquidity_all_available(
        pool_key, slippage_pct=1.0, execute=True, width_pct=7.5,
        reserve={"WETH": 0.3},
    )

    assert len(balance_calls) == 1
    _, k = balance_calls[0]
    assert k["width_pct"] == 7.5
    assert k["reserve"] == {"WETH": 0.3}
