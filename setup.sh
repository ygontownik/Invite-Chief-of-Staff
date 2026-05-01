#!/bin/bash
# setup.sh — Single-command interactive setup for the COS Pipeline.
#
# Walks a new firm through every step from a fresh git clone to a working
# dashboard. Replaces the 7-step manual sequence with one guided conversation.
#
# Usage:
#     ./setup.sh                # full guided setup
#     ./setup.sh --resume       # skip steps that already look complete
#     ./setup.sh --demo         # demo mode: skip OAuth, populate fake data
#
# Time: ~5 minutes of attention spread across ~15 minutes of wall-clock time
# (waiting for browser OAuth and pip installs).

set -e

REPO="$HOME/cos-pipeline"
CREDS="$HOME/credentials"
DASHBOARDS_CFG="$HOME/dashboards/config"

# Colors
G="\033[92m"  # green
R="\033[91m"  # red
Y="\033[93m"  # yellow
B="\033[94m"  # blue
DIM="\033[2m"
RESET="\033[0m"

ok()    { echo -e "${G}  ✓${RESET} $1"; }
warn()  { echo -e "${Y}  !${RESET} $1"; }
err()   { echo -e "${R}  ✗${RESET} $1"; }
info()  { echo -e "${B}  →${RESET} $1"; }
step()  { echo ""; echo -e "${B}══${RESET} ${1} ${B}══${RESET}"; }

ask() {
  local prompt="$1"
  local default="$2"
  local var
  if [ -n "$default" ]; then
    read -p "    $prompt [$default]: " var
    var="${var:-$default}"
  else
    read -p "    $prompt: " var
  fi
  echo "$var"
}

ask_secret() {
  local prompt="$1"
  local var
  read -s -p "    $prompt: " var
  echo "" >&2
  echo "$var"
}

DEMO_MODE=false
RESUME=false
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO_MODE=true ;;
    --resume) RESUME=true ;;
  esac
done

cd "$REPO"

# ── Banner ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${B}═══════════════════════════════════════════════════════════════${RESET}"
echo -e "${B}  COS Pipeline — Interactive Setup${RESET}"
echo -e "${B}═══════════════════════════════════════════════════════════════${RESET}"
if $DEMO_MODE; then
  echo -e "${Y}  Demo mode: will populate dashboard with synthetic data${RESET}"
  echo -e "${Y}  No OAuth, no real credentials needed${RESET}"
fi
echo ""

# ── Step 1: Python ──────────────────────────────────────────────────────────
step "[1/7] Python 3.11+ check"

if ! command -v python3 >/dev/null 2>&1; then
  err "python3 not found. Install Python 3.11+ from python.org or 'brew install python@3.12'"
  exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]); then
  err "Python $PY_VERSION found; need 3.9+"
  exit 1
fi
ok "Python $PY_VERSION"

# ── Step 2: Dependencies ────────────────────────────────────────────────────
step "[2/7] Python dependencies"

DEPS="pyyaml google-auth google-auth-oauthlib google-api-python-client anthropic pypdf"
echo "    Required: $DEPS"
echo ""

MISSING=""
for pkg in pyyaml google.auth googleapiclient anthropic pypdf; do
  if ! python3 -c "import $pkg" 2>/dev/null; then
    case "$pkg" in
      pyyaml)        MISSING+=" pyyaml" ;;
      google.auth)   MISSING+=" google-auth google-auth-oauthlib" ;;
      googleapiclient) MISSING+=" google-api-python-client" ;;
      anthropic)     MISSING+=" anthropic" ;;
      pypdf)         MISSING+=" pypdf" ;;
    esac
  fi
done

if [ -n "$MISSING" ]; then
  read -p "    Install missing:$MISSING ? [Y/n]: " confirm
  if [ "$confirm" != "n" ] && [ "$confirm" != "N" ]; then
    pip3 install --quiet $MISSING && ok "Installed:$MISSING" || { err "Install failed"; exit 1; }
  else
    warn "Skipping; may fail at runtime."
  fi
else
  ok "All deps already installed"
fi

# ── Step 3: firm_context.yaml ───────────────────────────────────────────────
step "[3/7] Firm identity (firm_context.yaml)"

if [ -f "$REPO/firm_context.yaml" ] && $RESUME; then
  info "firm_context.yaml exists; skipping (use --no-resume to overwrite)"
elif [ -f "$REPO/firm_context.yaml" ]; then
  warn "firm_context.yaml already exists."
  read -p "    Overwrite? [y/N]: " confirm
  if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    info "Keeping existing firm_context.yaml"
  else
    rm "$REPO/firm_context.yaml"
  fi
fi

if [ ! -f "$REPO/firm_context.yaml" ]; then
  if $DEMO_MODE; then
    cp firm_context.template.yaml firm_context.yaml
    # Replace template placeholders with a demo firm
    sed -i.bak \
      -e 's/YOUR NAME HERE/Sarah Mitchell/' \
      -e 's/YOUR ROLE HERE/managing director, infrastructure PE/' \
      -e 's/YOUR FIRM FULL NAME/Cascade Capital Partners (DEMO)/g' \
      -e 's/MIP/CCP/g' \
      firm_context.yaml
    rm firm_context.yaml.bak
    ok "Wrote demo firm_context.yaml (Cascade Capital — synthetic)"
  else
    P_NAME=$(ask "Your full name" "")
    P_ROLE=$(ask "Your role" "managing director, infrastructure PE")
    F_NAME=$(ask "Firm full name" "")
    F_SHORT=$(ask "Firm short name (3-5 chars)" "")

    cp firm_context.template.yaml firm_context.yaml
    # Replace top-level placeholders
    sed -i.bak \
      -e "s|YOUR NAME HERE|$P_NAME|" \
      -e "s|YOUR ROLE HERE|$P_ROLE|" \
      -e "s|YOUR FIRM FULL NAME|$F_NAME|g" \
      -e "s|MIP|$F_SHORT|g" \
      firm_context.yaml
    rm firm_context.yaml.bak
    ok "Wrote firm_context.yaml — edit it to add team, peer firms, focus details"
    info "Open in your editor: $REPO/firm_context.yaml"
  fi
fi

# ── Step 4: firm_config.json ────────────────────────────────────────────────
step "[4/7] Email & doc config (firm_config.json)"

if [ ! -f "$REPO/firm_config.json" ]; then
  cp firm_config.template.json firm_config.json

  if $DEMO_MODE; then
    # Demo doc IDs are placeholders; demo mode skips Drive writes
    sed -i.bak 's/YOUR FIRM FULL NAME/Cascade Capital Partners (DEMO)/g' firm_config.json
    rm firm_config.json.bak
    ok "Wrote demo firm_config.json"
  else
    PROVIDER=$(ask "Email provider [gmail/outlook]" "gmail")
    F_NAME=$(grep '"firm_name"' firm_config.template.json | head -1 | sed 's/.*: "\([^"]*\)".*/\1/')
    F_NAME=$(ask "Firm name (for Doc titles)" "${F_NAME:-Your Firm}")
    sed -i.bak \
      -e "s|\"email_provider\": \"gmail\"|\"email_provider\": \"$PROVIDER\"|" \
      -e "s|YOUR FIRM FULL NAME|$F_NAME|g" \
      firm_config.json
    rm firm_config.json.bak
    ok "Wrote firm_config.json"
  fi
else
  info "firm_config.json exists; skipping"
fi

# ── Step 5: Secrets ─────────────────────────────────────────────────────────
step "[5/7] API keys & secrets (macOS Keychain)"

if $DEMO_MODE; then
  info "Demo mode — skipping secrets setup"
else
  # Check what's already stored
  HAVE_ANTHROPIC=$(security find-generic-password -s "cos-pipeline/ANTHROPIC_API_KEY" -a "$USER" -w 2>/dev/null || echo "")
  HAVE_DASH_USER=$(security find-generic-password -s "cos-pipeline/DASHBOARD_USERNAME" -a "$USER" -w 2>/dev/null || echo "")
  HAVE_DASH_PASS=$(security find-generic-password -s "cos-pipeline/DASHBOARD_PASSWORD" -a "$USER" -w 2>/dev/null || echo "")

  if [ -n "$HAVE_ANTHROPIC" ] && [ -n "$HAVE_DASH_USER" ] && [ -n "$HAVE_DASH_PASS" ]; then
    info "All required secrets already in Keychain (run setup_keychain.sh to update)"
  else
    info "Calling setup_keychain.sh — interactive prompts follow"
    ./setup_keychain.sh
  fi
fi

# ── Step 6: Google Docs auto-create ─────────────────────────────────────────
step "[6/7] Google Docs (creates 4 blank Docs in your Drive)"

if $DEMO_MODE; then
  info "Demo mode — skipping Drive doc creation"
else
  if [ ! -f "$CREDS/gdrive_credentials.json" ]; then
    err "$CREDS/gdrive_credentials.json missing"
    info "Get an OAuth client from Google Cloud Console:"
    info "  1. https://console.cloud.google.com/apis/credentials"
    info "  2. Create OAuth 2.0 Client ID (type: Desktop app)"
    info "  3. Download JSON → save as $CREDS/gdrive_credentials.json"
    read -p "    Press Enter when done (or Ctrl-C to skip): "
  fi

  if [ -f "$CREDS/gdrive_credentials.json" ]; then
    read -p "    Auto-create 4 Drive Docs now? Will open browser for OAuth. [Y/n]: " confirm
    if [ "$confirm" != "n" ] && [ "$confirm" != "N" ]; then
      python3 setup.py --create-docs || warn "Doc creation had issues — see output"
    fi
  fi
fi

# ── Step 7: LaunchAgents + dashboard ────────────────────────────────────────
step "[7/7] Install LaunchAgents (3 scheduled tasks)"

if $DEMO_MODE; then
  # Demo mode: just install dashboard server and populate demo data
  read -p "    Install dashboard-only LaunchAgent + populate demo data? [Y/n]: " confirm
  if [ "$confirm" != "n" ] && [ "$confirm" != "N" ]; then
    ./setup_launchagents.sh dashboard
    python3 setup.py --demo  # populates dashboard-data.json with fake data
    ok "Demo dashboard ready at http://localhost:7777"
  fi
else
  read -p "    Install all 3 LaunchAgents (dashboard, capture, gmail)? [Y/n]: " confirm
  if [ "$confirm" != "n" ] && [ "$confirm" != "N" ]; then
    ./setup_launchagents.sh all
  fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${G}═══════════════════════════════════════════════════════════════${RESET}"
echo -e "${G}  ✓ Setup complete${RESET}"
echo -e "${G}═══════════════════════════════════════════════════════════════${RESET}"
echo ""
echo "  Dashboard:    http://localhost:7777"
echo "  Repo:         $REPO"
echo "  Firm config:  $REPO/firm_context.yaml + firm_config.json"
echo ""
if $DEMO_MODE; then
  echo "  This is DEMO mode. To switch to your real data:"
  echo "    rm firm_context.yaml firm_config.json"
  echo "    ./setup.sh"
  echo ""
fi
echo "  Manual run:   python3 cos_capture_pipeline.py --since 1h"
echo "  Validate:     python3 setup.py"
echo "  Logs:         ~/dashboards/logs/claude-tasks/"
echo ""

# Open dashboard if possible
if command -v open >/dev/null 2>&1; then
  read -p "    Open dashboard in browser now? [Y/n]: " confirm
  if [ "$confirm" != "n" ] && [ "$confirm" != "N" ]; then
    sleep 2  # give the server a moment to start
    open http://localhost:7777
  fi
fi
