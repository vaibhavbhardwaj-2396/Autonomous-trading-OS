#!/usr/bin/env bash
#
# Cron entrypoint for every trading cycle.
#
#   ./run_cycle.sh premarket | market_open | midday | preclose | daily_review | weekly_review
#
# Flow:
#   1. Python builds a deterministic briefing (guardrails, regime, candidates)
#   2. Claude Code reads that briefing plus the cycle prompt and does the judgment work
#   3. Everything is logged; failures alert on Telegram
#
# The deterministic step runs first on purpose: the numbers the agent reasons from are
# computed by tested code, not by the model.

set -uo pipefail

CYCLE="${1:-}"
PROJECT_DIR="/root/trading-agent"
VENV="$PROJECT_DIR/venv/bin/python"

# cron runs with a minimal PATH that excludes ~/.local/bin, where the Claude Code
# installer puts the binary. Without this, every scheduled run fails with
# "command not found" — and does so quietly, at 8:30am, when nobody is watching.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/${CYCLE}-${TIMESTAMP}.log"
BRIEFING_PATH="$LOG_DIR/briefing-${CYCLE}-${TIMESTAMP}.md"

VALID="premarket market_open midday preclose daily_review weekly_review"
if [[ -z "$CYCLE" ]] || [[ ! " $VALID " =~ " $CYCLE " ]]; then
    echo "Usage: $0 <${VALID// /|}>" >&2
    exit 2
fi

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

exec > >(tee -a "$LOG_FILE") 2>&1
echo "=============================================="
echo "  Cycle: $CYCLE"
echo "  Started: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=============================================="

alert() {
    "$VENV" -c "
import sys; sys.path.insert(0, 'scripts')
from telegram_notify import send_message
send_message('''$1''')
" 2>/dev/null || echo "(Telegram alert failed)"
}

# --- 1. Session check -------------------------------------------------------
# Research cycles work fine without a Kite session; trading cycles do not.
NEEDS_SESSION=false
case "$CYCLE" in
    market_open|midday|preclose) NEEDS_SESSION=true ;;
esac

if [[ "$NEEDS_SESSION" == true ]]; then
    if ! "$VENV" -c "
import sys; sys.path.insert(0, '.')
from engine.broker import get_broker
sys.exit(0 if get_broker().is_authenticated() else 1)
"; then
        echo "No valid broker session — cannot run trading cycle."
        alert "⚠️ ${CYCLE}: no broker session, so the agent could not trade. Send today's token with \`token <value>\` (INDstocks) or tap the login link (Kite)."
        exit 0
    fi
fi

# --- 1b. Process Telegram commands ------------------------------------------
# Runs before the briefing so a `pause`, `resume` or `approve` sent between cycles is
# reflected in the state the agent then reads. This is also how the drawdown ladder's
# human-acknowledgement requirement is actually satisfied.
echo ""
echo "--- Checking Telegram commands ---"
"$VENV" "$PROJECT_DIR/scripts/telegram_inbox.py" || echo "(inbox check failed — continuing)"

# --- 2. Build the briefing --------------------------------------------------
echo ""
echo "--- Building briefing ---"
if ! "$VENV" -m engine.briefing --cycle "$CYCLE" --out "$BRIEFING_PATH"; then
    echo "Briefing generation FAILED"
    alert "🔴 ${CYCLE}: briefing generation failed. No trading this cycle. Check logs/${CYCLE}-${TIMESTAMP}.log"
    exit 1
fi
echo "Briefing: $BRIEFING_PATH"

# --- 3. Run the agent -------------------------------------------------------
echo ""
echo "--- Running agent ---"

PROMPT="$(cat "$PROJECT_DIR/routines/${CYCLE}.md")

---

Your run briefing for this cycle is at: ${BRIEFING_PATH}
Read it first with the Read tool, then follow the instructions above.

Working directory: ${PROJECT_DIR}
Use ${VENV} as the python interpreter for all commands."

if [[ ! -x "$CLAUDE_BIN" ]]; then
    echo "Claude Code not found at '$CLAUDE_BIN'"
    alert "🔴 ${CYCLE}: Claude Code binary not found on the VPS. No run happened."
    exit 1
fi

timeout 900 "$CLAUDE_BIN" -p "$PROMPT" \
    --permission-mode acceptEdits \
    --add-dir "$PROJECT_DIR"
AGENT_EXIT=$?

echo ""
if [[ $AGENT_EXIT -eq 124 ]]; then
    echo "Agent TIMED OUT after 15 minutes"
    alert "🔴 ${CYCLE}: agent timed out after 15 min. Check open positions manually."
elif [[ $AGENT_EXIT -ne 0 ]]; then
    echo "Agent exited with code $AGENT_EXIT"
    alert "🔴 ${CYCLE}: agent failed (exit ${AGENT_EXIT}). Check logs/${CYCLE}-${TIMESTAMP}.log"
else
    echo "Agent completed cleanly"
fi

# --- 4. Always refresh the human-readable state -----------------------------
"$VENV" -c "
import sys; sys.path.insert(0, '.')
from engine.journal import render_portfolio_md
render_portfolio_md()
" || echo "(portfolio_state.md refresh failed)"

# --- 5. Prune old logs (keep 60 days) ---------------------------------------
find "$LOG_DIR" -name "*.log" -mtime +60 -delete 2>/dev/null
find "$LOG_DIR" -name "briefing-*.md" -mtime +60 -delete 2>/dev/null

echo ""
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S %Z')"
exit $AGENT_EXIT
