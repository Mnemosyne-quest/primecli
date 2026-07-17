"""Regression test for cmd_aero_rebuild's step ordering.

Pure/offline (per conftest): mocks every network/chain dependency and asserts
call order only.

Bug (found live 2026-07-04, core1 aero-cbbtc-200 rebuild): the pool lookup
(_aero_read_position, used to resolve pool_key for the sweep + re-mint steps)
ran AFTER cmd_aero_remove_liquidity in --execute mode. Removal fully burns the
NFT (unstake + remove + collect + burn), so the post-removal read returned None
and the function aborted with "Could not read position #N" — after the funds
were already unwound, stranding them loose/undeployed in the account until
manually swept and re-minted. Fixed by resolving the pool BEFORE removing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import primecli.degenprime as d


def _rig(monkeypatch, order_created_on: int = 0):
    """Common mock rig for cmd_aero_rebuild. order_created_on > 0 simulates an
    active rebalance order on the token being rebuilt (createdOn is field index 7
    of getRebalanceOrder's returned tuple, per cmd_aero_rebalance_status)."""
    calls = []

    # weth-usdc-100 is a V2 (non-Slipstream) pool, so this can't hit the
    # 14s V3 gauge anti-sniping sleep in Step 1->3.
    pool_key = "weth-usdc-100"
    pool_cfg = d.AERODROME_POOLS[pool_key]
    token0, token1 = pool_cfg["token0"], pool_cfg["token1"]

    order_tuple = [0] * 8
    order_tuple[7] = order_created_on
    mock_w3 = MagicMock()
    mock_w3.eth.contract.return_value.functions.getRebalanceOrder.return_value.call.return_value = tuple(order_tuple)

    monkeypatch.setattr(d, "get_w3", lambda: mock_w3)
    monkeypatch.setattr(d, "get_account", lambda: MagicMock(address="0x" + "1" * 40))
    monkeypatch.setattr(d, "get_prime_account", lambda w3, addr: "0x" + "2" * 40)
    monkeypatch.setattr(d.Web3, "to_checksum_address", staticmethod(lambda a: a))

    def fake_read_position(w3, token_id, pa):
        calls.append("read_position")
        return (token0, token1, None, None, None)

    # cmd_aero_rebuild resolves pool_key via _aero_match_pool_cfg(token0, token1,
    # tickSpacing, version) now (version-aware, since V2/V3 entries can share a pair)
    # -- stub the version/tickSpacing lookup this pulls from, same as fake_read_position
    # stubs the token0/token1 lookup. Raw tuple mirrors NPM.positions(): tickSpacing is
    # field index 4.
    raw_pos = (0, "0x" + "0" * 40, token0, token1, pool_cfg["tickSpacing"], 0, 0, 0)

    def fake_npm_for_token(w3, token_id, pa=None):
        calls.append("npm_for_token")
        return (MagicMock(), "v2", raw_pos)

    def fake_remove_liquidity(token_ids, percentage, execute=False):
        calls.append("remove_liquidity")

    def fake_sweep(w3, acct, account, pa_cs, pk, pc, execute=False):
        calls.append("sweep")

    def fake_add_liquidity_all(pk, slippage_pct, execute, width_pct):
        calls.append("add_liquidity")

    monkeypatch.setattr(d, "_aero_read_position", fake_read_position)
    monkeypatch.setattr(d, "_aero_npm_for_token", fake_npm_for_token)
    monkeypatch.setattr(d, "cmd_aero_remove_liquidity", fake_remove_liquidity)
    monkeypatch.setattr(d, "_aero_rebuild_sweep", fake_sweep)
    monkeypatch.setattr(d, "_cmd_aero_add_liquidity_all_available", fake_add_liquidity_all)

    return calls


def test_rebuild_resolves_pool_before_removing_position(monkeypatch):
    calls = _rig(monkeypatch)

    d.cmd_aero_rebuild(token_id=123, width_pct=12.58, slippage_pct=1.0, execute=True)

    assert calls == ["read_position", "npm_for_token", "remove_liquidity", "sweep", "add_liquidity"], (
        f"pool must be resolved BEFORE removal (removal burns the NFT, making a "
        f"post-removal read return None) -- got order {calls}"
    )


def test_rebuild_warns_when_an_active_order_will_be_orphaned(monkeypatch, capsys):
    """Confirmed live 2026-07-04: removing a position clears its rebalance order.
    A rebuild on a position that HAD an active order must say so, since nothing
    recreates the order automatically (the new tokenId doesn't exist until after
    Step 3's mint)."""
    _rig(monkeypatch, order_created_on=1783100000)

    d.cmd_aero_rebuild(token_id=123, width_pct=12.58, slippage_pct=1.0, execute=True)

    out = capsys.readouterr().out
    assert "active rebalance order" in out
    assert "aero-rebalance create" in out


def test_rebuild_no_warning_when_no_active_order(monkeypatch, capsys):
    _rig(monkeypatch, order_created_on=0)

    d.cmd_aero_rebuild(token_id=123, width_pct=12.58, slippage_pct=1.0, execute=True)

    out = capsys.readouterr().out
    assert "active rebalance order" not in out
