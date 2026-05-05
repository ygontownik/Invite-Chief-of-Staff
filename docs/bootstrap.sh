#!/bin/bash
# bootstrap.sh — One-command TCIP installer for new teams.
#
# Served from: https://ygontownik.github.io/Invite-Chief-of-Staff/bootstrap.sh
#
# Usage (copy this one line into Terminal):
#   curl -fsSL https://ygontownik.github.io/Invite-Chief-of-Staff/bootstrap.sh | bash
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

REPO_URL="https://github.com/ygontownik/Invite-Chief-of-Staff.git"
DEST="$HOME/cos-pipeline"
SHARED_KEYS_URL=""   # set via --shared-keys=<url> for admin-managed key distribution
GH_TOKEN=""

# Parse args (bootstrap.sh is usually piped from curl so args come from the wrapping call)
# Usage: curl ... | bash -s -- --shared-keys=<url>
for arg in "$@"; do
  case "$arg" in
    --shared-keys=*) SHARED_KEYS_URL="${arg#*=}" ;;
    --shared-keys)   SHARED_KEYS_URL="__prompt__" ;;  # will ask for URL interactively
  esac
done

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  TCIP Bootstrap — New Team Setup"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Dependency checks + auto-install ─────────────────────────────────────────

install_homebrew() {
  echo ""
  info "Installing Homebrew (this will take a few minutes)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || return 1
  # Add Homebrew to PATH for the rest of this script
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

install_python() {
  if ! command -v brew >/dev/null 2>&1; then
    install_homebrew || die "Homebrew install failed — install Python manually from python.org then re-run."
  fi
  info "Installing Python 3 via Homebrew..."
  brew install python || die "Python install failed — install manually from python.org then re-run."
}

# ── git ──────────────────────────────────────────────────────────────────────

if ! command -v git >/dev/null 2>&1; then
  warn "git not found — triggering Xcode Command Line Tools install..."
  echo ""
  echo "  A dialog will appear asking to install developer tools. Click Install."
  echo "  After it finishes (~5 min), re-run this bootstrap."
  echo ""
  xcode-select --install 2>/dev/null || true
  die "Re-run this script after Xcode Command Line Tools finishes installing."
fi
ok "git: $(git --version | awk '{print $3}')"

# ── Python ≥3.10 ─────────────────────────────────────────────────────────────

PY_BIN=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  [ -x "$cand" ] || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY_BIN="$cand"; break
  fi
done

if [ -z "$PY_BIN" ]; then
  warn "Python ≥3.10 not found — installing automatically..."
  install_python
  # Re-check after install
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    [ -x "$cand" ] || continue
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY_BIN="$cand"; break
    fi
  done
  [ -z "$PY_BIN" ] && die "Python install succeeded but python3 still not found — open a new Terminal and re-run."
fi
ok "Python: $PY_BIN"

# ── Node.js (optional) ───────────────────────────────────────────────────────

if command -v node >/dev/null 2>&1; then
  ok "Node.js: $(node --version)  (optional — enables Claude Code briefing)"
else
  warn "Node.js not found — Claude Code briefing feature will be skipped (optional)"
fi

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

  GH_TOKEN="$GH_TOKEN"
  CLONE_URL="https://${GH_USER}:${GH_TOKEN}@github.com/ygontownik/Invite-Chief-of-Staff.git"
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

# Pass shared-key config to setup_keychain.sh via env if admin mode is active
if [ -n "$SHARED_KEYS_URL" ] && [ "$SHARED_KEYS_URL" != "__prompt__" ]; then
  export TCIP_SHARED_KEYS_URL="$SHARED_KEYS_URL"
  export TCIP_GH_TOKEN="${GH_TOKEN:-}"
  info "Shared-key mode active — API keys will be loaded from admin config"
fi

# Strip our own flags before forwarding to setup.sh
SETUP_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --shared-keys*) ;;
    *) SETUP_ARGS+=("$arg") ;;
  esac
done

exec bash setup.sh "${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}"
