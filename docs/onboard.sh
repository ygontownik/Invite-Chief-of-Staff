#!/bin/bash
# onboard.sh — TCIP Pipeline new-machine bootstrap.
#
# Downloads and sets up everything needed to run the COS pipeline on a new Mac.
# Designed to be run ONCE on a fresh machine by a team member who has been
# added to the ygontownik/tcip-config GitHub repo.
#
# Usage (from the setup page):
#   curl -fsSL https://ygontownik.github.io/Dashboard/onboard.sh -o /tmp/tcip-onboard.sh
#   bash /tmp/tcip-onboard.sh

set -e

PIPELINE_REPO="https://github.com/ygontownik/Dashboard.git"
CONFIG_REPO="https://github.com/ygontownik/tcip-config.git"
PIPELINE_DIR="$HOME/cos-pipeline"
CONFIG_DIR="$HOME/cos-pipeline-config"

# Colors
G="\033[92m"; R="\033[91m"; Y="\033[93m"; B="\033[94m"; RESET="\033[0m"
ok()   { echo -e "${G}  ✓${RESET} $1"; }
warn() { echo -e "${Y}  !${RESET} $1"; }
err()  { echo -e "${R}  ✗${RESET} $1"; exit 1; }
info() { echo -e "${B}  →${RESET} $1"; }
step() { echo ""; echo -e "${B}══${RESET} ${1} ${B}══${RESET}"; }

echo ""
echo -e "${B}═══════════════════════════════════════════════════════════════${RESET}"
echo -e "${B}  TCIP Pipeline — New Machine Setup${RESET}"
echo -e "${B}═══════════════════════════════════════════════════════════════${RESET}"
echo ""

# ── Step 1: Python check ─────────────────────────────────────────────────────
step "[1/6] Checking Python"

if ! command -v python3 &>/dev/null; then
  err "python3 not found. Install from https://python.org or: brew install python@3.12"
fi
PY=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
MINOR=$(echo "$PY" | cut -d. -f2)
[ "$MINOR" -lt 9 ] && err "Need Python 3.9+, found $PY"
ok "Python $PY"

if ! command -v git &>/dev/null; then
  err "git not found. Run: xcode-select --install"
fi
ok "git $(git --version | awk '{print $3}')"

# ── Step 2: GitHub token ─────────────────────────────────────────────────────
step "[2/6] GitHub access for private config repo"

echo ""
echo "  You need a GitHub Personal Access Token (classic) to download"
echo "  the private TCIP config. If you don't have one yet:"
echo ""
echo "    1. Go to: https://github.com/settings/tokens/new"
echo "    2. Note: 'TCIP pipeline' — set expiration to 1 year"
echo "    3. Check the 'repo' scope (full control of private repositories)"
echo "    4. Click Generate token, then copy it"
echo ""
read -r -p "  Paste your GitHub username: " GH_USER
read -rs -p "  Paste your GitHub token (hidden): " GH_TOKEN
echo ""

if [ -z "$GH_USER" ] || [ -z "$GH_TOKEN" ]; then
  err "Username and token are required. Re-run the script to try again."
fi

# Quick validation — try to ls the config repo
info "Verifying access to tcip-config..."
if ! git ls-remote "https://$GH_USER:$GH_TOKEN@github.com/ygontownik/tcip-config.git" &>/dev/null; then
  err "Could not access tcip-config. Check your token and that Yoni has added your GitHub account."
fi
ok "Access confirmed"

# ── Step 3: Clone repos ──────────────────────────────────────────────────────
step "[3/6] Cloning repos"

if [ -d "$PIPELINE_DIR" ]; then
  warn "$PIPELINE_DIR already exists — pulling latest instead of cloning"
  git -C "$PIPELINE_DIR" pull --quiet
else
  info "Cloning pipeline code → $PIPELINE_DIR"
  git clone --quiet "$PIPELINE_REPO" "$PIPELINE_DIR"
fi
ok "Pipeline code: $PIPELINE_DIR"

if [ -d "$CONFIG_DIR" ]; then
  warn "$CONFIG_DIR already exists — pulling latest instead of cloning"
  git -C "$CONFIG_DIR" pull --quiet
else
  info "Cloning TCIP config → $CONFIG_DIR"
  git clone --quiet "https://$GH_USER:$GH_TOKEN@github.com/ygontownik/tcip-config.git" "$CONFIG_DIR"
fi
ok "TCIP config: $CONFIG_DIR"

# ── Step 4: COS_CONFIG_DIR in shell profile ──────────────────────────────────
step "[4/6] Shell environment"

PROFILE="$HOME/.zshrc"
[ ! -f "$PROFILE" ] && PROFILE="$HOME/.bashrc"

if grep -q "COS_CONFIG_DIR" "$PROFILE" 2>/dev/null; then
  ok "COS_CONFIG_DIR already in $PROFILE"
else
  echo "" >> "$PROFILE"
  echo '# TCIP Pipeline config location' >> "$PROFILE"
  echo "export COS_CONFIG_DIR=\"$CONFIG_DIR\"" >> "$PROFILE"
  ok "Added COS_CONFIG_DIR to $PROFILE"
fi
export COS_CONFIG_DIR="$CONFIG_DIR"

# ── Step 5: Run setup.sh ─────────────────────────────────────────────────────
step "[5/6] Running interactive setup"

echo ""
echo "  The setup script will now:"
echo "    • Install Python dependencies (pyyaml, google-auth, anthropic, pypdf)"
echo "    • Store your API key + dashboard password in macOS Keychain"
echo "    • Open a browser for Google OAuth (sign in with your Google account)"
echo "    • Install 3 background tasks (dashboard server, email triage, daily capture)"
echo ""
read -p "  Ready? Press Enter to continue (or Ctrl-C to stop here): "

cd "$PIPELINE_DIR"
./setup.sh

# ── Step 6: Personal config note ────────────────────────────────────────────
step "[6/6] Optional: personal podcast feeds"

echo ""
echo "  The shared config in $CONFIG_DIR/firm_context.yaml has the team"
echo "  setup. Your personal podcast feeds and briefing email go in your"
echo "  LOCAL copy of firm_context.yaml only (never committed to the repo)."
echo ""
echo "  To add your personal config:"
echo "    1. Open: $CONFIG_DIR/firm_context.yaml"
echo "    2. Copy the 'personal:' block from: $PIPELINE_DIR/firm_context.template.yaml"
echo "    3. Paste it at the bottom of your local firm_context.yaml and fill it in"
echo "    4. Do NOT commit that section — it's gitignored by convention"
echo ""

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${G}═══════════════════════════════════════════════════════════════${RESET}"
echo -e "${G}  ✓ Setup complete${RESET}"
echo -e "${G}═══════════════════════════════════════════════════════════════${RESET}"
echo ""
echo "  Dashboard:   http://localhost:7777"
echo "  Pipeline:    $PIPELINE_DIR"
echo "  Config:      $CONFIG_DIR"
echo ""
echo "  First run:   python3 $PIPELINE_DIR/cos_capture_pipeline.py --since 1h"
echo "  Validate:    python3 $PIPELINE_DIR/setup.py --validate"
echo ""

if command -v open &>/dev/null; then
  read -p "  Open dashboard in browser? [Y/n]: " yn
  [[ "$yn" != "n" && "$yn" != "N" ]] && open http://localhost:7777
fi
