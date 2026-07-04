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


def test_rebuild_resolves_pool_before_removing_position(monkeypatch):
    calls = []

    # weth-usdc-100 is a V2 (non-Slipstream) pool, so this can't hit the
    # 14s V3 gauge anti-sniping sleep in Step 1->3.
    pool_key = "weth-usdc-100"
    pool_cfg = d.AERODROME_POOLS[pool_key]
    token0, token1 = pool_cfg["token0"], pool_cfg["token1"]

    monkeypatch.setattr(d, "get_w3", lambda: MagicMock())
    monkeypatch.setattr(d, "get_account", lambda: MagicMock(address="0x" + "1" * 40))
    monkeypatch.setattr(d, "get_prime_account", lambda w3, addr: "0x" + "2" * 40)
    monkeypatch.setattr(d.Web3, "to_checksum_address", staticmethod(lambda a: a))

    def fake_read_position(w3, token_id, pa):
        calls.append("read_position")
        return (token0, token1, None, None, None)

    def fake_remove_liquidity(token_ids, percentage, execute=False):
        calls.append("remove_liquidity")

    def fake_sweep(w3, acct, account, pa_cs, pk, pc, execute=False):
        calls.append("sweep")

    def fake_add_liquidity_all(pk, slippage_pct, execute, width_pct):
        calls.append("add_liquidity")

    monkeypatch.setattr(d, "_aero_read_position", fake_read_position)
    monkeypatch.setattr(d, "cmd_aero_remove_liquidity", fake_remove_liquidity)
    monkeypatch.setattr(d, "_aero_rebuild_sweep", fake_sweep)
    monkeypatch.setattr(d, "_cmd_aero_add_liquidity_all_available", fake_add_liquidity_all)

    d.cmd_aero_rebuild(token_id=123, width_pct=12.58, slippage_pct=1.0, execute=True)

    assert calls == ["read_position", "remove_liquidity", "sweep", "add_liquidity"], (
        f"pool must be resolved BEFORE removal (removal burns the NFT, making a "
        f"post-removal read return None) -- got order {calls}"
    )
