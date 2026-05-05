#!/bin/bash
# setup_tailscale.sh — Install Tailscale on this Mac and print your remote dashboard URL.
#
# Usage (paste into Terminal):
#   curl -fsSL https://ygontownik.github.io/Invite-Chief-of-Staff/setup_tailscale.sh | bash

set -uo pipefail

G="\033[92m"; R="\033[91m"; Y="\033[93m"; B="\033[94m"; BOLD="\033[1m"; RESET="\033[0m"
ok()   { echo -e "${G}  ✓${RESET} $1"; }
err()  { echo -e "${R}  ✗${RESET} $1"; }
warn() { echo -e "${Y}  !${RESET} $1"; }
info() { echo -e "${B}  →${RESET} $1"; }
die()  { echo -e "${R}ABORT:${RESET} $1" >&2; exit 1; }

DASH_PORT="${DASH_PORT:-7777}"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Tailscale Setup — remote dashboard access"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── 1. Install ───────────────────────────────────────────────────────────────

TAILSCALE_CLI=""

# Check if already installed (CLI path varies by install method)
for cand in \
    /Applications/Tailscale.app/Contents/MacOS/Tailscale \
    "$(command -v tailscale 2>/dev/null || true)"; do
  if [ -x "$cand" ]; then
    TAILSCALE_CLI="$cand"
    break
  fi
done

if [ -n "$TAILSCALE_CLI" ]; then
  ok "Tailscale already installed: $TAILSCALE_CLI"
else
  echo "  Tailscale is not installed. Installing now..."
  echo ""
  if command -v brew >/dev/null 2>&1; then
    info "Installing via Homebrew (this takes ~30s)..."
    brew install --cask tailscale 2>&1 | grep -E "==>|Error|already" || true
    for cand in \
        /Applications/Tailscale.app/Contents/MacOS/Tailscale \
        "$(command -v tailscale 2>/dev/null || true)"; do
      if [ -x "$cand" ]; then
        TAILSCALE_CLI="$cand"; break
      fi
    done
    [ -n "$TAILSCALE_CLI" ] && ok "Installed via Homebrew" || true
  fi

  if [ -z "$TAILSCALE_CLI" ]; then
    info "Opening Tailscale download page..."
    open "https://tailscale.com/download/mac" 2>/dev/null || true
    echo ""
    echo "  Download and install Tailscale from the page that just opened."
    echo "  Then re-run this script:"
    echo ""
    echo "    curl -fsSL https://ygontownik.github.io/Invite-Chief-of-Staff/setup_tailscale.sh | bash"
    echo ""
    exit 0
  fi
fi

# ── 2. Connect ───────────────────────────────────────────────────────────────

echo ""
echo "  Connecting to Tailscale..."
echo "  Your browser will open — sign in or create a free account."
echo ""

# Open the Tailscale app (menu bar) and connect
open -a Tailscale 2>/dev/null || true
sleep 2

# Use CLI to bring up the connection (opens browser for auth if needed)
"$TAILSCALE_CLI" up 2>&1 | grep -v "^$" | head -5 || true

# Wait for the connection (up to 60s)
echo ""
printf "  Waiting for Tailscale connection"
WAITED=0
TS_IP=""
while [ -z "$TS_IP" ] && [ "$WAITED" -lt 60 ]; do
  sleep 3
  WAITED=$((WAITED + 3))
  TS_IP=$("$TAILSCALE_CLI" ip -4 2>/dev/null || echo "")
  [ -z "$TS_IP" ] && printf "." || true
done
echo ""

if [ -z "$TS_IP" ]; then
  warn "Could not get Tailscale IP after ${WAITED}s."
  warn "Once connected, run: tailscale ip -4"
  warn "Then open: http://<that-ip>:${DASH_PORT}"
  exit 0
fi

ok "Connected — Tailscale IP: ${TS_IP}"

# ── 3. Save IP to a local file so dashboard can display it ───────────────────

DASH_CONFIG="$HOME/cos-pipeline/data-tomac/tailscale_ip.txt"
mkdir -p "$(dirname "$DASH_CONFIG")" 2>/dev/null || true
echo "$TS_IP" > "$DASH_CONFIG" 2>/dev/null || true

# ── 4. Print result ──────────────────────────────────────────────────────────

echo ""
echo -e "${G}${BOLD}═══════════════════════════════════════════════════════════${RESET}"
echo -e "${G}${BOLD}  Your dashboard is now reachable from any device:${RESET}"
echo -e "${G}${BOLD}═══════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  ${BOLD}Mac (local):  ${RESET}http://localhost:${DASH_PORT}"
echo -e "  ${BOLD}iPhone / remote:  ${RESET}http://${TS_IP}:${DASH_PORT}"
echo ""
echo "  ── iPhone setup (takes 2 minutes) ──────────────────────"
echo ""
echo "  1. On your iPhone, open the App Store"
echo "     Search: Tailscale"
echo "     Install the app by Tailscale Inc."
echo ""
echo "  2. Open the Tailscale app on your iPhone"
echo "     Tap 'Log in' → sign in with the SAME account you just used on Mac"
echo "     Tap 'Allow' on the VPN permission screen"
echo "     Tailscale connects automatically — no configuration needed"
echo ""
echo "  3. In Safari or Chrome on your iPhone, open:"
echo -e "     ${BOLD}http://${TS_IP}:${DASH_PORT}${RESET}"
echo ""
echo "  ─────────────────────────────────────────────────────────"
echo ""
echo "  Bookmark that URL on your iPhone home screen:"
echo "  Safari → Share button → 'Add to Home Screen'"
echo ""
