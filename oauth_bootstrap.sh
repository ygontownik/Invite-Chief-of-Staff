#!/usr/bin/env bash
# oauth_bootstrap.sh — unified Google OAuth consent for the COS Pipeline.
#
# Replaces the four scattered bootstrap-*-auth.sh scripts under
# ~/dashboards/scripts/. Single entry point with a --scope picker.
#
# Scopes:
#   full          → ~/credentials/token.json
#                   drive + documents + mail.google.com + calendar.readonly
#                   (the dashboard server + capture pipeline use this)
#   drive         → ~/credentials/gdrive_token.pickle
#                   drive + documents
#                   (cos_otter_backfill.py)
#   gmail-read    → ~/credentials/gmail_token.pickle
#                   gmail.readonly + gmail.labels
#                   (cos_email_backfill.py)
#   gmail-compose → ~/credentials/gmail_mini_token.pickle
#                   gmail.readonly + gmail.compose
#                   (capture pipeline draft creation)
#   all           → run full, drive, gmail-read, gmail-compose in sequence
#
# Idempotency: if the target token file exists and is non-empty, the scope is
# skipped (no re-consent). Pass --force to delete the existing token first.
#
# Usage:
#   ./oauth_bootstrap.sh                       # interactive scope picker
#   ./oauth_bootstrap.sh --scope=full          # one scope
#   ./oauth_bootstrap.sh --scope=all           # all four
#   ./oauth_bootstrap.sh --scope=full --force  # force re-consent for full
#
# Forces Chrome to avoid Google's "browser may not be secure" block.

set -euo pipefail

CREDS_DIR="$HOME/credentials"
SCOPE=""
FORCE=false

for arg in "$@"; do
  case "$arg" in
    --scope=*)  SCOPE="${arg#*=}" ;;
    --force)    FORCE=true ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

# Pick a Python that has google-auth-oauthlib installed.
PYBIN=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$cand" ] && "$cand" -c 'import google_auth_oauthlib' 2>/dev/null; then
    PYBIN="$cand"; break
  fi
done
if [ -z "$PYBIN" ]; then
  echo "ERROR: no python3 with google-auth-oauthlib found." >&2
  echo "       Install: pip3 install google-auth-oauthlib google-api-python-client" >&2
  exit 1
fi

# Pick the OAuth client secrets file (legacy split: client_secret.json
# vs gdrive_credentials.json — accept either).
CLIENT_SECRET=""
for cand in "$CREDS_DIR/client_secret.json" "$CREDS_DIR/gdrive_credentials.json"; do
  if [ -f "$cand" ]; then CLIENT_SECRET="$cand"; break; fi
done
if [ -z "$CLIENT_SECRET" ]; then
  echo "ERROR: no OAuth client secrets at $CREDS_DIR/{client_secret.json,gdrive_credentials.json}" >&2
  echo "       Get a Desktop client from console.cloud.google.com and save it there." >&2
  exit 1
fi

interactive_pick() {
  echo ""
  echo "Pick OAuth scope to bootstrap:"
  echo "  [1] full         — Drive + Docs + Gmail (full) + Calendar (read)  → token.json"
  echo "  [2] drive        — Drive + Docs                                   → gdrive_token.pickle"
  echo "  [3] gmail-read   — Gmail readonly + labels                        → gmail_token.pickle"
  echo "  [4] gmail-compose — Gmail readonly + compose                      → gmail_mini_token.pickle"
  echo "  [5] all          — bootstrap all four in sequence"
  echo ""
  read -p "  Choice [1-5]: " choice
  case "$choice" in
    1) SCOPE="full" ;;
    2) SCOPE="drive" ;;
    3) SCOPE="gmail-read" ;;
    4) SCOPE="gmail-compose" ;;
    5) SCOPE="all" ;;
    *) echo "Invalid choice: $choice" >&2; exit 1 ;;
  esac
}

[ -z "$SCOPE" ] && interactive_pick

run_one() {
  local name="$1"
  local token_file="$2"
  local token_format="$3"   # 'pickle' or 'json'
  shift 3
  local scopes_csv
  scopes_csv=$(IFS=,; echo "$*")

  if [ -f "$token_file" ] && [ -s "$token_file" ] && ! $FORCE; then
    echo "  ✓ $name already bootstrapped at $token_file (pass --force to redo)"
    return 0
  fi

  if $FORCE && [ -f "$token_file" ]; then
    cp "$token_file" "${token_file}.bak"
    rm "$token_file"
    echo "  · Backed up existing $token_file → ${token_file}.bak"
  fi

  echo ""
  echo "── Bootstrapping $name ──"
  echo "  Token file : $token_file"
  echo "  Scopes     : $scopes_csv"
  echo ""
  echo "  Chrome will open. Sign in as the principal account and approve."
  echo ""

  CLIENT_SECRET="$CLIENT_SECRET" \
  TOKEN_FILE="$token_file" \
  TOKEN_FORMAT="$token_format" \
  SCOPES_CSV="$scopes_csv" \
  "$PYBIN" - <<'PY'
import json
import os
import pickle
import subprocess
import webbrowser
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

client_secret = os.environ['CLIENT_SECRET']
token_path    = Path(os.environ['TOKEN_FILE'])
token_format  = os.environ['TOKEN_FORMAT']
scopes        = os.environ['SCOPES_CSV'].split(',')


def _open_chrome(url, new=0, autoraise=True):
    subprocess.Popen(['open', '-a', 'Google Chrome', url])
    return True


webbrowser.open = _open_chrome

flow  = InstalledAppFlow.from_client_secrets_file(client_secret, scopes)
creds = flow.run_local_server(port=0)

token_path.parent.mkdir(parents=True, exist_ok=True)
if token_format == 'pickle':
    with open(token_path, 'wb') as f:
        pickle.dump(creds, f)
elif token_format == 'json':
    token_path.write_text(creds.to_json())
else:
    raise SystemExit(f'unknown token_format: {token_format}')

token_path.chmod(0o600)
print(f'  ✓ Wrote {token_path}')
print(f'  ✓ Scopes: {creds.scopes}')
PY
}

case "$SCOPE" in
  full)
    run_one "full"          "$CREDS_DIR/token.json" json \
      "https://www.googleapis.com/auth/drive" \
      "https://www.googleapis.com/auth/documents" \
      "https://mail.google.com/" \
      "https://www.googleapis.com/auth/calendar.readonly"
    ;;
  drive)
    run_one "drive"         "$CREDS_DIR/gdrive_token.pickle" pickle \
      "https://www.googleapis.com/auth/drive" \
      "https://www.googleapis.com/auth/documents"
    ;;
  gmail-read)
    run_one "gmail-read"    "$CREDS_DIR/gmail_token.pickle" pickle \
      "https://www.googleapis.com/auth/gmail.readonly" \
      "https://www.googleapis.com/auth/gmail.labels"
    ;;
  gmail-compose)
    run_one "gmail-compose" "$CREDS_DIR/gmail_mini_token.pickle" pickle \
      "https://www.googleapis.com/auth/gmail.readonly" \
      "https://www.googleapis.com/auth/gmail.compose"
    ;;
  all)
    run_one "full"          "$CREDS_DIR/token.json" json \
      "https://www.googleapis.com/auth/drive" \
      "https://www.googleapis.com/auth/documents" \
      "https://mail.google.com/" \
      "https://www.googleapis.com/auth/calendar.readonly"
    run_one "drive"         "$CREDS_DIR/gdrive_token.pickle" pickle \
      "https://www.googleapis.com/auth/drive" \
      "https://www.googleapis.com/auth/documents"
    run_one "gmail-read"    "$CREDS_DIR/gmail_token.pickle" pickle \
      "https://www.googleapis.com/auth/gmail.readonly" \
      "https://www.googleapis.com/auth/gmail.labels"
    run_one "gmail-compose" "$CREDS_DIR/gmail_mini_token.pickle" pickle \
      "https://www.googleapis.com/auth/gmail.readonly" \
      "https://www.googleapis.com/auth/gmail.compose"
    ;;
  *)
    echo "ERROR: unknown scope '$SCOPE'." >&2
    echo "Valid: full | drive | gmail-read | gmail-compose | all" >&2
    exit 1
    ;;
esac

echo ""
echo "Done. Restart any affected daemons to pick up new tokens, e.g.:"
echo "  launchctl kickstart -k gui/\$(id -u)/com.yoni.cosdashboard"
