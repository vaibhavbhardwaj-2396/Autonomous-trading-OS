#!/usr/bin/env bash
#
# Living Quant — VPS preflight and verification.
#
#   bash scripts/vps_setup.sh              # check everything, change nothing
#   bash scripts/vps_setup.sh --backup     # also take a timestamped backup first
#
# Runs every check that has actually bitten this project, in the order that a
# failure would matter. Changes nothing except (optionally) writing a backup
# tarball. If it exits 0, the box is ready.
#
# Why a script rather than a checklist: every deployment problem this project
# has had was environmental, not code — the wrong machine, a missing PATH, a
# firewall, a timezone. Those are exactly the things a human skims past at 11pm
# and a script cannot.

set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/trading-agent}"
VENV="$PROJECT_DIR/venv/bin/python"
DO_BACKUP=false
[[ "${1:-}" == "--backup" ]] && DO_BACKUP=true

PASS=0; WARN=0; FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
warn() { echo "  ⚠ $1"; [[ -n "${2:-}" ]] && echo "      $2"; WARN=$((WARN+1)); }
bad()  { echo "  ✗ $1"; [[ -n "${2:-}" ]] && echo "      $2"; FAIL=$((FAIL+1)); }
hdr()  { echo ""; echo "── $1 ──"; }

echo "=========================================================="
echo "  LIVING QUANT — VPS PREFLIGHT"
echo "  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=========================================================="

# ---------------------------------------------------------------------------
hdr "1. Right machine?"
# ---------------------------------------------------------------------------
# This has gone wrong twice. `scp` runs on the Mac; EVERYTHING ELSE runs here.
# Prompt tells you which is which:
#   vaibhavbhardwaj@... ~ %     ← your Mac
#   root@trading-agent-v1:~#    ← the VPS

if [[ "$(uname -s)" == "Darwin" ]]; then
    bad "This is macOS — you are on your Mac, not the VPS." \
        "SSH in first:  ssh root@<your-vps-ip>   then re-run this."
    exit 1
fi
ok "Linux host ($(uname -s) $(uname -r | cut -d- -f1))"

if [[ ! -d "$PROJECT_DIR" ]]; then
    bad "No project at $PROJECT_DIR" \
        "Upload it from your Mac first:
        scp ~/Downloads/trading-agent-vulture.zip root@<vps-ip>:/root/
      then on the VPS:  cd /root && unzip -o trading-agent-vulture.zip"
    exit 1
fi
ok "Project found at $PROJECT_DIR"
cd "$PROJECT_DIR" || exit 1

# ---------------------------------------------------------------------------
hdr "2. Backup"
# ---------------------------------------------------------------------------
if $DO_BACKUP; then
    BK="/root/trading-agent-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    if tar czf "$BK" --exclude='venv' --exclude='*.db' -C /root trading-agent 2>/dev/null; then
        ok "Backup written: $BK ($(du -h "$BK" | cut -f1))"
    else
        warn "Backup failed — continuing, but you have no rollback point"
    fi
else
    warn "No backup taken (pass --backup to make one)" \
         "Recommended before the first research deploy."
fi

# ---------------------------------------------------------------------------
hdr "3. Timezone — this one fails SILENTLY"
# ---------------------------------------------------------------------------
# The recorder cron uses IST clock times. On a UTC box every cycle fires 5h30m
# early: the 10:15 option-chain snapshot lands at 04:45 IST, the market is shut,
# NSE returns an empty or stale chain, and the run still reports success because
# the fetch technically worked. You would collect nothing for months and the log
# would look fine the whole time.
TZ_NOW="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo unknown)"
if [[ "$TZ_NOW" == "Asia/Kolkata" ]]; then
    ok "Timezone is Asia/Kolkata (now: $(date '+%H:%M %Z'))"
else
    bad "Timezone is '$TZ_NOW', not Asia/Kolkata" \
        "FIX THIS BEFORE ADDING CRON:  sudo timedatectl set-timezone Asia/Kolkata
      Cron times in the runbook are IST. On a UTC box the recorder runs while
      the market is closed and records nothing — and still reports success."
fi

# ---------------------------------------------------------------------------
hdr "4. Python environment"
# ---------------------------------------------------------------------------
if [[ -x "$VENV" ]]; then
    ok "venv present ($("$VENV" --version 2>&1))"
    PY="$VENV"
else
    bad "No venv at $VENV" "python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    # Fall back to system python so the checks BELOW still report on their own
    # subject rather than all failing for one missing interpreter — a cascade of
    # wrong diagnoses is worse than one right one.
    PY="$(command -v python3 || true)"
fi

if [[ -x "$VENV" ]]; then
    for mod in pandas numpy yfinance requests dotenv; do
        if "$VENV" -c "import $mod" 2>/dev/null; then
            ok "python: $mod"
        else
            bad "python: $mod missing" "venv/bin/pip install -r requirements.txt"
        fi
    done
    if "$VENV" -c "import sqlite3, hmmlearn" 2>/dev/null; then
        ok "python: sqlite3 + hmmlearn"
    else
        warn "hmmlearn missing — the shadow HMM will be skipped (not fatal)"
    fi
fi

# ---------------------------------------------------------------------------
hdr "5. Live state and secrets — must already exist, must NOT be in the zip"
# ---------------------------------------------------------------------------
[[ -f .env ]] && ok ".env present" || bad ".env missing" "cp .env.example .env && nano .env"

if [[ -f memory/state.json ]]; then
    CAP="$("$PY" -c "import json;print(json.load(open('memory/state.json'))['allocated_capital'])" 2>/dev/null)"
    if [[ -n "$CAP" ]]; then
        ok "state.json valid — allocated_capital = ₹$CAP"
    else
        bad "state.json exists but will not parse" "Restore it from your backup."
    fi
else
    bad "memory/state.json missing" \
        "Fresh install:  cp memory/state.json.template memory/state.json  then edit it.
      Upgrade: it should NOT have been deleted — restore from backup."
fi

# ---------------------------------------------------------------------------
hdr "6. Test suites"
# ---------------------------------------------------------------------------
run_suite() {
    local name="$1" expected="$2"
    local out passed failed
    out="$("$PY" -m "tests.$name" 2>&1 | grep -E '[0-9]+ passed,' | tail -1)"
    if [[ -z "$out" ]]; then
        bad "$name — did not run" "$("$PY" -m "tests.$name" 2>&1 | tail -3)"
        return
    fi
    passed="$(echo "$out" | grep -oE '^[ ]*[0-9]+' | tr -d ' ')"
    failed="$(echo "$out" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+')"
    if [[ "$failed" != "0" ]]; then
        bad "$name — $out"
    elif [[ -n "$expected" && "$passed" != "$expected" ]]; then
        bad "$name — expected $expected passing, got $passed" \
            "The KERNEL test count changed. Something moved in engine/ that
      should not have. Restore your backup and stop."
    else
        ok "$name — $out"
    fi
}

if [[ ! -x "$VENV" ]]; then
    bad "Suites skipped — no venv" "Create it (step 4) and re-run this script."
elif true; then
    echo "  (kernel suites have exact expected counts — a change is a red flag)"
    run_suite test_guardrails 62
    run_suite test_indicators 41
    run_suite test_research_store ""
    run_suite test_research_sources ""
    run_suite test_kernel_isolation ""
    run_suite test_replay_leakage ""
fi

# ---------------------------------------------------------------------------
hdr "7. Network — the research sources need these four hosts"
# ---------------------------------------------------------------------------
for host in api.bseindia.com nsearchives.nseindia.com www.nseindia.com raw.githubusercontent.com; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
            -A 'Mozilla/5.0' "https://$host/" 2>/dev/null)"
    if [[ "$code" =~ ^(200|301|302|403)$ ]]; then
        ok "$host reachable (HTTP $code)"
    else
        bad "$host unreachable (HTTP ${code:-none})" \
            "The probe and backfill need this. Check egress/firewall."
    fi
done

# ---------------------------------------------------------------------------
hdr "8. Disk"
# ---------------------------------------------------------------------------
AVAIL_GB="$(df -BG --output=avail /root 2>/dev/null | tail -1 | tr -dc '0-9')"
if [[ -n "$AVAIL_GB" ]]; then
    if (( AVAIL_GB >= 5 )); then
        ok "${AVAIL_GB}G free (backfill ~0.5G, recorder ~1G/year)"
    elif (( AVAIL_GB >= 2 )); then
        warn "${AVAIL_GB}G free — enough to start, watch it"
    else
        bad "${AVAIL_GB}G free — not enough for the price backfill"
    fi
fi

# ---------------------------------------------------------------------------
hdr "9. Isolation — the invariant"
# ---------------------------------------------------------------------------
if grep -rlE '^\s*(from|import)\s+research' engine/*.py >/dev/null 2>&1; then
    bad "engine/ imports research/ — the invariant is broken"
else
    ok "engine/ does not import research/"
fi
for f in engine/guardrails.py engine/execute.py run_cycle.sh; do
    [[ -f "$f" ]] && ok "$f present" || bad "$f MISSING"
done

# ---------------------------------------------------------------------------
echo ""
echo "=========================================================="
printf "  %d passed, %d warning(s), %d failure(s)\n" "$PASS" "$WARN" "$FAIL"
echo "=========================================================="
if (( FAIL > 0 )); then
    echo ""
    echo "  Fix the ✗ items before going further. Nothing above changed"
    echo "  your system, so it is safe to fix and re-run."
    exit 1
fi
cat <<'NEXT'

  Ready. Next, in this order:

    1. venv/bin/python -m research.verify_install
    2. venv/bin/python research/probe/probe.py | tee logs/probe.txt
    3. venv/bin/python -m research.backfill membership
    4. tmux new -s backfill
       venv/bin/python -m research.backfill prices --from 2019-01-01 --to 2026-08-31
    5. venv/bin/python -m research.recorder --cycle intraday
    6. crontab -e   (block is in docs/RESEARCH_DEPLOY.md §3)

NEXT
exit 0
