#!/usr/bin/env bash
# TCIP Deal Onboarding Launcher
# Usage: bash tcip_onboard.sh
# Checks prerequisites, then drops into interactive deal setup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/tcip_new_deal.py"
CREDENTIALS="$HOME/credentials/gdrive_credentials.json"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  TCIP NEW DEAL ONBOARDING"
echo "════════════════════════════════════════════════════════════"

# ── 1. Python script present ────────────────────────────────────
if [[ ! -f "$PYTHON_SCRIPT" ]]; then
  echo "❌  tcip_new_deal.py not found at $PYTHON_SCRIPT"
  echo "    Put it there and re-run."
  exit 1
fi

# ── 2. ANTHROPIC_API_KEY ─────────────────────────────────────────
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo ""
  echo "⚠️  ANTHROPIC_API_KEY is not set."
  read -rp "   Enter your Anthropic API key (or press Enter to abort): " key
  [[ -z "$key" ]] && echo "Aborted." && exit 1
  export ANTHROPIC_API_KEY="$key"
fi

# ── 3. Google credentials ────────────────────────────────────────
if [[ ! -f "$CREDENTIALS" ]]; then
  echo ""
  echo "❌  Google credentials not found at $CREDENTIALS"
  echo "    Add gdrive_credentials.json to ~/credentials/ and re-run."
  exit 1
fi

# tcip_new_deal.py looks for credentials.json in its own directory.
# Symlink from tools/ → ~/credentials/ so the script finds it.
CRED_LINK="$SCRIPT_DIR/credentials.json"
if [[ ! -e "$CRED_LINK" ]]; then
  ln -s "$CREDENTIALS" "$CRED_LINK"
  echo "✓  Linked credentials.json into tools/"
fi

# ── 4. Python packages ───────────────────────────────────────────
echo ""
echo "Checking Python packages..."
missing=()
for pkg in google.oauth2 googleapiclient google_auth_oauthlib; do
  python3 -c "import ${pkg%%.*}" 2>/dev/null || missing+=("$pkg")
done

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Installing missing packages: ${missing[*]}"
  pip install google-auth google-auth-oauthlib google-api-python-client --quiet
fi
echo "✓  Packages OK"

# ── 5. Hand off to interactive Python script ────────────────────
echo ""
echo "Starting deal setup..."
echo ""
exec python3 "$PYTHON_SCRIPT" "$@"
