#!/bin/bash
# preflight_subscription.sh — read-only validator for subscription-mode tenants.
#
# Run this BEFORE flipping a tenant to auth_mode=subscription. It checks
# every prerequisite the dispatch path will rely on at runtime, without
# making any Claude API/subscription calls (so it's safe to run on a
# rate-limited or cold subscription).
#
# Usage:
#   ./scripts/preflight_subscription.sh --instance=<slug>
#   ./scripts/preflight_subscription.sh --instance=re-dev --verbose
#
# Exits 0 only if EVERY check passes. Exits 1 with a summary when any
# fails. The summary lists each failed check with the suggested fix.
#
# Read-only: this script does NOT touch ~/Library/LaunchAgents/, the
# keychain, or any pipeline data dir. It does not make Claude calls.
# It does invoke `claude --version` and `python3 -c 'import
# claude_agent_sdk'` which are local-only.

set -uo pipefail

INSTANCE=""
VERBOSE=false
for arg in "$@"; do
  case "$arg" in
    --instance=*) INSTANCE="${arg#*=}" ;;
    --verbose|-v) VERBOSE=true ;;
    -h|--help)
      grep -E '^# ?' "$0" | sed 's/^# \?//' | head -30; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [ -z "$INSTANCE" ]; then
  echo "Error: --instance=<slug> is required" >&2
  exit 2
fi

G="\033[92m"; R="\033[91m"; Y="\033[93m"; B="\033[94m"; DIM="\033[2m"; RESET="\033[0m"
ok()    { echo -e "${G}  ✓${RESET} $1"; PASSED=$((PASSED+1)); }
warn()  { echo -e "${Y}  !${RESET} $1"; }
err()   { echo -e "${R}  ✗${RESET} $1"; FAILED=$((FAILED+1)); FIXES+=("$1 :: $2"); }
info()  { echo -e "${B}  →${RESET} $1"; }

PASSED=0
FAILED=0
declare -a FIXES=()

REPO="$HOME/cos-pipeline"
CONFIG_DIR="$HOME/cos-pipeline-config-${INSTANCE}"
DATA_DIR="$REPO/data-${INSTANCE}"
LOGS_DIR="$REPO/logs-${INSTANCE}"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Subscription-mode preflight — instance=${INSTANCE}"
echo "═══════════════════════════════════════════════════════════"

# ── A. Repo + config dir layout ─────────────────────────────────────────────
echo ""
echo "A. Repo + config layout"

if [ -d "$REPO" ]; then ok "cos-pipeline repo at $REPO"
else err "cos-pipeline repo missing" "git clone the repo to ~/cos-pipeline"; fi

if [ -d "$CONFIG_DIR" ]; then ok "config dir at $CONFIG_DIR"
else err "config dir missing: $CONFIG_DIR" \
    "Run: ./setup.sh --instance=${INSTANCE} --domain=<domain> first"; fi

if [ -f "$CONFIG_DIR/firm_context.yaml" ]; then ok "firm_context.yaml present"
else err "firm_context.yaml missing in $CONFIG_DIR" \
    "Copy firm_context.template.yaml and fill in the principal/firm fields"; fi

if [ -f "$CONFIG_DIR/firm_config.json" ]; then ok "firm_config.json present"
else err "firm_config.json missing in $CONFIG_DIR" \
    "Copy firm_config.template.json"; fi

# ── B. auth_mode field ─────────────────────────────────────────────────────
echo ""
echo "B. auth_mode field (firm_context.yaml)"

if [ -f "$CONFIG_DIR/firm_context.yaml" ]; then
  AUTH_MODE=$(grep -E '^auth_mode:' "$CONFIG_DIR/firm_context.yaml" 2>/dev/null \
              | head -1 | awk '{print $2}' | tr -d '"' | tr -d "'")
  if [ "$AUTH_MODE" = "subscription" ]; then
    ok "auth_mode = subscription"
  elif [ "$AUTH_MODE" = "api" ]; then
    warn "auth_mode = api  (this preflight is meant for subscription mode)"
    info "Re-run with: ./setup.sh.subscription.next --instance=${INSTANCE}"
  elif [ -z "$AUTH_MODE" ]; then
    err "auth_mode field absent" \
        "Run: ./setup.sh.subscription.next --instance=${INSTANCE}"
  else
    err "auth_mode = '$AUTH_MODE' (must be subscription or api)" \
        "Edit $CONFIG_DIR/firm_context.yaml :: auth_mode"
  fi
fi

# ── C. claude CLI + login ───────────────────────────────────────────────────
echo ""
echo "C. claude CLI"

CLAUDE_BIN=$(command -v claude 2>/dev/null || true)
if [ -n "$CLAUDE_BIN" ]; then
  ok "claude on PATH at $CLAUDE_BIN"
  if claude --version >/dev/null 2>&1; then
    ok "claude --version works"
    $VERBOSE && info "   $(claude --version 2>&1 | head -1)"
  else
    err "claude --version failed" "Run: claude doctor"
  fi
else
  err "claude not on PATH" \
      "Install Claude Code from https://claude.com/code"
fi

# ── D. Python ≥3.10 + claude_agent_sdk ─────────────────────────────────────
echo ""
echo "D. Python ≥3.10 + claude_agent_sdk"

PY_BIN=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  [ -x "$cand" ] || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY_BIN="$cand"; break
  fi
done
if [ -n "$PY_BIN" ]; then
  ok "Python ≥3.10 at $PY_BIN ($("$PY_BIN" --version 2>&1))"
else
  err "No Python ≥3.10 on PATH" \
      "brew install python@3.13 (or pyenv install 3.13)"
fi

if [ -n "$PY_BIN" ]; then
  if "$PY_BIN" -c 'import claude_agent_sdk' 2>/dev/null; then
    ok "claude_agent_sdk imports under $(basename "$PY_BIN")"
  else
    err "claude_agent_sdk not installed under $PY_BIN" \
        "$PY_BIN -m pip install --break-system-packages 'claude-agent-sdk>=0.1.72'"
  fi
fi

# ── E. claude_projects map ─────────────────────────────────────────────────
echo ""
echo "E. claude_projects map (firm_config.json)"

if [ -f "$CONFIG_DIR/firm_config.json" ]; then
  EXPECTED_PACKAGES="briefing capture research deals"
  for pkg in $EXPECTED_PACKAGES; do
    PID=$("$PY_BIN" -c "
import json, sys
try:
    d = json.load(open('$CONFIG_DIR/firm_config.json'))
    print(d.get('claude_projects', {}).get('$pkg', '') or '')
except Exception:
    print('')
" 2>/dev/null)
    if [ -n "$PID" ]; then
      ok "claude_projects.$pkg = ${PID:0:12}…"
    else
      warn "claude_projects.$pkg is blank (preamble will be inlined per call — v1 fallback)"
    fi
  done
fi

# ── F. Domain bundle prompts ────────────────────────────────────────────────
echo ""
echo "F. Domain bundle prompts"

DOMAIN=$(grep -E '^domain:' "$CONFIG_DIR/firm_context.yaml" 2>/dev/null \
         | head -1 | awk '{print $2}' | tr -d '"' | tr -d "'")
DOMAIN="${DOMAIN:-infra-pe}"
DOMAIN_DIR="$REPO/domains/$DOMAIN/prompts"
if [ -d "$DOMAIN_DIR" ]; then
  ok "domain bundle: $DOMAIN"
  for p in briefing-morning email-triage research-summary deal-summary; do
    if [ -s "$DOMAIN_DIR/$p.txt" ]; then
      ok "  prompt $p.txt ($(wc -l < "$DOMAIN_DIR/$p.txt") lines)"
    else
      err "prompt $p.txt missing or empty in $DOMAIN_DIR" \
          "Restore from cos-pipeline/domains/$DOMAIN/prompts/"
    fi
  done
else
  err "domain bundle directory missing: $DOMAIN_DIR" \
      "Set firm_context.yaml :: domain to one of: infra-pe / real-estate / generic-dealmaker"
fi

# ── G. LaunchAgent plist templates ──────────────────────────────────────────
echo ""
echo "G. LaunchAgent plist templates"

TEMPLATES_DIR="$REPO/launchagents-templates"
if [ -d "$TEMPLATES_DIR" ]; then
  ok "templates dir: $TEMPLATES_DIR"
  for t in queue-drain subscription-health; do
    f="$TEMPLATES_DIR/com.cos.SLUG.${t}.plist.template"
    if [ -f "$f" ]; then
      # plutil -lint after substitution
      if sed -e "s|<SLUG>|${INSTANCE}|g" -e "s|<PYTHON>|${PY_BIN:-/usr/bin/python3}|g" \
              "$f" | plutil -lint - >/dev/null 2>&1; then
        ok "  $t.plist.template lints clean for ${INSTANCE}"
      else
        err "$t.plist.template fails plutil -lint after substitution" \
            "Inspect $f for malformed XML"
      fi
    else
      err "missing template: $f" \
          "Restore from cos-pipeline/launchagents-templates/"
    fi
  done
else
  err "templates dir missing: $TEMPLATES_DIR" \
      "Restore from cos-pipeline/launchagents-templates/"
fi

# ── H. Data + logs dirs ────────────────────────────────────────────────────
echo ""
echo "H. Data + logs dirs"

if [ -d "$DATA_DIR" ]; then ok "data dir: $DATA_DIR"
else warn "data dir missing: $DATA_DIR (will be created on first run)"; fi
if [ -d "$LOGS_DIR" ]; then ok "logs dir: $LOGS_DIR"
else warn "logs dir missing: $LOGS_DIR (will be created on first run)"; fi

# ── I. _model_router smoke (no API call) ───────────────────────────────────
echo ""
echo "I. _model_router smoke (route resolution only — no API/subscription call)"

ROUTER_PY="$REPO/_model_router.py"
ROUTER_NEXT="$REPO/_model_router.py.next"
TARGET_ROUTER="$ROUTER_PY"
if [ -f "$ROUTER_NEXT" ] && ! [ -f "$ROUTER_PY" ]; then TARGET_ROUTER="$ROUTER_NEXT"; fi

if [ -n "$PY_BIN" ] && [ -f "$TARGET_ROUTER" ]; then
  if "$PY_BIN" "$TARGET_ROUTER" --dry-run --tenant="$INSTANCE" >/dev/null 2>&1; then
    ok "_model_router --dry-run resolves cleanly"
  else
    err "_model_router --dry-run failed" \
        "Run: $PY_BIN $TARGET_ROUTER --dry-run --tenant=$INSTANCE"
  fi
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "───────────────────────────────────────────────────────────"
echo "  PREFLIGHT: ${PASSED} passed, ${FAILED} failed"
echo "───────────────────────────────────────────────────────────"

if [ "$FAILED" -gt 0 ]; then
  echo ""
  echo "  Suggested fixes:"
  for f in "${FIXES[@]}"; do
    msg="${f%% :: *}"
    fix="${f##* :: }"
    echo -e "    [${R}✗${RESET}] $msg"
    echo "         → $fix"
  done
  echo ""
  exit 1
fi

echo ""
echo -e "  ${G}Ready for cutover.${RESET} Tenant '${INSTANCE}' passes all"
echo "  subscription-mode prerequisites. Next: review staged plists"
echo "  and load via 'launchctl load ~/Library/LaunchAgents/...'"
echo ""
exit 0
