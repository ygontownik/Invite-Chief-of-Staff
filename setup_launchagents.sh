#!/bin/bash
# setup_launchagents.sh — Install macOS LaunchAgents for the COS Pipeline.
#
# Generates 3 essential LaunchAgent plists, installs them, and starts them:
#   1. cos-dashboard-server   — always-on HTTP server on port 7777
#   2. cos-capture-pipeline   — daily 7:22am capture + reconciliation + drafts
#   3. cos-gmail-mini         — every 2h email triage on weekdays
#
# These are the minimum viable scheduled tasks for Package B. The full
# Claude Code SKILL-based pipelines (cos-otter-transcripts, cos-personal-briefing,
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

# Resolve keychain service prefix from firm_config.json (default: cos-pipeline)
# This is what setup_keychain.sh writes secrets under and what the load-secrets
# helper reads from. Firm 2 sets keychain_service_prefix in their firm_config.json.
_KCS_CFG="${COS_CONFIG_DIR:-$REPO}/firm_config.json"
[ ! -f "$_KCS_CFG" ] && _KCS_CFG="$REPO/firm_config.json"
KCS_PREFIX="cos-pipeline"
if [ -f "$_KCS_CFG" ]; then
  _p=$(python3 -c "import json; d=json.load(open('$_KCS_CFG')); print(d.get('keychain_service_prefix','cos-pipeline'))" 2>/dev/null)
  [ -n "$_p" ] && KCS_PREFIX="$_p"
fi

mkdir -p "$LAUNCH_DIR" "$LOG_DIR"

# Helper — write a plist that runs a shell command
write_plist() {
  local name="$1"
  local label="com.cos-pipeline.$name"
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
  local cmd="source $SECRETS_HELPER 2>/dev/null || true; cd $REPO && python3 cos-dashboard-server.py"
  write_plist "dashboard-server" "$sched" "$cmd" "true" "true"
}

install_capture() {
  echo ""
  echo "── Installing cos-capture-pipeline (daily 7:22am Mon-Fri) ──"
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

uninstall_all() {
  for name in dashboard-server capture-pipeline gmail-mini; do
    local plist="$LAUNCH_DIR/com.cos-pipeline.$name.plist"
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
  all|*)
    install_dashboard
    install_capture
    install_gmail
    ;;
esac

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Done. Check status with:"
echo "    launchctl list | grep cos-pipeline"
echo ""
echo "  Logs at:"
echo "    $LOG_DIR/{dashboard-server,capture-pipeline,gmail-mini}.{stdout,stderr}.log"
echo ""
echo "  Dashboard:"
echo "    http://localhost:7777"
echo "═══════════════════════════════════════════════════════════════"
