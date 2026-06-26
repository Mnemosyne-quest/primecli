"""Tests for health_monitor: the health computation, the valuation-complete gate,
and the run_tick rebalance safety property (no borrow/repay on incomplete data).

All offline: compute_health / valuation_complete are pure functions over dicts
shaped like `defi --json` (trimmed) output. run_tick shells out via
subprocess.run, so we monkeypatch that single seam to feed canned `defi --json`
and `prime-tier` output and to record any borrow/repay/gmx-deposit invocation —
no real process is ever spawned, no chain is touched.

Health pct formula (uniform-power fallback, with tier multiplier `max_mult`):
    equity   = sum(supplied.usd) - sum(borrowed.usd)
    max_debt = max_mult * equity
    pct      = 100 * (1 - debt / max_debt)
This matches the on-chain getHealthMeter zero-crossing (debt == max_mult·equity →
health 0), where max_mult = dc/(1-dc) is the asset's borrowing power (10 for the
0.909091 class, 5 for 0.833333). For premium (max_mult=10) with equity=2000 →
max_debt=20000, the debt levels below land on 86.5% (lever), 55% (in range) and
19% (de-lever) for a 30-70 range.
"""

from __future__ import annotations

import importlib
import json

import pytest

hm = importlib.import_module("primecli.health_monitor")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers to shape `defi --json`-style data


def _grouped(supplied, borrowed, health_ratio=1.5, **top):
    data = {
        "groups": [
            {
                "supplied": supplied,
                "borrowed": borrowed,
                "health_ratio": health_ratio,
            }
        ]
    }
    data.update(top)
    return data


# ──────────────────────────────────────────────────────────────────────────────
# compute_health — lever / in-range / de-lever / equity-near-zero branches


def test_compute_health_lever_branch_high_pct():
    """Low debt vs equity → high health pct → above a 30-70 range → would lever."""
    h = hm.compute_health(
        _grouped(
            [{"symbol": "USDC", "usd": 4700}],
            [{"symbol": "USDC", "usd": 2700}],
        ),
        max_mult=10,
    )
    assert h["equity"] == 2000
    assert h["health_pct"] == 86.5
    assert h["health_pct"] > 70  # lever territory


def test_compute_health_in_range_branch():
    """Mid debt → pct sits inside the 30-70 range → no action."""
    h = hm.compute_health(
        _grouped(
            [{"symbol": "USDC", "usd": 11000}],
            [{"symbol": "USDC", "usd": 9000}],
        ),
        max_mult=10,
    )
    assert h["equity"] == 2000
    assert h["health_pct"] == 55.0
    assert 30 <= h["health_pct"] <= 70


def test_compute_health_delever_branch_low_pct():
    """High debt vs equity → low health pct → below the range → would de-lever."""
    h = hm.compute_health(
        _grouped(
            [{"symbol": "USDC", "usd": 18200}],
            [{"symbol": "USDC", "usd": 16200}],
        ),
        max_mult=10,
    )
    assert h["equity"] == 2000
    assert h["health_pct"] == 19.0
    assert h["health_pct"] < 30  # de-lever territory


def test_compute_health_equity_near_zero_errors():
    """Equity at/under the 0.01 floor returns the error branch, not a pct."""
    h = hm.compute_health(
        _grouped(
            [{"symbol": "USDC", "usd": 1000}],
            [{"symbol": "USDC", "usd": 1000}],
        ),
        max_mult=10,
    )
    assert h["error"] == "equity near zero"
    assert h["health_pct"] == 0.0


def test_compute_health_basic_tier_lower_ceiling():
    """Basic tier (max_mult=5) gives a tighter max_debt, so the same equity/debt
    yields a lower health pct than premium would."""
    supplied = [{"symbol": "USDC", "usd": 6000}]
    borrowed = [{"symbol": "USDC", "usd": 4000}]
    basic = hm.compute_health(_grouped(supplied, borrowed), max_mult=5)
    premium = hm.compute_health(_grouped(supplied, borrowed), max_mult=10)
    assert basic["equity"] == premium["equity"] == 2000
    assert basic["max_debt"] == 2000 * 5
    assert premium["max_debt"] == 2000 * 10
    assert basic["health_pct"] < premium["health_pct"]


def test_compute_health_flat_format_and_position_detection():
    """DegenPrime's flat (no `groups`) shape parses, and position-type flags fire."""
    h = hm.compute_health(
        {
            "supplied": [
                {"symbol": "USDC", "usd": 500},
                {"symbol": "GM_AVAX_USDC", "usd": 1500},
            ],
            "borrowed": [{"symbol": "USDC", "usd": 900}],
        },
        max_mult=10,
    )
    assert h["raw_usdc"] == 500
    assert h["has_gmx"] is True
    assert h["has_lb"] is False


def test_compute_health_includes_group_items_as_collateral():
    """Staked LP rows can sit under group.items; health must count them as collateral."""
    h = hm.compute_health(
        {
            "groups": [
                {
                    "type": "Lending / Leverage",
                    "supplied": [{"symbol": "ETH", "usd": 16}],
                    "borrowed": [{"symbol": "USDC", "usd": 580}],
                },
                {
                    "type": "Aerodrome",
                    "items": [{"symbol": "AERO/cbBTC", "usd": 780}],
                },
            ],
        },
        max_mult=5,
    )
    assert h["supplied_usd"] == 796
    assert h["equity"] == 216
    assert h["health_pct"] > 40
    assert h["has_aero"] is True


def test_compute_health_does_not_double_count_group_item_duplicate():
    """GMX display items duplicate the supplied GM token row and must be ignored."""
    h = hm.compute_health(
        {
            "groups": [
                {
                    "type": "Lending / Leverage",
                    "supplied": [
                        {"symbol": "AVAX", "usd": 1},
                        {"symbol": "GM_AVAX_WAVAX_USDC", "usd": 2682},
                    ],
                    "borrowed": [{"symbol": "USDC", "usd": 2205}],
                },
                {
                    "type": "GMX V2 LP",
                    "items": [
                        {
                            "label": "GM_AVAX_WAVAX_USDC",
                            "symbol": "GM",
                            "usd": 2681,
                        }
                    ],
                },
                {
                    "type": "Savings",
                    "supplied": [{"symbol": "AVAX", "usd": 99}],
                },
            ],
        },
        max_mult=10,
    )
    assert h["supplied_usd"] == 2683
    assert h["debt_usd"] == 2205
    assert h["has_gmx"] is True


def test_compute_health_prefers_tool_reported_health_pct():
    """When the underlying tool already emits its own health meter, report that."""
    h = hm.compute_health(
        {
            "health_pct": 73.6,
            "health_ratio": 1.25,
            "groups": [
                {
                    "supplied": [{"symbol": "ETH", "usd": 16}],
                    "borrowed": [{"symbol": "USDC", "usd": 580}],
                },
                {"items": [{"symbol": "AERO/cbBTC", "usd": 780}]},
            ],
        },
        max_mult=5,
    )
    assert h["health_pct"] == 73.6
    assert h["health_ratio"] == 1.25


# ──────────────────────────────────────────────────────────────────────────────
# valuation_complete — the auto-action gate


def test_valuation_complete_ok_on_fully_priced_solvent():
    ok, reason = hm.valuation_complete(
        _grouped(
            [{"symbol": "USDC", "usd": 1000}, {"symbol": "GM_X", "usd": 500}],
            [{"symbol": "USDC", "usd": 200}],
            status="ok",
            solvent=True,
        )
    )
    assert ok is True
    assert reason == "ok"


def test_valuation_complete_not_ok_when_status_not_ok():
    ok, reason = hm.valuation_complete(
        _grouped(
            [{"symbol": "USDC", "usd": 1000}],
            [],
            status="error",
            solvent=True,
        )
    )
    assert ok is False
    assert "status" in reason


def test_valuation_complete_not_ok_when_row_missing_usd():
    """A trimmed row whose RedStone feed was missing comes back without `usd`."""
    ok, reason = hm.valuation_complete(
        _grouped(
            [{"symbol": "USDC", "usd": 1000}, {"symbol": "GM_X"}],  # GM_X unpriced
            [{"symbol": "USDC", "usd": 200}],
            status="ok",
            solvent=True,
        )
    )
    assert ok is False
    assert "GM_X" in reason


def test_valuation_complete_allows_reconciled_unpriced_dust():
    ok, reason = hm.valuation_complete(
        _grouped(
            [{"symbol": "USDC", "usd": 1000}, {"symbol": "DUST"}],
            [{"symbol": "USDC", "usd": 200}],
            status="ok",
            solvent=True,
            total_usd=1000.25,
        )
    )
    assert ok is True
    assert reason == "ok"


def test_valuation_complete_blocks_material_unpriced_residual():
    ok, reason = hm.valuation_complete(
        _grouped(
            [{"symbol": "USDC", "usd": 1000}, {"symbol": "MISSING"}],
            [{"symbol": "USDC", "usd": 200}],
            status="ok",
            solvent=True,
            total_usd=1100,
        )
    )
    assert ok is False
    assert "MISSING" in reason


def test_valuation_complete_not_ok_when_solvency_error_present():
    ok, reason = hm.valuation_complete(
        _grouped(
            [{"symbol": "USDC", "usd": 1000}],
            [],
            status="ok",
            solvent=True,
            solvency_error="oracle stale",
        )
    )
    assert ok is False
    assert "solvency_error" in reason


def test_valuation_complete_not_ok_when_solvent_missing():
    """`solvent: None` is trimmed out entirely → the gate must treat absence as not-ok."""
    ok, reason = hm.valuation_complete(
        _grouped(
            [{"symbol": "USDC", "usd": 1000}],
            [],
            status="ok",
            # solvent intentionally absent
        )
    )
    assert ok is False
    assert "solvent" in reason


# ──────────────────────────────────────────────────────────────────────────────
# run_tick — the rebalance safety property: NO action on incomplete valuation


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_fake_subprocess(monkeypatch, defi_payload, recorder):
    """Patch health_monitor.subprocess.run to serve canned defi/prime-tier output and
    record the subcommand of every invocation into `recorder`."""

    def fake_run(cmd, **kwargs):
        subcmd = cmd[2] if len(cmd) > 2 else ""
        recorder.append(subcmd)
        if subcmd == "defi":
            return _FakeCompleted(stdout=json.dumps(defi_payload))
        if subcmd == "prime-tier":
            return _FakeCompleted(stdout="premium")
        if subcmd == "pool-info":
            # Healthy pool (low utilization, low APR) so _usdc_borrow_feasible greenlights.
            return _FakeCompleted(stdout=json.dumps({"utilization": 40.0, "borrowingRate": 5.0}))
        # borrow / repay / gmx-deposit — pretend they succeed (should never be reached
        # on incomplete data).
        return _FakeCompleted(stdout="done")

    monkeypatch.setattr(hm.subprocess, "run", fake_run)


_ACTION_SUBCMDS = {"borrow", "repay", "gmx-deposit"}

# Below the 30 floor (pct=10) so the de-lever path WOULD fire if the gate let it.
_LOW_PCT_POSITION = {
    "supplied": [{"symbol": "USDC", "usd": 18200}],
    "borrowed": [{"symbol": "USDC", "usd": 16200}],
    "health_ratio": 1.1,
}


def _write_rebalance_strategy(tmp_path):
    strat = tmp_path / "strategy.json"
    strat.write_text(
        json.dumps(
            {
                "mode": "rebalance",
                "target_range": [30, 70],
                "center": 50,
                "position": "gmx",
                "market": "avax-usdc",
                "side": "short",
                "cooldown_secs": 0,
            }
        )
    )
    return str(strat)


def test_run_tick_no_action_on_incomplete_valuation(tmp_path, monkeypatch):
    """An unpriced position (row missing `usd`) at a pct that would otherwise trigger a
    de-lever must NOT cause any borrow/repay/gmx-deposit — the valuation gate forces
    observe-only and escalates."""
    incomplete = _grouped(
        _LOW_PCT_POSITION["supplied"] + [{"symbol": "GM_X"}],  # GM_X unpriced
        _LOW_PCT_POSITION["borrowed"],
        health_ratio=_LOW_PCT_POSITION["health_ratio"],
        status="ok",
        solvent=True,
    )
    calls: list[str] = []
    _install_fake_subprocess(monkeypatch, incomplete, calls)

    result = hm.run_tick(
        tool_path="/fake/deltaprime.py",
        strategy_path=_write_rebalance_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="prime",
        dry_run=False,
    )

    assert result["escalation"] == "incomplete_valuation"
    assert result["action"] == "observe (incomplete valuation)"
    assert not _ACTION_SUBCMDS.intersection(calls), (
        f"an action subcommand was invoked on incomplete data: {calls}"
    )


def test_run_tick_action_fires_on_complete_data_positive_control(tmp_path, monkeypatch):
    """Positive control: the SAME low pct with fully-priced data DOES drive a repay.
    This proves the previous test's no-action is caused by the valuation gate, not by
    the data simply never reaching the rebalance branch."""
    complete = _grouped(
        _LOW_PCT_POSITION["supplied"],
        _LOW_PCT_POSITION["borrowed"],
        health_ratio=_LOW_PCT_POSITION["health_ratio"],
        status="ok",
        solvent=True,
    )
    calls: list[str] = []
    _install_fake_subprocess(monkeypatch, complete, calls)

    result = hm.run_tick(
        tool_path="/fake/deltaprime.py",
        strategy_path=_write_rebalance_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="prime",
        dry_run=False,
    )

    assert "repay" in calls
    assert result.get("action", "").startswith("repaid")


def test_run_tick_aerodrome_high_health_borrows_usdc(tmp_path, monkeypatch):
    """Aerodrome LP leverage: health monitor borrows USDC, defisims deploys."""
    aero_high = _grouped(
        [{"symbol": "USDC", "usd": 1000}],
        [{"symbol": "USDC", "usd": 200}],
        health_ratio=1.5,
        status="ok",
        solvent=True,
        total_usd=2000,
    )
    aero_high["groups"].append({
        "type": "Aerodrome",
        "items": [{"symbol": "AERO/cbBTC", "usd": 1000}],
    })
    calls: list[str] = []
    _install_fake_subprocess(monkeypatch, aero_high, calls)

    result = hm.run_tick(
        tool_path="/fake/degenprime.py",
        strategy_path=_write_rebalance_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="degen",
        dry_run=False,
    )

    assert "pool-info" in calls, f"should check pool costs before borrow, calls={calls}"
    assert "borrow" in calls, f"should borrow USDC, calls={calls}"
    assert "defisims" in result["action"]


def test_run_tick_gmx_high_health_borrows_usdc(tmp_path, monkeypatch):
    """GMX leverage: health monitor borrows USDC, defisims deploys."""
    gmx_high = _grouped(
        [{"symbol": "GM_AVAX_WAVAX_USDC", "usd": 5000}],
        [{"symbol": "USDC", "usd": 200}],
        status="ok",
        solvent=True,
        total_usd=5000,
    )
    calls: list[str] = []
    _install_fake_subprocess(monkeypatch, gmx_high, calls)

    result = hm.run_tick(
        tool_path="/fake/deltaprime.py",
        strategy_path=_write_rebalance_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="prime",
        dry_run=False,
    )

    assert "pool-info" in calls, f"should check pool costs before borrow, calls={calls}"
    assert "borrow" in calls, f"should borrow USDC, calls={calls}"
    assert "defisims" in result["action"]


def test_run_tick_dry_run_never_executes(tmp_path, monkeypatch):
    """dry_run on complete low-pct data plans a repay but executes nothing."""
    complete = _grouped(
        _LOW_PCT_POSITION["supplied"],
        _LOW_PCT_POSITION["borrowed"],
        health_ratio=_LOW_PCT_POSITION["health_ratio"],
        status="ok",
        solvent=True,
    )
    calls: list[str] = []
    _install_fake_subprocess(monkeypatch, complete, calls)

    result = hm.run_tick(
        tool_path="/fake/deltaprime.py",
        strategy_path=_write_rebalance_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="prime",
        dry_run=True,
    )

    assert not _ACTION_SUBCMDS.intersection(calls)
    assert result["action"].startswith("would repay")


# ──────────────────────────────────────────────────────────────────────────────
# FIX #5a — _usdc_borrow_feasible fails CLOSED (lever postponed) when pool-info
# can't be read. Lever-up is never urgent, so an unreadable pool must not greenlight
# a borrow.


def test_usdc_borrow_feasible_fails_closed_on_error(monkeypatch):
    """pool-info rc!=0 AND a raising subprocess must both return (False, reason)."""

    def fake_run_rc1(cmd, **kwargs):
        return _FakeCompleted(returncode=1, stderr="boom")

    monkeypatch.setattr(hm.subprocess, "run", fake_run_rc1)
    ok, reason = hm._usdc_borrow_feasible("/fake/deltaprime.py")
    assert ok is False
    assert "rc=1" in reason or "cannot confirm" in reason

    def fake_run_raise(cmd, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(hm.subprocess, "run", fake_run_raise)
    ok, reason = hm._usdc_borrow_feasible("/fake/deltaprime.py")
    assert ok is False
    assert "postponing lever" in reason


def test_run_tick_gmx_lever_postponed_when_pool_info_fails(tmp_path, monkeypatch):
    """End-to-end: a GMX position that would lever, but pool-info fails → no borrow,
    action says lever postponed (fail-closed)."""
    gmx_high = _grouped(
        [{"symbol": "GM_AVAX_WAVAX_USDC", "usd": 5000}],
        [{"symbol": "USDC", "usd": 200}],
        status="ok",
        solvent=True,
        total_usd=5000,
    )
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        subcmd = cmd[2] if len(cmd) > 2 else ""
        calls.append(subcmd)
        if subcmd == "defi":
            return _FakeCompleted(stdout=json.dumps(gmx_high))
        if subcmd == "prime-tier":
            return _FakeCompleted(stdout="premium")
        if subcmd == "pool-info":
            return _FakeCompleted(returncode=1, stderr="pool-info unavailable")
        return _FakeCompleted(stdout="done")

    monkeypatch.setattr(hm.subprocess, "run", fake_run)

    result = hm.run_tick(
        tool_path="/fake/deltaprime.py",
        strategy_path=_write_rebalance_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="prime",
        dry_run=False,
    )

    assert "pool-info" in calls
    assert "borrow" not in calls, f"must not borrow when pool-info fails, calls={calls}"
    assert "postponed" in result["action"], result["action"]


# ──────────────────────────────────────────────────────────────────────────────
# FIX #4 — stranded-debt guard: a GMX lever-up whose borrow would fall below the
# autofarm deploy floor is skipped (the GMX/avax autofarm is NOT scheduled, so the
# borrow would only sit as raw USDC, lowering health for no yield).


def _write_small_lever_gmx_strategy(tmp_path):
    strat = tmp_path / "strategy.json"
    strat.write_text(
        json.dumps(
            {
                "mode": "rebalance",
                "target_range": [30, 70],
                "center": 50,
                "position": "gmx",
                "market": "avax-usdc",
                "side": "short",
                "cooldown_secs": 0,
            }
        )
    )
    return str(strat)


def test_run_tick_gmx_small_lever_skips_to_avoid_stranding(tmp_path, monkeypatch):
    """A GMX position above the range whose borrow_amt < deploy floor ($100) must
    NOT borrow — it would strand as raw USDC. Action mentions the skip/strand."""
    # equity=20, debt=10, premium max_mult=10 → max_debt=200, pct=95%, delta=90 (< 100 floor)
    gmx_small = _grouped(
        [{"symbol": "GM_AVAX_WAVAX_USDC", "usd": 30}],
        [{"symbol": "USDC", "usd": 10}],
        status="ok",
        solvent=True,
        total_usd=30,
    )
    calls: list[str] = []
    _install_fake_subprocess(monkeypatch, gmx_small, calls)

    result = hm.run_tick(
        tool_path="/fake/deltaprime.py",
        strategy_path=_write_small_lever_gmx_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="prime",
        dry_run=False,
    )

    assert "borrow" not in calls, f"small GMX lever must not borrow, calls={calls}"
    assert "skipped" in result["action"] and "strand" in result["action"], result["action"]


# ──────────────────────────────────────────────────────────────────────────────
# FIX #6 — de-lever rollback: after a SYNCHRONOUS LP close, repay the freed USDC
# IMMEDIATELY (partial ok) BEFORE the volatile swap, so a failed swap can't strand
# the position LP-closed + fully indebted + exposed. Escalate loudly if debt remains.


class _SeqFake:
    """Stateful subprocess fake: serves a SEQUENCE of `defi --json` payloads (advancing
    one step per `defi` call), canned returncodes per subcommand, and records every
    invocation's (subcmd, full cmd list) so tests can assert ORDER and amounts."""

    def __init__(self, defi_sequence, rc_by_subcmd=None):
        self._defi_seq = list(defi_sequence)
        self._defi_i = 0
        self._rc = rc_by_subcmd or {}
        self.calls = []  # list of subcmd strings, in order
        self.cmds = []   # list of full cmd lists, in order

    def run(self, cmd, **kwargs):
        subcmd = cmd[2] if len(cmd) > 2 else ""
        self.calls.append(subcmd)
        self.cmds.append(list(cmd))
        if subcmd == "defi":
            i = min(self._defi_i, len(self._defi_seq) - 1)
            payload = self._defi_seq[i]
            self._defi_i += 1
            return _FakeCompleted(stdout=json.dumps(payload))
        if subcmd == "prime-tier":
            return _FakeCompleted(stdout="premium")
        rc = self._rc.get(subcmd, 0)
        return _FakeCompleted(returncode=rc, stdout="done", stderr=("fail" if rc else ""))


def _write_aero_delever_strategy(tmp_path):
    strat = tmp_path / "strategy.json"
    strat.write_text(
        json.dumps(
            {
                "mode": "rebalance",
                "target_range": [30, 70],
                "center": 50,
                "position": "aero",
                "side": "short",
                "cooldown_secs": 0,
            }
        )
    )
    return str(strat)


def _aero_delever_initial():
    """pct=19% (de-lever): raw USDC $2 + Aerodrome LP item $180 collateral, USDC debt $162.
    No swappable raw supplied asset (the LP sits under group.items) → forces LP close."""
    return {
        "status": "ok",
        "solvent": True,
        "total_usd": 182,
        "groups": [
            {
                "type": "Lending / Leverage",
                "supplied": [{"symbol": "USDC", "usd": 2, "amount": 2}],
                "borrowed": [{"symbol": "USDC", "usd": 162}],
                "health_ratio": 1.1,
            },
            {
                "type": "Aerodrome",
                "items": [{"symbol": "AERO/cbBTC", "token_id": 123, "usd": 180}],
            },
        ],
    }


def test_delever_repays_freed_usdc_before_swap(tmp_path, monkeypatch):
    """LP close frees partial USDC (< repay) plus a volatile asset. The freed USDC must
    be repaid BEFORE any swap; the swap only covers the remaining shortfall."""
    # After LP close: $40 raw USDC freed + $140 cbBTC sitting in supplied (swappable).
    after_close = {
        "status": "ok",
        "solvent": True,
        "total_usd": 182,
        "groups": [
            {
                "type": "Lending / Leverage",
                "supplied": [
                    {"symbol": "USDC", "usd": 40, "amount": 40},
                    {"symbol": "cbBTC", "usd": 140, "amount": 0.002},
                ],
                "borrowed": [{"symbol": "USDC", "usd": 162}],
                "health_ratio": 1.1,
            },
        ],
    }
    # 3rd defi (post-swap) — USDC now covers the shortfall.
    after_swap = {
        "status": "ok",
        "solvent": True,
        "total_usd": 182,
        "groups": [
            {
                "type": "Lending / Leverage",
                "supplied": [{"symbol": "USDC", "usd": 62, "amount": 62}],
                "borrowed": [{"symbol": "USDC", "usd": 162}],
                "health_ratio": 1.1,
            },
        ],
    }
    fake = _SeqFake([_aero_delever_initial(), after_close, after_swap])
    monkeypatch.setattr(hm.subprocess, "run", fake.run)

    result = hm.run_tick(
        tool_path="/fake/degenprime.py",
        strategy_path=_write_aero_delever_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="degen",
        dry_run=False,
    )

    # Order: aero close → repay (freed) → swap (shortfall) → repay (shortfall).
    assert "aero-remove-liquidity" in fake.calls, fake.calls
    first_repay = fake.calls.index("repay")
    first_swap = fake.calls.index("swap")
    assert first_repay < first_swap, (
        f"freed USDC must be repaid BEFORE the swap; order={fake.calls}"
    )
    # The first repay used the freed amount ($40), not the full repay_amt ($62).
    first_repay_cmd = fake.cmds[first_repay]
    amt = first_repay_cmd[first_repay_cmd.index("--amount") + 1]
    # Compare numerically — the repay amount is formatted to 8 dp (precision needed for
    # small-decimal tokens like cbBTC), so the exact string ("40.00000000") is incidental.
    assert float(amt) == 40.0, f"first repay should be the freed $40, got {amt}"
    assert result.get("lp_repay", "").startswith("repaid $40.00")


def test_delever_escalates_loudly_when_debt_remains_after_lp_close(tmp_path, monkeypatch):
    """LP close frees NO usable USDC (only a volatile asset). Debt remains → escalate
    LOUDLY with write_escalation('delever-debt-remains-after-lp-close') + a notify, and
    no repay is attempted (nothing to repay)."""
    # After LP close: no USDC freed, only cbBTC. A swap, if attempted, would fail —
    # but the repay-first design escalates before swapping (no USDC to repay first).
    after_close_no_usdc = {
        "status": "ok",
        "solvent": True,
        "total_usd": 182,
        "groups": [
            {
                "type": "Lending / Leverage",
                "supplied": [{"symbol": "cbBTC", "usd": 180, "amount": 0.003}],
                "borrowed": [{"symbol": "USDC", "usd": 162}],
                "health_ratio": 1.1,
            },
        ],
    }
    fake = _SeqFake(
        [_aero_delever_initial(), after_close_no_usdc],
        rc_by_subcmd={"swap": 1},  # swap would fail if reached
    )
    monkeypatch.setattr(hm.subprocess, "run", fake.run)

    escalations: list[tuple] = []
    notifies: list[str] = []
    monkeypatch.setattr(hm, "write_escalation",
                        lambda sd, reason, payload: escalations.append((reason, payload)))
    monkeypatch.setattr(hm, "_notify", lambda text: notifies.append(text))

    result = hm.run_tick(
        tool_path="/fake/degenprime.py",
        strategy_path=_write_aero_delever_strategy(tmp_path),
        state_dir=str(tmp_path / "state"),
        label="degen",
        dry_run=False,
    )

    reasons = [r for r, _ in escalations]
    assert "delever-debt-remains-after-lp-close" in reasons, reasons
    assert notifies, "expected a loud notify when debt remains after LP close"
    assert "repay" not in fake.calls, f"nothing to repay yet, calls={fake.calls}"
    assert "escalate" in result["action"], result["action"]


# ──────────────────────────────────────────────────────────────────────────────
# Oracle-flap guards: a partial misprice (passes the unpriced gate but inflates/deflates
# equity) must NOT fire a destructive escalation. Regression for 2026-06-26, when a false
# health_swing closed core1's AERO/cbBTC LP.


def _observer_strategy(tmp_path):
    strat = tmp_path / "obs-strategy.json"
    strat.write_text(json.dumps({"mode": "observer"}))
    return str(strat)


def _stoploss_strategy(tmp_path, drawdown_pct=20):
    strat = tmp_path / "sl-strategy.json"
    strat.write_text(json.dumps({
        "mode": "rebalance", "target_range": [30, 70], "center": 50,
        "cooldown_secs": 0, "stop_loss_drawdown_pct": drawdown_pct,
    }))
    return str(strat)


def _priced(supplied_usd, debt_usd, health_pct, health_ratio=1.5):
    """Fully-priced, solvent payload with explicit equity (supplied−debt) and a
    tool-reported health_pct (>10 so the false-low guard stays inert)."""
    return _grouped(
        [{"symbol": "USDC", "usd": supplied_usd}],
        [{"symbol": "USDC", "usd": debt_usd}],
        health_ratio=health_ratio,
        status="ok",
        solvent=True,
        health_pct=health_pct,
    )


def _tick(tmp_path, monkeypatch, payload, strat, sd, label="core1"):
    calls: list[str] = []
    _install_fake_subprocess(monkeypatch, payload, calls)
    return hm.run_tick(tool_path="/fake/degenprime.py", strategy_path=strat,
                       state_dir=sd, label=label, dry_run=False)


def test_health_swing_suppressed_on_misprice_equity_jump(tmp_path, monkeypatch):
    """A misprice that inflates equity (and health) must NOT fire health_swing — the
    equity jumped implausibly vs the prior tick (the bug that closed core1's LP)."""
    sd = str(tmp_path / "state")
    strat = _observer_strategy(tmp_path)
    # Tick 1: healthy, equity ~$300 (1700−1400), health 53%.
    _tick(tmp_path, monkeypatch, _priced(1700, 1400, 53.0), strat, sd)
    # Tick 2: MISPRICE — health 93%, equity inflated to ~$2000 (3400−1400 = 6.7x jump).
    r2 = _tick(tmp_path, monkeypatch, _priced(3400, 1400, 93.0), strat, sd)
    assert r2.get("escalation") != "health_swing", r2
    assert r2.get("swing_guard") == "implausible_equity_jump", r2


def test_health_swing_fires_on_real_swing_stable_equity(tmp_path, monkeypatch):
    """A genuine health swing on STABLE equity still escalates."""
    sd = str(tmp_path / "state")
    strat = _observer_strategy(tmp_path)
    _tick(tmp_path, monkeypatch, _priced(1700, 1400, 53.0), strat, sd)
    # Tick 2: real drop to 38%, equity stable (~$300, same supplied/debt).
    r2 = _tick(tmp_path, monkeypatch, _priced(1700, 1400, 38.0), strat, sd)
    assert r2.get("escalation") == "health_swing", r2


def test_stop_loss_requires_two_confirmations(tmp_path, monkeypatch):
    """A genuine drawdown must hold for 2 consecutive trusted reads before a full close."""
    sd = str(tmp_path / "state")
    strat = _stoploss_strategy(tmp_path, 20)
    # Tick 1: baseline equity ~$1000 (2400−1400), health 50% (in range).
    _tick(tmp_path, monkeypatch, _priced(2400, 1400, 50.0), strat, sd)
    # Tick 2: equity ~$700 (2100−1400) = 30% drawdown, plausible (0.7x) → pending, no close.
    r2 = _tick(tmp_path, monkeypatch, _priced(2100, 1400, 40.0), strat, sd)
    assert r2.get("escalation") != "stop_loss", r2
    assert "pending confirmation" in r2.get("action", ""), r2
    # Tick 3: drawdown persists → 2nd confirmation → full close.
    r3 = _tick(tmp_path, monkeypatch, _priced(2100, 1400, 40.0), strat, sd)
    assert r3.get("escalation") == "stop_loss", r3
    assert r3.get("action") == "escalate_close", r3


def test_stop_loss_defers_on_untrusted_deflation(tmp_path, monkeypatch):
    """A single deflating-misprice tick (equity collapses implausibly) must NOT trigger a
    stop-loss close — it's deferred as an untrusted read."""
    sd = str(tmp_path / "state")
    strat = _stoploss_strategy(tmp_path, 20)
    # Tick 1: baseline equity ~$1000.
    _tick(tmp_path, monkeypatch, _priced(2400, 1400, 50.0), strat, sd)
    # Tick 2: MISPRICE deflation — equity ~$100 (1500−1400 = 0.1x) → 90% "drawdown" but
    # implausible jump → deferred, not closed. health 15 (>10, keeps the false-low guard inert).
    r2 = _tick(tmp_path, monkeypatch, _priced(1500, 1400, 15.0), strat, sd)
    assert r2.get("escalation") != "stop_loss", r2
    assert "deferred" in r2.get("action", ""), r2
