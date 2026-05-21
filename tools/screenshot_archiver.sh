#!/bin/bash
# screenshot_archiver.sh — daily Desktop screenshot triage.
#
# Source of truth: DRIVE-ARCHITECTURE.md §5 + DRIVE-RECOMMENDATIONS.md §6.5.
# Moves macOS screenshots out of ~/Desktop into ~/Desktop/Screenshots/YYYY-MM/
# and trashes anything in the archive older than 30 days.
#
# Schedule: ~/Library/LaunchAgents/com.yoni.screenshot_archiver.plist (daily 06:00).
# Logs:     ~/Library/Logs/screenshot_archiver.log
#
# Idempotent. Safe to re-run.

set -u

DESKTOP="$HOME/Desktop"
ARCHIVE="$HOME/Desktop/Screenshots"
MONTH=$(date +%Y-%m)
LOG="$HOME/Library/Logs/screenshot_archiver.log"
TS=$(date +"%Y-%m-%dT%H:%M:%S%z")

mkdir -p "$ARCHIVE/$MONTH"

# Move any Screenshot *.png from the Desktop into this month's archive folder.
# macOS default naming is "Screenshot YYYY-MM-DD at H.MM.SS AM.png".
# nullglob-style guard: only iterate if matches exist.
shopt -s nullglob 2>/dev/null || true

moved=0
for f in "$DESKTOP"/Screenshot\ *.png; do
    [ -e "$f" ] || continue
    if mv -n "$f" "$ARCHIVE/$MONTH/" 2>/dev/null; then
        moved=$((moved + 1))
    fi
done

# Trash anything in the archive tree older than 30 days.
pruned=0
while IFS= read -r -d '' f; do
    rm -f "$f" && pruned=$((pruned + 1))
done < <(find "$ARCHIVE" -type f -name "*.png" -mtime +30 -print0 2>/dev/null)

# Remove empty month folders left behind after pruning (but keep the root).
find "$ARCHIVE" -mindepth 1 -type d -empty -delete 2>/dev/null

echo "$TS moved=$moved pruned=$pruned month=$MONTH" >> "$LOG"
