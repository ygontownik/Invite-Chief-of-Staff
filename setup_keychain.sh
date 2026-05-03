#!/bin/bash
# setup_keychain.sh — Store COS pipeline secrets in macOS Keychain.
#
# Why Keychain: LaunchAgent shells don't source ~/.zshrc, so environment
# variables aren't available to scheduled tasks. Keychain entries are accessible
# from any process via `security find-generic-password`.
#
# Usage:   ./setup_keychain.sh
# Reads:   prompts for each secret (input is silent for passwords/keys)
# Writes:  Keychain entries under service prefix $SERVICE_PREFIX

set -e

# B6 (ID excision): resolve SERVICE_PREFIX from firm_config.json :: keychain_service_prefix.
# Search order (per DECISIONS C3 + C11):
#   1. $SERVICE_PREFIX env var (explicit override)
#   2. $COS_CONFIG_DIR/firm_config.json :: keychain_service_prefix
#   3. ~/cos-pipeline-config-tomac/firm_config.json (canonical: slug-suffixed per C3)
#   4. ~/cos-pipeline-config/firm_config.json (legacy: pre-C3; symlinked to -tomac/)
#   5. ~/cos-pipeline/firm_config.json :: keychain_service_prefix (legacy)
#   6. "cos-pipeline" (last-resort default — emits a warning)
# Per DECISIONS.md C11, the canonical format is `cos-pipeline-<slug>`
# (e.g. cos-pipeline-tomac, cos-pipeline-re-dev).
_resolve_service_prefix() {
  if [ -n "${SERVICE_PREFIX:-}" ]; then
    echo "$SERVICE_PREFIX"
    return
  fi
  local _cfg
  for _cfg in \
      "${COS_CONFIG_DIR:+$COS_CONFIG_DIR/firm_config.json}" \
      "$HOME/cos-pipeline-config-tomac/firm_config.json" \
      "$HOME/cos-pipeline-config/firm_config.json" \
      "$HOME/cos-pipeline/firm_config.json"; do
    if [ -n "$_cfg" ] && [ -f "$_cfg" ]; then
      local _val
      _val=$(python3 -c "import json; d=json.load(open('$_cfg')); print(d.get('keychain_service_prefix',''))" 2>/dev/null)
      if [ -n "$_val" ]; then
        echo "$_val"
        return
      fi
    fi
  done
  echo "[setup_keychain] WARNING: keychain_service_prefix not set in firm_config.json — defaulting to 'cos-pipeline'." >&2
  echo "cos-pipeline"
}

SERVICE_PREFIX="$(_resolve_service_prefix)"
USER_ACCOUNT="$USER"

echo "═══════════════════════════════════════════════════════════════"
echo "  COS Pipeline — macOS Keychain Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Service prefix: $SERVICE_PREFIX"
echo "  Account:        $USER_ACCOUNT"
echo ""
echo "  Set SERVICE_PREFIX env var to use a different prefix:"
echo "    SERVICE_PREFIX=my-firm ./setup_keychain.sh"
echo ""
echo "─────────────────────────────────────────────────────────────"
echo ""

prompt_and_store() {
  local key="$1"
  local label="$2"
  local silent="$3"   # 1 = hidden input (passwords, API keys)

  local existing
  existing=$(security find-generic-password -s "$SERVICE_PREFIX/$key" -a "$USER_ACCOUNT" -w 2>/dev/null || echo "")

  if [ -n "$existing" ]; then
    echo "  ✓  $key already set (skipping — delete first to update)"
    return
  fi

  echo "  → $label"
  if [ "$silent" = "1" ]; then
    read -rs -p "    Value: " value
    echo ""
  else
    read -r -p "    Value: " value
  fi

  if [ -z "$value" ]; then
    echo "    (skipped — empty)"
    return
  fi

  security add-generic-password \
    -s "$SERVICE_PREFIX/$key" \
    -a "$USER_ACCOUNT" \
    -w "$value" \
    -U  # update if exists

  echo "    ✓ Stored"
  echo ""
}

# ── Required secrets ──────────────────────────────────────────────
echo "── REQUIRED ─────────────────────────────────────────────────"
echo ""
prompt_and_store "ANTHROPIC_API_KEY"     "Anthropic API key (sk-ant-...)"     1
prompt_and_store "DASHBOARD_USERNAME"    "Dashboard HTTP Basic Auth username" 0
prompt_and_store "DASHBOARD_PASSWORD"    "Dashboard HTTP Basic Auth password" 1

# ── Seed users.json so the dashboard login actually works ──────────
# Pre-session-4 bug: setup_keychain.sh stored DASHBOARD_USERNAME/PASSWORD in
# keychain but no consumer read them — the dashboard reads users.json. This
# step writes the entered creds into $CONFIG_DIR/config/users.json as the
# first user so users can log in with what they typed.
DUSER=$(security find-generic-password -s "$SERVICE_PREFIX/DASHBOARD_USERNAME" -a "$USER_ACCOUNT" -w 2>/dev/null || echo "")
DPASS=$(security find-generic-password -s "$SERVICE_PREFIX/DASHBOARD_PASSWORD" -a "$USER_ACCOUNT" -w 2>/dev/null || echo "")

if [ -n "$DUSER" ] && [ -n "$DPASS" ]; then
  CFG_DIR="${COS_CONFIG_DIR:-$HOME/cos-pipeline-config-tomac}"
  USERS_JSON="$CFG_DIR/config/users.json"
  mkdir -p "$(dirname "$USERS_JSON")"
  if [ -f "$USERS_JSON" ]; then
    # Append only if username doesn't already exist.
    EXISTS=$(python3 -c "
import json
try:
    d = json.load(open('$USERS_JSON'))
    users = d.get('users', d) if isinstance(d, dict) else d
    print('1' if any(u.get('username') == '$DUSER' for u in users) else '0')
except Exception:
    print('0')
" 2>/dev/null)
    if [ "$EXISTS" = "0" ]; then
      python3 - <<PYEOF
import json, datetime
p = "$USERS_JSON"
d = json.load(open(p))
if not isinstance(d, dict) or 'users' not in d:
    d = {'_comment': 'Managed via /admin/ — do not hand-edit while server is running.', 'users': []}
d['users'].append({
    'username': '$DUSER',
    'password': '$DPASS',
    'name': '$DUSER',
    'email': '',
    'tiles': ['/'],
    'created_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
})
with open(p, 'w') as f:
    json.dump(d, f, indent=2)
print('  ✓ Seeded $USERS_JSON with login user: $DUSER')
PYEOF
    else
      echo "  · users.json already has user '$DUSER' — not duplicating"
    fi
  else
    python3 - <<PYEOF
import json, datetime, os
p = "$USERS_JSON"
os.makedirs(os.path.dirname(p), exist_ok=True)
d = {
    '_comment': 'Managed via /admin/ — do not hand-edit while server is running.',
    'users': [{
        'username': '$DUSER',
        'password': '$DPASS',
        'name': '$DUSER',
        'email': '',
        'tiles': ['/'],
        'created_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
    }],
}
with open(p, 'w') as f:
    json.dump(d, f, indent=2)
print('  ✓ Created $USERS_JSON with login user: $DUSER')
PYEOF
  fi
fi

# ── Optional secrets ──────────────────────────────────────────────
echo "── OPTIONAL (press Enter to skip) ───────────────────────────"
echo ""
prompt_and_store "ASSEMBLYAI_API_KEY"    "AssemblyAI key for podcast transcription"  1
prompt_and_store "SMTP_PASSWORD"         "SMTP app password for briefing emails"      1
prompt_and_store "TWILIO_AUTH_TOKEN"     "Twilio auth token for phone recording"      1

# ── Generate load-secrets.sh helper ───────────────────────────────
LOAD_SCRIPT="$HOME/.cos-pipeline-load-secrets.sh"
cat > "$LOAD_SCRIPT" <<EOF
#!/bin/bash
# Auto-generated by setup_keychain.sh on $(date)
# Source this in any shell that needs pipeline secrets:
#     source ~/.cos-pipeline-load-secrets.sh

_kc() { security find-generic-password -s "$SERVICE_PREFIX/\$1" -a "$USER_ACCOUNT" -w 2>/dev/null; }

export ANTHROPIC_API_KEY="\$(_kc ANTHROPIC_API_KEY)"
export DASHBOARD_USERNAME="\$(_kc DASHBOARD_USERNAME)"
export DASHBOARD_PASSWORD="\$(_kc DASHBOARD_PASSWORD)"

# Optional — only export if set
ASSEMBLYAI_KEY=\$(_kc ASSEMBLYAI_API_KEY) && [ -n "\$ASSEMBLYAI_KEY" ] && export ASSEMBLYAI_API_KEY="\$ASSEMBLYAI_KEY"
SMTP_PW=\$(_kc SMTP_PASSWORD) && [ -n "\$SMTP_PW" ] && export SMTP_PASSWORD="\$SMTP_PW"
TWILIO_TOK=\$(_kc TWILIO_AUTH_TOKEN) && [ -n "\$TWILIO_TOK" ] && export TWILIO_AUTH_TOKEN="\$TWILIO_TOK"

unset -f _kc
EOF
chmod +x "$LOAD_SCRIPT"

echo "═══════════════════════════════════════════════════════════════"
echo "  ✓  Setup complete"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Loader script written to:  $LOAD_SCRIPT"
echo ""
echo "  To use in shell:           source $LOAD_SCRIPT"
echo "  To use in LaunchAgent:     have your wrapper script source it"
echo ""
echo "  To inspect a stored value: security find-generic-password -s '$SERVICE_PREFIX/KEY_NAME' -a '$USER_ACCOUNT' -w"
echo "  To delete a stored value:  security delete-generic-password -s '$SERVICE_PREFIX/KEY_NAME' -a '$USER_ACCOUNT'"
echo ""
