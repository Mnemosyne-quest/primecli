"""Health monitoring and strategy system for Prime/Degen accounts.

Sits on top of the primecli tool commands (defi --json, borrow, repay, etc.)
to provide automated health tracking, configurable rebalancing, and equity-drawdown
stop-loss. Designed for cron-based operation.

Two modes:
  Observer (default) — logs state, escalates on issues. No auto-actions.
  Rebalance         — with a strategy.json, auto-rebalances within a target range.

Strategy config (JSON):
  {
    "mode": "rebalance",
    "target_range": [30, 70],
    "center": 50,
    "cooldown_secs": 3600,
    "position": "gmx",
    "market": "avax-usdc",
    "side": "short"
  }
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Tier config ─────────────────────────────────────────────────────
TIER_MAX = {"basic": 5, "premium": 10}


# ════════════════════════════════════════════════════════════════════
# Health computation
# ════════════════════════════════════════════════════════════════════

def compute_health(defi_data: dict, max_mult: int = 10) -> dict:
    """Compute health (0-100%) using the frontend formula from DeltaPrime docs.

    Uses the cross-margin health formula (all assets assumed same borrowing power):
      equity    = total_supplied_usd - total_debt_usd
      health_pct = 100 * (1 - debt / (max_mult * equity))
      (0% = liquidation, 100% = no debt)

    Background:
      The frontend uses Pr = tier / (tier + 1) and computes:
        health_pct = (Pr * supplied - debt) / (Pr * equity) * 100
      which simplifies to: 100 * (1 - debt / (max_mult * equity)).

    DIFFERENT from the equity-based "health_pct" in defi --json / prime-summary
    (which uses max_debt = equity * (tier - 1)).
    The on-chain health_ratio (1.0=liquidation) is NOT used here.
    """
    # Parse groups (DeltaPrime format) or flat format (DegenPrime)
    groups = defi_data.get("groups", [])
    if groups:
        g = groups[0]
        supplied = g.get("supplied", [])
        borrowed = g.get("borrowed", [])
        health_ratio = g.get("health_ratio", 0) or 0
        # Use precomputed health_pct from defi --json if available (primecli >= 0.5.4)
        precomputed = g.get("health_pct")
        if precomputed is not None:
            # Override health_pct with frontend formula (ignores precomputed value)
            supplied_usd = sum(s.get("usd", 0) or 0 for s in supplied)
            debt_usd = sum(b.get("usd", 0) or 0 for b in borrowed)
            equity = max(supplied_usd - debt_usd, 0.01)
            raw_usdc = sum(s.get("usd", 0) for s in supplied if s.get("symbol") == "USDC")
            symbols = [s.get("symbol", "") for s in supplied]
            has_gmx = sum(s.get("usd", 0) for s in supplied if "GM_" in s.get("symbol", "")) > 1.0
            has_lb = any(sym in ("LB_AVAX_USDC", "LB_WAVAX_USDC", "JOE") or "TRADERJOE" in sym.upper() for sym in symbols)
            has_aero = any("AERO" in sym.upper() or "CL_POSITION" in sym.upper() for sym in symbols)
            # Frontend formula: health_pct = 100 * (1 - debt / (max_mult * equity))
            fe_health = max(0.0, 100.0 * (1.0 - round(debt_usd, 2) / (max_mult * equity)))
            fe_max_debt = round(max_mult * equity, 2)
            return {
                "health_pct": round(fe_health, 1),
                "health_ratio": round(health_ratio, 4),
                "supplied_usd": round(supplied_usd, 2),
                "debt_usd": round(debt_usd, 2),
                "equity": round(equity, 2),
                "max_debt": round(max(0, max_mult * equity), 2),
                "raw_usdc": round(raw_usdc, 2),
                "has_gmx": has_gmx,
                "has_lb": has_lb,
                "has_aero": has_aero,
                "action": "computed from defi --json health_pct",
            }
    else:
        supplied = defi_data.get("supplied", [])
        borrowed = defi_data.get("borrowed", [])
        health_ratio = 0

    supplied_usd = sum(s.get("usd", 0) or 0 for s in supplied)
    debt_usd = sum(b.get("usd", 0) or 0 for b in borrowed)
    equity = supplied_usd - debt_usd

    if equity <= 0.01:
        return {
            "health_pct": 0.0,
            "health_ratio": round(health_ratio, 4),
            "supplied_usd": round(supplied_usd, 2),
            "debt_usd": round(debt_usd, 2),
            "equity": round(equity, 2),
            "error": "equity near zero",
        }

    max_debt = round(max_mult * equity, 2)  # frontend formula: max debt before liquidation

    # Raw USDC in account
    raw_usdc = sum(s.get("usd", 0) for s in supplied if s.get("symbol") == "USDC")

    # Position type detection
    symbols = [s.get("symbol", "") for s in supplied]
    has_gmx = sum(s.get("usd", 0) for s in supplied if "GM_" in s.get("symbol", "")) > 1.0
    has_lb = any(
        sym in ("LB_AVAX_USDC", "LB_WAVAX_USDC", "JOE")
        or "TRADERJOE" in sym.upper()
        for sym in symbols
    )
    has_aero = any("AERO" in sym.upper() or "CL_POSITION" in sym.upper() for sym in symbols)

    if max_debt > 0.01 and debt_usd >= 0:
        health_pct = max(0.0, 100.0 * (1.0 - round(debt_usd, 2) / max_debt))
    else:
        health_pct = 100.0

    # Center target (50% health): target_debt = max_debt * 0.5
    delta_debt = (max_debt * 0.5) - debt_usd

    return {
        "health_pct": round(health_pct, 1),
        "health_ratio": round(health_ratio, 4),
        "supplied_usd": round(supplied_usd, 2),
        "debt_usd": round(debt_usd, 2),
        "equity": round(equity, 2),
        "max_debt": round(max_debt, 2),
        "delta_debt": round(delta_debt, 2),
        "raw_usdc": round(raw_usdc, 2),
        "has_gmx": has_gmx,
        "has_lb": has_lb,
        "has_aero": has_aero,
    }


def valuation_complete(defi_data: dict) -> tuple[bool, str]:
    """Gate auto-actions on complete, trustworthy valuation data.

    Returns (ok, reason). Auto-lever/de-lever must NEVER run on incomplete data: a
    missing RedStone feed leaves a position unpriced, so equity/debt/health are wrong
    and a borrow/repay sized from them is dangerous.

    `defi --json` is trimmed (null/empty fields are dropped), so a position whose feed
    was missing comes back as a row WITHOUT a `usd` key, and `solvent: None` is dropped
    entirely. Checks: (a) top-level status == "ok", (b) no solvency_error key, (c)
    solvent is True, (d) every supplied/borrowed row carries a usd value."""
    if defi_data.get("status") != "ok":
        return False, f"status={defi_data.get('status')!r}"
    if "solvency_error" in defi_data:
        return False, f"solvency_error={defi_data['solvency_error']!r}"
    if defi_data.get("solvent") is not True:
        return False, f"solvent={defi_data.get('solvent')!r}"
    groups = defi_data.get("groups", [])
    rows = []
    if groups:
        for g in groups:
            rows.extend(g.get("supplied", []))
            rows.extend(g.get("borrowed", []))
    else:
        rows.extend(defi_data.get("supplied", []))
        rows.extend(defi_data.get("borrowed", []))
    for r in rows:
        if r.get("usd") is None:
            return False, f"unpriced position: {r.get('symbol', '?')}"
    return True, "ok"


# ════════════════════════════════════════════════════════════════════
# Strategy loading
# ════════════════════════════════════════════════════════════════════

def load_strategy(strategy_path: str) -> dict:
    """Load strategy config from JSON file. Returns empty dict if not found."""
    path = Path(strategy_path)
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


# ════════════════════════════════════════════════════════════════════
# State / history helpers
# ════════════════════════════════════════════════════════════════════

def append_history(state_dir: str, entry: dict):
    """Append one health tick to the JSONL history file."""
    path = Path(state_dir) / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    # Trim to last 1000 lines
    lines = path.read_text().strip().split("\n")
    if len(lines) > 1000:
        path.write_text("\n".join(lines[-1000:]) + "\n")


def load_baseline_equity(state_dir: str) -> float | None:
    """Load baseline equity for stop-loss tracking."""
    path = Path(state_dir) / "baseline-equity"
    if path.exists():
        try:
            return float(path.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def save_baseline_equity(state_dir: str, equity: float):
    """Save baseline equity for stop-loss tracking."""
    path = Path(state_dir) / "baseline-equity"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(equity))


def load_last_health(state_dir: str) -> float | None:
    """Load last recorded health pct for swing detection."""
    path = Path(state_dir) / "last-health-pct"
    if path.exists():
        try:
            return float(path.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def save_last_health(state_dir: str, pct: float):
    """Save current health pct."""
    path = Path(state_dir) / "last-health-pct"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pct))


def write_escalation(state_dir: str, reason: str, payload: dict):
    """Write escalation marker for the escalation handler to pick up."""
    path = Path(state_dir) / "escalate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)
    # Also write a cooldown marker
    cooldown_dir = Path(state_dir) / "last-escalation"
    cooldown_dir.mkdir(parents=True, exist_ok=True)
    (cooldown_dir / reason).write_text(str(int(time.time())))


# ════════════════════════════════════════════════════════════════════
# One tick — the core function called by cron
# ════════════════════════════════════════════════════════════════════

def run_tick(
    tool_path: str,
    strategy_path: str,
    state_dir: str,
    label: str = "prime",
    dry_run: bool = False,
) -> dict:
    """Run one health monitoring tick.

    Args:
        tool_path: Path to the primecli Python script (deltaprime.py or degenprime.py).
        strategy_path: Path to strategy.json (rebalance config).
        state_dir: Directory for state files (history, baseline, etc.).
        label: Human label for log messages ("prime", "degen").
        dry_run: If True, don't execute any on-chain actions (just print what would happen).

    Returns:
        Dict with tick result.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    result = {"ts": now_iso, "label": label, "mode": "observer"}

    # 1. Fetch account state
    try:
        raw = subprocess.run(
            [sys.executable, tool_path, "defi", "--json"],
            capture_output=True, text=True, timeout=90,
        )
        if raw.returncode != 0:
            result["error"] = f"defi failed: {raw.stderr[:200]}"
            return result
        defi_data = json.loads(raw.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        result["error"] = f"defi error: {e}"
        return result

    # 2. Determine tier
    try:
        tier_out = subprocess.run(
            [sys.executable, tool_path, "prime-tier"],
            capture_output=True, text=True, timeout=30,
        )
        tier_str = tier_out.stdout
        if "premium" in tier_str.lower():
            max_mult = TIER_MAX.get("premium", 10)
            tier = "premium"
        elif "basic" in tier_str.lower():
            max_mult = TIER_MAX.get("basic", 5)
            tier = "basic"
        else:
            max_mult = TIER_MAX.get("basic", 5)
            tier = "basic"
    except Exception:
        max_mult = 5
        tier = "basic"

    # 3. Compute health
    health = compute_health(defi_data, max_mult)
    health["tier"] = tier
    if health.get("error") == "equity near zero":
        # Only escalate if there's actual debt — an empty unfunded wallet is not an emergency
        if health.get("debt_usd", 0) and health["debt_usd"] > 0.5:
            write_escalation(state_dir, "equity-near-zero", {
                "reason": "equity_near_zero",
                "equity": health["equity"],
                "debt": health["debt_usd"],
                "health_pct": health["health_pct"],
                "label": label,
            })
            result["mode"] = "escalated"
        else:
            result["action"] = "none (unfunded account)"
        result.update(health)
        return result

    # 4. Load strategy (position/market/side are optional hints now — auto-detected from defi_data)
    strategy = load_strategy(strategy_path)
    mode = strategy.get("mode", "observer")
    health["mode"] = mode
    result.update(health)
    result["mode"] = mode

    # 5. Health swing detection (always)
    last_pct = load_last_health(state_dir)
    if last_pct is not None and health["health_pct"] is not None:
        diff = abs(health["health_pct"] - last_pct)
        if diff > 10:
            write_escalation(state_dir, "health-swing", {
                "reason": "health_swing",
                "from_pct": last_pct,
                "to_pct": health["health_pct"],
                "delta": diff,
                "label": label,
            })
            result["escalation"] = "health_swing"
    save_last_health(state_dir, health["health_pct"] or 0.0)

    # 6. Append to history
    entry = {
        "ts": now_iso, "mode": mode,
        "pct": health["health_pct"],
        "equity": health["equity"],
        "debt": health["debt_usd"],
        "hr": health["health_ratio"],
    }
    append_history(state_dir, entry)

    # 7. Rebalance mode logic
    if mode == "rebalance":
        # Valuation gate: never auto-lever/de-lever on incomplete or untrustworthy data
        # (missing RedStone feed → unpriced position → wrong equity/debt/health). Escalate
        # and fall back to observe-only.
        val_ok, val_reason = valuation_complete(defi_data)
        if not val_ok:
            write_escalation(state_dir, "incomplete-valuation", {
                "reason": "incomplete_valuation",
                "detail": val_reason,
                "health_pct": health["health_pct"],
                "equity": health["equity"],
                "debt": health["debt_usd"],
                "label": label,
            })
            result["escalation"] = "incomplete_valuation"
            result["action"] = "observe (incomplete valuation)"
            return result

        target_range = strategy.get("target_range", [30, 70])
        center = strategy.get("center", 50)
        cooldown_secs = strategy.get("cooldown_secs", 3600)
        stop_loss_drawdown = strategy.get("stop_loss_drawdown_pct", 0)
        position_type = strategy.get("position", "")
        market = strategy.get("market", "avax-usdc")
        side = strategy.get("side", "short")
        low, high = target_range[0], target_range[1]

        pct = health["health_pct"]
        equity = health["equity"]
        debt = health["debt_usd"]
        raw_usdc = health.get("raw_usdc", 0)

        # ── Stop-loss: equity drawdown ──────────────────────────────
        if stop_loss_drawdown > 0:
            baseline = load_baseline_equity(state_dir)
            if baseline is None or baseline == 0:
                save_baseline_equity(state_dir, equity)
                result["baseline_recorded"] = equity
            elif equity and baseline > 0:
                drawdown = (1 - equity / baseline) * 100
                if drawdown >= stop_loss_drawdown:
                    write_escalation(state_dir, "stop-loss", {
                        "reason": "stop_loss_equity_drawdown",
                        "drawdown_pct": round(drawdown, 1),
                        "threshold_pct": stop_loss_drawdown,
                        "baseline_equity": baseline,
                        "current_equity": equity,
                        "debt": debt,
                        "health_pct": pct,
                        "label": label,
                        "action": "full_close",
                    })
                    result["escalation"] = "stop_loss"
                    result["action"] = "escalate_close"
                    return result

        # ── In range → no action ────────────────────────────────────
        if low <= pct <= high:
            result["action"] = "none"
            return result

        # ── Hard floor ─────────────────────────────────────────────
        if pct < 20:
            write_escalation(state_dir, "health-floor", {
                "reason": "health_below_floor",
                "pct": pct,
                "equity": equity,
                "debt": debt,
                "label": label,
            })

        # ── Act ─────────────────────────────────────────────────────
        target_debt = max(health["max_debt"], 0) * (1 - center / 100.0)
        delta = target_debt - debt

        if pct < low:
            # De-lever: repay USDC
            repay_amt = abs(delta)
            if repay_amt < 1:
                result["action"] = "none (repay too small)"
                return result

            # Check cooldown (bypass under 20%)
            cooldown_file = Path(state_dir) / f"last-action-delever"
            if pct >= 20 and cooldown_file.exists():
                last_act = int(cooldown_file.read_text().strip())
                if time.time() - last_act < cooldown_secs:
                    result["action"] = "delever (cooldown)"
                    return result

            if repay_amt > raw_usdc:
                # Need to withdraw from position first — escalate
                write_escalation(state_dir, "repay-no-usdc", {
                    "reason": "repay_needs_position_close",
                    "repay_needed": repay_amt,
                    "raw_usdc": raw_usdc,
                    "health_pct": pct,
                    "label": label,
                })
                result["action"] = "escalate (need close)"
                return result

            if dry_run:
                result["action"] = f"would repay ${repay_amt:.2f} USDC"
                return result

            # Execute repay
            try:
                r = subprocess.run(
                    [sys.executable, tool_path, "repay", "--pool", "usdc",
                     "--amount", f"{repay_amt:.2f}", "--execute"],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0:
                    cooldown_file.write_text(str(int(time.time())))
                    result["action"] = f"repaid ${repay_amt:.2f}"
                else:
                    result["error"] = f"repay failed: {r.stderr[:200]}"
            except Exception as e:
                result["error"] = f"repay error: {e}"

        elif pct > high:
            # Lever: borrow + deploy into position
            borrow_amt = delta
            if borrow_amt < 1:
                result["action"] = "none (borrow too small)"
                return result

            cooldown_file = Path(state_dir) / f"last-action-lever"
            if cooldown_file.exists():
                last_act = int(cooldown_file.read_text().strip())
                if time.time() - last_act < cooldown_secs:
                    result["action"] = "lever (cooldown)"
                    return result

            if dry_run:
                result["action"] = f"would borrow ${borrow_amt:.2f} and deploy"
                return result

            # Borrow
            try:
                r = subprocess.run(
                    [sys.executable, tool_path, "borrow", "--pool", "usdc",
                     "--amount", f"{borrow_amt:.2f}", "--execute"],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode != 0:
                    result["error"] = f"borrow failed: {r.stderr[:200]}"
                    return result
            except Exception as e:
                result["error"] = f"borrow error: {e}"
                return result

            # Deploy into whatever positions are open (detected dynamically from defi_data)
            has_gmx = health.get("has_gmx", False)
            has_lb = health.get("has_lb", False)
            has_aero = health.get("has_aero", False)
            open_positions = []
            if has_gmx: open_positions.append("gmx")
            if has_lb:  open_positions.append("lb")
            if has_aero: open_positions.append("aero")

            if not open_positions:
                # No open positions — just borrow and leave as USDC (or deploy to default)
                result["action"] = f"borrowed ${borrow_amt:.2f} (no positions to deploy into)"
                cooldown_file.write_text(str(int(time.time())))
            else:
                # Split borrow amount proportionally across open positions
                split_amt = borrow_amt / len(open_positions)
                deployed_ok = 0
                deployed_fail = 0

                for pos_type in open_positions:
                    if pos_type == "gmx":
                        # Use market/side from strategy as hint, fall back to sensible defaults
                        mkt = strategy.get("market", "avax-usdc") if tool_path else "avax-usdc"
                        sd = strategy.get("side", "long") if tool_path else "long"
                        try:
                            r = subprocess.run(
                                [sys.executable, tool_path, "gmx-deposit",
                                 "--market", mkt, "--amount", f"{split_amt:.2f}",
                                 "--side", sd, "--fee-buffer", "1.5", "--execute"],
                                capture_output=True, text=True, timeout=120,
                            )
                            if r.returncode == 0:
                                deployed_ok += 1
                            else:
                                result["warning"] = f"gmx deposit failed: {r.stderr[:200]}"
                                deployed_fail += 1
                        except Exception as e:
                            result["error"] = f"gmx deposit error: {e}"
                            deployed_fail += 1

                    elif pos_type == "lb":
                        # LB deposits need pair + amount-x + amount-y (not a single amount),
                        # so just leave as USDC for now — manual deployment required.
                        result["action"] = f"lb-add needs pair + dual amounts — leaving ${split_amt:.2f} as USDC"

                    elif pos_type == "aero":
                        result["action"] = f"aero deposit not yet supported by tool — leaving ${split_amt:.2f} as USDC"

                if deployed_ok > 0:
                    cooldown_file.write_text(str(int(time.time())))
                    result["action"] = f"borrowed ${borrow_amt:.2f}, deployed ${split_amt:.2f} to {deployed_ok} position(s)"
                else:
                    result["warning"] = f"borrow ok but all deposits failed" 

    return result


# ════════════════════════════════════════════════════════════════════
# CLI entry point
# ════════════════════════════════════════════════════════════════════

def cli():
    """Entry point for `primecli health ...` subcommand."""
    args = sys.argv[2:] if len(sys.argv) > 2 else []
    # Default state/config paths
    label = os.environ.get("PRIMECLI_LABEL", "prime")
    config_base = os.environ.get(
        "PRIMECLI_CONFIG_DIR",
        os.path.expanduser(f"~/.primecli/{label}/"),
    )
    strategy_path = os.path.join(config_base, "strategy.json")
    state_dir = os.path.join(config_base, "state")

    subcmd = args[0] if args else "status"

    # Resolve the tool path: same dir as the module that imports us
    tool_path = os.environ.get("PRIMECLI_TOOL", "")

    if subcmd == "status":
        # Print current health state
        if not tool_path:
            print("PRIMECLI_TOOL not set. Pass --tool or set env.")
            return
        raw = subprocess.run(
            [sys.executable, tool_path, "defi", "--json"],
            capture_output=True, text=True, timeout=90,
        )
        tier = "basic"  # default
        try:
            t = subprocess.run(
                [sys.executable, tool_path, "prime-tier"],
                capture_output=True, text=True, timeout=30,
            )
            if "premium" in t.stdout.lower():
                tier = "premium"
            elif "basic" in t.stdout.lower():
                tier = "basic"
        except Exception:
            pass
        max_mult = TIER_MAX.get(tier, 10)
        if raw.returncode == 0:
            health = compute_health(json.loads(raw.stdout), max_mult)
            health["tier"] = tier
            print(json.dumps(health, indent=2))
        else:
            print(f"Error: {raw.stderr[:200]}")

    elif subcmd == "strategy":
        # Show / configure strategy
        strategy = load_strategy(strategy_path)
        if strategy:
            print(json.dumps(strategy, indent=2))
        else:
            print(f"No strategy at {strategy_path}")

    elif subcmd == "monitor":
        # Run one tick
        if not tool_path:
            print("PRIMECLI_TOOL not set.")
            return
        result = run_tick(
            tool_path=tool_path,
            strategy_path=strategy_path,
            state_dir=state_dir,
            label=label,
            dry_run="--dry-run" in args,
        )
        print(json.dumps(result, indent=2, default=str))

    else:
        print(f"Unknown health subcommand: {subcmd}")
        print("Usage: primecli health [status|strategy|monitor] [--dry-run]")
