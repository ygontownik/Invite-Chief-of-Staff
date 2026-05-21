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
with lock('wrap', holder='wrap-skill', timeout_seconds=60):
    print('wrap lock acquired')
"
```

If lock can't be acquired in 60s, another /wrap is running in a parallel chat
(or `wrap_auto.sh` cron is mid-run). Wait for it to finish, then re-invoke
/wrap — the second run will read the first's output and add this session's
incremental delta.

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

**CRITICAL:** Use Python with content-filter, NOT bare `find -newermt`.
- `find -newermt` is flaky on macOS with ISO timestamps (silently empty).
- `mtime > T0` alone returns 100s of false positives because background
  hooks (intel_capture, learning_capture) touch many old JSONLs.

Real filter: a JSONL is a "live parallel chat" only if it contains an
ACTUAL USER-TYPED message (not a tool result, not a system reminder)
timestamped after T0:

```bash
python3 << 'PYEOF'
import json, os
from datetime import datetime, timezone
from pathlib import Path

T0_ISO = open('/tmp/wrap_t0.txt').read().strip()
T0_DT = datetime.fromisoformat(T0_ISO)
T0_EPOCH = T0_DT.timestamp()

PROJECTS = Path.home() / ".claude/projects/-Users-ygontownik-Documents-Claude"
all_jsonls = sorted(PROJECTS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
this_session = all_jsonls[0] if all_jsonls else None
print(f"This session: {this_session.name if this_session else 'NONE'}")

def has_real_user_msg_since(jsonl_path, t0_iso):
    """Return True if the JSONL has a user-TYPED message (not tool result,
    not system reminder) timestamped after t0_iso. Reads only the tail."""
    try:
        # Read last ~200 lines for speed (typical chats fit, long ones we get recent)
        with open(jsonl_path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))  # last 200KB
            tail = f.read().decode('utf-8', errors='ignore')
    except Exception:
        return False
    for line in tail.splitlines():
        if '"type":"user"' not in line and '"role":"user"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = obj.get('timestamp', '')
        if ts <= t0_iso:
            continue
        # Must NOT be a tool_result wrapper
        msg = obj.get('message', {})
        content = msg.get('content', [])
        if isinstance(content, list):
            # Real user text: at least one element with type=text and no tool_use_id
            for c in content:
                if isinstance(c, dict) and c.get('type') == 'text' and 'tool_use_id' not in c:
                    txt = c.get('text', '')
                    # Filter out system-reminder pseudo-user messages
                    if '<system-reminder>' in txt and txt.count('\n') < 3:
                        continue
                    return True
        elif isinstance(content, str) and content.strip():
            return True
    return False

mtime_candidates = [p for p in all_jsonls
                    if p.stat().st_mtime > T0_EPOCH and p != this_session]
print(f"JSONLs with mtime > T0: {len(mtime_candidates)}")

real_parallel = [p for p in mtime_candidates if has_real_user_msg_since(p, T0_ISO)]
print(f"With real user activity since T0: {len(real_parallel)}")
for p in real_parallel[:10]:
    mt = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    print(f"  {p.name} (mtime={mt}, {p.stat().st_size:,}B)")

with open('/tmp/wrap_parallel_sessions.txt', 'w') as f:
    f.write('\n'.join(str(p) for p in real_parallel))
PYEOF
```

**Fallback signal if JSONL scan finds 0 but commits suggest otherwise:**
parallel work may have committed/pushed before /wrap saw their JSONL flush.
In that case, use `git log --since="$T0"` filtering to identify
parallel-chat commits and group those by author/timestamp clusters.

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

### 4b. Pick up daily-briefing + dashboard-state changes since T0

**MUST actually run this — do not claim "no new content" without verifying.**

Run the Drive API check explicitly:

```bash
python3 << 'PYEOF'
import os, pickle
from datetime import datetime, timezone
from googleapiclient.discovery import build

T0_ISO = open('/tmp/wrap_t0.txt').read().strip()

with open(os.path.expanduser('~/credentials/gdrive_token.pickle'), 'rb') as f:
    creds = pickle.load(f)
drive = build('drive', 'v3', credentials=creds)
docs = build('docs', 'v1', credentials=creds)

TARGETS = {
    "Personal Briefing Log":  "14wE3L6ZRsjhhx2psRKbaHS5i0kgEoteWYZusqETiAZ0",
    "Dashboard State":         "1TWhl8GcFO2l3mD7jCpaEQk8fGRurW1YD-2v529DgQ3Q",
}

for name, fid in TARGETS.items():
    meta = drive.files().get(fileId=fid, fields="modifiedTime,name").execute()
    mtime = meta["modifiedTime"]
    if mtime > T0_ISO:
        # Export plain text
        doc = docs.documents().get(documentId=fid).execute()
        text_parts = []
        for el in doc.get("body", {}).get("content", []):
            if "paragraph" in el:
                for e in el["paragraph"].get("elements", []):
                    text_parts.append(e.get("textRun", {}).get("content", ""))
        full_text = "".join(text_parts)
        print(f"=== {name} (modified {mtime} — NEWER than T0) ===")
        # Print last ~2000 chars (most recent content for append-only docs)
        print(full_text[-2000:])
        print()
    else:
        print(f"{name}: unchanged since T0 ({mtime})")
PYEOF
```

If any output > unchanged: fold the new content into SESSION-HANDOFF §2
"intelligence + dashboard changes since last wrap". Read-only, no LLM.

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

### 5b. DECISIONS.md — append ONLY if decisions were made

**Conditional:** evaluate from STEP 3 + STEP 4 extracted content whether any
material decisions occurred this session (across all chats). Heuristic:
look for "DECISION:" markers, explicit "decided to X" / "chose X over Y"
phrases, or commit messages containing "decision:" or "decided".

If zero material decisions: **SKIP this sub-step entirely** — do NOT write
an empty `## YYYY-MM-DD` header. Most sessions don't have decisions; the
file should stay terse.

If one or more decisions found, append for each:

```
## 2026-05-21
### Decision: <one-line summary>
- Context: <why decision was needed>
- Choice: <what was decided>
- Rationale: <why this choice>
- Rejected: <alternative not chosen, with reason>
- Reversibility: <can we undo? how?>
```

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

### 5e. SESSION-HANDOFF-YYYY-MM-DD.md — overwrite (with no-op detection)

**No-op short-circuit:** Before overwriting, check whether anything
meaningful changed since last /wrap:
- New commits across any of the 3 repos?
- New entries in proposed-learnings.jsonl?
- New DEAL-INTEL emissions in any deal log.json?
- Any items in SYNC_FAILURES from step 6?
- Any items in CADENCE_STALENESS from step 8?

If ALL are empty → log "no-op /wrap: zero delta since last wrap, skipping
SESSION-HANDOFF overwrite" and SKIP this sub-step entirely. Avoids noise
commits for /wrap invocations that don't change anything.

Otherwise: full overwrite of today's date file. Git preserves history.

Structure (mirror the 2026-05-21 version):
1. How to use this doc (paste block for next chat)
2. What this session shipped (grouped by chat-session if multi-chat)
3. Current state — system health (after vs before diff)
4. Live work-in-flight (incl. outstanding requests numbered for OR1 continuity)
   **4b. Outstanding — MUST include any SYNC_FAILURES from step 6
   (e.g., "TCIP -- My Skills Drive sync failed: <reason>")
   and CADENCE_STALENESS from step 8.**
5. Phases NOT executed (deferred items + why)
6. New learnings added this session (table with id, rule_code, title)
7. Quick diagnostic commands
8. Architectural promise (don't break the loop)

---

## STEP 6 — Derived-view regen via composite skills (no duplication)

`/wrap` is the orchestrator. Sync work is delegated to the canonical
composites — if they gain steps later, `/wrap` picks them up automatically.

### 6a. Mirror personal skills (wrap_auto.sh has the same step — interactive /wrap mirrors immediately so new skills don't wait until 11pm cron)

```bash
PERSONAL="$HOME/.claude/commands"
PUBLIC="$HOME/cos-pipeline/slash_commands"
mkdir -p "$PUBLIC"
for src in "$PERSONAL"/*.md; do
    [ -f "$src" ] || continue
    name=$(basename "$src")
    dst="$PUBLIC/$name"
    if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"; then
        cp "$src" "$dst"
        echo "  mirrored: $name"
    fi
done
```

### 6b. Invoke `/sync-system`

Runs sync_registry + sync_learnings + sync_system_docs internally,
all 3 in sequence with coordination locks.

**Capture any per-step failures** — /sync-system surfaces failures
like "TCIP -- My Skills: FAIL — Invalid argument" but they're
easy to miss in scrolling output. Parse the output for `FAIL` /
`ERROR` lines and add each to `SYNC_FAILURES` for Step 5e
(SESSION-HANDOFF §4b outstanding).

### 6c. Auto-generated maps + Drive setup mirror

```bash
# SYSTEM-MAP.md — live system scan, not a canonical-source view
python3 ~/cos-pipeline/tools/generate-system-map.py

# Drive setup mirror — sync ~/.claude/commands/, LaunchAgents, globals
# to _System/_Claude Code Setup/ for browsable visibility
python3 ~/cos-pipeline/tools/sync_setup_to_drive.py --apply
```

Skip-on-failure: capture errors → SESSION-HANDOFF §4b but continue.

**Cross-reference with periodic cadences:** dash-state-hook already runs
several syncs on intervals (ref_doc_sync 2h, project_inst_sync 24h,
chat_capture 4h, artifact_pull 4h, skill_telemetry 30min). /wrap doesn't
re-trigger those — instead surfaces "X has not run in N hours" warnings
if any have stalled (see Step 8 LaunchAgent + cadence check).

---

## STEP 7 — Heavy lifting (things you'd forget to run)

```bash
# 7a. Log compaction — archive entries >30d old across all deals
python3 ~/cos-pipeline/tools/log_compaction.py

# 7b. Orphan Drive cleanup — trash IDs flagged in _orphan_ids_pending_cleanup
python3 ~/cos-pipeline/tools/orphan_drive_cleanup.py --apply
```

**Note:** `reference_integrity_audit.py` is NOT called here standalone —
it's invoked by `/check-system` in Step 8. Single run, no duplication.

---

## STEP 8 — Verification via composite skill

```
8a. Invoke /check-system
    (runs coordination.py status + system_health.py + 
     reference_integrity_audit.py)
```

Capture the output. Diff against `last-wrap.json` health counts to surface
"warns went from X → Y" or "new fail: <name>" deltas.

```bash
# 8b. LaunchAgent state check — any newly down?
launchctl list | grep -iE "tomac|yoni|claude" | awk '$2 != "0" && $2 != "-" {print}'

# 8c. Cadence staleness check — are scheduled syncs falling behind?
# FIXED 2026-05-21 after /wrap pt 3: function `last_run_seconds_ago`
# does NOT exist in coordination.py. State field is 'last_run'
# (singular) with direct ISO strings as values.
python3 << 'PYEOF'
import sys
from datetime import datetime, timezone
sys.path.insert(0, '/Users/ygontownik/cos-pipeline/tools')
from coordination import _read_state

state = _read_state()
last_runs = state.get('last_run', {})  # singular field — NOT 'last_runs'
CADENCES_HRS = {
    'sync_registry.py':             24,
    'sync_learnings.py':            24,
    'sync_system_docs.py':          24,
    'log_compaction.py':           168,  # weekly floor
    'reference_integrity_audit.py': 24,
}
now = datetime.now(timezone.utc)
stale = []
for script, threshold_h in CADENCES_HRS.items():
    ts_str = last_runs.get(script)
    if not ts_str:
        stale.append(f'{script}: never recorded in coordination state')
        continue
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        hours = (now - dt).total_seconds() / 3600
        if hours > threshold_h:
            stale.append(f'{script}: {hours:.1f}h since last run (threshold {threshold_h}h)')
    except Exception as e:
        stale.append(f'{script}: parse-error {e}')

if stale:
    print('STALE syncs (flag in handoff §4b):')
    for s in stale: print(f'  - {s}')
else:
    print('All syncs within cadence.')
PYEOF
```

Flag any newly-down LaunchAgents or stale syncs in the handoff §4b.

---

## STEP 9 — Commit + push (narrow git add per repo)

### 9.pre — Public/private (PD1) hard gate

Before any push, re-run the PD1 check against `~/cos-pipeline/` (the public
repo). PD1 enforces "tenant slug leaks never ship to public" — hardcoded
`tomac`, `yoni`, account numbers, private deal aliases, etc. If the check
returns FAIL on any *hard* hit (not allow-listed soft hit), STOP the push,
do NOT commit, and present the violations to Yoni for fix-and-recommit.

```bash
python3 -c "
import json, sys
sys.path.insert(0, '/Users/ygontownik/cos-pipeline/tools/checks')
from check_pd1 import run
r = run()
status = r.get('status', 'fail')
print(f'PD1 gate: {status} · {r.get(\"summary\", \"\")}')
if status == 'fail':
    print()
    print('  ▼ Hard hits (must fix before push):')
    for v in r.get('details', {}).get('violations', []):
        print(f'    {v.get(\"file\",\"?\")}:{v.get(\"line\",\"?\")}  {v.get(\"snippet\",\"\")[:80]}')
    print()
    print('PUSH HALTED. Fix the leaks, commit, then re-run /wrap.')
    print('Override (rare, document in §4b): /wrap --skip-pd1-gate')
    sys.exit(2)
" || exit 2
```

The gate is hard-coded to FAIL only. WARN (allow-listed soft hits) does
not block — those have already been reviewed and accepted into the
allow-list. PASS is normal happy path.

**Override:** `/wrap --skip-pd1-gate` skips the gate but logs the override
in SESSION-HANDOFF §4b as an explicit outstanding item: "PD1 gate
overridden — N hard hits shipped to public, must be cleaned up next
session." Reserve this for genuine emergencies (e.g., a tenant-slug
match that's actually a false positive that the allow-list hasn't
caught yet — fix the allow-list, then re-run cleanly).

### 9a. Commits + pushes

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

### 9c. Dashboard warmup (NEW — added 2026-05-21 after /wrap verification)

/wrap touches files the dashboard reads (deal-system-data.json regen by
sync_registry, log.json compaction, drive-docs.yaml changes, etc.).
Dashboard server has a 20-min auto-warmup thread, but that means the
dashboard could serve stale data for up to 20 min after /wrap commits.

Cheap fix: POST /warmup immediately. ~5ms.

```bash
# 9c.1 Warmup
curl -s -o /dev/null -w "/warmup HTTP %{http_code}\n" \
    -X POST http://localhost:7777/warmup

# 9c.2 Verify cache + surface deal_sync_stale flag
STATUS=$(curl -s http://localhost:7777/cache-status)
echo "$STATUS" | python3 -c "
import json, sys
s = json.load(sys.stdin)
print(f'Cache fetched: {s.get(\"fetchedAt\")}')
print(f'deal_sync_stale: {s.get(\"deal_sync_stale\")}')
oldest = max(((k, v.get('ageMin', 0)) for k, v in s.get('sections', {}).items()),
             key=lambda x: x[1], default=('?', 0))
print(f'Oldest section: {oldest[0]} ({oldest[1]:.1f} min)')
"
```

If `deal_sync_stale: true` surfaces, add to SESSION-HANDOFF §4b:
"deal-sync is overdue (>2h since last run) — invoke /deal-sync or wait
for next dash-state-hook cycle."

### 9b. Uncommitted-leftover audit (NEW — added 2026-05-21 after /wrap pt 3)

After all commits are made, scan each repo for remaining uncommitted files.
These are either (a) parallel-chat work this /wrap couldn't attribute, or
(b) auto-modified state files that shouldn't have been touched.

```bash
for repo in ~/cos-pipeline ~/dashboards ~/cos-pipeline-config-tomac; do
  LEFTOVER=$(git -C "$repo" status --short | wc -l)
  if [ "$LEFTOVER" -gt 0 ]; then
    echo "WARN: $(basename $repo) has $LEFTOVER uncommitted file(s) after /wrap"
    git -C "$repo" status --short | head -20
  fi
done
```

For each repo with leftovers, classify each file:
- **State files** (`data/*.json`, `_state.json`, etc.) — likely auto-touched by hooks; acceptable to leave (will be picked up by wrap_auto.sh's next run if real change)
- **Generated outputs** (`compiled/*`, `tomac-cove-build/*`) — likely auto-regenerated; acceptable
- **Source-code files** (`*.py`, `*.sh`, `*.md`, `*.yaml`, `*.html`, `*.js`) — REAL work from parallel chats. Add to SESSION-HANDOFF §4b outstanding so next session knows to attribute + commit them.

Do NOT bulk-add uncommitted files into /wrap's commit. Attribution matters.

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

Then PRINT (don't write to file — emit to terminal) a **session summary**
followed by the next-chat paste-block. The summary lets Yoni eyeball what
happened in a single screen without opening the handoff doc.

### 11a. Session summary (terminal-only, colloquial narrative + structured detail)

Print a **plain-English recap** of what shipped this session, followed
by a **detail block** for drilling in. The narrative should read like
you're telling Yoni what happened over the shoulder — not like a CI
report. Active voice, concrete numbers, three short paragraphs.

Compose from data already collected in earlier steps. Print exactly this
shape:

```
═══════════════════════════════════════════════════════════════
/wrap — session recap · <ISO timestamp>
═══════════════════════════════════════════════════════════════

<Paragraph 1 — What you actually did.
 Lead with the headline outcome in plain English. Name the deals or
 systems touched. Use numbers (files moved, lines changed, time saved)
 not jargon. Example: "Cleaned up the local file backlog and made the
 daily cleanup permanent. The organizer moved 331 files out of the
 top of ~/Downloads and ~/Desktop into deal folders + month-bucketed
 _Unsorted; a follow-up pass after tightening the atime logic caught
 43 more stragglers. From here on the LaunchAgent fires every morning
 at 6:15 — no manual cleanup needed.">

<Paragraph 2 — Drive + git side.
 What happened to shared state. Which Drive docs got edited-in-place,
 whether anything new got created (should usually be 0), what shipped
 to GitHub. Example: "On the Drive side, five system docs got
 edited-in-place via Deal Sync Writer — no new docs, no orphan IDs,
 all 147 registered fileIDs still resolve. Three commits across two
 repos pushed cleanly: the LF3 refinement in cos-pipeline, the Office
 lock-file pattern in cos-pipeline-config-tomac, and the new /wrap
 summary in cos-pipeline.">

<Paragraph 3 — Health delta + what's next.
 System health before vs after, and the 1-3 most important outstanding
 items. Example: "Health went from 3 fails to 1 — loose_local_files
 flipped to PASS, the only remaining fail is past-due deal actions
 which is operational, not a system issue. The biggest open item is
 the align_infra alias regex bug — \b doesn't match at underscore
 boundaries, so align_infra_*.md files are going to _Unsorted instead
 of _Routed/align_infra/. ~5 min to fix.">

──── Detail ────────────────────────────────────────────────────

Health         <p> pass · <w> warn · <f> fail   (Δ since last wrap: <±fails> fail · <±warns> warn)
               <one-liners on notable transitions: "loose_local_files: FAIL → PASS",
                "past-due deal actions: 34 (unchanged)", etc.>

Drive          Edited in place (I11)              <N> docs
                   • <doc name>   <fileId tail>
                   • <doc name>   <fileId tail>
                   ...
               Created                              <N> docs   <✓ EP1 clean if 0 / ⚠ review if >0>
               Orphans trashed                      <N> IDs
               Registered IDs resolving           <hit>/<total>  <✓ or ⚠>

Local files    Downloads top: <before> → <after>   (<n> routed)
               Desktop top:   <before> → <after>   (<n> routed)
               Documents:     <before> → <after>

Public/private (PD1) check
               <PASS / WARN / FAIL>  <summary line from check_pd1.py>
               <If FAIL: list the file:line hits — these were caught and force-fixed
                BEFORE push by the STEP 9 gate, OR if --skip-pd1-gate was passed,
                they shipped and are flagged in §4b outstanding>

Commits        <repo-name padded>  <sha>  <subject>
               <repo-name padded>  <sha>  <subject>
               <repo-name padded>  <sha>  <subject>

Pushed         <all 3 repos ✓ / partial: list which / none / blocked by PD1 gate>

Learnings      captured <N> new
                   • <L00XX (CODE): title>
                   • <L00YY (CODE): title>

Outstanding    <N> items rolled into SESSION-HANDOFF-<today>.md §4b:
               1. <item>
               2. <item>
               ...   (truncate at 5; "+ N more in §4b" if longer)

Failures       <none / list each: step, what failed, where logged>

═══════════════════════════════════════════════════════════════
Next-chat paste-block ↓↓↓
═══════════════════════════════════════════════════════════════
```

**Narrative writing rules:**
- Three short paragraphs maximum. If the session was tiny, one paragraph is fine.
- Lead with the *outcome*, not the *activity*. ("Downloads top went from 280 to 5" beats "Ran the organizer apply.")
- Name deals, systems, files concretely — never "we did some stuff with deals."
- Active voice. Past tense. ("Fixed X" not "X was fixed" or "fixing X.")
- No jargon the user didn't already say. If the rule is L0023, write "the raw-anthropic-import check," not "L0023 enforcement."
- If something didn't happen that should have, say it in the third paragraph: "Skipped the DRIVE-ARCHITECTURE.md §7 extension because it's still pending design input."

**Data sources for the detail block:**
- Health: STEP 8 snapshot + the pre-/wrap baseline captured at STEP 1
- Drive edited-in-place: list every fileId touched by sync_system_docs.py + sync_learnings.py + sync_setup_to_drive.py (these all use Deal Sync Writer setContent)
- Drive created: should be 0 in normal /wrap. If >0, it means /new-deal ran and registered new IDs — show them
- Orphans trashed: STEP 7b output (orphan_drive_cleanup.py result)
- Registered IDs resolving: STEP 7 Drive integrity audit
- Local files moved: tail of `~/dashboards/logs/local-organizer.log` since last wrap + `~/dashboards/logs/local-file-router.log` since last wrap
- PD1 check: STEP 9's pre-push PD1 gate result (see STEP 9 — PD1 must PASS or WARN to allow push; FAIL halts unless --skip-pd1-gate was passed)
- Commits: `git -C <repo> log <last-wrap-sha>..HEAD --oneline` for each of the 3 repos
- Pushed: STEP 9 result
- Learnings: STEP 5d output (count + ids of approved candidates)
- Outstanding: SESSION-HANDOFF §4b numbered list (truncate at 5 items, link to §4b for the rest)
- Failures: SYNC_FAILURES from STEP 6 + CADENCE_STALENESS from STEP 8 + any STEP-level errors logged to wrap.log

### 11b. Next-chat paste-block

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
SESSION-HANDOFF-<today>.md §4b. Session summary + next-chat paste-block above."
