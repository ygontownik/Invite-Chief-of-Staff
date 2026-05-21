---
description: Close a dashboard build session — gather recent changes, rewrite Dashboard State doc in Drive
---

# /dash-close — Dashboard session close

You are closing a Claude Code session that touched the dashboard codebase. Your job:
collect what changed, synthesize current state, write it to the Dashboard State doc in Drive.

---

## STEP 1 — Collect git history (last 7 days across all three repos)

```bash
echo "=== cos-pipeline ===" && cd ~/cos-pipeline && git log --oneline --since="7 days ago" --format="%h %as %s" 2>/dev/null | head -20
echo "=== cos-pipeline-config-tomac ===" && cd ~/cos-pipeline-config-tomac && git log --oneline --since="7 days ago" --format="%h %as %s" 2>/dev/null | head -15
echo "=== dashboards ===" && cd ~/dashboards && git log --oneline --since="7 days ago" --format="%h %as %s" 2>/dev/null | head -15
```

---

## STEP 2 — Collect current infrastructure state

```bash
# Dashboard server running?
curl -s http://localhost:7777/cache-status 2>/dev/null | head -5 || echo "Server not responding"

# Deal data freshness
ls -la ~/dashboards/data/compiled/deal-system-data.json ~/dashboards/data/compiled/deal-pipeline-data.json 2>/dev/null

# Any pending git pushes?
cd ~/cos-pipeline && git status --short && git log origin/main..HEAD --oneline 2>/dev/null | head -5
cd ~/cos-pipeline-config-tomac && git status --short && git log origin/main..HEAD --oneline 2>/dev/null | head -5
```

---

## STEP 3 — Read current state doc (to preserve open items + deferred backlog)

Use the Drive MCP read tool to fetch the current Dashboard State doc:
File ID: 1TWhl8GcFO2l3mD7jCpaEQk8fGRurW1YD-2v529DgQ3Q

Extract: DEFERRED ITEMS and NEXT PRIORITIES sections so they carry forward.

---

## STEP 4 — Synthesize and write new state doc

Compose the new state doc content using this exact structure:

```
DASHBOARD BUILDOUT STATE
Last Updated: YYYY-MM-DD HH:MM
Updated By: Claude Code /dash-close

================================================================
RECENT CHANGES (last 7 days)
================================================================
[Git log entries organized by repo, one bullet per meaningful commit]
[Include commit hash, date, description]

================================================================
CURRENT STATE
================================================================
Server:        Running on localhost:7777 / 192.168.4.21:7777 | [or: not running]
Design system: [any notable state]
Deals wired:   [deal IDs with Claude Project URLs if known]
Pending push:  [list any commits not yet pushed, or "none"]

================================================================
DEFERRED ITEMS
================================================================
[Carry forward from previous doc, mark any newly resolved items as DONE]
[Add any new deferred items identified this session]

================================================================
NEXT PRIORITIES
================================================================
1. [Most urgent — one line, specific]
2. [Second]
3. [Third]

================================================================
QUICK COMMANDS
================================================================
Dashboard:     http://localhost:7777
Deals:         http://localhost:7777/deals/
Admin:         http://localhost:7777/admin/routines
Push all:      cd ~/cos-pipeline && git push && cd ~/cos-pipeline-config-tomac && git push
State doc:     https://docs.google.com/document/d/1TWhl8GcFO2l3mD7jCpaEQk8fGRurW1YD-2v529DgQ3Q/edit
Briefing:      ~/dashboards/docs/PROJECT-BRIEFING-DASHBOARD.md
```

Write content to /tmp/dash_state.txt, then run:
```bash
python3 ~/dashboards/scripts/update-dash-state.py --file /tmp/dash_state.txt
```

Confirm the script prints "Done. Updated at ..." and the doc URL.

---

## STEP 5 — Report

Output a one-paragraph summary of what changed this session and what's next.
