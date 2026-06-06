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
