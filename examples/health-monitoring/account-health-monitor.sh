#!/bin/bash
# Account Health Monitor — Parakletos
#
# Prime/Degen account health monitor. Supports DeltaPrime (Avalanche)
# and DegenPrime (Base). Pass --chain avalanche or --chain base.
#
# Two modes:
#   Pure observer (default) — reads state, logs, escalates on issues.
#   Rebalance mode          — if state/account-health/{prime,degen}/strategy.json
#                             exists, applies the configured strategy.
#
# strategy.json format:
#   { "mode": "rebalance", "target_range": [30, 70], "center": 50,
#     "cooldown_secs": 10800, "position": "gmx", "market": "avax-usdc", "side": "short" }
#
# Usage:
#   account-health-monitor.sh                    # DeltaPrime (Avalanche) - default
#   account-health-monitor.sh --chain base       # DegenPrime (Base)

set -uo pipefail

PY=/root/.openclaw/venv/bin/python3
NOTIFY=/root/.openclaw/workspace/scripts/notify.sh

# ── Chain config ────────────────────────────────────────────────────
CHAIN="avalanche"
if [ "${1:-}" = "--chain" ] && [ -n "${2:-}" ]; then
    CHAIN="$2"
fi

if [ "$CHAIN" = "base" ]; then
    TOOL=/root/.openclaw/workspace/scripts/degenprime.py
    CASE="degen"
    RPC="https://mainnet.base.org"
    EOA="0x0218f5b006FD43181018F584Ed4Be13c356b3428"
else
    TOOL=/root/.openclaw/workspace/scripts/deltaprime.py
    CASE="prime"
    RPC="https://api.avax.network/ext/bc/C/rpc"
    EOA="0x0218f5b006FD43181018F584Ed4Be13c356b3428"
fi

LOG="/var/log/paraklaudios/account-health-${CASE}.log"
STATE_DIR="/root/.openclaw/workspace/state/account-health/${CASE}"
HISTORY_FILE="$STATE_DIR/history.jsonl"
STRATEGY_FILE="$STATE_DIR/strategy.json"

mkdir -p "$STATE_DIR"

log() { echo "$(date -Iseconds) $*" >> "$LOG"; }

# ── Load strategy (optional) ─────────────────────────────────────────
STRATEGY=""
TARGET_LOW=""
TARGET_HIGH=""
TARGET_CENTER=""
COOLDOWN_SECS=""
STOP_LOSS_DRAWDOWN_PCT=""
BASELINE_EQUITY=""
POSITION_TYPE=""
MARKET=""
SIDE=""

if [ -f "$STRATEGY_FILE" ]; then
    STRATEGY=$(cat "$STRATEGY_FILE")
    TARGET_LOW=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('target_range',[0])[0])" 2>/dev/null || echo "")
    TARGET_HIGH=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('target_range',[0,0])[1])" 2>/dev/null || echo "")
    TARGET_CENTER=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('center',50))" 2>/dev/null || echo "50")
    COOLDOWN_SECS=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('cooldown_secs',10800))" 2>/dev/null || echo "10800")
    STOP_LOSS_DRAWDOWN_PCT=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('stop_loss_drawdown_pct',0))" 2>/dev/null || echo "0")
    POSITION_TYPE=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('position',''))" 2>/dev/null || echo "")
    MARKET=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('market',''))" 2>/dev/null || echo "")
    SIDE=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('side',''))" 2>/dev/null || echo "")
    MODE=$(echo "$STRATEGY" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('mode','observer'))" 2>/dev/null || echo "observer")

    # Read baseline equity (set automatically, or by agent on position open)
    BASELINE_FILE="$STATE_DIR/baseline-equity"
    [ -f "$BASELINE_FILE" ] && BASELINE_EQUITY=$(cat "$BASELINE_FILE")

    log "Strategy loaded: $MODE  range=${TARGET_LOW}-${TARGET_HIGH}%  center=${TARGET_CENTER}%  cooldown=${COOLDOWN_SECS}s  stop_loss_drawdown=${STOP_LOSS_DRAWDOWN_PCT}%  baseline_equity=${BASELINE_EQUITY:-\$(current)}  position=$POSITION_TYPE"
else
    MODE="observer"
    log "No strategy file — pure observer mode"
fi

# ── Rate-limited notify ─────────────────────────────────────────────
last_notified() { echo "$STATE_DIR/notify-$1"; }
should_notify() {
    local key="$1" min_secs="${2:-300}"
    local f=$(last_notified "$key")
    [ ! -f "$f" ] && return 0
    local last now; last=$(cat "$f"); now=$(date +%s)
    [ $((now - last)) -ge "$min_secs" ]
}
mark_notified() { date +%s > "$(last_notified "$1")"; }
notify_ratelimited() {
    local key="$1" min_secs="${2:-300}"; shift 2
    should_notify "$key" "$min_secs" || return 1
    "$NOTIFY" "$*" >/dev/null 2>&1 || true; mark_notified "$key"
}
notify_now() {
    "$NOTIFY" "$*" >/dev/null 2>&1 || true; log "NOTIFY: $*"
}

# ── Escalation marker ────────────────────────────────────────────────
write_escalation() {
    local reason="$1" min_secs="${2:-7200}"
    local f="$STATE_DIR/last-escalation-$reason"
    if [ -f "$f" ]; then
        local last now; last=$(cat "$f"); now=$(date +%s)
        [ $((now - last)) -lt "$min_secs" ] && { log "Escalation $reason suppressed"; return 1; }
    fi
    shift 2; echo "$*" > "$STATE_DIR/escalate.json"
    date +%s > "$f"; log "Escalation written: $reason"
}

# ── Read tier ───────────────────────────────────────────────────────
read_tier() {
    local out
    if [ "$CHAIN" = "base" ]; then
        out=$(timeout 30 "$PY" "$TOOL" tier 2>/dev/null) || { echo "basic"; return; }
        echo "$out" | grep -qi "premium" && { echo "premium"; return; }
        echo "basic"
    else
        out=$(timeout 30 "$PY" "$TOOL" prime-tier 2>/dev/null) || { echo "unknown"; return; }
        echo "$out" | grep -qi "premium" && { echo "premium"; return; }
        echo "$out" | grep -qi "basic" && { echo "basic"; return; }
        echo "unknown"
    fi
}

# ── Fetch health state ──────────────────────────────────────────────
declare -A TIER_MAX=( ["basic"]=5 ["premium"]=10 )

fetch_state() {
    local raw_json tier max_mult
    tier=$(read_tier)
    max_mult="${TIER_MAX[$tier]:-5}"

    if [ "$CHAIN" = "base" ]; then
        raw_json=$(timeout 90 "$PY" "$TOOL" summary --json 2>/dev/null) || { echo '{"error":"summary failed"}'; return; }
    else
        raw_json=$(timeout 90 "$PY" "$TOOL" defi --json 2>/dev/null) || { echo '{"error":"defi failed"}'; return; }
    fi

    "$PY" -c "
import json, sys
raw = json.loads('''$raw_json''')

chain = '$CHAIN'
if chain == 'base':
    supplied = raw.get('supplied', [])
    borrowed = raw.get('borrowed', [])
    supplied_usd = sum(s.get('usd', 0) or 0 for s in supplied)
    debt_usd = sum(b.get('usd', 0) or 0 for b in borrowed)
    equity = supplied_usd - debt_usd
    health_ratio = 0
else:
    groups = raw.get('groups', [])
    if not groups:
        print(json.dumps({'error': 'no lending group'}))
        sys.exit(0)
    g = groups[0]
    supplied = g.get('supplied', [])
    borrowed = g.get('borrowed', [])
    supplied_usd = sum(s.get('usd', 0) or 0 for s in supplied)
    debt_usd = sum(b.get('usd', 0) or 0 for b in borrowed)
    equity = supplied_usd - debt_usd
    health_ratio = g.get('health_ratio', 0) or 0

if equity <= 0.01:
    print(json.dumps({
        'bruno_pct': 0.0, 'health_ratio': round(health_ratio, 4),
        'supplied_usd': round(supplied_usd, 2), 'debt_usd': round(debt_usd, 2),
        'equity': round(equity, 2), 'tier': '$tier',
        'max_leverage': $max_mult, 'max_debt': 0, 'error': 'equity near zero'
    }))
    sys.exit(0)

max_debt = equity * ($max_mult - 1)
raw_usdc = 0
for s in supplied:
    if s.get('symbol') == 'USDC': raw_usdc = float(s.get('usd', 0))

symbols = [s.get('symbol', '') for s in supplied]
has_gmx  = any('GM_' in sym for sym in symbols)
has_lb   = any(sym in ('LB_AVAX_USDC', 'LB_WAVAX_USDC', 'JOE') or 'TRADERJOE' in sym.upper() for sym in symbols)
has_aero = any('AERO' in sym.upper() or 'CL_POSITION' in sym.upper() for sym in symbols)

bruno_pct = ((max_debt - min(debt_usd, max_debt)) / max_debt * 100) if max_debt > 0 and debt_usd >= 0 else 100.0
bruno_pct = max(0.0, min(100.0, bruno_pct))
delta_debt = (max_debt * (1 - ${TARGET_CENTER:-50} / 100.0)) - debt_usd

print(json.dumps({
    'bruno_pct': round(bruno_pct, 1), 'health_ratio': round(health_ratio, 4),
    'supplied_usd': round(supplied_usd, 2), 'debt_usd': round(debt_usd, 2),
    'equity': round(equity, 2), 'tier': '$tier',
    'max_leverage': $max_mult, 'max_debt': round(max_debt, 2),
    'delta_debt': round(delta_debt, 2),
    'raw_usdc': round(raw_usdc, 2),
    'has_gmx': has_gmx, 'has_lb': has_lb, 'has_aero': has_aero,
}))
sys.exit(0)
print(json.dumps({'error': 'no lending group'}))
" 2>/dev/null || echo '{"error":"parse failed"}'
}

# ── Cooldown checks (rebalance mode only) ───────────────────────────
can_act() {
    local direction="$1"
    local f="$STATE_DIR/last-action-$direction"
    [ ! -f "$f" ] && return 0
    local last_ts now elapsed
    last_ts=$(cat "$f"); now=$(date +%s)
    elapsed=$((now - last_ts))
    [ "$elapsed" -ge "${COOLDOWN_SECS:-10800}" ]
}
mark_acted() {
    local direction="$1"
    date +%s > "$STATE_DIR/last-action-$direction"
    log "Cooldown set: $direction (${COOLDOWN_SECS:-10800}s)"
}

# ── Get EOA gas balance ─────────────────────────────────────────────
get_eoa_gas() {
    "$PY" -c "
from web3 import Web3
w3=Web3(Web3.HTTPProvider('$RPC'))
addr=Web3.to_checksum_address('$EOA')
print(w3.eth.get_balance(addr)/1e18)
" 2>/dev/null || echo "0"
}

# ── Run tool command (rebalance mode only) ──────────────────────────
run_tool() {
    local label="$1"; shift
    log "Executing: $*"
    local out; out=$(timeout 120 "$PY" "$TOOL" "$@" 2>&1)
    local rc=$?
    echo "$out" | tail -3 >> "$LOG"
    if [ $rc -ne 0 ]; then
        log "FAILED (rc=$rc): $label"
        echo "$out" | grep -iE "error|revert|fail|exception" | head -3 >> "$LOG"
        return 1
    fi
    if echo "$out" | grep -qiE "✗|error|revert|cannot|insufficient|refused"; then
        log "TOOL ERROR: $label"
        return 1
    fi
    log "OK: $label"
    echo "$out"
    return 0
}

# ── Check if GMX is frozen ──────────────────────────────────────────
is_gmx_frozen() {
    local out
    out=$(timeout 30 "$PY" "$TOOL" gmx-positions 2>/dev/null) || return 1
    echo "$out" | grep -qiE "frozen|pending|no positions" && return 0
    local current_gm
    current_gm=$(echo "$out" | grep -oP 'Raw GM balance:\s*\K[0-9.]+' 2>/dev/null || echo "0")
    local last_known
    last_known=$(cat "$STATE_DIR/last-gm-balance" 2>/dev/null || echo "0")
    [ "$(echo "$last_known == 0" | bc -l 2>/dev/null)" = "1" ] && { echo "$current_gm" > "$STATE_DIR/last-gm-balance"; return 1; }
    if [ -f "$STATE_DIR/pending-deposit" ]; then
        local changed
        changed=$(echo "$current_gm - $last_known" | bc -l 2>/dev/null || echo "0")
        [ "$(echo "$changed < 0.01" | bc -l 2>/dev/null)" = "1" ] && return 0
        rm -f "$STATE_DIR/pending-deposit"
    fi
    echo "$current_gm" > "$STATE_DIR/last-gm-balance"
    return 1
}

# ════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════
log "=== account-health check start ==="

state=$(fetch_state)
error=$(echo "$state" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)

if [ -n "$error" ]; then
    log "Fetch error: $error"
    if echo "$error" | grep -qE "defi failed|summary failed|parse failed"; then
        notify_ratelimited "data-down" 600 "⚠️ *Health monitor* ($CASE): Data fetch failed." 2>&1 || true
        write_escalation "data-down" 7200 "{\"reason\":\"data_down\",\"error\":\"$error\",\"chain\":\"$CHAIN\",\"timestamp\":\"$(date -Iseconds)\"}"
    fi
    exit 0
fi

# Parse state
pct=$(echo "$state" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('bruno_pct','?'))" 2>/dev/null)
hr=$(echo "$state" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('health_ratio','?'))" 2>/dev/null)
debt=$(echo "$state" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('debt_usd','?'))" 2>/dev/null)
equity=$(echo "$state" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('equity','?'))" 2>/dev/null)
delta_debt=$(echo "$state" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('delta_debt','?'))" 2>/dev/null)
raw_usdc=$(echo "$state" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('raw_usdc','?'))" 2>/dev/null)
has_gmx=$(echo "$state" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('has_gmx',False))" 2>/dev/null)
state_error=$(echo "$state" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('error',''))" 2>/dev/null)

pos_types=""
[ "$has_gmx" = "True" ] || [ "$has_gmx" = "true" ] && pos_types="${pos_types}gmx "

log "Health: ${pct}%  ratio=$hr  equity=\$${equity}  debt=\$${debt}  raw_USDC=\$${raw_usdc}  positions=[${pos_types:-none}]  mode=$MODE"

# ── Equity near zero (always escalates) ─────────────────────────────
if [ -n "$state_error" ] && echo "$state_error" | grep -q "equity near zero"; then
    log "WARN: equity near zero (\$${equity}) — liquidation risk"
    notify_ratelimited "equity-zero" 1800 "🚨 *Health monitor* ($CASE): Equity near zero (\$${equity})." 2>&1 || true
    write_escalation "equity-zero" 3600 "{\"reason\":\"equity_near_zero\",\"equity\":$equity,\"debt\":$debt,\"health_pct\":$pct,\"chain\":\"$CHAIN\"}"
    exit 0
fi

# ── Health swing detection (always, regardless of mode) ─────────────
LAST_FILE="$STATE_DIR/last-health-pct"
if [ -f "$LAST_FILE" ]; then
    last_pct=$(cat "$LAST_FILE")
    if [ -n "$pct" ] && [ "$pct" != "?" ] && [ -n "$last_pct" ] && [ "$last_pct" != "?" ]; then
        diff=$(echo "$pct - $last_pct" | bc -l 2>/dev/null | sed 's/-//' || echo "0")
        if awk "BEGIN{exit !($diff > 10)}" 2>/dev/null; then
            log "Health swing: ${last_pct}% → ${pct}% (Δ${diff}pp)"
            write_escalation "health-swing" 3600 "{\"reason\":\"health_swing\",\"from_pct\":$last_pct,\"to_pct\":$pct,\"delta\":$diff,\"chain\":\"$CHAIN\"}"
        fi
    fi
fi
echo "$pct" > "$LAST_FILE"

# Append to history
echo "{\"ts\":\"$(date -Iseconds)\",\"mode\":\"$MODE\",\"pct\":$pct,\"equity\":$equity,\"debt\":$debt,\"hr\":$hr,\"pos\":\"${pos_types:-none}\",\"raw_usdc\":$raw_usdc}" >> "$HISTORY_FILE" 2>/dev/null
tail -1000 "$HISTORY_FILE" > "${HISTORY_FILE}.tmp" 2>/dev/null && mv "${HISTORY_FILE}.tmp" "$HISTORY_FILE" 2>/dev/null

# ════════════════════════════════════════════════════════════════════
# REBALANCE MODE (only when strategy.json exists)
# ════════════════════════════════════════════════════════════════════
if [ "$MODE" = "rebalance" ] && [ -n "$TARGET_LOW" ] && [ -n "$TARGET_HIGH" ]; then

    # Equity-drawdown stop-loss: if equity drops X% below the baseline (recorded at
    # position open), escalate for full close. The agent handles the multi-step unwind:
    # withdraw from position, keeper settle, swap to USDC, repay all debt.
    #
    # First tick after configuring stop_loss_drawdown_pct auto-records current equity
    # as baseline. This allows setting the parameter at any time after opening a position.
    BASELINE_FILE="$STATE_DIR/baseline-equity"
    if [ -n "$STOP_LOSS_DRAWDOWN_PCT" ] && [ "$STOP_LOSS_DRAWDOWN_PCT" != "0" ] && [ -n "$equity" ]; then
        if [ -z "$BASELINE_EQUITY" ] || [ "$BASELINE_EQUITY" = "0" ]; then
            # No baseline yet — record current equity as baseline
            echo "$equity" > "$BASELINE_FILE"
            BASELINE_EQUITY=$equity
            log "Stop-loss baseline recorded: \$${equity} (drawdown threshold: ${STOP_LOSS_DRAWDOWN_PCT}%)"
        else
            # Compare current equity to baseline
            drawdown=$(echo "scale=4; (1 - $equity / $BASELINE_EQUITY) * 100" | bc -l 2>/dev/null || echo "0")
            if awk "BEGIN{exit !($drawdown >= $STOP_LOSS_DRAWDOWN_PCT)}" 2>/dev/null; then
                log "STOP LOSS: equity dropped ${drawdown}% from baseline of \$${BASELINE_EQUITY} — escalating for full close"
                notify_ratelimited "stop-loss" 900 "🛑 *Strategy* ($CASE): Equity dropped ${drawdown}% from \$${BASELINE_EQUITY} (threshold: ${STOP_LOSS_DRAWDOWN_PCT}%). Full close needed. Agent dispatched." 2>&1 || true
                write_escalation "stop-loss" 1800 "{\"reason\":\"stop_loss_equity_drawdown\",\"drawdown_pct\":$drawdown,\"threshold_pct\":$STOP_LOSS_DRAWDOWN_PCT,\"baseline_equity\":$BASELINE_EQUITY,\"current_equity\":$equity,\"debt\":$debt,\"health_pct\":$pct,\"health_ratio\":$hr,\"chain\":\"$CHAIN\",\"action\":\"full_close\"}"
                exit 0
            fi
        fi
    fi

    # Hard floor: if health drops below 20%, bypass cooldown and escalate immediately
    if awk "BEGIN{exit !($pct < 20)}" 2>/dev/null; then
        log "HARD FLOOR: health ${pct}% < 20% — bypassing cooldown, escalating"
        notify_ratelimited "health-floor" 900 "🚨 *Strategy* ($CASE): Health ${pct}% below 20% hard floor. Agent needs to intervene." 2>&1 || true
        write_escalation "health-floor-breach" 1800 "{\"reason\":\"health_below_floor\",\"pct\":$pct,\"equity\":$equity,\"debt\":$debt,\"chain\":\"$CHAIN\"}"
        # Also go ahead and de-lever if we can (cooldown bypassed for action below 20%)
    fi

    # In range → done (or already handled by floor above)
    if awk "BEGIN{exit !($pct >= $TARGET_LOW && $pct <= $TARGET_HIGH)}" 2>/dev/null; then
        log "In target range (${TARGET_LOW}-${TARGET_HIGH}%) — no action"
        exit 0
    fi

    action=""
    if awk "BEGIN{exit !($pct < $TARGET_LOW)}" 2>/dev/null; then
        action="delever"
        # Bypass cooldown if below hard floor
        if awk "BEGIN{exit !($pct >= 20)}" 2>/dev/null; then
            can_act "delever" || { log "Delever in cooldown — skipped"; exit 0; }
        fi
    fi

    if awk "BEGIN{exit !($pct > $TARGET_HIGH)}" 2>/dev/null; then
        action="lever"
        can_act "lever" || { log "Lever in cooldown — skipped"; exit 0; }
    fi

    [ -z "$action" ] && { log "No action needed"; exit 0; }
    log "ACTION: $action  (health ${pct}%, target ${TARGET_LOW}-${TARGET_HIGH}%)"

    # ── DE-LEVER: repay USDC ────────────────────────────────────────
    if [ "$action" = "delever" ]; then
        repay_amt=$(printf "%.2f" "$(echo "$delta_debt" | "$PY" -c "import sys; print(abs(float(sys.stdin.read())))" 2>/dev/null)" 2>/dev/null)
        awk "BEGIN{exit !($repay_amt < 1)}" 2>/dev/null && { log "Repay amount too small (\$${repay_amt})"; exit 0; }

        if awk "BEGIN{exit !($repay_amt > $raw_usdc)}" 2>/dev/null; then
            log "Insufficient raw USDC (\$${raw_usdc}) to repay \$${repay_amt} — funds locked"
            write_escalation "repay-no-usdc" 7200 "{\"reason\":\"strategy_repay_blocked\",\"repay_needed\":$repay_amt,\"raw_usdc\":$raw_usdc,\"health_pct\":$pct,\"equity\":$equity}"
            notify_ratelimited "repay-blocked" 3600 "⚠️ *Strategy* ($CASE): Need to de-lever but only \$${raw_usdc} raw USDC. Agent needed." 2>&1 || true
            exit 0
        fi

        eoa_gas=$(get_eoa_gas)
        awk "BEGIN{exit !($eoa_gas < 0.01)}" 2>/dev/null && {
            log "Gas too low (${eoa_gas})"; exit 0
        }

        run_tool "repay USDC" "repay" "--pool" "usdc" "--amount" "$repay_amt" "--execute" && {
            mark_acted "delever"
            notify_now "✅ *Strategy* ($CASE): Repaid \$${repay_amt} USDC. Health toward ${TARGET_CENTER}%."
        } || {
            notify_ratelimited "repay-failed" 3600 "⚠️ *Strategy* ($CASE): Repay failed." 2>&1 || true
            write_escalation "repay-failed" 7200 "{\"reason\":\"strategy_repay_failed\",\"amount\":$repay_amt}"
        }
        exit 0
    fi

    # ── LEVER: borrow + deploy ──────────────────────────────────────
    if [ "$action" = "lever" ]; then
        borrow_amt=$(printf "%.2f" "$delta_debt" 2>/dev/null)
        awk "BEGIN{exit !($borrow_amt < 1)}" 2>/dev/null && { log "Borrow too small (\$${borrow_amt})"; exit 0; }

        # GMX-specific path (configurable via strategy.json position field)
        if [ "$POSITION_TYPE" = "gmx" ]; then
            is_gmx_frozen && {
                log "GMX frozen — skipping"
                notify_ratelimited "gmx-frozen" 3600 "⏳ *Strategy* ($CASE): GMX frozen." 2>&1 || true
                exit 0
            }

            eoa_gas=$(get_eoa_gas)
            awk "BEGIN{exit !($eoa_gas < 0.05)}" 2>/dev/null && { log "Gas too low (${eoa_gas})"; exit 0; }

            run_tool "borrow USDC" "borrow" "--pool" "usdc" "--amount" "$borrow_amt" "--execute" || {
                notify_ratelimited "borrow-failed" 3600 "⚠️ *Strategy* ($CASE): Borrow of \$${borrow_amt} failed." 2>&1 || true
                write_escalation "borrow-failed" 7200 "{\"reason\":\"strategy_borrow_failed\",\"amount\":$borrow_amt}"
                exit 0
            }

            mkt="${MARKET:-avax-usdc}" sd="${SIDE:-short}"
            log "Depositing \$${borrow_amt} into GMX $mkt (${sd})"
            run_tool "gmx-deposit" "gmx-deposit" "--market" "$mkt" "--amount" "$borrow_amt" "--side" "$sd" "--fee-buffer" "1.5" "--execute"

            if [ $? -ne 0 ]; then
                date +%s > "$STATE_DIR/pending-deposit"
                log "GMX deposit failed — raw USDC in account. Will retry."
                notify_ratelimited "deposit-failed" 3600 "⚠️ *Strategy* ($CASE): Deposit failed. Will retry." 2>&1 || true
                write_escalation "deposit-failed" 7200 "{\"reason\":\"strategy_deposit_failed\",\"amount\":$borrow_amt}"
                mark_acted "lever"
                exit 0
            fi

            date +%s > "$STATE_DIR/pending-deposit"
            mark_acted "lever"
            notify_now "✅ *Strategy* ($CASE): Borrowed \$${borrow_amt} + GMX deposit. Health toward ${TARGET_CENTER}%."

        else
            # Unknown position type — escalate
            log "Position type '$POSITION_TYPE' not auto-implemented — escalating"
            write_escalation "strategy-unsupported" 7200 "{\"reason\":\"strategy_unsupported\",\"position\":\"$POSITION_TYPE\",\"borrow_amt\":$borrow_amt,\"health_pct\":$pct}"
            notify_now "ℹ️ *Strategy* ($CASE): Health ${pct}% above ${TARGET_HIGH}%. Position '$POSITION_TYPE' not auto-deployable — escalated." 2>&1 || true
        fi
    fi

else
    log "Observer mode — no action"
fi

log "=== account-health check done ==="
exit 0
