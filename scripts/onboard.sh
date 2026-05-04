#!/bin/bash
# onboard.sh — Complete subscription-tenant onboarding in one command.
#
# Runs preflight → installs LaunchAgents → fires smoke call.
# Replaces the 7 manual paste steps in CUTOVER_RUNBOOK.md §B.2-B.4.
#
# Usage:
#   ./scripts/onboard.sh --instance=<slug>
#   ./scripts/onboard.sh --instance=re-dev --yes   # skip confirmation prompt
#
# Prerequisites (must be done before this script):
#   1. ./setup.sh --instance=<slug> --domain=<domain> --auth-mode=subscription
#   2. Claude.ai project creation (browser — no script for this)
#
# What this does:
#   1. Runs preflight_subscription.sh — aborts on any red ✗
#   2. Shows staged LaunchAgents for review
#   3. Prompts for confirmation (or --yes to skip)
#   4. Copies plists to ~/Library/LaunchAgents/ and loads them
#   5. Verifies both agents loaded (launchctl list)
#   6. Fires validate_tenant.py --call --tenant=<slug> as the smoke test
#   7. Prints PASS / FAIL summary

set -uo pipefail

INSTANCE=""
YES=false
for arg in "$@"; do
  case "$arg" in
    --instance=*) INSTANCE="${arg#*=}" ;;
    --yes|-y)     YES=true ;;
    -h|--help)
      sed -n 's/^# \?//p' "$0" | head -20; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [ -z "$INSTANCE" ]; then
  echo "Error: --instance=<slug> is required" >&2
  echo "Usage: $0 --instance=re-dev" >&2
  exit 2
fi

REPO="$HOME/cos-pipeline"
STAGED="$REPO/data-${INSTANCE}/staged-launchagents"
LA_DIR="$HOME/Library/LaunchAgents"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

G="\033[92m"; R="\033[91m"; Y="\033[93m"; RESET="\033[0m"
ok()   { echo -e "${G}  ✓${RESET} $1"; }
err()  { echo -e "${R}  ✗${RESET} $1"; }
warn() { echo -e "${Y}  !${RESET} $1"; }
die()  { echo -e "${R}ABORT:${RESET} $1" >&2; exit 1; }
hr()   { echo "───────────────────────────────────────────────────────────"; }

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  onboard.sh — instance=${INSTANCE}"
echo "═══════════════════════════════════════════════════════════"

# ── Step 1: preflight ────────────────────────────────────────────────────────
echo ""
echo "STEP 1 — Preflight checks"
hr

if ! bash "$SCRIPT_DIR/preflight_subscription.sh" --instance="$INSTANCE"; then
  die "Preflight failed. Fix the issues above, then re-run $0 --instance=$INSTANCE"
fi

# ── Step 2: locate staged plists ────────────────────────────────────────────
echo ""
echo "STEP 2 — Staged LaunchAgents"
hr

if [ ! -d "$STAGED" ]; then
  die "Staged LaunchAgents directory not found: $STAGED
  Run ./setup.sh --instance=$INSTANCE --auth-mode=subscription first."
fi

PLISTS=("$STAGED"/*.plist)
if [ ${#PLISTS[@]} -eq 0 ] || [ ! -f "${PLISTS[0]}" ]; then
  die "No .plist files found in $STAGED"
fi

echo "  Plists to install:"
for p in "${PLISTS[@]}"; do
  label=$(basename "$p" .plist)
  echo "    $label"
  # Verify not already loaded
  if launchctl list | grep -q "^[^[:space:]]*[[:space:]]*[^[:space:]]*[[:space:]]*${label}$" 2>/dev/null; then
    warn "    already loaded — will unload before reloading"
  fi
done

# ── Step 3: confirm ──────────────────────────────────────────────────────────
echo ""
if [ "$YES" = false ]; then
  printf "  Install and load these LaunchAgents? [y/N] "
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "  Aborted."; exit 0 ;;
  esac
fi

# ── Step 4: copy + load ─────────────────────────────────────────────────────
echo ""
echo "STEP 3 — Installing LaunchAgents"
hr

LOAD_FAILED=0
for p in "${PLISTS[@]}"; do
  label=$(basename "$p" .plist)
  dest="$LA_DIR/$(basename "$p")"

  # Unload if already present
  if launchctl list 2>/dev/null | grep -q "$label"; then
    launchctl unload "$dest" 2>/dev/null || true
  fi

  cp "$p" "$dest"
  if launchctl load "$dest" 2>&1; then
    ok "Loaded: $label"
  else
    err "Failed to load: $label"
    LOAD_FAILED=$((LOAD_FAILED + 1))
  fi
done

if [ "$LOAD_FAILED" -gt 0 ]; then
  die "$LOAD_FAILED LaunchAgent(s) failed to load. Check output above."
fi

# ── Step 5: verify loaded ────────────────────────────────────────────────────
echo ""
echo "STEP 4 — Verify LaunchAgents are registered"
hr

VERIFY_FAILED=0
for p in "${PLISTS[@]}"; do
  label=$(basename "$p" .plist)
  if launchctl list 2>/dev/null | grep -q "$label"; then
    ok "$label"
  else
    err "$label not found in launchctl list"
    VERIFY_FAILED=$((VERIFY_FAILED + 1))
  fi
done

if [ "$VERIFY_FAILED" -gt 0 ]; then
  die "LaunchAgent verification failed. Check 'launchctl list | grep com.cos.${INSTANCE}'"
fi

# ── Step 6: smoke call (1 subscription quota) ────────────────────────────────
echo ""
echo "STEP 5 — Subscription dispatch smoke test (1 subscription call)"
hr

PY_BIN=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  [ -x "$cand" ] || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY_BIN="$cand"; break
  fi
done

if [ -z "$PY_BIN" ]; then
  die "Python ≥3.10 not found — cannot run smoke test"
fi

if [ ! -f "$REPO/validate_tenant.py" ]; then
  die "validate_tenant.py not found at $REPO/validate_tenant.py"
fi

echo "  Running validate_tenant.py --briefing --tenant=$INSTANCE ..."
echo "  (fires 1 real subscription call — takes ~30s)"
echo ""

if "$PY_BIN" "$REPO/validate_tenant.py" --briefing --tenant="$INSTANCE" 2>&1; then
  SMOKE_OK=true
else
  SMOKE_OK=false
fi

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
hr
if [ "$SMOKE_OK" = true ]; then
  echo -e "${G}  ONBOARDING COMPLETE — ${INSTANCE} is live.${RESET}"
  echo ""
  echo "  LaunchAgents installed:"
  for p in "${PLISTS[@]}"; do
    echo "    ~/Library/LaunchAgents/$(basename "$p")"
  done
  echo ""
  echo "  Next:"
  echo "    • Watch dispatch.jsonl grow on the next scheduled fire:"
  echo "        tail -f $REPO/data-${INSTANCE}/dispatch.jsonl"
  echo "    • Daily health check:"
  echo "        $PY_BIN $REPO/_subscription_health.py --tenant=$INSTANCE"
  echo "    • Rollback if needed:"
  echo "        ./scripts/onboard.sh --instance=$INSTANCE --uninstall  (coming soon)"
  echo ""
else
  echo -e "${R}  SMOKE TEST FAILED — LaunchAgents are installed but validate_tenant failed.${RESET}"
  echo ""
  echo "  LaunchAgents were loaded. To unload:"
  for p in "${PLISTS[@]}"; do
    label=$(basename "$p" .plist)
    echo "    launchctl unload $LA_DIR/$(basename "$p")"
  done
  echo ""
  echo "  Debug:"
  echo "    $PY_BIN $REPO/validate_tenant.py --dry-run --tenant=$INSTANCE"
  echo "    cat $REPO/data-${INSTANCE}/dispatch.jsonl | tail -3 | python3 -m json.tool"
  echo ""
  exit 1
fi
