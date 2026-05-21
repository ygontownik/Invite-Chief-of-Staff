---
name: wrap
description: End-of-session wrap-up. Surgically updates catch-up docs (SESSION-HANDOFF, CHANGELOG, DECISIONS, drive-docs.yaml, LEARNINGS-LEDGER), runs all derived-view sync scripts, compacts logs, audits Drive integrity, cleans up orphan IDs, runs health snapshot, commits + pushes all 3 repos, and prints the exact paste-block for the next chat. Reads from all parallel-chat transcripts modified since the last /wrap so work done in other Claude Code chats gets folded in. Heavy by design — invoke once per substantial session.
argument-hint: "[--dry-run] [--include-heavy]"
---

# /wrap — Session wrap-up and handoff

Surgically updates every "catch-the-next-chat-up" doc, runs every derived-view
regenerator, captures parallel-chat work, ships the result.

**Invariant:** SESSION-HANDOFF is a per-day snapshot (full overwrite OK — git
preserves history). CHANGELOG / DECISIONS / drive-docs.yaml / LEARNINGS-LEDGER
are append-or-upsert only (never bulk rewrite).

**Args:**
- `--dry-run`: report what would change, skip writes + commits + pushes
- `--include-heavy`: also trigger `/deal-sync`, `/capture-deal-chats`, and
  `/refresh-project-instructions`. By default these are skipped (they have
  their own dash-state-hook periodic cadence) — /wrap surfaces "X hasn't
  run in N hours" warnings instead.

---

## STEP 1 — Acquire wrap lock

```bash
python3 -c "
import sys
sys.path.insert(0, '/Users/ygontownik/cos-pipeline/tools')
from coordination import lock
with lock('wrap', timeout=60):
    print('wrap lock acquired')
"
```

If lock can't be acquired in 60s, another /wrap is running in a parallel chat.
Wait for it to finish, then re-invoke /wrap — the second run will read the
first's output and add this session's incremental delta.

---

## STEP 2 — Read last-wrap timestamp + this session info

```bash
python3 << 'PYEOF'
import json, os
from pathlib import Path
LAST_WRAP = Path.home() / "dashboards/data/last-wrap.json"
if LAST_WRAP.exists():
    d = json.loads(LAST_WRAP.read_text())
    print(f"Last /wrap: {d.get('timestamp', 'never')} (session={d.get('session_id', '?')})")
else:
    print("First /wrap — using 24h lookback as fallback")
PYEOF
```

Capture the timestamp as `T0`. If no last-wrap.json, use `now - 24h` as fallback.

---

## STEP 3 — Gather delta from THIS session

3a. Read this session's own transcript for tool invocations + key emissions:

```bash
# Find the current session's JSONL file (most recent in projects dir)
ls -t ~/.claude/projects/-Users-ygontownik-Documents-Claude/*.jsonl 2>/dev/null | head -1
```

Read the last few hundred messages from that file. Extract:
- Files modified (look for tool_use blocks with file_path)
- Skill invocations (look for `<command-name>` tags or "Skill" tool calls)
- /propose-learning emissions (capture each `id`, `rule_code`, `title`)
- ---DEAL-INTEL--- blocks
- ---SESSION-CLOSE--- blocks
- DECISIONS made (look for "DECISION:" markers or major architectural choices)

3b. Git status + log across the 3 repos:

```bash
for repo in ~/cos-pipeline ~/dashboards ~/cos-pipeline-config-tomac; do
  echo "=== $(basename $repo) ==="
  echo "--- uncommitted ---"
  git -C "$repo" status --short
  echo "--- commits since T0 ---"
  git -C "$repo" log --oneline --since="$T0"
done
```

3c. Coordination state — which sync_* ran since T0:

```bash
python3 ~/cos-pipeline/tools/coordination.py status
```

---

## STEP 4 — Cross-chat reconciliation (parallel sessions)

```bash
PROJECTS_DIR="$HOME/.claude/projects/-Users-ygontownik-Documents-Claude"
THIS_SESSION_JSONL=$(ls -t "$PROJECTS_DIR"/*.jsonl 2>/dev/null | head -1)

# Find OTHER session JSONLs modified since T0 (excluding this one)
find "$PROJECTS_DIR" -name "*.jsonl" -newermt "$T0" 2>/dev/null | \
  grep -v "$(basename "$THIS_SESSION_JSONL")"
```

For each parallel-chat JSONL: read it line-by-line, extract:
- `<command-name>` tags (skill invocations) — capture skill + timestamp
- Any `/propose-learning` outputs (search for "L00\d{2}" patterns + "rule_code")
- Any ---DEAL-INTEL--- emissions (deal_id + summary one-liner)
- Any ---SESSION-CLOSE--- blocks (deal_id + activities)
- Files modified via tool_use:Edit / Write / Bash with `git add`

Group findings by short session_id (first 8 chars of UUID) so the SESSION-HANDOFF
shows "session abc12345 did X, session def67890 did Y".

**Note:** dash-state-hook.py's `run_intel_capture_scan` and
`run_learning_capture_scan` already proactively extract from these JSONLs on
every Stop event. /wrap is the consolidator — it should NOT duplicate the
extraction; it reads the already-extracted artifacts:

```
~/dashboards/data/compiled/proposed-learnings.jsonl  (queued LC1 candidates)
~/dashboards/data/deals/<deal_id>/log.json           (per-deal intel append)
~/dashboards/data/compiled/skill-telemetry.jsonl     (skill usage telemetry)
```

If those buffers contain new entries since T0, surface them in the handoff.

---

## STEP 5 — Surgical doc updates

### 5a. CHANGELOG.md — append today's block

Read `~/dashboards/docs/CHANGELOG.md`. If today's date `## YYYY-MM-DD` heading
exists, append bullets to that block. Otherwise prepend a new block at the top.

Bullets format (one per substantive change, grouped by chat-session if multi-chat):
```
## 2026-05-21
**session-abc12345:**
- fix(tcip_new_deal): overwrite stub on same-deal_id collision (L0049/DR1) — commit 0178170
- feat(load-secrets): load CLAUDE_CODE_OAUTH_TOKEN from Keychain — commit 9441094

**session-def67890:**
- onboard FIT deal end-to-end smoke test — commit XXXXXXX
```

### 5b. DECISIONS.md — append only if decisions made

Read `~/dashboards/docs/DECISIONS.md`. For each material decision flagged
this session (architectural choice, irreversible action, trade-off made):

```
## 2026-05-21
### Decision: <one-line summary>
- Context: <why decision was needed>
- Choice: <what was decided>
- Rationale: <why this choice>
- Rejected: <alternative not chosen, with reason>
- Reversibility: <can we undo? how?>
```

If no decisions made, skip — do not write an empty header.

### 5c. drive-docs.yaml — surgical upsert by key

For each deal-doc / folder / reference-doc added or modified this session,
upsert the specific key. Never bulk-rewrite the file.

Use `yaml.safe_load` + targeted dict modification + `yaml.safe_dump` with
`sort_keys=False`, `allow_unicode=True`, `width=200` to preserve the existing
key order as much as PyYAML permits.

### 5d. LEARNINGS-LEDGER.yaml — batch-process queued candidates

If `~/dashboards/data/compiled/proposed-learnings.jsonl` has new entries since
T0 (deduped against existing ledger per LC1 filters):

1. Group candidates by source session_id (for parallel-chat attribution)
2. Present each as a one-line approve / reject / modify choice
3. For approved candidates, invoke /propose-learning logic inline
4. After processing, truncate the queue file (per L0047/LC1 reset
   convention)

### 5e. SESSION-HANDOFF-YYYY-MM-DD.md — full overwrite

Always overwrite today's date file. Git preserves history.

Structure (mirror the 2026-05-21 version):
1. How to use this doc (paste block for next chat)
2. What this session shipped (grouped by chat-session if multi-chat)
3. Current state — system health (after vs before diff)
4. Live work-in-flight (incl. outstanding requests numbered for OR1 continuity)
5. Phases NOT executed (deferred items + why)
6. New learnings added this session (table with id, rule_code, title)
7. Quick diagnostic commands
8. Architectural promise (don't break the loop)

---

## STEP 6 — Run ALL derived-view regenerators (parallel where independent)

```bash
# 6a. Propagate LEARNINGS edits to CLAUDE.md + MEMORY.md + Drive gdocs
python3 ~/cos-pipeline/tools/sync_learnings.py --apply --push-drive &
LEARNINGS_PID=$!

# 6b. Propagate drive-docs.yaml to GAS scripts + local_file_router + deal-system-data.json
python3 ~/cos-pipeline/tools/sync_registry.py --apply &
REGISTRY_PID=$!

# 6c. Push 5 narrative gdocs to Drive (README, System Reference, User Manual,
#     Skills Catalog, My Skills)
python3 ~/cos-pipeline/tools/sync_system_docs.py --apply &
SYSDOCS_PID=$!

# 6d. Regenerate SYSTEM-MAP.md (auto-picks up new periodic jobs + drive-docs entries)
python3 ~/cos-pipeline/tools/generate-system-map.py &
SYSMAP_PID=$!

wait $LEARNINGS_PID $REGISTRY_PID $SYSDOCS_PID $SYSMAP_PID
echo "All 4 sync regenerators done"
```

If any of those exit non-zero, capture the error, log it, but don't abort
the rest of /wrap (skip-on-failure pattern).

---

## STEP 7 — Heavy lifting (the things you'd forget to run)

```bash
# 7a. Log compaction — archive entries >30d old across all deals
python3 ~/cos-pipeline/tools/log_compaction.py

# 7b. Orphan Drive cleanup — trash IDs flagged in _orphan_ids_pending_cleanup
python3 ~/cos-pipeline/tools/orphan_drive_cleanup.py --apply

# 7c. Reference integrity audit — every Drive ID in drive-docs.yaml resolves?
python3 ~/cos-pipeline/tools/reference_integrity_audit.py 2>&1 | tail -20
```

If reference_integrity_audit surfaces broken IDs, ADD them to outstanding in
the handoff (don't try to auto-fix — broken IDs need human judgment).

---

## STEP 8 — Verification + health snapshot

```bash
# 8a. Run /check-system composite
# (coordination + system_health + sync timestamps)
```

Invoke the /check-system skill. Capture the output. Diff against the
last-wrap.json's stored health counts to surface "we went from X warns
to Y warns" or new fails.

```bash
# 8b. LaunchAgent state check — any newly down?
launchctl list | grep -iE "tomac|yoni|claude" | awk '$2 != "0" && $2 != "-" {print}'
```

Flag any newly down LaunchAgents in the handoff.

---

## STEP 9 — Commit + push (narrow git add per repo)

For each of the 3 repos:

```bash
for repo in ~/cos-pipeline ~/dashboards ~/cos-pipeline-config-tomac; do
  cd "$repo"
  echo "=== Processing $(basename $repo) ==="

  # Detect modified files actually touched this session vs prior-session leftovers
  CHANGED=$(git status --short)
  if [ -z "$CHANGED" ]; then
    echo "  Nothing to commit."
    continue
  fi

  # Narrow add: specific files only, never `-A`
  # Auto-draft commit message from delta + LEARNINGS captured + decisions
  # (Skill should compose this from STEP 3-5 outputs)

  # git add <specific files>
  # git commit -m "$(cat <<'EOF' ... EOF)"
  # git push
done
```

**Commit message structure:**
```
<type>(<scope>): <one-line summary>

<paragraph describing the change + why>

<bullet list of specific edits if multi-faceted>

<LEARNINGS captured: L00XX (CODE) / L00YY (CODE) if applicable>

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

If pre-commit hook fails, fix the issue and create a NEW commit (never --amend).

---

## STEP 10 — Heavy operations (only if --include-heavy)

These have their own dash-state-hook cadence. Skip by default. With
`--include-heavy`, invoke inline:

```bash
# /deal-sync — synthesize log.json into status.md + master_brief.md for each deal
# /capture-deal-chats — Chrome MCP scrape of claude.ai project chats
# /refresh-project-instructions — push reference-doc changes to claude.ai projects
```

Without --include-heavy: surface "X has not run in N hours" warnings only.

```bash
python3 -c "
import sys; sys.path.insert(0, '/Users/ygontownik/cos-pipeline/tools')
from coordination import last_run_seconds_ago
for script in ['deal_extract_helpers.py', 'capture-deal-chats', 'refresh-project-instructions']:
    s = last_run_seconds_ago(script)
    if s is None or s > 24*3600:
        print(f'WARN: {script} has not run in {(s or 0)/3600:.1f}h')
"
```

---

## STEP 11 — Write last-wrap.json + emit next-chat paste-block

```bash
python3 << 'PYEOF'
import json, os
from datetime import datetime, timezone
from pathlib import Path
P = Path.home() / "dashboards/data/last-wrap.json"
P.parent.mkdir(parents=True, exist_ok=True)
data = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "session_id": "$THIS_SESSION_UUID",
    "health_after": {"pass": ?, "warn": ?, "fail": ?},  # from STEP 8
    "learnings_captured": ["L00XX (CODE1)", "L00YY (CODE2)"],
    "commits": {
        "cos-pipeline": "<sha>",
        "dashboards": "<sha>",
        "cos-pipeline-config-tomac": "<sha>",
    },
    "outstanding_count": ?,  # from handoff §4b
}
P.write_text(json.dumps(data, indent=2))
print(f"Wrote {P}")
PYEOF
```

Then PRINT (don't write to file — emit to terminal) the exact paste-block
for the next chat:

```
read ~/dashboards/docs/SESSION-HANDOFF-<today>.md
/load-context <inferred-task-type>

You're picking up from session 2026-05-21 (end-of-day). Most recent wrap:
<timestamp>. Last session shipped <X items>, <Y items deferred>.

<1-3 paragraph context primer drawn from the SESSION-HANDOFF §2 "what shipped">

Priorities for this chat:
1. <highest-priority outstanding item from §4b>
2. <next>
3. <next>

Stay action-oriented. Skip-on-failure. Mark chapters as you shift work.
```

The `<inferred-task-type>` is one of: dashboard, deal_general, new_deal,
drive_org, cos_pipeline, financial_modeling. Choose based on the dominant
work category in the outstanding items.

---

## STEP 12 — Release wrap lock

```bash
python3 -c "
import sys
sys.path.insert(0, '/Users/ygontownik/cos-pipeline/tools')
from coordination import unlock
unlock('wrap')
print('wrap lock released')
"
```

---

## What /wrap PROACTIVELY does that you'd otherwise forget

- Captures parallel-chat work (proposed-learnings.jsonl + per-deal log.json + skill-telemetry.jsonl)
- Pushes LEARNINGS edits to Practice Patterns + Yoni Personal Context gdocs (so claude.ai projects pick up rule changes)
- Regenerates GAS scripts when drive-docs.yaml changed (so Deal Filer + Drive Organizer pick up new aliases)
- Regenerates SYSTEM-MAP.md (so the architecture doc stays current)
- Compacts oversized log.json files
- Trashes orphan Drive IDs from prior failed /new-deal runs
- Audits every registered Drive ID still resolves
- Surfaces stale LaunchAgents + sync_* runs

## What /wrap doesn't do (by design)

- LLM-heavy operations (`/deal-sync`, `/capture-deal-chats`, `/refresh-project-instructions`)
  — invoke with `--include-heavy` if you want them now, otherwise they run on cadence.
- Anything that requires browser interaction beyond what Chrome MCP can automate.
- Rewriting docs that should be append-or-upsert only.

## Error handling

- Skip-on-failure throughout: a busted sync script doesn't block the rest.
- Errors logged to `~/dashboards/logs/wrap.log` with timestamps + which step.
- Final handoff doc lists any /wrap step that failed in §4b (outstanding).

## OUTSTANDING REQUESTS (per OR1)

Always end with: "/wrap completed. Outstanding items rolled into
SESSION-HANDOFF-<today>.md §4b. Next-chat paste-block above."
