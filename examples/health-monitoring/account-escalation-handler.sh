#!/bin/bash
# Account Health Escalation Handler — Parakletos
#
# Checks for escalation markers left by account-health-monitor.sh.
# Supports both DeltaPrime (avalanche) and DegenPrime (base).
# When found, spawns an isolated OpenClaw agent session via cron job.
# Runs every 5 min via crontab. Zero LLM cost when no escalation active.

set -uo pipefail

OPENCLAW=/data/mise/installs/node/22.22.2/bin/openclaw
NOTIFY=/root/.openclaw/workspace/scripts/notify.sh
PY=/root/.openclaw/venv/bin/python3

default_log="/var/log/paraklaudios/account-health-parakletos.log"

# Determine which chain this escalation is for from the state dir
# Check both state dirs, pick the one with an active escalation
for suffix in prime degen; do
    f="/root/.openclaw/workspace/state/account-health/${suffix}/escalate.json"
    if [ -f "$f" ]; then
        LOG="/var/log/paraklaudios/account-health-${suffix}.log"
        STATE_DIR="/root/.openclaw/workspace/state/account-health/${suffix}"
        ESCALATE_FILE="$STATE_DIR/escalate.json"
        CASE="$suffix"
        break
    fi
done

# No escalation found for either chain
if [ -z "${CASE:-}" ]; then
    # Check the old default location too
    f="/root/.openclaw/workspace/state/account-health/escalate.json"
    [ ! -f "$f" ] && exit 0
    # Legacy escalation — use prime as default
    LOG="$default_log"
    STATE_DIR="/root/.openclaw/workspace/state/account-health"
    ESCALATE_FILE="$STATE_DIR/escalate.json"
    CASE="prime"
fi

log() { echo "$(date -Iseconds) [escalation/$CASE] $*" >> "$LOG"; }

log "Escalation marker found for $CASE"
payload=$(cat "$ESCALATE_FILE")
rm -f "$ESCALATE_FILE"

ENV_FILE=/root/.openclaw/.env
reason=$(echo "$payload" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('reason','unknown'))" 2>/dev/null || echo "unknown")
log "Reason: $reason"

# ── Read gateway token ─────────────────────────────────────────────
GW_TOKEN=$(grep -oP 'OPENCLAW_GATEWAY_TOKEN=\K.*' "$ENV_FILE" 2>/dev/null | tr -d '"' | tr -d "'" || echo "")
if [ -z "$GW_TOKEN" ]; then
    log "FATAL: no gateway token in $ENV_FILE"
    "$NOTIFY" "🚨 *Escalation failed* ($CASE): Cannot reach agent. Payload: $(echo "$payload" | head -c 200)" >/dev/null 2>&1 || true
    exit 1
fi
export OPENCLAW_GATEWAY_TOKEN="$GW_TOKEN"

# ── Build agent prompt ─────────────────────────────────────────────
CHAIN_LABEL="DeltaPrime (Avalanche)"
[ "$CASE" = "degen" ] && CHAIN_LABEL="DegenPrime (Base)"

TOOL_PATH="/root/.openclaw/workspace/scripts/deltaprime.py"
[ "$CASE" = "degen" ] && TOOL_PATH="/root/.openclaw/workspace/scripts/degenprime.py"

PROMPT=$(cat <<PROMPT_END
🚀 **Account Health — Escalation Required ($CHAIN_LABEL)**

The automated health monitor hit a situation it could not auto-resolve on the ${CASE} account.

**Task:**
1. Run \`${TOOL_PATH} summary\` (or \`defi --json\`) to assess the account state.
2. Check open positions (GMX, Aerodrome, LB, etc.) using the tool's position commands.
3. Take appropriate action: withdraw from positions if needed, repay debt, adjust leverage, or decide where to deploy new funds.
4. Report back to Bruno with what you did and the resulting state (health%, equity, debt).

**Escalation context:**
${payload}
PROMPT_END
)

log "Spawning isolated agent session for: $reason ($CASE)"
OUTPUT=$("$OPENCLAW" cron add --at +10s --delete-after-run --announce \
    --session isolated --description "account-escalation-${CASE}-$(date +%s)" \
    "account-escalation-${CASE}-$(date +%s)" "$PROMPT" 2>&1)
RC=$?

if [ $RC -ne 0 ]; then
    log "FAILED to spawn agent (rc=$RC): $OUTPUT"
    "$NOTIFY" "🚨 *Escalation* ($CASE): Agent spawn failed (rc=$RC). Check logs.\nReason: $reason" >/dev/null 2>&1 || true
    exit 1
fi

log "Agent session spawned: $(echo "$OUTPUT" | grep -o '"id":"[^"]*"' | head -1)"
"$NOTIFY" "🤖 *Escalation* ($CASE): Agent spawned for: $reason" >/dev/null 2>&1 || true
exit 0
