"""Offline tests for the 2026-08-12 stale-read / residual-top-up fixes.

Live incident (2026-08-11, parakletos-2 VIRTUAL/ETH rebuild): the mint's final
balance read returned PRE-swap balances (RPC/indexer lag — the same lag visible
on the post-mint tokenId read, retried 8x), so `_aero_fit_amounts_to_range`
under-deployed BOTH legs (~94.5% each) and ~$39 of VIRTUAL+ETH stayed loose.

This locks:
  * `_aero_read_pool_legs_stable` retries until two consecutive reads agree
    (a lagging read disagrees with the next one; two fresh reads agree).
  * `_cmd_aero_add_liquidity_all_available` tops the fresh NFT up via the
    increase path whenever >$5 of a pool leg remains after the mint, with
    reserved symbols held at 100% (post-mint, the remainder of a reserved
    asset IS the protected pile).
  * `_aero_precision_balance`'s stale-read guard: a pass whose read still shows
    the pre-swap balance retries the read and STOPs rather than issuing a
    duplicate swap.

Pure/offline (per conftest): every chain/network/signing dependency is mocked.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import primecli.degenprime as dp

SYNTH_POOL_KEY = "test-tka-usdc"
SYNTH_POOL = {
    "token0": "0x" + "a" * 40,  # TKA (18 dec)
    "token1": "0x" + "b" * 40,  # USDC (6 dec)
    "tickSpacing": 10,
    "symbol0": "TKA", "symbol1": "USDC",
    "decimals0": 18, "decimals1": 6,
    "gauge_alive": True, "slipstreamVersion": 0,
    "pool": "0x" + "c" * 40, "gauge": "0x" + "d" * 40,
}


# ─────────────────────── _aero_read_pool_legs_stable ───────────────────────

def _wire_reads(monkeypatch, queue):
    """Script _aero_in_account_balance from a flat list of (sym0, sym1) pairs."""
    it = iter(queue)

    def _bal(account, symbol):
        return next(it)

    monkeypatch.setattr(dp, "_aero_in_account_balance", _bal)
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_stable_read_returns_first_agreeing_pair(monkeypatch):
    _wire_reads(monkeypatch, [
        10 ** 18, 10 ** 6,   # first read
        10 ** 18, 10 ** 6,   # second read agrees -> stable
    ])
    b0, b1, stable = dp._aero_read_pool_legs_stable(MagicMock(), "TKA", "USDC")
    assert stable is True
    assert b0 == 10 ** 18 and b1 == 10 ** 6


def test_stable_read_retries_through_a_stale_read(monkeypatch):
    # First pair is STALE (pre-swap); the retry returns the fresh balances.
    _wire_reads(monkeypatch, [
        2 * 10 ** 18, 10 ** 6,   # stale (pre-swap)
        10 ** 18, 2 * 10 ** 6,   # fresh
        10 ** 18, 2 * 10 ** 6,   # agrees -> stable
    ])
    b0, b1, stable = dp._aero_read_pool_legs_stable(MagicMock(), "TKA", "USDC")
    assert stable is True
    assert b0 == 10 ** 18 and b1 == 2 * 10 ** 6


def test_stable_read_reports_unstable_after_exhausting_attempts(monkeypatch):
    _wire_reads(monkeypatch, [
        2 * 10 ** 18, 10 ** 6,
        10 ** 18, 2 * 10 ** 6,
        3 * 10 ** 18, 4 * 10 ** 6,
    ])
    b0, b1, stable = dp._aero_read_pool_legs_stable(MagicMock(), "TKA", "USDC")
    assert stable is False
    assert b0 == 3 * 10 ** 18


# ─────────────────── _cmd_aero_add_liquidity_all_available ───────────────────

def _rig_mint(monkeypatch, residual_bals, price_0_in_1: float = 2000.0):
    """Mock everything around the mint; post-mint residual balances come from
    `residual_bals` ({symbol: wei}). Returns a recorder dict with the calls."""
    rec = {"increase": []}
    monkeypatch.setitem(dp.AERODROME_POOLS, SYNTH_POOL_KEY, dict(SYNTH_POOL))
    mock_w3 = MagicMock()

    # slot0 for the POOL contract: tick chosen so price0_in_1 == price_0_in_1.
    # (1.0001 ** tick) * 10 ** (dec0 - dec1) == price_0_in_1
    tick = int(round(math.log(price_0_in_1 / (10 ** (18 - 6))) / math.log(1.0001)))
    pool_c = MagicMock()
    pool_c.functions.slot0.return_value.call.return_value = [2 ** 96, tick, 0, 0, 0, 0, 0]
    acct_c = MagicMock()
    mock_w3.eth.contract.side_effect = [acct_c, pool_c]

    monkeypatch.setattr(dp, "get_w3", lambda: mock_w3)
    monkeypatch.setattr(dp, "get_account", lambda: MagicMock(address="0x" + "1" * 40))
    monkeypatch.setattr(dp, "get_prime_account", lambda w3, addr: "0x" + "2" * 40)
    monkeypatch.setattr(dp.Web3, "to_checksum_address", staticmethod(lambda a: a))
    monkeypatch.setattr(dp, "_aero_use_all_available",
                        lambda *a, **k: (10 ** 18, 10 ** 6, -100, 100, 0))
    monkeypatch.setattr(dp, "_aero_precision_balance",
                        lambda *a, **k: (-100, 100, 0))
    monkeypatch.setattr(dp, "_aero_read_pool_legs_stable",
                        lambda *a, **k: (10 ** 18, 10 ** 6, True))
    monkeypatch.setattr(dp, "_aero_pool_address", lambda cfg: SYNTH_POOL["pool"])
    monkeypatch.setattr(dp, "build_redstone_payload", lambda feeds: b"\x00payload")
    monkeypatch.setattr(dp, "_aero_simulate_mint", lambda *a, **k: (True, None))
    monkeypatch.setattr(dp, "_sign_and_send", lambda *a, **k: {"status": 1})
    monkeypatch.setattr(dp, "_aero_decode_minted_token_id", lambda *a, **k: 999)
    monkeypatch.setattr(dp, "_read_prices_usd",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no feed")))
    monkeypatch.setattr(dp, "_aero_in_account_balance",
                        lambda acct, sym: residual_bals.get(sym, 0))

    def _fake_increase(pool_key, token_id, amount0=None, amount1=None,
                       slippage_pct=1.0, execute=False, reserve=None):
        rec["increase"].append({
            "pool_key": pool_key, "token_id": token_id,
            "amount0": amount0, "amount1": amount1, "execute": execute,
            "reserve": reserve,
        })

    monkeypatch.setattr(dp, "cmd_aero_increase_liquidity", _fake_increase)
    return rec


def test_post_mint_residual_fires_topup_above_dust(monkeypatch):
    rec = _rig_mint(monkeypatch, residual_bals={"TKA": 10 ** 16, "USDC": 0})
    # TKA 0.01 @ $2000 = $20 > $5 -> top up.
    dp._cmd_aero_add_liquidity_all_available(
        SYNTH_POOL_KEY, 1.0, execute=True, width_pct=4.6, reserve=None)
    assert len(rec["increase"]) == 1
    call = rec["increase"][0]
    assert call["token_id"] == 999
    assert abs(call["amount0"] - 0.01) < 1e-9
    assert call["amount1"] is None
    assert call["execute"] is True
    assert call["reserve"] == {}


def test_post_mint_residual_topup_holds_reserved_symbols_at_100pct(monkeypatch):
    rec = _rig_mint(monkeypatch, residual_bals={"TKA": 10 ** 16, "USDC": 0})
    dp._cmd_aero_add_liquidity_all_available(
        SYNTH_POOL_KEY, 1.0, execute=True, width_pct=4.6,
        reserve={"AERO": 0.5})
    call = rec["increase"][0]
    # After the mint, whatever remains of a reserved asset IS the pile: hold 100%.
    assert call["reserve"] == {"AERO": 1.0}


def test_post_mint_no_topup_when_residual_is_dust(monkeypatch):
    rec = _rig_mint(monkeypatch, residual_bals={"TKA": 3, "USDC": 0})
    # 3 wei of TKA -> ~$0.00 -> no top-up.
    dp._cmd_aero_add_liquidity_all_available(
        SYNTH_POOL_KEY, 1.0, execute=True, width_pct=4.6, reserve=None)
    assert rec["increase"] == []


def test_post_mint_no_topup_when_legs_are_unpriced(monkeypatch):
    """Both legs unpriced (no RedStone feed, no stable) -> conservative no-fire:
    the top-up must never guess amounts without a price basis."""
    rec = _rig_mint(monkeypatch, residual_bals={"TKA": 10 ** 16, "USDC": 0})
    monkeypatch.setattr(dp, "_read_prices_usd",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no feed")))
    # Break the stable-leg fallback: pretend the pool's sym1 is NOT a stable.
    _cfg = dict(SYNTH_POOL, symbol1="TKB", token1="0x" + "f" * 40)
    monkeypatch.setitem(dp.AERODROME_POOLS, SYNTH_POOL_KEY, _cfg)
    dp._cmd_aero_add_liquidity_all_available(
        SYNTH_POOL_KEY, 1.0, execute=True, width_pct=4.6, reserve=None)
    assert rec["increase"] == []


# ─────────────────────── _aero_precision_balance stale guard ───────────────────

def _wire_scripted(monkeypatch, queue, swaps):
    """Scripted reads (flat sym0/sym1 pairs) + a 1:1 in-memory swap recorder."""
    it = iter(queue)

    def _bal(account, symbol):
        return next(it)

    def _swap(account, from_sym, to_sym, amount_human, slippage_pct, execute=False):
        swaps.append((from_sym, to_sym, amount_human))
        return True

    monkeypatch.setattr(dp, "_aero_in_account_balance", _bal)
    monkeypatch.setattr(dp, "_swap_with_usdc_fallback", _swap)
    monkeypatch.setattr(dp, "_aero_pool_address", lambda cfg: "0x" + "e" * 40)
    monkeypatch.setattr("time.sleep", lambda s: None)


def _pool_cfg_6():
    return {
        "symbol0": "TKA", "symbol1": "TKB",
        "decimals0": 6, "decimals1": 6, "tickSpacing": 10,
        "token0": "0x" + "a" * 40, "token1": "0x" + "b" * 40,
    }


def test_stale_read_after_swap_retries_and_does_not_double_swap(monkeypatch):
    swaps = []
    # Pass 0: 1000/0 -> sells ~500 TKA. Pass 1 first read is STALE (1000/0, the
    # pre-swap state); the guard retries and gets the fresh 500/500 -> balanced.
    _wire_scripted(monkeypatch, [
        1000 * 10 ** 6, 0,          # pass 0 read
        1000 * 10 ** 6, 0,          # pass 1 read: STALE
        500 * 10 ** 6, 500 * 10 ** 6,  # guard retry: fresh
        500 * 10 ** 6, 500 * 10 ** 6,  # guard retry (unused)
        500 * 10 ** 6, 500 * 10 ** 6,  # guard retry (unused)
    ], swaps)
    w3 = MagicMock()
    w3.eth.contract.return_value.functions.slot0.return_value.call.return_value = [2 ** 96, 0, 0, 0, 0, 0, 0]

    dp._aero_precision_balance(
        w3, MagicMock(), _pool_cfg_6(), -10, 10, 0,
        slippage_pct=1.0, execute=True, width_pct=None, reserve=None,
    )
    # Exactly ONE swap despite the stale read (no duplicate).
    assert len(swaps) == 1
    assert swaps[0][0] == "TKA" and swaps[0][1] == "TKB"


def test_persistently_stale_read_stops_loop(monkeypatch):
    swaps = []
    # Every read returns the pre-swap state; the guard exhausts its retries and
    # stops rather than issuing a second (duplicate) swap.
    _wire_scripted(monkeypatch, [
        1000 * 10 ** 6, 0,          # pass 0 read
        1000 * 10 ** 6, 0,          # pass 1 read: STALE
        1000 * 10 ** 6, 0,          # retry 1
        1000 * 10 ** 6, 0,          # retry 2
        1000 * 10 ** 6, 0,          # retry 3
    ], swaps)
    w3 = MagicMock()
    w3.eth.contract.return_value.functions.slot0.return_value.call.return_value = [2 ** 96, 0, 0, 0, 0, 0, 0]

    dp._aero_precision_balance(
        w3, MagicMock(), _pool_cfg_6(), -10, 10, 0,
        slippage_pct=1.0, execute=True, width_pct=None, reserve=None,
    )
    assert len(swaps) == 1
