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
    "cooldown_secs": 900,
    "position": "gmx",
    "market": "avax-usdc",
    "side": "short",
    "swap_target": "AERO",
    "max_usdc_utilization_pct": 90.0,
    "max_usdc_borrow_apr_pct": 15.0
  }

  Pool cost thresholds: before lever-up borrow, checks USDC pool utilization
  and borrow APR via pool-info. If utilization >= max_usdc_utilization_pct or
  borrow APR >= max_usdc_borrow_apr_pct, lever-up is postponed (prevents
  borrowing on congested pools). Defaults: 90% utilization, 15% APR.
"""

import json
import os
import re
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

def _extract_position_rows(defi_data: dict) -> tuple[list, list]:
    """Return supplied/borrowed rows from a defi --json payload.

    Staked LPs can be reported as grouped `items` instead of `supplied`, but they
    are still account collateral and must be included in monitor health math.
    """
    supplied = []
    borrowed = []
    groups = defi_data.get("groups", [])
    def _append_items(items):
        supplied_symbols = {str(r.get("symbol", "")) for r in supplied}
        for item in items:
            label = str(item.get("label", ""))
            symbol = str(item.get("symbol", ""))
            if label in supplied_symbols or symbol in supplied_symbols:
                continue
            supplied.append(item)

    if groups:
        for g in groups:
            group_type = str(g.get("type", "")).lower()
            is_lending_group = "lending" in group_type or "leverage" in group_type
            if is_lending_group or (not supplied and not borrowed):
                supplied.extend(g.get("supplied", []))
                borrowed.extend(g.get("borrowed", []))
            _append_items(g.get("items", []))
    else:
        supplied.extend(defi_data.get("supplied", []))
        borrowed.extend(defi_data.get("borrowed", []))
        _append_items(defi_data.get("items", []))
    return supplied, borrowed


def compute_health(
    defi_data: dict,
    max_mult: int = 10,
    per_asset_powers: dict[str, int] | None = None,
) -> dict:
    """Compute health (0-100%) using the cross-margin formula from DeltaPrime docs.

    Cross-margin formula (when per_asset_powers is provided):
      Pr_i  = power_i / (power_i + 1)        # borrowing power ratio per asset
      Cw_i  = supplied_usd_i x Pr_i           # weighted collateral per asset
      Bw_i  = borrowed_usd_i x Pr_i           # weighted borrows per asset
      H     = (SigmaCw + SigmaBw - B) / SigmaCw x 100

    Falls back to the simplified uniform formula when per_asset_powers is None
    (all assets assumed at max_mult borrowing power):
      health_pct = 100 * (1 - debt / (max_mult * equity))

    DIFFERENT from the equity-based "health_pct" in defi --json / prime-summary
    (which uses max_debt = equity * (tier - 1)).
    The on-chain health_ratio (1.0=liquidation) is NOT used here.
    """
    # Parse groups (DeltaPrime format) or flat format (DegenPrime)
    groups = defi_data.get("groups", [])
    if groups:
        g = groups[0]
        health_ratio = defi_data.get("health_ratio", g.get("health_ratio", 0)) or 0
    else:
        health_ratio = defi_data.get("health_ratio", 0) or 0
    supplied, borrowed = _extract_position_rows(defi_data)

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

    # ── Cross-margin formula (per-asset borrowing powers) ─────────────
    if per_asset_powers is not None:
        powers: dict[str, int] = per_asset_powers
        sum_cw = 0.0  # SigmaCw
        sum_bw = 0.0  # SigmaBw
        total_debt = 0.0

        for s in supplied:
            sym = s.get("symbol", "")
            usd_val = s.get("usd", 0) or 0
            p = powers.get(sym, max_mult)
            pr = p / (p + 1)
            sum_cw += usd_val * pr

        for b in borrowed:
            sym = b.get("symbol", "")
            usd_val = b.get("usd", 0) or 0
            p = powers.get(sym, max_mult)
            pr = p / (p + 1)
            sum_bw += usd_val * pr
            total_debt += usd_val

        if sum_cw > 0.01:
            # H = (SigmaCw + SigmaBw - B) / SigmaCw * 100
            health_pct = max(0.0, (sum_cw + sum_bw - total_debt) / sum_cw * 100.0)
        else:
            health_pct = 0.0

        max_debt = round(max_mult * equity, 2)

    # ── Simplified formula fallback (uniform borrowing power) ─────────
    else:
        max_debt = round(max_mult * equity, 2)

        if max_debt > 0.01 and debt_usd >= 0:
            health_pct = max(0.0, 100.0 * (1.0 - round(debt_usd, 2) / max_debt))
        else:
            health_pct = 100.0

    # Common features regardless of formula variant
    raw_usdc = sum(s.get("usd", 0) for s in supplied if s.get("symbol") == "USDC")

    symbols = [s.get("symbol", "") for s in supplied]
    has_gmx = sum(s.get("usd", 0) for s in supplied if "GM_" in s.get("symbol", "")) > 1.0
    has_lb = any(
        sym in ("LB_AVAX_USDC", "LB_WAVAX_USDC", "JOE")
        or "TRADERJOE" in sym.upper()
        for sym in symbols
    )
    has_aero = any("AERO" in sym.upper() or "CL_POSITION" in sym.upper() for sym in symbols)

    # Center target (50% health): target_debt = max_debt * 0.5
    delta_debt = (max_debt * 0.5) - debt_usd

    reported_health = defi_data.get("health_pct")
    if reported_health is not None:
        try:
            reported_pct = float(reported_health)
        except (TypeError, ValueError):
            reported_pct = None
    else:
        reported_pct = None

    # ── Sanity guard against defi computation glitches ─────────────
    # defi --json can transiently return health_pct=0 when RedStone DC
    # resolution hiccups (RPC 429), even though health_ratio shows the
    # position is clearly solvent (>1.05). When the reported number is
    # suspiciously low (<10%) but the protocol says we're safe AND our
    # own equity-based computation says something sane, trust ourselves.
    _local_pct = health_pct
    if reported_pct is not None and reported_pct < 10.0 and health_ratio > 1.05 and equity > 10:
        if _local_pct is not None and _local_pct > 15.0:
            # Keep the locally-computed value; log the discrepancy.
            health_pct = _local_pct
            print(
                f"WARN: defi reported health={reported_pct:g}% but health_ratio={health_ratio} "
                f"and local calc gives {_local_pct:g}% — using local value "
                f"(transient DC-resolution glitch, not actual risk)",
                file=sys.stderr,
            )
        else:
            # Local calc also looks bad — trust defi, let escalation fire.
            # Use the lower (more conservative) value of the two.
            health_pct = min(reported_pct, _local_pct) if _local_pct is not None else reported_pct
    else:
        health_pct = reported_pct if reported_pct is not None else health_pct

    if health_pct is not None and 0 <= health_pct < 100 and debt_usd > 0:
        max_debt = round(debt_usd / (1.0 - health_pct / 100.0), 2)
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
    supplied, borrowed = _extract_position_rows(defi_data)
    unpriced_supplied = [r for r in supplied if r.get("usd") is None]
    for r in borrowed:
        if r.get("usd") is None:
            return False, f"unpriced position: {r.get('symbol', '?')}"
    if unpriced_supplied:
        total_usd = defi_data.get("total_usd")
        priced_supplied = sum(r.get("usd", 0) or 0 for r in supplied)
        if total_usd is None:
            return False, f"unpriced position: {unpriced_supplied[0].get('symbol', '?')}"
        residual = float(total_usd) - float(priced_supplied)
        tolerance = max(1.0, abs(float(total_usd)) * 0.005)
        if residual > tolerance:
            return False, f"unpriced position: {unpriced_supplied[0].get('symbol', '?')}"
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


NOTIFY_SCRIPT = os.environ.get(
    "PRIMECLI_NOTIFY_SCRIPT",
    os.path.expanduser("/root/.openclaw/workspace/scripts/notify.sh"),
)


def _notify(text: str):
    """Send a Telegram notification via the notify.sh script."""
    if not os.path.exists(NOTIFY_SCRIPT):
        return
    try:
        subprocess.run(
            ["bash", NOTIFY_SCRIPT, text],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def _rate_limited_notify(state_dir: str, reason: str, text: str, cooldown_secs: int = 21600):
    """Send a low-severity notify at most once per cooldown window (default 6h) per reason.

    Used for soft, recurring conditions (e.g. a lever-up that would strand) so a 5-min
    cron doesn't spam Telegram every tick. Not an escalation — no escalate.json is written.
    """
    marker = Path(state_dir) / "last-notify" / reason
    try:
        if marker.exists():
            last = int(marker.read_text().strip())
            if time.time() - last < cooldown_secs:
                return
    except (ValueError, OSError):
        pass
    _notify(text)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(time.time())))
    except OSError:
        pass


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


def load_last_equity(state_dir: str) -> float | None:
    """Load last recorded equity (USD), used to sanity-check a health swing against an
    implausible equity jump — a partial misprice can pass the unpriced gate yet inflate or
    deflate equity, which would otherwise fire a spurious health_swing escalation."""
    path = Path(state_dir) / "last-equity-usd"
    if path.exists():
        try:
            return float(path.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def save_last_equity(state_dir: str, equity: float):
    """Save current equity (USD) for swing-plausibility checks."""
    path = Path(state_dir) / "last-equity-usd"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(equity))


def load_health_swing_streak(state_dir: str) -> int:
    """Consecutive trustworthy ticks a health swing vs the frozen pre-swing baseline
    has held. Escalation requires >=2 so a single-tick misprice, or an in-progress
    multi-step autofarm converge that just hasn't settled yet, can't trigger it."""
    path = Path(state_dir) / "health-swing-streak"
    if path.exists():
        try:
            return int(path.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def save_health_swing_streak(state_dir: str, n: int):
    path = Path(state_dir) / "health-swing-streak"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(n))


def load_stop_loss_streak(state_dir: str) -> int:
    """Consecutive trusted ticks the stop-loss drawdown has held. A full close requires
    >=2 so a single-tick misprice that deflates equity can't trigger it."""
    path = Path(state_dir) / "stop-loss-streak"
    if path.exists():
        try:
            return int(path.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def save_stop_loss_streak(state_dir: str, n: int):
    path = Path(state_dir) / "stop-loss-streak"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(n))



def _usdc_borrow_feasible(tool_path: str, max_util: float = 90.0, max_apr: float = 15.0):
    """Check if borrowing USDC is feasible given pool conditions.

    Returns (feasible: bool, reason: str).
    Uses the protocol tool's pool-info command.
    """
    try:
        r = subprocess.run(
            [sys.executable, tool_path, "pool-info", "usdc", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return False, f"pool-info rc={r.returncode} (cannot confirm pool safe; postponing lever)"
        data = json.loads(r.stdout)
    except Exception as e:
        return False, f"pool-info failed: {e} (postponing lever)"

    util = data.get("utilization", 0)
    apr = data.get("borrowingRate", 0)

    if util >= max_util:
        return False, f"USDC pool at {util:.1f}% utilization (max {max_util:.0f}%)"
    if apr >= max_apr:
        return False, f"USDC borrow APR {apr:.2f}% exceeds max {max_apr:.0f}%"
    return True, ""


def write_escalation(state_dir: str, reason: str, payload: dict, cooldown_secs: int = 1200) -> bool:
    """Write escalation marker for the escalation handler to pick up.

    Debounced per (state_dir, reason): if the last escalation of this reason fired
    within cooldown_secs, this is a no-op (returns False, no escalate.json written,
    no fresh isolated agent spawned). Without this, a genuine multi-tick swing (e.g.
    the health readings during an in-progress autofarm converge) re-triggers the
    generic close-everything/repay-USDC-only/redeploy playbook on every 5-min tick —
    confirmed 2026-07-03: 4 health-swing agents spawned for parakletos-4 in 15 minutes
    during a converge, racing each other and the autofarm cron, which flattened the
    position's intended 50/50 USDC/ETH debt split down to ~all-USDC. The cooldown
    marker file already existed for this but was never read anywhere until now.
    """
    cooldown_dir = Path(state_dir) / "last-escalation"
    cooldown_dir.mkdir(parents=True, exist_ok=True)
    marker = cooldown_dir / reason
    if marker.exists():
        try:
            last = int(marker.read_text().strip())
            if time.time() - last < cooldown_secs:
                return False
        except (ValueError, OSError):
            pass
    path = Path(state_dir) / "escalate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)
    marker.write_text(str(int(time.time())))
    return True


# ════════════════════════════════════════════════════════════════════
# LP de-lever: close LP positions to free assets for repayment
# ════════════════════════════════════════════════════════════════════


def _gather_supply_rows(defi_data: dict) -> list:
    """Extract flat list of supplied rows from defi data, covering both flat-supplied and grouped."""
    rows = []
    groups = defi_data.get("groups", [])
    if groups:
        for g in groups:
            rows.extend(g.get("supplied", []))
    else:
        rows.extend(defi_data.get("supplied", []))
    return rows


def _gather_borrow_rows(defi_data: dict) -> list:
    """Extract flat list of borrowed rows from defi data, covering both flat-borrowed and grouped."""
    rows = []
    groups = defi_data.get("groups", [])
    if groups:
        for g in groups:
            rows.extend(g.get("borrowed", []))
    else:
        rows.extend(defi_data.get("borrowed", []))
    return rows


def _token_amount(row: dict) -> float:
    """Best-effort token amount for a supplied/borrowed row.

    In this tool's trimmed `defi --json`, the token amount lands in the `balance`
    field (a STRING like "0.123456") while `amount` is often null/absent. Some
    shapes carry a float `amount` instead, and synthetic LP rows carry a non-numeric
    `balance` like "$1,234.00 (estimated ...)". Prefer a clean numeric `balance`,
    fall back to a numeric `amount`, else 0.0 (never raises)."""
    bal = row.get("balance")
    if bal is not None:
        try:
            return float(bal)
        except (TypeError, ValueError):
            pass  # non-numeric (e.g. synthetic "$... (estimated)") — fall through
    amt = row.get("amount")
    if amt is not None:
        try:
            return float(amt)
        except (TypeError, ValueError):
            pass
    return 0.0


# Lending-pool name for a supplied/borrowed asset symbol. The contracts label
# wrapped-native as "ETH"/"WETH" but the repay/borrow pool is `weth`; everything
# else lowercases to its pool name. Returns None for symbols that aren't a
# borrowable pool (so we never try to repay a non-pool leg).
_SYMBOL_TO_POOL = {
    "ETH": "weth", "WETH": "weth", "WAVAX": "wavax", "AVAX": "wavax",
    "USDC": "usdc", "CBBTC": "cbbtc", "WBTC": "wbtc", "BTCB": "btcb",
    "AERO": "aero", "ARB": "arb", "BRETT": "brett", "KAITO": "kaito",
    "CBDOGE": "cbdoge", "CBXRP": "cbxrp", "VIRTUAL": "virtual",
}


def _symbol_to_pool(symbol: str) -> str | None:
    """Map an asset symbol to its lowercase lending-pool name, or None if not a pool."""
    if not symbol:
        return None
    return _SYMBOL_TO_POOL.get(symbol.upper(), symbol.lower())


def _plan_token_repays(
    freed_rows: list,
    borrowed_rows: list,
    remaining_repay_usd: float,
    usdc_symbol: str = "USDC",
) -> tuple[list, float, float]:
    """Pass-1 allocation: repay debt legs directly with matching freed tokens (NO swap).

    Pure / side-effect-free so it can be unit-tested. For each borrowed leg whose
    asset is also a freed supplied token, plan a direct repay of
        min(freed_token_balance, outstanding_leg_token_balance, repay_need_in_that_asset)
    in TOKEN units. Repaying a debt leg with that exact asset can never strand the
    position (no price exposure), so we do it first and unconditionally.

    Args:
        freed_rows: supplied rows now holding the freed LP legs (symbol/balance/usd).
        borrowed_rows: outstanding debt rows (symbol/balance/usd).
        remaining_repay_usd: total USD of debt we still want to retire this tick.
        usdc_symbol: stable symbol (repaid in pass 2 via swap, not here unless freed).

    Returns:
        (repays, remaining_after_usd, usdc_shortfall_usd) where
          repays = [{"pool": str, "symbol": str, "amount": float, "usd": float}, ...]
          remaining_after_usd = repay need still outstanding after pass-1 (USD)
          usdc_shortfall_usd  = portion of that remainder owed on the USDC pool (drives the pass-2 swap)
    """
    # Index freed token balances by uppercased symbol.
    freed_by_sym: dict[str, dict] = {}
    for r in freed_rows:
        sym = str(r.get("symbol", "")).upper()
        if not sym:
            continue
        amt = _token_amount(r)
        usd = r.get("usd", 0) or 0
        if amt <= 0 or usd <= 0:
            continue
        # Per-token USD price for converting a USD repay-need into token units.
        price = (usd / amt) if amt > 0 else 0.0
        prev = freed_by_sym.get(sym)
        if prev:
            prev["amount"] += amt
            prev["usd"] += usd
            prev["price"] = (prev["usd"] / prev["amount"]) if prev["amount"] > 0 else price
        else:
            freed_by_sym[sym] = {"amount": amt, "usd": usd, "price": price}

    repays: list = []
    remaining = max(0.0, remaining_repay_usd)
    usdc_shortfall = 0.0

    for b in borrowed_rows:
        if remaining < 1.0:
            break
        sym = str(b.get("symbol", "")).upper()
        pool = _symbol_to_pool(sym)
        if not pool:
            continue
        leg_usd = b.get("usd", 0) or 0
        if leg_usd <= 0:
            continue
        leg_tok = _token_amount(b)
        freed = freed_by_sym.get(sym)
        if not freed or freed["amount"] <= 0:
            # No matching freed token — if this is USDC debt, it feeds the pass-2 swap.
            # USD value alone is enough here; a debt row may carry usd without a token
            # amount (and must NOT be skipped, or the de-lever silently does nothing).
            if sym == usdc_symbol.upper():
                usdc_shortfall += min(leg_usd, remaining)
            continue
        price = freed["price"] or (leg_usd / leg_tok if leg_tok > 0 else 0.0)
        if price <= 0:
            continue
        # Outstanding leg in token units — derive it from USD when the debt row omits a
        # token amount (price comes from the matching freed token).
        if leg_tok <= 0:
            leg_tok = leg_usd / price
        # How many tokens does the remaining USD repay-need correspond to for this asset?
        need_tok = remaining / price
        repay_tok = min(freed["amount"], leg_tok, need_tok)
        if repay_tok <= 0:
            continue
        repay_usd = repay_tok * price
        if repay_usd < 0.50:
            continue
        repays.append({
            "pool": pool, "symbol": sym,
            "amount": round(repay_tok, 8), "usd": round(repay_usd, 2),
        })
        # Consume freed balance and shrink the remaining need.
        freed["amount"] -= repay_tok
        freed["usd"] = max(0.0, freed["usd"] - repay_usd)
        remaining = max(0.0, remaining - repay_usd)
        # If this leg was USDC, any still-unmet USDC remainder is a pass-2 swap target.
        if sym == usdc_symbol.upper():
            leftover_leg = leg_usd - repay_usd
            if leftover_leg > 0:
                usdc_shortfall += min(leftover_leg, remaining)

    return repays, round(remaining, 2), round(usdc_shortfall, 2)


def _delever_lp_positions(
    defi_data: dict,
    tool_path: str,
    shortfall_usd: float,
    strategy: dict,
    state_dir: str,
    label: str,
    health_pct: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Close LP positions to free assets for USDC debt repayment.

    After raw-supplied swap candidates are exhausted, try reducing LP size.
    Supports:
      - GMX V2 GM/GM+ (deltaprime): partial `gmx-withdraw`, freeing the shortfall value
      - Aerodrome CL (degenprime): full `aero-remove-liquidity` per NFT
      - TraderJoe LB (deltaprime): full `lb-remove` per pair

    When dry_run=True this is 100% read-only: it broadcasts NOTHING (no `--execute`
    subprocess of any kind) and instead returns an estimate of what it WOULD free,
    e.g. {"ok": True, "dry_run": True, "freed": 123.45,
          "detail": "would close Aerodrome tokenId 7 (~$123.45)"}.

    Returns a dict:
      {"ok": True, "freed": 123.45, "detail": "closed GMX avax-usdc"}  on success
      {"ok": False, "detail": "no LP positions found"}               if nothing to do
      {"ok": False, "error": "gmx-withdraw failed: ..."}            on failure
    """
    groups = defi_data.get("groups", [])
    if not groups:
        return {"ok": False, "detail": "no groups in defi data"}

    result = {"ok": False, "detail": "no LP positions found", "freed": 0.0}

    for g in groups:
        gtype = g.get("type", "")
        chain = defi_data.get("chain", "")

        # ── GMX V2 GM/GM+ (DeltaPrime / Avalanche) ────────────────
        if gtype == "GMX V2 LP":
            items = g.get("items", [])
            for item in items:
                market_label = item.get("label", "")
                gm_balance = item.get("balance", 0)
                item_usd = item.get("usd", 0) or 0
                if not market_label or not gm_balance or float(gm_balance) <= 0:
                    continue

                # Calculate how many GM tokens to withdraw to cover shortfall
                gm_bal_float = float(gm_balance)
                need_ratio = shortfall_usd / item_usd if item_usd > 0 else 1.0
                gm_to_withdraw = min(gm_bal_float, need_ratio * gm_bal_float)
                if gm_to_withdraw < 0.001:
                    continue

                if dry_run:
                    est = item_usd * (gm_to_withdraw / gm_bal_float) if gm_bal_float > 0 else 0.0
                    detail = f"would withdraw {gm_to_withdraw:.4f} GM from {market_label} (~${est:.2f}, keeper-async)"
                    return {"ok": True, "dry_run": True, "async": True, "freed": round(est, 2), "detail": detail}

                # GMX withdraw is ASYNC (keeper-executed). Assets don't arrive
                # on this tick. Record a pending marker and return async=True
                # so the caller doesn't re-fetch+repay immediately.
                try:
                    r = subprocess.run(
                        [sys.executable, tool_path, "gmx-withdraw",
                         "--market", market_label,
                         "--amount", f"{gm_to_withdraw:.4f}",
                         "--slippage", "1", "--fee-buffer", "2", "--execute"],
                        capture_output=True, text=True, timeout=300,
                    )
                    if r.returncode == 0:
                        # Record pending marker
                        pfx = Path(state_dir)
                        pfx.mkdir(parents=True, exist_ok=True)
                        _pending_gmx_marker_path(state_dir).write_text(json.dumps({
                            "market": market_label,
                            "gm_amount": gm_to_withdraw,
                            "gm_before": gm_bal_float,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "label": label,
                        }))
                        detail = f"GMX {market_label}: withdraw {gm_to_withdraw:.4f} GM submitted (keeper pending)"
                        result = {"ok": True, "async": True, "freed": 0.0, "detail": detail}
                        _notify(f"🔄 ⚠️ De-lever on {label}: health low — withdrawing GMX position to free collateral. {detail}")
                    else:
                        err = r.stderr[:300]
                        result = {"ok": False, "error": f"gmx-withdraw failed: {err}"}
                        write_escalation(state_dir, "gmx-withdraw-failed", {
                            "reason": "gmx_withdraw_failed",
                            "market": market_label, "amount_gm": gm_to_withdraw,
                            "stderr": err, "label": label,
                        })
                except Exception as e:
                    result = {"ok": False, "error": f"gmx-withdraw exception: {e}"}
                if result.get("ok"):
                    return result

        # ── Aerodrome CL (DegenPrime / Base) ──────────────────────────
        elif gtype == "Aerodrome" or gtype == "Aerodrome LP":
            items = g.get("items", [])
            for item in items:
                token_id = item.get("token_id", None)
                item_usd = item.get("usd", 0) or 0
                if token_id is None:
                    continue

                if dry_run:
                    detail = f"would close Aerodrome tokenId {token_id} (~${item_usd:.2f})"
                    return {"ok": True, "dry_run": True, "freed": round(item_usd, 2), "detail": detail}

                try:
                    r = subprocess.run(
                        [sys.executable, tool_path, "aero-remove-liquidity",
                         "--token-id", str(token_id), "--execute"],
                        capture_output=True, text=True, timeout=300,
                    )
                    if r.returncode == 0:
                        detail = f"closed Aerodrome tokenId {token_id}"
                        result = {"ok": True, "freed": item_usd, "detail": detail}
                        _notify(f"🔄 ⚠️ De-lever on {label}: health was {health_pct}% — closing Aerodrome LP position #{token_id} (${item_usd:.2f} worth of collateral)")
                    else:
                        err = r.stderr[:300]
                        result = {"ok": False, "error": f"aero-remove-liquidity failed: {err}"}
                        write_escalation(state_dir, "aero-remove-failed", {
                            "reason": "aero_remove_failed",
                            "token_id": token_id, "stderr": err, "label": label,
                        })
                except Exception as e:
                    result = {"ok": False, "error": f"aero-remove-liquidity exception: {e}"}
                # One NFT per tick (close the biggest first, autofarm re-deploys)
                if result["ok"]:
                    return result

        # ── TraderJoe LB (DeltaPrime / Avalanche) ────────────────────
        elif gtype == "TraderJoe V2 LB":
            items = g.get("items", [])
            if not items:
                continue
            # Gather unique pairs from LB items
            pairs = set()
            for item in items:
                pair_hint = item.get("label", "")
                if pair_hint:
                    # Normalise: remove trailing bin-range like "-5" or "-10"
                    for known in ["avax-usdc", "wbtc-usdc", "weth-usdc"]:
                        if known in pair_hint:
                            pairs.add(known)
                            break
                # Also try from token_x/token_y
                tx = item.get("token_x", {}).get("symbol", "")
                ty = item.get("token_y", {}).get("symbol", "")
                if tx and ty:
                    guess = f"{tx}-{ty}".lower()
                    if "avax-usdc" in guess or "wbtc-usdc" in guess or "weth-usdc" in guess:
                        pairs.add(guess)

            for pair in pairs:
                if dry_run:
                    # LB items don't carry a reliable per-pair usd; sum the group.
                    grp_usd = sum((it.get("usd", 0) or 0) for it in items)
                    detail = f"would close LB {pair} (~${grp_usd:.2f})"
                    return {"ok": True, "dry_run": True, "freed": round(grp_usd, 2), "detail": detail}

                try:
                    r = subprocess.run(
                        [sys.executable, tool_path, "lb-remove",
                         "--pair", pair, "--slippage", "1", "--execute"],
                        capture_output=True, text=True, timeout=300,
                    )
                    if r.returncode == 0:
                        detail = f"closed LB {pair}"
                        result = {"ok": True, "freed": 0.0, "detail": detail}
                        _notify(f"🔄 ⚠️ De-lever on {label}: health was {health_pct}% — closing TraderJoe LB position ({pair})")
                    else:
                        err = r.stderr[:300]
                        result = {"ok": False, "error": f"lb-remove failed: {err}"}
                        write_escalation(state_dir, "lb-remove-failed", {
                            "reason": "lb_remove_failed", "pair": pair,
                            "stderr": err, "label": label,
                        })
                except Exception as e:
                    result = {"ok": False, "error": f"lb-remove exception: {e}"}
                # One pair per tick
                if result["ok"]:
                    return result

    return result


# ════════════════════════════════════════════════════════════════════
# Pending GMX de-lever: async-withdrawal state tracking
# ════════════════════════════════════════════════════════════════════

_PENDING_GMX_FILE = "pending-delever-gmx.json"
_GMX_PENDING_MAX_AGE = 3600  # 1 hour — escalate after this


def _pending_gmx_marker_path(state_dir: str) -> Path:
    return Path(state_dir) / _PENDING_GMX_FILE


def _check_pending_gmx_delever(state_dir: str, defi_data: dict, label: str) -> dict:
    """Check if a previously-submitted GMX async withdrawal has settled.

    Returns:
      {"settled": True, "detail": "..."}  — GM position gone / reduced, proceed with repay
      {"pending": True, "detail": "..."}   — still waiting for keeper, skip this tick
      {"none": True}                         — no pending marker
      {"escalate": True, "detail": "..."}  — marker too old, keeper may have failed
    """
    marker_path = _pending_gmx_marker_path(state_dir)
    if not marker_path.exists():
        return {"none": True}

    try:
        marker = json.loads(marker_path.read_text())
    except (json.JSONDecodeError, OSError):
        marker_path.unlink(missing_ok=True)
        return {"none": True}

    market = marker.get("market", "")
    gm_before = marker.get("gm_amount", 0)
    ts = marker.get("ts", "")
    label_from_marker = marker.get("label", label)

    # Check current GMX position size from defi_data
    gm_now = 0.0
    for g in defi_data.get("groups", []):
        if g.get("type") == "GMX V2 LP":
            for item in g.get("items", []):
                if item.get("label") == market:
                    gm_now = float(item.get("balance", 0) or 0)
                    break

    settled = gm_now < gm_before * 0.95  # dropped by at least 5%

    # Check age
    age = 0
    try:
        age = time.time() - datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        age = _GMX_PENDING_MAX_AGE + 1  # stale timestamp → escalate

    if settled:
        lvl = "partial" if gm_now > 0 else "full"
        detail = f"{market}: GM {gm_before:.2f} -> {gm_now:.2f} ({lvl} close)"
        marker_path.unlink(missing_ok=True)
        _notify(f"✅ GMX de-lever on {label}: withdraw complete — {detail}")
        return {"settled": True, "detail": detail}

    if age > _GMX_PENDING_MAX_AGE:
        # Keeper may have dropped it — escalate
        write_escalation(state_dir, "pending-gmx-stale", {
            "reason": "pending_gmx_withdraw_stale",
            "market": market, "gm_requested": gm_before,
            "age_secs": int(age), "max_age": _GMX_PENDING_MAX_AGE,
            "label": label,
        })
        marker_path.unlink(missing_ok=True)
        return {"escalate": True, "detail": f"GMX withdraw stale after {int(age)}s"}

    return {"pending": True, "detail": f"GMX {market} waiting for keeper ({int(age)}s)"}


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

    # 4. Load strategy (do this early so rebalance mode is known before equity checks)
    strategy = load_strategy(strategy_path)
    mode = strategy.get("mode", "observer")
    health["mode"] = mode
    result.update(health)
    result["mode"] = mode

    # 5. Check for unfunded or unpriced accounts
    if health.get("error") == "equity near zero":
        # Check if there are actual token balances without USD prices (RedStone off?)
        has_balances = False
        groups = defi_data.get("groups", [])
        if groups:
            for g in groups:
                for s in g.get("supplied", []):
                    bal = s.get("balance", 0)
                    try:
                        if float(bal) > 0:
                            has_balances = True
                            break
                    except (ValueError, TypeError):
                        pass
                if has_balances:
                    break
        else:
            for s in defi_data.get("supplied", []):
                bal = s.get("balance", 0)
                try:
                    if float(bal) > 0:
                        has_balances = True
                        break
                except (ValueError, TypeError):
                    pass

        if has_balances:
            # Positions exist but USD prices unavailable — skip this tick
            result["action"] = "skip (unpriced positions)"
            return result

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
        return result

    # 6. Health swing detection — only on a TRUSTWORTHY read.
    # An unpriced read returns early above, but a PARTIAL misprice (one wrong feed) passes
    # that gate while inflating/deflating equity, yielding a bogus health number. Acting on
    # it fired a spurious health_swing escalation whose agent destructively CLOSED core1's
    # AERO/cbBTC LP on a false 53%→93%→53% swing during a RedStone flap (2026-06-26). So
    # suppress the escalation when the valuation is incomplete OR the equity jumped
    # implausibly vs the previous reading (a real LP's equity does not multiply between
    # 5-min ticks — that's a misprice or a deposit, neither of which is a liquidation risk).
    #
    # That plausibility gate alone wasn't enough: a multi-step autofarm converge (remove
    # LP, borrow, swap, mint) causes several genuinely large tick-to-tick swings while it
    # executes — not misreads, real transient values — and each one passed the gate.
    # Confirmed 2026-07-03: parakletos-4's lever-up converge produced five different >10pp
    # readings in a row (61.3/73.4/84.3/100.0/65.8), each re-triggering because the
    # previous (already-anomalous) tick became the next comparison baseline — 4 escalation
    # agents spawned in 15 minutes, racing each other and the autofarm cron on the same
    # live position. So — mirroring the stop-loss streak pattern below (Bruno, 2026-06-26)
    # — a swing must hold for 2 consecutive trustworthy ticks against the SAME frozen
    # pre-swing baseline before it escalates. The baseline is frozen (not advanced to the
    # anomalous reading) while a swing is unconfirmed, so a converge that settles within
    # one tick self-resolves with zero escalations; a genuinely sustained change confirms
    # and fires within ~2 ticks, same as today's already-approved stop-loss behavior.
    val_ok, _swing_val_reason = valuation_complete(defi_data)
    last_pct = load_last_health(state_dir)
    last_eq = load_last_equity(state_dir)
    cur_pct = health["health_pct"]
    cur_eq = health.get("equity")
    eq_plausible = (
        last_eq is not None and cur_eq is not None and last_eq > 0 and cur_eq > 0
        and 0.5 <= (cur_eq / last_eq) <= 2.0
    )
    trustworthy = val_ok and (last_eq is None or eq_plausible)
    advance_pct_baseline = True
    if trustworthy:
        if last_pct is not None and cur_pct is not None:
            # Only a DROP is a liquidation-risk signal worth an escalation + intervention
            # agent. A large upward swing (e.g. a repay that just fixed itself on retry)
            # is good news, not danger -- escalating it anyway spawns the same "close
            # everything, rebalance to ~50%, redeploy" playbook a real crash would trigger,
            # against an account that isn't actually in trouble. Confirmed 2026-07-05
            # (parakletos-2): a 29.5%->55.5% recovery swing escalated exactly like a crash
            # would have. Treat an improving health reading like "no swing".
            diff = last_pct - cur_pct
            if diff > 10:
                streak = load_health_swing_streak(state_dir) + 1
                save_health_swing_streak(state_dir, streak)
                if streak >= 2:
                    escalated = write_escalation(state_dir, "health-swing", {
                        "reason": "health_swing",
                        "from_pct": last_pct,
                        "to_pct": cur_pct,
                        "delta": diff,
                        "label": label,
                        "confirmations": streak,
                    })
                    result["escalation"] = "health_swing" if escalated else "health_swing (cooldown, suppressed)"
                    save_health_swing_streak(state_dir, 0)
                else:
                    result["action"] = f"health_swing pending confirmation ({streak}/2)"
                    # Freeze the baseline: keep comparing the NEXT tick against this same
                    # pre-swing last_pct instead of advancing to the anomalous cur_pct.
                    advance_pct_baseline = False
            else:
                save_health_swing_streak(state_dir, 0)
    else:
        result["swing_guard"] = (
            "incomplete_valuation" if not val_ok else "implausible_equity_jump"
        )
    # Track latest readings regardless (consecutive-tick basis), so a transient misprice
    # can't permanently poison the baseline and a real deposit only skips a single tick.
    # Exception: an unconfirmed pending swing (see above) intentionally does NOT advance
    # the health-pct baseline, so the next tick still confirms/resolves against the same
    # pre-swing reference rather than the anomalous one.
    if cur_pct is not None and advance_pct_baseline:
        save_last_health(state_dir, cur_pct)
    if cur_eq is not None:
        save_last_equity(state_dir, cur_eq)

    # 7. Append to history
    entry = {
        "ts": now_iso, "mode": mode,
        "pct": health["health_pct"],
        "equity": health["equity"],
        "debt": health["debt_usd"],
        "hr": health["health_ratio"],
    }
    append_history(state_dir, entry)

    # 8. Rebalance mode logic
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
        swap_target = strategy.get("swap_target", "")
        lever_up_enabled = bool(strategy.get("lever_up_enabled", True))
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
                    # Confirm over 2 consecutive TRUSTED reads before a full close. A single
                    # misprice that deflates equity (passes the unpriced gate but reads
                    # implausibly low) must never trigger a stop-loss close on its own — a
                    # plain plausibility block would risk suppressing a real crash, so we
                    # require persistence instead: a one-tick deflation reverts and resets
                    # the streak; a genuine drawdown holds and closes within ~2 ticks.
                    # (Bruno, 2026-06-26.) `trustworthy` comes from the swing-guard above.
                    if trustworthy:
                        sl_streak = load_stop_loss_streak(state_dir) + 1
                        save_stop_loss_streak(state_dir, sl_streak)
                        if sl_streak >= 2:
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
                                "confirmations": sl_streak,
                            })
                            result["escalation"] = "stop_loss"
                            result["action"] = "escalate_close"
                            return result
                        result["action"] = f"stop_loss pending confirmation ({sl_streak}/2)"
                        return result
                    else:
                        result["action"] = "stop_loss deferred (untrusted read)"
                        return result
                else:
                    save_stop_loss_streak(state_dir, 0)

# ── Act ─────────────────────────────────────────────────────
        target_debt = max(health["max_debt"], 0) * (1 - center / 100.0)
        delta = target_debt - debt

        # ── In range → no action ──────────────────────────────────────
        if low <= pct <= high:
            result["action"] = "none"
            return result

# ── De-lever ───────────────────────────────────────────────────
        # ── Hard floor: escalate immediately if critically low ──────────
        if pct is not None and pct < 10:
            write_escalation(state_dir, "health-critical", {
                "reason": "health_below_10_percent",
                "pct": pct, "equity": equity, "debt": debt,
                "label": label,
            })
            result["action"] = "escalate (critical health)"
            return result

        if pct < low:
            repay_amt = abs(delta)
            if repay_amt < 1:
                result["action"] = "none (repay too small)"
                return result

            # Check for pending GMX async withdrawal from a previous tick
            pending_check = _check_pending_gmx_delever(state_dir, defi_data, label)
            if pending_check.get("settled"):
                # GMX withdraw completed — assets should be in supplied now.
                # Re-fetch defi and proceed with swap+repay.
                result["pending_gmx_settled"] = pending_check["detail"]
                try:
                    raw_refetch = subprocess.run(
                        [sys.executable, tool_path, "defi", "--json"],
                        capture_output=True, text=True, timeout=90,
                    )
                    if raw_refetch.returncode == 0:
                        defi_data = json.loads(raw_refetch.stdout)
                        raw_usdc = sum(
                            (s.get("usd", 0) or 0) if s.get("symbol", "").upper() == "USDC" else 0
                            for s in _gather_supply_rows(defi_data)
                        )
                except Exception as e:
                    result["warning"] = f"post-GMX re-fetch failed: {e}"
                # Fall through to swap+repay logic below
            elif pending_check.get("pending"):
                result["action"] = f"delever (pending: {pending_check['detail']})"
                return result
            elif pending_check.get("escalate"):
                result["action"] = f"delever (stale: {pending_check['detail']})"
                return result

            cooldown_file = Path(state_dir) / "last-action-delever"
            if pct >= 20 and cooldown_file.exists():
                last_act = int(cooldown_file.read_text().strip())
                if time.time() - last_act < cooldown_secs:
                    result["action"] = "delever (cooldown)"
                    return result

            if repay_amt > raw_usdc:
                # Build supply_rows to find a swap source for the shortfall
                supply_rows = _gather_supply_rows(defi_data)

                # Find swappable non-USDC supplied assets with value
                swap_candidates = []
                for s in supply_rows:
                    sym = s.get("symbol", "")
                    usd_val = s.get("usd", 0) or 0
                    raw_amt = float(s.get("amount", s.get("balance", 0)) or 0)
                    if sym.upper() == "USDC" or usd_val < 1 or raw_amt <= 0:
                        continue
                    swap_candidates.append((sym, usd_val, raw_amt))
                swap_candidates.sort(key=lambda x: x[1], reverse=True)

                swap_source = None
                swap_amt_usd = 0.0
                need_usd = repay_amt - raw_usdc
                # Prefer swap_target if set, otherwise largest supplied asset
                if swap_target:
                    for sym, usd_val, raw_amt in swap_candidates:
                        if sym.upper() == swap_target.upper():
                            swap_source = sym
                            swap_amt_usd = min(usd_val * 0.95, need_usd)
                            break
                if swap_source is None and swap_candidates:
                    swap_source = swap_candidates[0][0]
                    swap_amt_usd = min(swap_candidates[0][1] * 0.95, need_usd)

                if swap_source and swap_amt_usd >= 0.50 and dry_run:
                    # Dry-run must broadcast NOTHING — report the swap+repay it WOULD do.
                    result["action"] = (
                        f"would swap ${swap_amt_usd:.2f} {swap_source} -> USDC, "
                        f"then repay ${repay_amt:.2f} USDC"
                    )
                    return result

                if swap_source and swap_amt_usd >= 0.50:
                    try:
                        sr = subprocess.run(
                            [sys.executable, tool_path, "swap",
                             "--from", swap_source, "--to", "USDC",
                             "--amount", f"{swap_amt_usd:.2f}",
                             "--slippage", "1.0", "--execute"],
                            capture_output=True, text=True, timeout=180,
                        )
                        if sr.returncode == 0:
                            result["swap"] = f"swapped ${swap_amt_usd:.2f} {swap_source} -> USDC"
                        else:
                            write_escalation(state_dir, "repay-swap-failed", {
                                "reason": "repay_swap_failed",
                                "swap_source": swap_source, "swap_amount_usd": swap_amt_usd,
                                "stderr": sr.stderr[:200], "health_pct": pct, "label": label,
                            })
                            result["error"] = f"swap failed: {sr.stderr[:200]}"
                            result["action"] = "escalate (swap failed)"
                            return result
                    except Exception as e:
                        result["error"] = f"swap error: {e}"
                        return result
                else:
                    # No swappable raw assets — try closing LP positions.
                    lp_shortfall = repay_amt - raw_usdc
                    lp_result = _delever_lp_positions(
                        defi_data, tool_path, lp_shortfall,
                        strategy, state_dir, label,
                        health_pct=pct, dry_run=dry_run,
                    )
                    # DRY-RUN: _delever_lp_positions broadcast NOTHING. Report what it
                    # WOULD have closed/repaid and stop — never touch the chain.
                    if dry_run:
                        if lp_result.get("ok"):
                            result["lp_close"] = lp_result.get("detail", "")
                            result["lp_freed"] = lp_result.get("freed", 0.0)
                            result["action"] = (
                                f"would close LP + repay up to ${repay_amt:.2f} "
                                f"({lp_result.get('detail', '')})"
                            )
                        else:
                            result["action"] = (
                                f"would escalate: no swappable assets and no LP to close "
                                f"({lp_result.get('detail', '')})"
                            )
                        return result

                    if lp_result.get("ok"):
                        result["lp_close"] = lp_result["detail"]
                        result["lp_freed"] = lp_result.get("freed", 0.0)
                        # GMX withdraw is ASYNC (keeper pending) — assets aren't here yet.
                        if lp_result.get("async"):
                            result["action"] = f"delever ({lp_result['detail']})"
                            return result

                        # LP closed synchronously (Aerodrome/LB). The LP is now gone but the
                        # debt is still outstanding. A VIRTUAL/ETH or AERO/cbBTC LP frees the
                        # VOLATILE legs (ETH, VIRTUAL, cbBTC, AERO), NOT USDC — so we de-lever
                        # using ALL freed tokens, safest-first:
                        #   Pass 1 — repay each debt leg with its own freed asset (NO swap, can't
                        #            strand: repaying USDC-debt with USDC, ETH-debt with ETH, etc.).
                        #   Pass 2 — swap the largest remaining freed volatile token -> USDC and
                        #            repay the USDC shortfall (the ONLY swap, run AFTER debt is
                        #            already partially reduced, so a swap failure isn't catastrophic).
                        try:
                            raw2 = subprocess.run(
                                [sys.executable, tool_path, "defi", "--json"],
                                capture_output=True, text=True, timeout=90,
                            )
                            if raw2.returncode == 0:
                                defi_data = json.loads(raw2.stdout)
                        except Exception as e:
                            result["lp_swap_error"] = str(e)

                        freed_rows = _gather_supply_rows(defi_data)
                        borrowed_rows = _gather_borrow_rows(defi_data)
                        total_debt_after = sum((b.get("usd", 0) or 0) for b in borrowed_rows)
                        repay_target = min(repay_amt, total_debt_after)

                        # ── Pass 1: direct token-matched repays (no swap) ───────────
                        repay_plan, remaining_after, usdc_shortfall = _plan_token_repays(
                            freed_rows, borrowed_rows, repay_target,
                        )
                        repaid_usd = 0.0
                        repaid_notes = []
                        pass1_failures = []
                        for rp in repay_plan:
                            try:
                                rr = subprocess.run(
                                    [sys.executable, tool_path, "repay",
                                     "--pool", rp["pool"],
                                     "--amount", f"{rp['amount']:.8f}", "--execute"],
                                    capture_output=True, text=True, timeout=120,
                                )
                                if rr.returncode == 0:
                                    repaid_usd += rp["usd"]
                                    repaid_notes.append(f"${rp['usd']:.2f} {rp['symbol']}")
                                else:
                                    pass1_failures.append(f"{rp['symbol']}: {rr.stderr[:120]}")
                            except Exception as e:
                                pass1_failures.append(f"{rp['symbol']}: {e}")

                        if repaid_notes:
                            result["lp_repay"] = "repaid " + " + ".join(repaid_notes) + " (direct, freed from LP)"
                        if pass1_failures:
                            result["repay_failures"] = "; ".join(pass1_failures)

                        # Repay-first safety: if pass-1 couldn't directly repay ANY debt (no
                        # freed token token-matched a debt leg) yet debt still needs retiring,
                        # escalate LOUDLY rather than fall through to a price-risky swap as the
                        # SOLE de-lever action. A PARTIAL direct repay (repaid_usd >= $0.50)
                        # instead proceeds to the pass-2 swap below for the remaining shortfall.
                        if repaid_usd < 0.50 and remaining_after >= 1.0:
                            write_escalation(state_dir, "delever-debt-remains-after-lp-close", {
                                "reason": "delever_debt_remains_after_lp_close",
                                "repay_needed": repay_amt, "repaid_usd": round(repaid_usd, 2),
                                "lp_freed": lp_result.get("freed", 0.0),
                                "lp_detail": lp_result.get("detail", ""),
                                "pass1_failures": pass1_failures,
                                "health_pct": pct, "label": label,
                            })
                            _notify(
                                f"🚨 {label}: LP position closed but CANNOT repay debt — "
                                f"no freed tokens matched any existing debt leg. "
                                f"Still owe ${repay_amt:.2f}, health at {pct}%. Manual intervention needed."
                            )
                            result["action"] = "escalate (debt remains after LP close)"
                            return result

                        # Successful real de-lever (at least partial) — record cooldown.
                        cooldown_file.write_text(str(int(time.time())))

                        # If pass-1 covered the need (or there's no USDC-pool shortfall to
                        # swap for), we're done.
                        if remaining_after < 1.0 or usdc_shortfall < 0.50:
                            result["action"] = f"repaid ${repaid_usd:.2f} (LP de-lever, token-matched)"
                            _notify(
                                f"🔄 ✅ Rebalance {label}: closed LP and repaid ${repaid_usd:.2f} "
                                f"from freed tokens directly (no swap needed — "
                                f"freed USDC matched debt USDC). Health was {pct}%."
                            )
                            return result

                        # ── Pass 2: swap largest remaining freed volatile -> USDC, repay USDC ─
                        # Debt is already partially reduced, so a swap failure here is NOT
                        # catastrophic (the existing safety property is preserved).
                        swap_candidates2 = []
                        for s in freed_rows:
                            sym = str(s.get("symbol", ""))
                            usd_val = s.get("usd", 0) or 0
                            raw_amt = _token_amount(s)
                            if sym.upper() == "USDC" or usd_val < 1 or raw_amt <= 0:
                                continue
                            swap_candidates2.append((sym, usd_val, raw_amt))
                        swap_candidates2.sort(key=lambda x: x[1], reverse=True)
                        if not swap_candidates2:
                            result["action"] = (
                                f"repaid ${repaid_usd:.2f}; ${usdc_shortfall:.2f} USDC shortfall "
                                f"(no swappable freed asset)"
                            )
                            return result
                        src2 = swap_candidates2[0][0]
                        amt2 = min(swap_candidates2[0][1] * 0.95, usdc_shortfall)
                        if amt2 < 0.50:
                            result["action"] = (
                                f"repaid ${repaid_usd:.2f}; ${usdc_shortfall:.2f} USDC shortfall "
                                f"(swap too small)"
                            )
                            return result
                        try:
                            sr2 = subprocess.run(
                                [sys.executable, tool_path, "swap",
                                 "--from", src2, "--to", "USDC",
                                 "--amount", f"{amt2:.2f}",
                                 "--slippage", "1.0", "--execute"],
                                capture_output=True, text=True, timeout=180,
                            )
                        except Exception as e:
                            result["action"] = f"repaid ${repaid_usd:.2f}; shortfall swap error: {e}"
                            return result
                        if sr2.returncode != 0:
                            result["warning"] = f"shortfall swap failed: {sr2.stderr[:200]}"
                            result["action"] = (
                                f"repaid ${repaid_usd:.2f}; ${usdc_shortfall:.2f} USDC shortfall "
                                f"(swap failed)"
                            )
                            return result
                        result["swap"] = f"swapped ${amt2:.2f} {src2} -> USDC"
                        # Re-read USDC and repay the USDC shortfall.
                        usdc_now = 0.0
                        try:
                            raw3 = subprocess.run(
                                [sys.executable, tool_path, "defi", "--json"],
                                capture_output=True, text=True, timeout=90,
                            )
                            if raw3.returncode == 0:
                                defi3 = json.loads(raw3.stdout)
                                usdc_now = sum(
                                    (s.get("usd", 0) or 0)
                                    for s in _gather_supply_rows(defi3)
                                    if str(s.get("symbol", "")).upper() == "USDC"
                                )
                        except Exception as e:
                            result["lp_swap_error"] = str(e)
                        second_repay = min(usdc_shortfall, usdc_now)
                        if second_repay < 0.50:
                            result["action"] = (
                                f"repaid ${repaid_usd:.2f}; ${usdc_shortfall:.2f} USDC shortfall "
                                f"(no USDC after swap)"
                            )
                            return result
                        try:
                            r2 = subprocess.run(
                                [sys.executable, tool_path, "repay", "--pool", "usdc",
                                 "--amount", f"{second_repay:.2f}", "--execute"],
                                capture_output=True, text=True, timeout=120,
                            )
                            if r2.returncode == 0:
                                total_repaid = repaid_usd + second_repay
                                result["action"] = f"repaid ${total_repaid:.2f} (LP de-lever)"
                                _notify(
                                    f"🔄 ✅ Rebalance {label}: closed LP, swapped freed tokens to "
                                    f"USDC, and repaid ${total_repaid:.2f} total. "
                                    f"Health was {pct}%."
                                )
                            else:
                                result["warning"] = f"shortfall repay failed: {r2.stderr[:200]}"
                                result["action"] = f"repaid ${repaid_usd:.2f}; shortfall repay failed"
                        except Exception as e:
                            result["action"] = f"repaid ${repaid_usd:.2f}; shortfall repay error: {e}"
                        return result
                    else:
                        write_escalation(state_dir, "repay-no-usdc", {
                            "reason": "repay_needs_position_close",
                            "repay_needed": repay_amt, "raw_usdc": raw_usdc,
                            "lp_tried": f"{lp_result.get('detail', '')} {lp_result.get('error', '')}",
                            "health_pct": pct, "label": label,
                        })
                        result["action"] = "escalate (need close)"
                        return result

            if dry_run:
                result["action"] = f"would repay ${repay_amt:.2f} USDC"
                return result

            try:
                r = subprocess.run(
                    [sys.executable, tool_path, "repay", "--pool", "usdc",
                     "--amount", f"{repay_amt:.2f}", "--execute"],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0:
                    cooldown_file.write_text(str(int(time.time())))
                    result["action"] = f"repaid ${repay_amt:.2f}"
                    new_debt_after = max(0.0, debt - repay_amt)
                    new_health_pct = min(100.0, max(0.0, 100 * (1 - new_debt_after / (max_mult * equity)))) if max_mult > 0 and equity > 0.01 else 0.0
                    _notify(
                        f"✅ DE-LEVER — {label}:\n"
                        f"  Repaid: ${repay_amt:.2f} USDC\n"
                        f"  Pre-health: {pct}% → {new_health_pct:.1f}%\n"
                        f"  Source: raw USDC balance (no swap needed)\n"
                        f"  Debt reduced: ${debt:.2f} → ${new_debt_after:.2f}"
                    )
                else:
                    result["error"] = f"repay failed: {r.stderr[:200]}"
            except Exception as e:
                result["error"] = f"repay error: {e}"

        # ── Lever-up ───────────────────────────────────────────────────
        elif pct > high:
            if not lever_up_enabled:
                result["action"] = "observe (lever-up disabled; strategy automation owns leverage)"
                return result
            # Borrow USDC to target 50% health. Protocol-agnostic.
            # Defisims autofarm handles deploying borrowed USDC into LP positions.
            # The borrowed USDC sits as raw collateral, lowering health toward center.
            borrow_amt = delta
            if borrow_amt < 1:
                result["action"] = "none (borrow too small)"
                _notify(
                    f"ℹ️ LEVER-UP SKIPPED — {label}:\n"
                    f"  Health: {pct}% (above {high}% target)\n"
                    f"  Would borrow: ${borrow_amt:.2f} but amount too small (${borrow_amt:.2f} < $1.00)\n"
                    f"  Will retry on next tick"
                )
                return result

            # Stranded-debt guard (GMX): the GMX/avax defisims autofarm is NOT
            # scheduled, so a borrow that lands below the autofarm deploy floor would
            # never be deployed — it would just sit as raw USDC, lowering health for no
            # yield. Skip the lever; levering is never urgent. Repays are unaffected.
            if position_type == "gmx":
                min_deploy_usd = strategy.get("min_deploy_usd", 100)
                if borrow_amt < min_deploy_usd:
                    result["action"] = (
                        f"observe (lever-up skipped: borrow ${borrow_amt:.2f} < "
                        f"deploy floor ${min_deploy_usd}; would strand as raw USDC)"
                    )
                    _rate_limited_notify(
                        state_dir, "lever-strand",
                        f"ℹ️ {label}: lever-up skipped (borrow ${borrow_amt:.2f} "
                        f"< deploy floor ${min_deploy_usd}; would strand as raw USDC). "
                        f"Health {pct}%.",
                    )
                    return result

            cooldown_file = Path(state_dir) / "last-action-lever"
            # Bypass cooldown when health is critically high (>85%): the position is
            # severely under-leveraged and missing yield. Mirror of the de-lever bypass
            # at pct < 20%. The cooldown still applies in the 70-85% range where
            # the divergence from center is smaller and overtrading is a concern.
            if pct is not None and pct <= 85.0 and cooldown_file.exists():
                last_act = int(cooldown_file.read_text().strip())
                if time.time() - last_act < cooldown_secs:
                    result["action"] = "lever (cooldown)"
                    return result

            # Check pool conditions before borrowing
            _max_util = strategy.get("max_usdc_utilization_pct", 90.0)
            _max_apr = strategy.get("max_usdc_borrow_apr_pct", 15.0)
            _feasible, _reason = _usdc_borrow_feasible(tool_path, _max_util, _max_apr)
            if not _feasible:
                result["action"] = f"observe (lever postponed: {_reason})"
                return result

            if dry_run:
                result["action"] = f"would borrow ${borrow_amt:.2f} USDC (defisims deploys)"
                return result

            # Phase 1: Announce intent BEFORE borrow
            _post_health = min(100.0, max(0.0, 100 * (1 - (debt + borrow_amt) / (max_mult * (equity + borrow_amt))))) if max_mult > 0 and equity + borrow_amt > 0.01 else 0.0
            _notify(
                f"⏳ LEVER-UP INTENT — {label}:\n"
                f"  Pre-health: {pct}% (target ~50%)\n"
                f"  Will borrow: ${borrow_amt:.2f} USDC\n"
                f"  Current position: ${equity:.2f} equity, ${debt:.2f} debt\n"
                f"  Post-borrow health: ~{_post_health:.1f}%\n"
                f"  Next step: defisims autofarm will deploy borrowed USDC into LP on next tick\n"
                f"  ⚠️ Borrowed USDC sits RAW until deploy — rate/price risk while un-deployed"
            )

            # Borrow USDC
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

            # If swap_target is set, swap borrowed USDC to that token
            if swap_target and swap_target.upper() != "USDC":
                try:
                    sr = subprocess.run(
                        [sys.executable, tool_path, "swap",
                         "--from", "USDC", "--to", swap_target,
                         "--amount", f"{borrow_amt:.2f}",
                         "--slippage", "1.0", "--execute"],
                        capture_output=True, text=True, timeout=180,
                    )
                    if sr.returncode == 0:
                        result["action"] = f"borrowed ${borrow_amt:.2f}, swapped to {swap_target}"
                        _notify(
                            f"✅ LEVER-UP EXECUTED — {label}:\n"
                            f"  Borrowed: ${borrow_amt:.2f} USDC → swapped to {swap_target}\n"
                            f"  Pre-health: {pct}% → post-health: check next monitor tick\n"
                            f"  Next: defisims autofarm will manage {swap_target} position\n"
                            f"  ⚠️ Position is over-levered (+1 tick cost) until LP deploy completes"
                        )
                    else:
                        result["warning"] = f"swap to {swap_target} failed after borrow: {sr.stderr[:200]}"
                except Exception as e:
                    result["error"] = f"borrow+swap error: {e}"
            else:
                result["action"] = f"borrowed ${borrow_amt:.2f} USDC (defisims autofarm deploys)"
                _notify(
                    f"✅ LEVER-UP EXECUTED — {label}:\n"
                    f"  Borrowed: ${borrow_amt:.2f} USDC\n"
                    f"  Pre-health: {pct}% → post-health: check next monitor tick\n"
                    f"  Raw USDC now in account: ~${(raw_usdc + borrow_amt):.2f}\n"
                    f"  Next: defisims autofarm will deploy into LP (may take 1-2 ticks)\n"
                    f"  ⚠️ Position is over-levered (+1 tick cost) until LP deploy completes"
                )

            cooldown_file.write_text(str(int(time.time())))
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
