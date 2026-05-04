#!/bin/bash
# bootstrap.sh — One-command TCIP installer for new teams.
#
# Served from: https://ygontownik.github.io/Dashboard/bootstrap.sh
#
# Usage (copy this one line into Terminal):
#   curl -fsSL https://ygontownik.github.io/Dashboard/bootstrap.sh | bash
#
# What this does:
#   1. Checks dependencies (git, python3 ≥3.10, node optional)
#   2. Prompts for GitHub username + Personal Access Token
#   3. Clones the pipeline repo into ~/cos-pipeline
#   4. Hands off to setup.sh (interactive — asks slug, domain, API keys, OAuth)
#
# setup.sh handles everything from there:
#   • Python dependencies   • macOS Keychain key storage
#   • Google OAuth          • Drive folder/Doc creation (setup_new_firm.py)
#   • LaunchAgents          • Final validation + dashboard URL

set -uo pipefail

G="\033[92m"; R="\033[91m"; Y="\033[93m"; B="\033[94m"; RESET="\033[0m"
ok()   { echo -e "${G}  ✓${RESET} $1"; }
err()  { echo -e "${R}  ✗${RESET} $1"; }
warn() { echo -e "${Y}  !${RESET} $1"; }
info() { echo -e "${B}  →${RESET} $1"; }
die()  { echo -e "${R}ABORT:${RESET} $1" >&2; exit 1; }

REPO_URL="https://github.com/ygontownik/Dashboard.git"
DEST="$HOME/cos-pipeline"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  TCIP Bootstrap — New Team Setup"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Dependency checks ────────────────────────────────────────────────────────

MISSING=0

if ! command -v git >/dev/null 2>&1; then
  err "git not found — install Xcode Command Line Tools: xcode-select --install"
  MISSING=$((MISSING + 1))
fi

PY_BIN=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  [ -x "$cand" ] || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY_BIN="$cand"; break
  fi
done

if [ -z "$PY_BIN" ]; then
  err "Python ≥3.10 not found — install from python.org or: brew install python"
  MISSING=$((MISSING + 1))
else
  ok "Python: $PY_BIN"
fi

command -v git >/dev/null 2>&1 && ok "git: $(git --version | awk '{print $3}')"

if command -v node >/dev/null 2>&1; then
  ok "Node.js: $(node --version)  (optional — enables Claude Code briefing)"
else
  warn "Node.js not found — Claude Code briefing feature will be skipped (optional)"
fi

[ "$MISSING" -gt 0 ] && die "Fix the above, then re-run the bootstrap."

# ── Clone ────────────────────────────────────────────────────────────────────

if [ -d "$DEST/.git" ]; then
  warn "~/cos-pipeline already exists — pulling latest instead of cloning"
  git -C "$DEST" pull --ff-only 2>&1 | tail -3
else
  echo ""
  echo "  The pipeline repo is private. You need a GitHub Personal Access Token."
  echo "  Get one at: https://github.com/settings/tokens/new"
  echo "    Note: TCIP pipeline   Expiration: 1 year   Scope: repo (top checkbox)"
  echo ""
  printf "  GitHub username: "
  read -r GH_USER
  printf "  GitHub token (ghp_…): "
  read -rs GH_TOKEN
  echo ""

  CLONE_URL="https://${GH_USER}:${GH_TOKEN}@github.com/ygontownik/Dashboard.git"
  info "Cloning pipeline repo → ~/cos-pipeline ..."
  if git clone "$CLONE_URL" "$DEST" 2>&1 | grep -v "Cloning\|remote:\|Receiving\|Resolving\|Unpacking" || true; then
    [ -d "$DEST/.git" ] && ok "Cloned → $DEST" || die "git clone failed — check your token and try again"
  fi
fi

# ── Hand off to setup.sh ─────────────────────────────────────────────────────

echo ""
echo "  Cloning complete. Handing off to the interactive setup..."
echo ""

cd "$DEST"
exec bash setup.sh "$@"
