#!/bin/bash
# setup.sh.next — Phase 2 Track D polished orchestrator for the COS Pipeline.
#
# This is the .next companion to setup.sh. It is NOT live yet. The morning
# review approves the diff (see SETUP_DIFF.md) before this replaces setup.sh.
#
# What's new vs setup.sh (per PLAN_v3.1 §Track D):
#   D1   setup_new_firm.py is wired in as Step 6 (Doc/folder seeding)
#   D2   Friendly error if old firm_config.json :: docs schema is detected
#   D4   --validate preflight gate (exits 0 only on full pass)
#   D5   --instance=<short> drives port / data dir / logs / config / keychain / launch label
#   D6   --domain=<name> copies a domain bundle into firm_context.yaml
#   D7   Step 0 dashboard credentials callout (set up auth before anything else)
#   D8   Step 4 transcripts source picker (Otter / Beside / Fireflies / Zoom / none)
#   D9   If `which claude` succeeds, copy briefing-morning SKILL automatically
#   D10  Demo mode is no longer the default; --demo must be passed explicitly
#
# Usage:
#     ./setup.sh.next --instance=tomac --domain=infra-pe
#     ./setup.sh.next --instance=re-dev --domain=real-estate
#     ./setup.sh.next --instance=test --domain=generic-dealmaker --validate
#     ./setup.sh.next --resume                     # skip steps that look complete
#     ./setup.sh.next --demo                       # synthetic data, no OAuth
#
# Per DECISIONS.md C4–C12:
#   - tomac slug → port 7777, re-dev → 7778, +1 each additional
#   - data dir : ~/cos-pipeline/data-<slug>/
#   - logs    : ~/cos-pipeline/logs-<slug>/
#   - config  : ~/cos-pipeline-config-<slug>/  (separate private git repo)
#   - keychain prefix : cos-pipeline-<slug>
#   - LaunchAgent label prefix : com.cos.<slug>.

set -e

REPO="$HOME/cos-pipeline"
CREDS="$HOME/credentials"

# ── Colors ──────────────────────────────────────────────────────────────────
G="\033[92m"; R="\033[91m"; Y="\033[93m"; B="\033[94m"; DIM="\033[2m"; RESET="\033[0m"
ok()    { echo -e "${G}  ✓${RESET} $1"; }
warn()  { echo -e "${Y}  !${RESET} $1"; }
err()   { echo -e "${R}  ✗${RESET} $1"; }
info()  { echo -e "${B}  →${RESET} $1"; }
step()  { echo ""; echo -e "${B}══${RESET} ${1} ${B}══${RESET}"; }

ask() {
  local prompt="$1"; local default="$2"; local var
  if [ -n "$default" ]; then
    read -p "    $prompt [$default]: " var; var="${var:-$default}"
  else
    read -p "    $prompt: " var
  fi
  echo "$var"
}

# ── Args ─────────────────────────────────────────────────────────────────────
DEMO_MODE=false
RESUME=false
VALIDATE_ONLY=false
UNINSTALL=false
ASSUME_YES=false
PURGE_DATA=false
PURGE_CONFIG=false
INSTANCE=""
DOMAIN=""
for arg in "$@"; do
  case "$arg" in
    --demo)            DEMO_MODE=true ;;
    --resume)          RESUME=true ;;
    --validate)        VALIDATE_ONLY=true ;;
    --uninstall)       UNINSTALL=true ;;
    --yes|-y)          ASSUME_YES=true ;;
    --purge-data)      PURGE_DATA=true ;;
    --purge-config)    PURGE_CONFIG=true ;;
    --instance=*)      INSTANCE="${arg#*=}" ;;
    --domain=*)        DOMAIN="${arg#*=}" ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed 's/^# \?//'; exit 0 ;;
  esac
done

# ── Instance resolution (D5 + DECISIONS C4-C6,C11) ───────────────────────────
# Default slug = "tomac" only when run interactively without --instance, since
# tomac is the primary tenant. Any other tenant (re-dev, test, …) MUST pass it.
if [ -z "$INSTANCE" ]; then
  if $VALIDATE_ONLY; then
    err "--validate requires --instance=<short> (e.g. acme, peakcap)"; exit 1
  fi
  if $UNINSTALL; then
    err "--uninstall requires --instance=<short> (no default — too dangerous)"; exit 1
  fi
  INSTANCE=$(ask "Instance slug (e.g. acme, peakcap)" "acme")
fi

# Slug sanitation: lowercase, hyphenated, no spaces.
INSTANCE=$(echo "$INSTANCE" | tr '[:upper:]' '[:lower:]' | tr ' _' '--')
if [[ ! "$INSTANCE" =~ ^[a-z][a-z0-9-]{1,15}$ ]]; then
  err "Invalid instance slug: '$INSTANCE' (must be 2-16 chars, [a-z0-9-])"; exit 1
fi

# Port allocation per C6 + C17 — delegated to multi_tenant.py (single source of
# truth; reads/writes ~/cos-pipeline/data-shared/tenant-ports.json). RESERVED:
# tomac=7777, re-dev=7778. Other slugs: hash-derived in 7779–7977 range.
PORT=$(cd "$REPO" && python3 -c "
import sys
sys.path.insert(0, '.')
import multi_tenant as mt
print(mt.slug_to_port('$INSTANCE'))
")
if [ -z "$PORT" ] || [ "$PORT" -lt 1 ]; then
  err "Port allocation failed for slug '$INSTANCE' (multi_tenant.slug_to_port)"; exit 1
fi

DATA_DIR="$REPO/data-$INSTANCE"
LOG_DIR="$REPO/logs-$INSTANCE"
CONFIG_DIR="$HOME/cos-pipeline-config-$INSTANCE"
KCS_PREFIX="cos-pipeline-$INSTANCE"
LAUNCH_PREFIX="com.cos.$INSTANCE."

export COS_CONFIG_DIR="$CONFIG_DIR"
export SERVICE_PREFIX="$KCS_PREFIX"

cd "$REPO"

# ── Banner ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${B}═══════════════════════════════════════════════════════════════${RESET}"
echo -e "${B}  COS Pipeline — Interactive Setup${RESET}  (instance: ${G}$INSTANCE${RESET})"
echo -e "${B}═══════════════════════════════════════════════════════════════${RESET}"
echo "  Port           : $PORT"
echo "  Data dir       : $DATA_DIR"
echo "  Logs dir       : $LOG_DIR"
echo "  Config dir     : $CONFIG_DIR"
echo "  Keychain prefix: $KCS_PREFIX"
echo "  LaunchAgent    : ${LAUNCH_PREFIX}*"
[ -n "$DOMAIN" ] && echo "  Domain bundle  : $DOMAIN"
$DEMO_MODE && echo -e "${Y}  Demo mode: synthetic data, no OAuth${RESET}"
$VALIDATE_ONLY && echo -e "${Y}  Mode: --validate (preflight only, no changes)${RESET}"
echo ""

# ── Validate-only path (D4) ─────────────────────────────────────────────────
if $VALIDATE_ONLY; then
  step "Preflight validation for instance: $INSTANCE"
  FAIL=0

  # Gate 1: config dir exists and is a git repo
  if [ -d "$CONFIG_DIR/.git" ]; then ok "Config dir is a git repo: $CONFIG_DIR"
  else err "Config dir not a git repo: $CONFIG_DIR (per DECISIONS C3)"; FAIL=$((FAIL+1)); fi

  # Gate 2: firm_context.yaml has all required top-level keys.
  #
  # REQUIRED list rewritten 2026-05-03 (session 4) to match reality per C8/C11:
  #   - google_docs DROPPED — drive-docs.yaml is canonical per C8 (firm_context's
  #     google_docs is OPTIONAL bootstrap fallback, not required for any consumer).
  #   - keychain_service_prefix DROPPED from this gate — that field lives in
  #     firm_config.json per C11, not firm_context.yaml. Checked separately below.
  #   - domain KEPT — needed for setup.sh --domain= bundling per C12/C13.
  YAML="$CONFIG_DIR/firm_context.yaml"
  if [ ! -f "$YAML" ]; then
    err "Missing firm_context.yaml at $YAML"; FAIL=$((FAIL+1))
  else
    REQUIRED_KEYS="principal team owner_whitelist domain"
    for k in $REQUIRED_KEYS; do
      python3 -c "
import yaml, sys
d = yaml.safe_load(open('$YAML')) or {}
sys.exit(0 if '$k' in d else 1)
" 2>/dev/null && ok "firm_context.yaml :: $k present" || { err "firm_context.yaml :: $k MISSING"; FAIL=$((FAIL+1)); }
    done

    # Per-tenant config check: keychain_service_prefix must live in firm_config.json
    # (per C11). Check via the canonical location, not firm_context.yaml.
    JSONCFG="$CONFIG_DIR/firm_config.json"
    if [ -f "$JSONCFG" ]; then
      KSP=$(python3 -c "import json; print(json.load(open('$JSONCFG')).get('keychain_service_prefix',''))" 2>/dev/null)
      if [ -n "$KSP" ]; then
        ok "firm_config.json :: keychain_service_prefix = $KSP"
      else
        err "firm_config.json :: keychain_service_prefix MISSING (C11 contract)"
        FAIL=$((FAIL+1))
      fi
    fi

    # D2: refuse old schema (firm_config.json :: docs).
    # Per C8, drive-docs.yaml is canonical for doc IDs. firm_config.json :: docs
    # is the deprecated location and must be removed.
    OLDJSON="$CONFIG_DIR/firm_config.json"
    if [ -f "$OLDJSON" ] && python3 -c "import json,sys; d=json.load(open('$OLDJSON')); sys.exit(0 if 'docs' in d else 1)" 2>/dev/null; then
      err "Old schema detected: firm_config.json :: docs (deprecated per C8)"
      err "Doc IDs are canonical in drive-docs.yaml. Remove firm_config.json :: docs."
      FAIL=$((FAIL+1))
    fi
  fi

  # Gate 3: required Docs exist via Drive API
  if [ -f "$YAML" ] && [ -f "$CREDS/token.json" ]; then
    python3 - <<EOF || { err "Drive API check FAILED — see output above"; FAIL=$((FAIL+1)); }
import yaml, sys
from pathlib import Path
try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
except Exception as e:
    print(f"  ! google libs missing: {e}"); sys.exit(1)
SCOPES = ["https://www.googleapis.com/auth/drive.file","https://www.googleapis.com/auth/documents"]
creds = Credentials.from_authorized_user_file(str(Path.home()/"credentials"/"token.json"), SCOPES)
drive = build("drive","v3",credentials=creds)
y = yaml.safe_load(open("$YAML")) or {}
docs = (y.get("google_docs") or {})
missing = []
for key, did in docs.items():
    if not did:
        missing.append(key); continue
    try:
        drive.files().get(fileId=did, fields="id,name,trashed").execute()
    except Exception as e:
        missing.append(f"{key} ({did}): {type(e).__name__}")
if missing:
    print("  ✗ Missing/inaccessible Docs:", missing); sys.exit(1)
print(f"  ✓ All {len(docs)} required Docs accessible")
EOF
  else
    warn "Skipping Drive doc check (token.json or yaml absent)"
  fi

  # Gate 4: real auth surfaces (rewritten 2026-05-03 — was checking phantom
  # DASHBOARD_USERNAME/PASSWORD keychain entries that no consumer reads).
  #
  # Truth: dashboard server reads OWNER_PASSWORD/PARTNER_PASSWORD from env
  # (set in plist EnvironmentVariables) at cos-dashboard-server.py:31-32; per-
  # user creds live in $CONFIG_DIR/config/users.json. API keys (ANTHROPIC,
  # ASSEMBLYAI) live in keychain at $KCS_PREFIX/<KEY> for $USER.
  for k in ANTHROPIC_API_KEY ASSEMBLYAI_API_KEY; do
    if security find-generic-password -s "$KCS_PREFIX/$k" -a "$USER" -w >/dev/null 2>&1; then
      ok "Keychain: $KCS_PREFIX/$k"
    else
      err "Keychain MISSING: $KCS_PREFIX/$k"; FAIL=$((FAIL+1))
    fi
  done

  # Dashboard auth env (OWNER_PASSWORD, PARTNER_PASSWORD) — must be in either
  # the dashboard plist's EnvironmentVariables block OR the current shell env.
  for envk in OWNER_PASSWORD PARTNER_PASSWORD; do
    plist="$HOME/Library/LaunchAgents/com.yoni.cosdashboard.plist"
    plist_alt="$HOME/Library/LaunchAgents/com.cos.${INSTANCE}.dashboard.plist"
    plist_have=""
    for cand in "$plist_alt" "$plist"; do
      if [ -f "$cand" ] && plutil -extract "EnvironmentVariables.$envk" raw "$cand" >/dev/null 2>&1; then
        plist_have="$cand"; break
      fi
    done
    if [ -n "$plist_have" ]; then
      ok "Dashboard env: $envk set in $(basename "$plist_have")"
    elif [ -n "${!envk:-}" ]; then
      ok "Dashboard env: $envk set in current shell env"
    else
      err "Dashboard env MISSING: $envk (checked $plist_alt, $plist, shell env)"
      FAIL=$((FAIL+1))
    fi
  done

  # users.json — at least one user with username + password.
  USERS_JSON="$CONFIG_DIR/config/users.json"
  if [ ! -f "$USERS_JSON" ]; then
    err "Per-user config MISSING: $USERS_JSON"; FAIL=$((FAIL+1))
  else
    USER_COUNT=$(python3 -c "
import json, sys
try:
    d = json.load(open('$USERS_JSON'))
    users = d.get('users', d) if isinstance(d, dict) else d
    valid = [u for u in users if isinstance(u, dict) and u.get('username') and u.get('password')]
    print(len(valid))
except Exception:
    print(0)
" 2>/dev/null)
    if [ "${USER_COUNT:-0}" -ge 1 ]; then
      ok "users.json: $USER_COUNT user(s) with username+password"
    else
      err "users.json: 0 valid users (need at least 1 with username + password)"
      FAIL=$((FAIL+1))
    fi
  fi

  # Gate 5: port free
  if python3 -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))!=0 else 1)" 2>/dev/null; then
    ok "Port $PORT is free"
  else
    # Port in use: only OK if it's our own dashboard for this instance.
    warn "Port $PORT in use — assuming our dashboard (verify with: lsof -i :$PORT)"
  fi

  # Gate 6: no LaunchAgent label collision with a *different* instance
  COLLIDE=$(ls "$HOME/Library/LaunchAgents/" 2>/dev/null | grep -E '^com\.cos\.[a-z0-9-]+\.' | grep -v "^${LAUNCH_PREFIX}" | head -3 || true)
  # Collisions across instances are EXPECTED (multi-tenant). Only fail if a
  # *generic* (com.cos-pipeline.*) LaunchAgent exists — that's the legacy
  # single-tenant naming and would conflict with port allocation.
  LEGACY=$(ls "$HOME/Library/LaunchAgents/" 2>/dev/null | grep '^com\.cos-pipeline\.' || true)
  if [ -n "$LEGACY" ]; then
    err "Legacy LaunchAgent labels found (com.cos-pipeline.*) — must be renamed to ${LAUNCH_PREFIX}*"
    echo "$LEGACY" | sed 's/^/      /'
    FAIL=$((FAIL+1))
  else
    ok "No legacy LaunchAgent label collisions"
  fi

  echo ""
  if [ "$FAIL" -eq 0 ]; then
    echo -e "${G}═══ VALIDATE: PASS ($INSTANCE) ═══${RESET}"; exit 0
  else
    echo -e "${R}═══ VALIDATE: FAIL ($FAIL gates) ═══${RESET}"; exit 1
  fi
fi

# ── Uninstall path (Track 2 Build #6) ───────────────────────────────────────
# Tear down a tenant install: bootout LaunchAgents, remove plists, sweep
# canonical keychain entries, and (with confirmation) remove data/log/config
# dirs. Idempotent — running twice in a row is safe.
#
# Slug-isolated: only touches com.cos.<slug>.* labels, cos-pipeline-<slug>/*
# keychain entries, and *-<slug>/ directories. Other tenants on the same Mac
# are unaffected.
#
# Flags:
#   --uninstall              required mode flag
#   --instance=<slug>        required (no default — too dangerous)
#   --yes / -y               skip confirmation prompts
#   --purge-data             remove data-<slug>/ + logs-<slug>/
#   --purge-config           remove ~/cos-pipeline-config-<slug>/
if $UNINSTALL; then
  step "Uninstall instance: $INSTANCE"
  echo "  LaunchAgent prefix : ${LAUNCH_PREFIX}*"
  echo "  Keychain prefix    : ${KCS_PREFIX}/*"
  echo "  Data dir           : $DATA_DIR  $($PURGE_DATA && echo '(WILL REMOVE)' || echo '(keep)')"
  echo "  Logs dir           : $LOG_DIR   $($PURGE_DATA && echo '(WILL REMOVE)' || echo '(keep)')"
  echo "  Config dir         : $CONFIG_DIR  $($PURGE_CONFIG && echo '(WILL REMOVE)' || echo '(keep)')"
  echo ""

  if ! $ASSUME_YES; then
    read -p "    Proceed with uninstall? [y/N]: " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
      err "Aborted."; exit 1
    fi
  fi

  REMOVED=0; FAILED=0

  # 1. LaunchAgents — bootout + delete plists via _scheduler.unregister().
  info "Sweeping LaunchAgents matching ${LAUNCH_PREFIX}*"
  AGENT_LABELS=$(python3 -c "
import sys
sys.path.insert(0, '$REPO')
import _scheduler
for label in _scheduler.list_registered(prefix='${LAUNCH_PREFIX}'):
    print(label)
")
  if [ -z "$AGENT_LABELS" ]; then
    ok "No LaunchAgents to remove (already clean)"
  else
    while IFS= read -r label; do
      [ -z "$label" ] && continue
      if python3 -c "
import sys
sys.path.insert(0, '$REPO')
import _scheduler
sys.exit(0 if _scheduler.unregister('$label') else 1)
" 2>/dev/null; then
        ok "Removed $label"
        REMOVED=$((REMOVED+1))
      else
        err "Failed to remove $label"
        FAILED=$((FAILED+1))
      fi
    done <<< "$AGENT_LABELS"
  fi

  # 2. Keychain — delete every entry whose service starts with $KCS_PREFIX/.
  # security has no glob, so dump and grep service names.
  info "Sweeping keychain entries under ${KCS_PREFIX}/*"
  KC_SERVICES=$(security dump-keychain ~/Library/Keychains/login.keychain-db 2>/dev/null \
    | grep -E '"svce"<blob>="'"${KCS_PREFIX}"'/' \
    | sed 's/.*"svce"<blob>="\(.*\)"$/\1/' \
    | sort -u)
  if [ -z "$KC_SERVICES" ]; then
    ok "No keychain entries to remove (already clean)"
  else
    while IFS= read -r svc; do
      [ -z "$svc" ] && continue
      if security delete-generic-password -s "$svc" -a "$USER" >/dev/null 2>&1; then
        ok "Removed keychain $svc (account=$USER)"
        REMOVED=$((REMOVED+1))
      else
        warn "Could not delete $svc (may not match account=$USER)"
      fi
    done <<< "$KC_SERVICES"
  fi

  # 3. Data + log dirs (gated by --purge-data).
  if $PURGE_DATA; then
    for d in "$DATA_DIR" "$LOG_DIR"; do
      if [ -d "$d" ]; then
        if ! $ASSUME_YES; then
          read -p "    Delete $d? [y/N]: " confirm
          [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { warn "Skipped $d"; continue; }
        fi
        rm -rf "$d" && ok "Removed $d" && REMOVED=$((REMOVED+1)) \
          || { err "Failed to remove $d"; FAILED=$((FAILED+1)); }
      fi
    done
  else
    info "Skipping data/log dir removal (pass --purge-data to remove)"
  fi

  # 4. Config dir (gated by --purge-config — separate flag because configs
  #    often hold the only copy of firm_context.yaml + drive-docs.yaml).
  if $PURGE_CONFIG; then
    if [ -d "$CONFIG_DIR" ]; then
      if ! $ASSUME_YES; then
        read -p "    Delete $CONFIG_DIR (config repo, possibly only copy)? [y/N]: " confirm
        [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && warn "Skipped $CONFIG_DIR" \
          || { rm -rf "$CONFIG_DIR" && ok "Removed $CONFIG_DIR" && REMOVED=$((REMOVED+1)); }
      else
        rm -rf "$CONFIG_DIR" && ok "Removed $CONFIG_DIR" && REMOVED=$((REMOVED+1))
      fi
    fi
  else
    info "Skipping config dir removal (pass --purge-config to remove)"
  fi

  echo ""
  if [ "$FAILED" -eq 0 ]; then
    echo -e "${G}═══ UNINSTALL: complete ($REMOVED items removed) ═══${RESET}"; exit 0
  else
    echo -e "${R}═══ UNINSTALL: $FAILED failures ($REMOVED items removed) ═══${RESET}"; exit 1
  fi
fi

# ── Step 0: Dashboard credentials callout (D7) ──────────────────────────────
step "[0/8] Dashboard credentials"

cat <<EOF
  The dashboard at http://localhost:$PORT is protected by login.
  In Step 5 you'll be prompted for a USERNAME and PASSWORD; setup_keychain.sh
  will (a) store them in macOS Keychain for daemon access and (b) seed them
  into $CONFIG_DIR/config/users.json so you can log into the dashboard.

  Pick credentials NOW so you have them ready:
    • Username : something memorable (default suggestion: your first name)
    • Password : long random string — save it to your password manager FIRST

  After install completes, log in at http://localhost:$PORT/ with the
  username + password you typed in Step 5. Add additional users any time
  via the Admin tile (Access Management tab).
EOF
read -p "    Press Enter when you have chosen + saved credentials… " _

# ── Step 1: Host prerequisites (Phase 0 in BOOTSTRAP_PLAN) ──────────────────
step "[1/8] Host prerequisites"

# 1a. macOS — primary supported host today (Linux is roadmap; cloud is roadmap).
if [ "$(uname)" != "Darwin" ]; then
  err "Unsupported OS: $(uname). macOS 13+ is the only tier-1 host today."
  err "Linux/cloud support is in BOOTSTRAP_PLAN roadmap, not yet built."
  exit 1
fi
MACOS_VERSION=$(sw_vers -productVersion 2>/dev/null || echo "?")
MACOS_MAJOR=$(echo "$MACOS_VERSION" | cut -d. -f1)
if [ "$MACOS_MAJOR" -lt 13 ] 2>/dev/null; then
  warn "macOS $MACOS_VERSION detected; tier-1 support is 13+. Continuing anyway."
else
  ok "macOS $MACOS_VERSION"
fi

# 1b. Python 3.11+ (BOOTSTRAP_PLAN Phase 0 raises floor from 3.9 → 3.11).
if ! command -v python3 >/dev/null 2>&1; then
  err "python3 not found. Install via Homebrew: 'brew install python@3.12'"; exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1); PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]); then
  err "Python $PY_VERSION found; need 3.11+ (BOOTSTRAP_PLAN Phase 0)"
  err "Install via: 'brew install python@3.12'"
  exit 1
fi
ok "Python $PY_VERSION"

# 1c. Homebrew (used by tenants to install python/git on a fresh Mac).
if command -v brew >/dev/null 2>&1; then
  ok "Homebrew $(brew --version | head -1 | awk '{print $2}')"
else
  warn "Homebrew not found. Install: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
fi

# 1d. git — required for config repo + pulling updates.
if ! command -v git >/dev/null 2>&1; then
  err "git not found. Install: 'xcode-select --install' or 'brew install git'"; exit 1
fi
ok "git $(git --version | awk '{print $3}')"

# 1e. GitHub CLI — needed for repo creation in step 3d. Auto-install via brew.
if command -v gh >/dev/null 2>&1; then
  ok "GitHub CLI $(gh --version | head -1 | awk '{print $3}')"
elif command -v brew >/dev/null 2>&1; then
  info "Installing GitHub CLI…"
  brew install gh --quiet && ok "GitHub CLI installed" \
    || warn "brew install gh failed — repo creation in step 3d will be skipped"
else
  warn "GitHub CLI unavailable (no Homebrew) — repo creation in step 3d will be skipped"
fi

# 1f. Claude Code CLI — optional in cloud-mode; warn-only on Mac since SKILL
# daemons need it for the briefing/capture flows.
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code CLI present"
else
  warn "Claude Code CLI not found — SKILL-based daemons (briefing, capture) won't fire."
  warn "Install per https://docs.claude.com/claude-code if needed."
fi

# ── Step 2: Dependencies ────────────────────────────────────────────────────
step "[2/8] Python dependencies"
DEPS="pyyaml google-auth google-auth-oauthlib google-api-python-client anthropic pypdf"
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
  fi
else
  ok "All deps already installed"
fi

# ── Step 3: Config dir + firm_context.yaml ──────────────────────────────────
step "[3/8] Config dir + firm_context.yaml"

mkdir -p "$DATA_DIR" "$LOG_DIR" "$CONFIG_DIR"

# DECISIONS C3: config dir is a separate private git repo
if [ ! -d "$CONFIG_DIR/.git" ]; then
  (cd "$CONFIG_DIR" && git init -q && git commit --allow-empty -m "init $INSTANCE config" -q) || warn "git init in $CONFIG_DIR failed"
  ok "Initialized git repo in $CONFIG_DIR"
fi

# D2: detect old schema and bail loud
OLD_JSON="$CONFIG_DIR/firm_config.json"
if [ -f "$OLD_JSON" ]; then
  if python3 -c "import json,sys; d=json.load(open('$OLD_JSON')); sys.exit(0 if 'docs' in d else 1)" 2>/dev/null; then
    err "Old schema detected: $OLD_JSON :: docs"
    err "Schema migrated; move docs into firm_context.yaml :: google_docs and re-run."
    err "(Per DECISIONS C8 — firm_context.yaml :: google_docs is canonical.)"
    exit 1
  fi
fi

YAML="$CONFIG_DIR/firm_context.yaml"
if [ -f "$YAML" ] && $RESUME; then
  info "$YAML exists; resuming"
elif [ ! -f "$YAML" ]; then
  if $DEMO_MODE; then
    cp firm_context.template.yaml "$YAML"
    sed -i.bak \
      -e 's/YOUR NAME HERE/Sarah Mitchell/' \
      -e 's/YOUR ROLE HERE/managing director, infrastructure PE/' \
      -e 's/YOUR FIRM FULL NAME/Cascade Capital Partners (DEMO)/g' \
      -e 's/MIP/CCP/g' "$YAML"
    rm -f "$YAML.bak"
    ok "Wrote demo firm_context.yaml"
  else
    P_NAME=$(ask "Your full name" "")
    P_ROLE=$(ask "Your role" "managing director")
    F_NAME=$(ask "Firm full name" "")
    F_SHORT=$(ask "Firm short name (3-5 chars)" "$INSTANCE")
    cp firm_context.template.yaml "$YAML"
    sed -i.bak \
      -e "s|YOUR NAME HERE|$P_NAME|" \
      -e "s|YOUR ROLE HERE|$P_ROLE|" \
      -e "s|YOUR FIRM FULL NAME|$F_NAME|g" \
      -e "s|MIP|$F_SHORT|g" "$YAML"
    rm -f "$YAML.bak"
    ok "Wrote $YAML"
  fi
fi

# Stamp keychain_service_prefix into firm_context.yaml (DECISIONS C11)
python3 - <<EOF
import yaml
p = "$YAML"
y = yaml.safe_load(open(p)) or {}
y["keychain_service_prefix"] = "$KCS_PREFIX"
yaml.safe_dump(y, open(p, "w"), sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)
EOF

# ── Step 3b: Domain bundle (D6 + DECISIONS C12, C13) ────────────────────────
if [ -n "$DOMAIN" ]; then
  step "[3b/8] Domain bundle: $DOMAIN"
  case "$DOMAIN" in
    real-estate|infra-pe|generic-dealmaker) : ;;
    *) err "Invalid --domain=$DOMAIN (allowed: real-estate, infra-pe, generic-dealmaker)"; exit 1 ;;
  esac
  SRC="$REPO/domains/$DOMAIN"
  if [ ! -d "$SRC" ]; then
    err "Domain bundle not found: $SRC"; exit 1
  fi
  cp -R "$SRC/"* "$CONFIG_DIR/"
  python3 - <<EOF
import yaml
p = "$YAML"
y = yaml.safe_load(open(p)) or {}
y["domain"] = "$DOMAIN"
yaml.safe_dump(y, open(p, "w"), sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)
EOF
  ok "Domain bundle '$DOMAIN' copied to $CONFIG_DIR; firm_context.yaml :: domain stamped"
fi

# ── Step 3c: Market Intelligence — sectors, email senders, websites ───────────
echo ""
echo "  ── Market Intelligence personalisation ──────────────────────────────"
echo ""

# 1. Sectors
echo "  What sectors or topics does this user focus on?"
echo "  (Drives briefing themes, source suggestions, and synthesis style.)"
echo ""
echo "    1) Power & Utilities        — generation, transmission, grid"
echo "    2) Digital Infrastructure   — data centers, fiber, towers"
echo "    3) Midstream & LNG          — pipelines, gas processing, export"
echo "    4) Real Estate              — CRE, multifamily, industrial"
echo "    5) Private Credit           — lending, structured finance"
echo "    6) Macro / Capital Markets  — rates, M&A, dealmaking"
echo "    7) Healthcare Infrastructure"
echo ""
echo "  Enter numbers separated by spaces (e.g. 1 2 3) or press Enter to skip:"
read -r _SECTOR_PICKS

_SECTOR_LIST=""
for _sp in $_SECTOR_PICKS; do
  case "$_sp" in
    1) _SECTOR_LIST+="Power & Utilities|" ;;
    2) _SECTOR_LIST+="Digital Infrastructure|" ;;
    3) _SECTOR_LIST+="Midstream & LNG|" ;;
    4) _SECTOR_LIST+="Real Estate|" ;;
    5) _SECTOR_LIST+="Private Credit|" ;;
    6) _SECTOR_LIST+="Macro / Capital Markets|" ;;
    7) _SECTOR_LIST+="Healthcare Infrastructure|" ;;
  esac
done
_SECTOR_LIST="${_SECTOR_LIST%|}"

if [ -n "$_SECTOR_LIST" ]; then
  python3 - <<PYEOF
import yaml
sectors = "$_SECTOR_LIST".split("|")
p = "$YAML"
y = yaml.safe_load(open(p)) or {}
y.setdefault("personal", {}).setdefault("intelligence", {})["sectors"] = sectors
yaml.safe_dump(y, open(p, "w"), sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)
PYEOF
  ok "Sectors written: $_SECTOR_LIST"
else
  info "No sectors selected — edit personal.intelligence.sectors in firm_context.yaml later."
fi

# 2. Email senders
echo ""
echo "  What email addresses send you research or newsletters?"
echo "  Works with Gmail and Outlook — no labeling or folders required."
echo "  Example: newsletter@rbnenergy.com, digest@bloomberg.com"
echo ""
printf "  Sender addresses (comma-separated, or Enter to skip): "
read -r _SENDERS_RAW

if [ -n "$_SENDERS_RAW" ]; then
  python3 - <<PYEOF
import yaml, re
raw = "$_SENDERS_RAW"
senders = [s.strip() for s in re.split(r"[,\s]+", raw) if s.strip() and "@" in s]
p = "$YAML"
y = yaml.safe_load(open(p)) or {}
y.setdefault("personal", {}).setdefault("intelligence", {})["research_senders"] = senders
yaml.safe_dump(y, open(p, "w"), sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)
print(f"  Saved {len(senders)} sender(s).")
PYEOF
  ok "Research senders saved."
else
  info "No email senders — edit personal.intelligence.research_senders in firm_context.yaml later."
fi

# 3. Websites
echo ""
echo "  What websites do you read for market intelligence?"
echo "  Auto-detects RSS if available; otherwise scrapes the homepage daily."
echo "  Example: https://rbnenergy.com, https://www.spglobal.com/commodityinsights/en"
echo ""
printf "  Website URLs (comma-separated, or Enter to skip): "
read -r _WEBSITES_RAW

if [ -n "$_WEBSITES_RAW" ]; then
  python3 - <<PYEOF
import yaml, re
raw = "$_WEBSITES_RAW"
sites = [s.strip() for s in re.split(r"[,\s]+", raw) if s.strip().startswith("http")]
p = "$YAML"
y = yaml.safe_load(open(p)) or {}
y.setdefault("personal", {}).setdefault("intelligence", {})["websites"] = sites
yaml.safe_dump(y, open(p, "w"), sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)
print(f"  Saved {len(sites)} website(s).")
PYEOF
  ok "Websites saved — will be checked daily at 6:45am."
else
  info "No websites — edit personal.intelligence.websites in firm_context.yaml later."
fi

# ── Step 3d: GitHub repos (naming convention) ────────────────────────────────
step "[3d/8] GitHub repos"

if $DEMO_MODE; then
  info "Demo mode — skipping GitHub repo creation"
elif ! command -v gh >/dev/null 2>&1; then
  warn "GitHub CLI not found — skipping repo creation (re-run with --resume after: brew install gh)"
else
  # Authenticate inline if needed — no separate step required
  if ! gh auth status >/dev/null 2>&1; then
    info "GitHub CLI needs authentication — launching gh auth login…"
    gh auth login || warn "GitHub auth failed — skipping repo creation"
  fi

  if gh auth status >/dev/null 2>&1; then
    FIRST_NAME=$(echo "${P_NAME%% *}" | awk '{print toupper(substr($0,1,1)) tolower(substr($0,2))}')
    GH_USER=$(gh api user --jq .login 2>/dev/null)
    DASH_REPO="Private-${FIRST_NAME}-Dashboard"
    CONF_REPO="Private-${FIRST_NAME}-Config"

    info "Creating GitHub repos for $FIRST_NAME (github.com/${GH_USER})…"

    # Dashboard repo
    gh repo create "$DASH_REPO" --private \
      --description "Personal dashboard server for $P_NAME. PRIVATE — $FIRST_NAME only." \
      >/dev/null 2>&1 \
      && ok "Created: $DASH_REPO" \
      || info "$DASH_REPO already exists — skipping"

    # Config repo
    gh repo create "$CONF_REPO" --private \
      --description "Personal runtime config for $P_NAME. PRIVATE — $FIRST_NAME only. Contains doc IDs and credentials. Never share." \
      >/dev/null 2>&1 \
      && ok "Created: $CONF_REPO" \
      || info "$CONF_REPO already exists — skipping"

    # Wire local config dir to config repo and push
    CONF_REMOTE="https://github.com/${GH_USER}/${CONF_REPO}.git"
    EXISTING_REMOTE=$(cd "$CONFIG_DIR" && git remote get-url origin 2>/dev/null || echo "")
    if [ -z "$EXISTING_REMOTE" ]; then
      (cd "$CONFIG_DIR" \
        && git remote add origin "$CONF_REMOTE" \
        && git add -A \
        && git commit -m "init $INSTANCE config" -q 2>/dev/null || true \
        && git branch -M main 2>/dev/null || true \
        && git push -u origin main -q 2>/dev/null) \
        && ok "Config repo wired and pushed → $CONF_REMOTE" \
        || warn "Push failed — run: cd $CONFIG_DIR && git push -u origin main"
    else
      info "Config remote already set — skipping"
    fi
  fi
fi

# ── Step 3e: Seed dashboard-tiles.yaml (tile registry) ──────────────────────
step "[3e/8] Dashboard tile registry"

# Public default tile registry lives at config/dashboard-tiles.template.yaml.
# Copy to the tenant config dir on first install only — skip if tenant has
# already customized their copy. The live tenant copy is what the server
# reads (resolved via the synthetic ~/dashboards/config -> $CONFIG_DIR/config
# symlink set up in Step 6d).
TILES_SRC="$REPO/config/dashboard-tiles.template.yaml"
TILES_DST="$CONFIG_DIR/config/dashboard-tiles.yaml"
mkdir -p "$CONFIG_DIR/config"
if [ -f "$TILES_DST" ]; then
  info "Tile registry exists at $TILES_DST — keeping tenant customizations"
elif [ -f "$TILES_SRC" ]; then
  cp "$TILES_SRC" "$TILES_DST" && ok "Seeded tile registry → $TILES_DST"
else
  warn "Template missing: $TILES_SRC — server will fall back to built-in defaults"
fi

# ── Step 4: Transcripts source picker (D8) ──────────────────────────────────
step "[4/8] Transcripts source"
echo "    Pick which transcript app(s) feed this instance:"
echo "      1) Otter AI"
echo "      2) Beside"
echo "      3) Fireflies"
echo "      4) Zoom"
echo "      5) None (skip transcript ingestion for now)"
TS_PICK=$(ask "Choice (1-5)" "1")
case "$TS_PICK" in
  1) TS_NAME="otter" ;;
  2) TS_NAME="beside" ;;
  3) TS_NAME="fireflies" ;;
  4) TS_NAME="zoom" ;;
  5) TS_NAME="none" ;;
  *) TS_NAME="otter"; warn "Unrecognized; defaulting to Otter" ;;
esac
python3 - <<EOF
import yaml
p = "$YAML"
y = yaml.safe_load(open(p)) or {}
y.setdefault("personal", {})
y["personal"]["transcript_source"] = "$TS_NAME"
yaml.safe_dump(y, open(p, "w"), sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)
EOF
ok "Transcript source: $TS_NAME (stamped to firm_context.yaml :: personal.transcript_source)"

# ── Step 5: Secrets (Keychain) ──────────────────────────────────────────────
step "[5/8] API keys & secrets (macOS Keychain, prefix: $KCS_PREFIX)"
if $DEMO_MODE; then
  info "Demo mode — skipping secrets"
else
  HAVE_A=$(security find-generic-password -s "$KCS_PREFIX/ANTHROPIC_API_KEY" -a "$USER" -w 2>/dev/null || echo "")
  HAVE_U=$(security find-generic-password -s "$KCS_PREFIX/DASHBOARD_USERNAME" -a "$USER" -w 2>/dev/null || echo "")
  HAVE_P=$(security find-generic-password -s "$KCS_PREFIX/DASHBOARD_PASSWORD" -a "$USER" -w 2>/dev/null || echo "")
  if [ -n "$HAVE_A" ] && [ -n "$HAVE_U" ] && [ -n "$HAVE_P" ]; then
    info "All required secrets present under '$KCS_PREFIX'"
  else
    SERVICE_PREFIX="$KCS_PREFIX" ./setup_keychain.sh
  fi
fi

# ── Step 6: Google OAuth + Drive folders/Docs ───────────────────────────────
# OAuth bootstrap is consolidated in oauth_bootstrap.sh (Track 2 follow-up
# to BOOTSTRAP_PLAN Phase 4). Replaces the four scattered bootstrap-*-auth.sh
# scripts under ~/dashboards/scripts/.
step "[6/8] Google OAuth + Drive folders/Docs"
if $DEMO_MODE; then
  info "Demo mode — skipping OAuth + Drive seeding"
else
  # 6a. OAuth client secrets must be present (either canonical name accepted).
  if [ ! -f "$CREDS/client_secret.json" ] && [ ! -f "$CREDS/gdrive_credentials.json" ]; then
    # Auto-detect the file from ~/Downloads — subscriber just needs to save it there.
    mkdir -p "$CREDS"
    DL_CRED="$HOME/Downloads/gdrive_credentials.json"
    if [ -f "$DL_CRED" ]; then
      cp "$DL_CRED" "$CREDS/gdrive_credentials.json"
      ok "Found gdrive_credentials.json in Downloads — moved to ~/credentials/"
    else
      warn "Waiting for gdrive_credentials.json (Yoni will send this via secure channel)"
      info "Save it to ~/Downloads/gdrive_credentials.json — this script will pick it up automatically."
      echo ""
      printf "    Checking every 5s... (Ctrl-C to skip OAuth): "
      WAITED=0
      while [ ! -f "$DL_CRED" ] && [ ! -f "$CREDS/gdrive_credentials.json" ]; do
        sleep 5
        WAITED=$((WAITED + 5))
        printf "."
        [ "$WAITED" -ge 300 ] && break
      done
      echo ""
      if [ -f "$DL_CRED" ]; then
        cp "$DL_CRED" "$CREDS/gdrive_credentials.json"
        ok "Found gdrive_credentials.json — moved to ~/credentials/"
      elif [ ! -f "$CREDS/gdrive_credentials.json" ]; then
        warn "gdrive_credentials.json not found after ${WAITED}s — skipping OAuth (run ./setup.sh --resume later)"
      fi
    fi
  fi

  # 6b. Bootstrap tokens. Idempotent — skips scopes whose token already exists.
  if [ -f "$CREDS/client_secret.json" ] || [ -f "$CREDS/gdrive_credentials.json" ]; then
    info "Bootstrapping OAuth tokens (skip per-scope if already consented)"
    "$REPO/oauth_bootstrap.sh" --scope=all \
      || warn "oauth_bootstrap.sh reported issues — review above"
  fi

  # 6c. Drive folder/Doc seeding (depends on tokens from 6b).
  if [ -f "$CREDS/client_secret.json" ] || [ -f "$CREDS/gdrive_credentials.json" ]; then
    COS_CONFIG_DIR="$CONFIG_DIR" python3 setup_new_firm.py --config "$CONFIG_DIR" \
      || warn "setup_new_firm.py reported issues — review output above"
  fi
fi

# ── Step 6d: Synthetic ~/dashboards/ tree (subscriber path-bridge) ──────────
# The dashboard server hardcodes the legacy `~/dashboards/` layout for
# `_HERE.parent.parent / 'data'`, `... / 'config'`, and a few
# `Path.home() / 'dashboards' / ...` literals. To avoid a refactor across
# ~24 server callsites, we create a synthetic ~/dashboards/ tree of
# symlinks pointing at the tenant's actual dirs. The server reads through
# the symlinks; it never knows it's a synthetic tree.
#
# Idempotent: if a target path already exists as a real dir/file (Yoni's
# legacy install), skip. If it exists as the correct symlink, skip. If it
# points elsewhere, warn and leave alone — never clobber.
step "[6d/8] Synthetic ~/dashboards/ tree (subscriber path-bridge)"

DASH_TREE="$HOME/dashboards"

ensure_link() {
  # ensure_link <link-path> <symlink-target>
  # Compares canonical (resolved) paths so a relative-form and absolute-form
  # symlink to the same file are treated as equivalent.
  local link="$1"
  local target="$2"
  if [ -L "$link" ]; then
    local existing target_canon
    existing="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$link" 2>/dev/null)"
    target_canon="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$target" 2>/dev/null)"
    if [ "$existing" = "$target_canon" ]; then
      ok "symlink ok: ${link/#$HOME/~}"
    else
      warn "symlink ${link/#$HOME/~} resolves to $existing (expected $target_canon) — leaving as-is"
    fi
  elif [ -e "$link" ]; then
    info "skip ${link/#$HOME/~} — real path exists (likely owner of legacy ~/dashboards/ tree)"
  else
    mkdir -p "$(dirname "$link")"
    ln -s "$target" "$link" && ok "linked ${link/#$HOME/~} -> ${target/#$HOME/~}"
  fi
}

mkdir -p "$DASH_TREE/app"

# Server invocation path — what the LaunchAgent fires.
ensure_link "$DASH_TREE/app/cos-dashboard-server.py"  "$REPO/cos-dashboard-server.py"
ensure_link "$DASH_TREE/app/cos-dashboard-refresh.py" "$REPO/cos-dashboard-refresh.py"
ensure_link "$DASH_TREE/app/cos-dashboard-fetch.py"   "$REPO/cos-dashboard-fetch.py"
ensure_link "$DASH_TREE/app/deal-dashboard-refresh.py" "$REPO/deal-dashboard-refresh.py"

# UI assets (live in the public repo).
ensure_link "$DASH_TREE/app/templates" "$REPO/templates"
ensure_link "$DASH_TREE/app/static"    "$REPO/static"

# Tenant data + logs (created by setup.sh in this run).
ensure_link "$DASH_TREE/data" "$DATA_DIR"
ensure_link "$DASH_TREE/logs" "$LOG_DIR"

# Tenant config (separate private git repo, populated above in Step 3).
ensure_link "$DASH_TREE/config" "$CONFIG_DIR/config"

ok "Synthetic ~/dashboards/ tree ready — server can resolve all legacy paths."

# ── Step 7: SKILL copy (D9) + LaunchAgents ──────────────────────────────────
step "[7/8] Claude Code SKILLs + LaunchAgents"

if command -v claude >/dev/null 2>&1; then
  info "Claude Code detected — copying briefing-morning SKILL"
  SKILL_SRC="$REPO/skills/briefing-morning"
  SKILL_DST="$HOME/.claude/scheduled-tasks/briefing-morning"
  if [ -d "$SKILL_SRC" ]; then
    mkdir -p "$HOME/.claude/scheduled-tasks"
    cp -R "$SKILL_SRC" "$SKILL_DST" 2>/dev/null && ok "Copied SKILL → $SKILL_DST" \
      || warn "SKILL copy failed (destination may already exist)"
  else
    warn "SKILL source missing: $SKILL_SRC — skipped"
  fi

  # ── Slash commands (all .md files in slash_commands/) ────────────────────
  info "Installing COS pipeline slash commands"
  CMD_SRC_DIR="$REPO/slash_commands"
  if [ -d "$CMD_SRC_DIR" ]; then
    mkdir -p "$HOME/.claude/commands"
    for f in "$CMD_SRC_DIR"/*.md; do
      [ -f "$f" ] || continue
      name=$(basename "$f")
      cp "$f" "$HOME/.claude/commands/$name" \
        && ok "Installed /${name%.md} → $HOME/.claude/commands/$name" \
        || warn "Failed to copy $name"
    done
  else
    warn "Slash-commands dir missing: $CMD_SRC_DIR — skipped"
  fi

  # ── COS pipeline rules → ~/.claude/CLAUDE.md (idempotent append) ─────────
  info "Installing COS pipeline rules into ~/.claude/CLAUDE.md"
  FRAG_SRC="$REPO/templates/CLAUDE.md.subscriber-fragment.md"
  CLAUDE_MD="$HOME/.claude/CLAUDE.md"
  if [ -f "$FRAG_SRC" ]; then
    mkdir -p "$HOME/.claude"
    touch "$CLAUDE_MD"
    if grep -q "<COS-PIPELINE-RULES-START>" "$CLAUDE_MD"; then
      # Replace existing block in place
      python3 -c "
import re, sys
src = open('$FRAG_SRC').read()
dst = open('$CLAUDE_MD').read()
out = re.sub(
    r'# <COS-PIPELINE-RULES-START>.*?# <COS-PIPELINE-RULES-END>',
    re.search(r'# <COS-PIPELINE-RULES-START>.*?# <COS-PIPELINE-RULES-END>', src, re.S).group(0),
    dst, flags=re.S)
open('$CLAUDE_MD', 'w').write(out)
" && ok "Updated COS pipeline rules block in ~/.claude/CLAUDE.md" \
        || warn "Failed to update COS pipeline rules block"
    else
      printf "\n\n" >> "$CLAUDE_MD"
      cat "$FRAG_SRC" >> "$CLAUDE_MD"
      ok "Appended COS pipeline rules to ~/.claude/CLAUDE.md"
    fi
  else
    warn "CLAUDE.md fragment missing: $FRAG_SRC — skipped"
  fi

  # ── Stop hook → ~/.claude/settings.json ───────────────────────────────────
  info "Registering Stop hook in ~/.claude/settings.json"
  HOOK_PATH="$REPO/tools/dash-state-hook.py"
  SETTINGS="$HOME/.claude/settings.json"
  if [ -f "$HOOK_PATH" ]; then
    mkdir -p "$HOME/.claude"
    [ -f "$SETTINGS" ] || echo "{}" > "$SETTINGS"
    python3 - <<EOF && ok "Registered Stop hook → $SETTINGS" || warn "Failed to register Stop hook"
import json
p = "$SETTINGS"
hook_path = "$HOOK_PATH"
with open(p) as f:
    s = json.load(f)
s.setdefault("hooks", {})
stop = s["hooks"].setdefault("Stop", [])
# Idempotent: replace any existing dash-state-hook entry, otherwise append.
target_cmd = f"/opt/homebrew/bin/python3 {hook_path}"
found = False
for entry in stop:
    for h in entry.get("hooks", []):
        if "dash-state-hook" in h.get("command", ""):
            h["command"] = target_cmd
            found = True
if not found:
    stop.append({"matcher": "", "hooks": [{"type": "command", "command": target_cmd}]})
with open(p, "w") as f:
    json.dump(s, f, indent=2)
EOF
  else
    warn "Stop hook source missing: $HOOK_PATH — skipped"
  fi
else
  info "Claude Code not installed (which claude failed) — skipping SKILL copy"
fi

if $DEMO_MODE; then
  read -p "    Install dashboard-only LaunchAgent (port $PORT)? [Y/n]: " confirm
  if [ "$confirm" != "n" ] && [ "$confirm" != "N" ]; then
    SERVICE_PREFIX="$KCS_PREFIX" COS_CONFIG_DIR="$CONFIG_DIR" \
      LAUNCH_LABEL_PREFIX="$LAUNCH_PREFIX" DASH_PORT="$PORT" \
      ./setup_launchagents.sh dashboard
  fi
else
  read -p "    Install all LaunchAgents under prefix ${LAUNCH_PREFIX}*? [Y/n]: " confirm
  if [ "$confirm" != "n" ] && [ "$confirm" != "N" ]; then
    SERVICE_PREFIX="$KCS_PREFIX" COS_CONFIG_DIR="$CONFIG_DIR" \
      LAUNCH_LABEL_PREFIX="$LAUNCH_PREFIX" DASH_PORT="$PORT" \
      ./setup_launchagents.sh all
  fi
fi

# ── Step 7b: Claude OAuth — bind scheduled tasks to Pro/Max subscription ───
# Runs ONLY when auth_mode is subscription (the default for new tenants per
# firm_context.template.yaml). Tenants on api mode skip this entirely.
# Codified 2026-05-05. Closes the prior gap where new GitHub users had to
# manually run claude /login + claude setup-token + inject-claude-oauth-
# token.sh after install. Now: at most ONE browser sign-in (or zero, if
# the user is already logged in to Claude Code).
echo ""
step "[7b/8] Claude OAuth (Pro/Max binding for scheduled tasks)"

AUTH_MODE_FOR_OAUTH=$(grep -E '^auth_mode:' "$CONFIG_DIR/firm_context.yaml" 2>/dev/null \
                     | head -1 | awk -F: '{print $2}' | tr -d ' \r')
AUTH_MODE_FOR_OAUTH="${AUTH_MODE_FOR_OAUTH:-subscription}"

if [ "$AUTH_MODE_FOR_OAUTH" = "api" ]; then
  info "auth_mode=api in firm_context.yaml — scheduled tasks will use"
  info "ANTHROPIC_API_KEY from keychain. Skipping subscription OAuth setup."
elif [ -x "$HOME/dashboards/scripts/setup-claude-oauth.sh" ]; then
  # New-tenant plists follow the LAUNCH_LABEL_PREFIX convention. Pass the
  # discovered label list so a tenant with prefix com.cos.acme.* gets all
  # their plists injected automatically (not the hardcoded tomac default).
  DISCOVERED_LABELS=$(ls "$HOME/Library/LaunchAgents/${LAUNCH_PREFIX}"*.plist 2>/dev/null \
                     | xargs -n1 basename 2>/dev/null | sed 's/\.plist$//' | tr '\n' ' ')
  if [ -n "$DISCOVERED_LABELS" ]; then
    info "Will inject OAuth token into: $DISCOVERED_LABELS"
    CLAUDE_OAUTH_PLIST_LABELS="$DISCOVERED_LABELS" \
      "$HOME/dashboards/scripts/setup-claude-oauth.sh" \
      || warn "Subscription OAuth setup reported issues — review above"
  else
    warn "No plists matched ${LAUNCH_PREFIX}* yet — re-run after Step 7"
    warn "completes with: ~/dashboards/scripts/setup-claude-oauth.sh"
  fi
else
  warn "$HOME/dashboards/scripts/setup-claude-oauth.sh not found —"
  warn "skipping. Re-run setup.sh after the dashboards repo is in place,"
  warn "or run scripts/setup-claude-oauth.sh manually."
fi

# ── Step 7c: Tailscale (optional — remote/iPhone access) ────────────────────
echo ""
read -p "  Set up Tailscale for remote/iPhone dashboard access? [Y/n]: " _ts_confirm
if [ "$_ts_confirm" != "n" ] && [ "$_ts_confirm" != "N" ]; then
  TAILSCALE_SCRIPT="$REPO/docs/setup_tailscale.sh"
  if [ -f "$TAILSCALE_SCRIPT" ]; then
    DASH_PORT="$PORT" bash "$TAILSCALE_SCRIPT"
  else
    warn "setup_tailscale.sh not found at $TAILSCALE_SCRIPT — skipping"
  fi
else
  info "Tailscale skipped — run later: DASH_PORT=$PORT bash ~/cos-pipeline/docs/setup_tailscale.sh"
fi

# ── Step 8: Validate ────────────────────────────────────────────────────────
step "[8/8] Validation"
"$0" --instance="$INSTANCE" --validate || warn "Validation reported issues — review above"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${G}═══════════════════════════════════════════════════════════════${RESET}"
echo -e "${G}  ✓ Setup complete — instance: $INSTANCE${RESET}"
echo -e "${G}═══════════════════════════════════════════════════════════════${RESET}"
echo ""
echo "  Dashboard    : http://localhost:$PORT"
echo "  Config dir   : $CONFIG_DIR"
echo "  Data dir     : $DATA_DIR"
echo "  Logs dir     : $LOG_DIR"
echo "  Manual run   : COS_CONFIG_DIR=$CONFIG_DIR python3 cos_capture_pipeline.py --since 1h"
echo "  Re-validate  : ./setup.sh --instance=$INSTANCE --validate"
echo ""
open "http://localhost:$PORT" 2>/dev/null || true
