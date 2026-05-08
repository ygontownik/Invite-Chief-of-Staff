#!/bin/bash
# setup_launchagents.sh — Install macOS LaunchAgents for the COS Pipeline.
#
# Generates 3 essential LaunchAgent plists, installs them, and starts them:
#   1. cos-dashboard-server   — always-on HTTP server on port 7777
#   2. inbox-capture   — daily 7:22am capture + reconciliation + drafts
#   3. cos-gmail-mini         — every 2h email triage on weekdays
#
# These are the minimum viable scheduled tasks for Package B. The full
# Claude Code SKILL-based pipelines (cos-otter-transcripts, morning-briefing,
# etc.) require Claude Code to be installed separately and SKILL.md files copied
# into ~/.claude/scheduled-tasks/. See docs/PACKAGE_B.md for those.
#
# Usage:
#     ./setup_launchagents.sh                    # install all 3 (default)
#     ./setup_launchagents.sh dashboard          # just the dashboard server
#     ./setup_launchagents.sh capture            # just the capture pipeline
#     ./setup_launchagents.sh gmail              # just the gmail mini
#     ./setup_launchagents.sh --uninstall        # stop and remove all 3

set -e

REPO="$HOME/cos-pipeline"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/dashboards/logs/claude-tasks"
SECRETS_HELPER="$HOME/.cos-pipeline-load-secrets.sh"

# B6 (ID excision): keychain_service_prefix is REQUIRED in firm_config.json.
# Per DECISIONS.md C11, the canonical format is `cos-pipeline-<slug>` (e.g.
# cos-pipeline-config-tomac, cos-pipeline-config-re-dev). Search order:
#   1. $COS_CONFIG_DIR/firm_config.json (preferred — set by setup.sh)
#   2. ~/cos-pipeline-config/firm_config.json (legacy single-tenant)
#   3. ~/cos-pipeline-config-*/firm_config.json (any per-tenant dir)
#   4. $REPO/firm_config.json (legacy: alongside code)
# Falls back to "cos-pipeline" with a stderr warning so installers don't blow up.
KCS_PREFIX=""
_KCS_CANDIDATES=(
  "${COS_CONFIG_DIR:+$COS_CONFIG_DIR/firm_config.json}"
  "$HOME/cos-pipeline-config/firm_config.json"
)
for _d in "$HOME"/cos-pipeline-config-*; do
  [ -d "$_d" ] && _KCS_CANDIDATES+=("$_d/firm_config.json")
done
_KCS_CANDIDATES+=("$REPO/firm_config.json")
for _KCS_CFG in "${_KCS_CANDIDATES[@]}"; do
  if [ -n "$_KCS_CFG" ] && [ -f "$_KCS_CFG" ]; then
    _p=$(python3 -c "import json; d=json.load(open('$_KCS_CFG')); print(d.get('keychain_service_prefix',''))" 2>/dev/null)
    if [ -n "$_p" ]; then
      KCS_PREFIX="$_p"
      break
    fi
  fi
done
if [ -z "$KCS_PREFIX" ]; then
  echo "[setup_launchagents] WARNING: keychain_service_prefix not set in any firm_config.json — defaulting to 'cos-pipeline'." >&2
  KCS_PREFIX="cos-pipeline"
fi

mkdir -p "$LAUNCH_DIR" "$LOG_DIR"

# Helper — write a plist that runs a shell command
write_plist() {
  local name="$1"
  local label="${LAUNCH_LABEL_PREFIX:-com.cos-pipeline.}$name"
  local plist="$LAUNCH_DIR/$label.plist"
  local schedule_xml="$2"
  local cmd="$3"
  # XML-escape ampersands in the bash command (e.g., &&) so the plist parses
  cmd="${cmd//&/&amp;}"

  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-l</string>
        <string>-c</string>
        <string>$cmd</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOME/.local/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>

$schedule_xml

    <key>StandardOutPath</key>
    <string>$LOG_DIR/$name.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/$name.stderr.log</string>

    <key>RunAtLoad</key><$4/>
    <key>KeepAlive</key><$5/>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
PLIST

  echo "  ✓ Wrote $plist"

  # Reload if already loaded
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "  ✓ Loaded $label"
}

install_dashboard() {
  echo ""
  echo "── Installing cos-dashboard-server (always-on HTTP :7777) ──"
  local sched=""  # No schedule — KeepAlive=true
  # DASH_PORT is consumed by cos-dashboard-server.py via $COS_DASHBOARD_PORT env or default 7777.
  local cmd="source $SECRETS_HELPER 2>/dev/null || true; export COS_DASHBOARD_PORT=${DASH_PORT:-7777}; cd $REPO && python3 cos-dashboard-server.py"
  write_plist "dashboard-server" "$sched" "$cmd" "true" "true"
}

install_capture() {
  echo ""
  echo "── Installing inbox-capture (daily 7:22am Mon-Fri) ──"
  local sched=$(cat <<'XML'
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>22</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>22</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>22</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>22</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>22</integer></dict>
    </array>
XML
)
  local cmd="source $SECRETS_HELPER 2>/dev/null || true; cd $REPO && python3 cos_capture_pipeline.py 2>&1"
  write_plist "capture-pipeline" "$sched" "$cmd" "false" "false"
}

install_gmail() {
  echo ""
  echo "── Installing cos-gmail-mini (every 2h Mon-Fri 8am-8pm, on the :05 mark) ──"
  # Build XML for every 2h on each weekday at :05 to avoid collisions with otter-transcripts
  local sched="    <key>StartCalendarInterval</key>"$'\n'"    <array>"$'\n'
  for day in 1 2 3 4 5; do
    for hour in 8 10 12 14 16 18 20; do
      sched+="        <dict><key>Weekday</key><integer>$day</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>5</integer></dict>"$'\n'
    done
  done
  sched+="    </array>"
  local cmd="source $SECRETS_HELPER 2>/dev/null || true; cd $REPO && python3 cos_gmail_mini_v2.py 2>&1"
  write_plist "gmail-mini" "$sched" "$cmd" "false" "false"
}

install_market_fetch() {
  echo ""
  echo "── Installing cos-market-fetch (daily 6:45am Mon-Fri) ──"
  local sched=$(cat <<'XML'
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>45</integer></dict>
    </array>
XML
)
  local cmd="source $SECRETS_HELPER 2>/dev/null || true; cd $REPO && python3 cos_market_fetch.py 2>&1"
  write_plist "market-fetch" "$sched" "$cmd" "false" "false"
}

uninstall_all() {
  for name in dashboard-server capture-pipeline gmail-mini market-fetch; do
    local plist="$LAUNCH_DIR/${LAUNCH_LABEL_PREFIX:-com.cos-pipeline.}$name.plist"
    if [ -f "$plist" ]; then
      launchctl unload "$plist" 2>/dev/null || true
      rm "$plist"
      echo "  ✓ Removed $plist"
    fi
  done
}

# ── Main dispatch ───────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  COS Pipeline — LaunchAgent Setup"
echo "═══════════════════════════════════════════════════════════════"

case "${1:-all}" in
  --uninstall|uninstall)
    echo ""
    echo "Uninstalling all COS Pipeline LaunchAgents..."
    uninstall_all
    ;;
  dashboard)
    install_dashboard
    ;;
  capture)
    install_capture
    ;;
  gmail)
    install_gmail
    ;;
  market-fetch)
    install_market_fetch
    ;;
  all|*)
    install_dashboard
    install_capture
    install_gmail
    # Only install market-fetch if the subscriber has configured blogs sources
    _has_blogs=$(python3 -c "
import sys, yaml
try:
    ctx = yaml.safe_load(open('firm_context.yaml'))
    blogs = (ctx.get('personal') or {}).get('content_feeds', {}).get('blogs') or []
    print('yes' if blogs else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")
    if [ "$_has_blogs" = "yes" ]; then
      install_market_fetch
    else
      echo ""
      echo "── Skipping cos-market-fetch (no blogs sources configured) ──"
      echo "   Add feeds under personal.content_feeds.blogs in firm_context.yaml"
      echo "   then run: ./setup_launchagents.sh market-fetch"
    fi
    ;;
esac

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Done. Check status with:"
echo "    launchctl list | grep ${LAUNCH_LABEL_PREFIX:-com.cos-pipeline.}"
echo ""
echo "  Logs at:"
echo "    $LOG_DIR/{dashboard-server,capture-pipeline,gmail-mini,market-fetch}.{stdout,stderr}.log"
echo ""
echo "  Dashboard:"
echo "    http://localhost:${DASH_PORT:-7777}"
echo "═══════════════════════════════════════════════════════════════"
