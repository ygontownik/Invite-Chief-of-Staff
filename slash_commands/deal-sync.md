---
description: Process new source files in each TCIP deal folder; update status + master brief docs; regenerate firm-context pipeline section
argument-hint: "[--dry-run] [--deal-id <id>]"
---

# /deal-sync — Per-deal extract, update, and pipeline regeneration

You are running the TCIP deal-extract pipeline. All AI work happens in
**this** Claude Code session — never call the Anthropic API directly,
never spawn another Claude session for the LLM portion. Drive I/O,
dedup state, and folder moves are handled by:

```
~/cos-pipeline/tools/deal_extract_helpers.py
```

Run that as `python3 ~/cos-pipeline/tools/deal_extract_helpers.py
<sub-command> ...`. Pass `--dry-run` (before the sub-command) on every
write helper if `--dry-run` was passed to /deal-sync.

---

## STEP 0 — Parse flags

`$ARGUMENTS` may contain:
- `--dry-run` — do not write to Drive; print intended actions
- `--deal-id <id>` — process only one deal (e.g. `pngts`)

Default: process every deal in the registry.

---

## STEP 1 — Enumerate deals

Run:

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py list-deals \
  ${DEAL_ID:+--filter $DEAL_ID}
```

Parse the JSON. You now have, for each deal: `deal_id`,
`drive_folder_id`, `status_id`, `brief_id`, `last_run`.

---

## STEP 2 — For each deal, gather inputs from BOTH sources

`/deal-sync` is a regenerator, not an extractor. The actual extraction
work happens upstream:
- **`cos_capture_pipeline.py`** routes Gmail/Otter/calendar/awaiting-external
  into `~/dashboards/data/deals/<deal>/log.json` — already deal-tagged with
  source URLs and structured fields.
- **`intel_capture.py`** routes `---DEAL-INTEL---` blocks from Claude Code
  transcripts and claude.ai project chats into the same `log.json`.

So /deal-sync reads from TWO buckets per deal, merges them, and rebuilds
the status + brief docs:

### 2a. New `log.json` entries since last_run

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-log-entries <deal_id>
```

Returns a JSON array of entries from `~/dashboards/data/deals/<deal>/log.json`
that are: (a) dated on/after last_run AND (b) not already captured (per
`captured_log_ids` in deal_extract_state.json).

Each entry has `{id, date, source, who, what, source_url, source_title, match}`.
This is the canonical deal-tagged feed — already structured.

### 2b. New raw source files in the deal Drive folder

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py list-new-files <deal_id>
```

Returns a JSON array of `{file_id, name, mime, modified}` for files
in the deal Drive folder modified since `last_run`, **excluding**:
- The deal's own `*_status.md`, `*_master_brief.md`, `*_dashboard_entry.json`
- Anything in the `_Ready/` subfolder (already processed)
- Files already in dedup state with outcome=success
- Mime types we cannot read

These are ad-hoc drops (a transcript you uploaded directly, a memo
you stashed). For most deals these will be sparse — the bulk of intel
flows through 2a.

**If BOTH 2a and 2b are empty for this deal:** bump `last_run`, move on:

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py update-last-run <deal_id>
```

**If `--dry-run`:** print the inputs and stop here for this deal.
Do not read content, do not write docs.

---

## STEP 3 — Read current state for the deal

Once, per deal, before processing files. Each deal's `_Claude Context/`
subfolder in Drive contains five Claude-readable artifacts:

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-deal-doc <deal_id> status  > /tmp/<deal_id>_status_current.md
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-deal-doc <deal_id> brief   > /tmp/<deal_id>_brief_current.md
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-deal-doc <deal_id> lps     > /tmp/<deal_id>_lps_current.md
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-deal-doc <deal_id> terms   > /tmp/<deal_id>_terms_current.md
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-deal-doc <deal_id> actions > /tmp/<deal_id>_actions_current.md
```

Read all five into your context:

| Doc | Captures | Maintained by |
|---|---|---|
| status.md | Critical Driver, deadlines, open items, counterparties | /deal-sync patches in-place (full rewrite only on stage change or structural pivot) |
| master_brief.md | Long-form deal narrative — thesis, history | /deal-sync appends sections; never regenerates prior content |
| lps.md | LP / customer pipeline (status, last touch, bite, next action) | Manual (informs status/brief regen) |
| terms.md | Economics log — fees, hurdle, carry, capital stack | Manual (informs status/brief regen) |
| actions.md | Open / awaiting / closed actions | Local copy is canonical (compile-dashboard.py reads it); Drive copy is a mirror |

**Use LPs + TERMS + actions as read-only context** when synthesizing the
new status/brief — they ground deal economics + pipeline state. Don't
rewrite them in this step (LPs and TERMS are manually maintained;
actions.md is structured local input for the dashboard).

If a deal has no fully populated status/brief yet (just scaffolded — a
stub), use `pngts_status.md` and `pngts_master_brief.md` as your
structural template.

---

## STEP 4 — Process inputs (log entries first, then raw files)

**Process log.json entries (Step 2a) first** — they're already structured
and don't need text extraction. THEN process raw Drive files (Step 2b).

### 4-pre. Fold log.json entries into in-memory status + brief

Each log entry has the form:
```json
{ "id": "abc123", "date": "2026-05-08", "source": "intel|email|call|...",
  "who": "Gideon Powell", "what": "...", "source_url": "...",
  "source_title": "..." }
```

For each entry: merge its `what` content into your in-memory status +
brief (e.g. add to counterparties, action items, key facts). When done
processing all entries, mark each captured:

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py mark-log-captured <deal_id> <entry_id>
```

These are NOT moved to `_Ready/` (they live in log.json, not Drive
folders) — `mark-log-captured` is the dedup signal.

### 4a. Read raw file content (Step 2b items)

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-file <file_id>
```

If the helper returns a `[BINARY PDF — ...]` or `[BINARY DOCX — ...]`
marker, the file cannot be extracted with the current toolchain. Mark
it processed=`failed`, log a one-line note in your final summary, and
move on. Do NOT block on it.

### 4b. Synthesize updated docs

**Step 1 — Decide update mode before writing anything.**

| Mode | Triggers |
|------|----------|
| **INCREMENTAL** | 1–3 log entries only; no raw files; no stage change; no new counterparty |
| **FULL-REWRITE** | Stage change detected; >3 significant new facts; first-time population of a stub; major structural pivot; raw file contains a full call transcript or deal memo |

Default to **INCREMENTAL** unless a full-rewrite trigger is present. When in doubt, increment.

---

**INCREMENTAL mode** (merge new intel into a complete fresh status doc):

*Status doc*: Start from `_status_current.md`. Merge each piece of new intel into the appropriate section (update counterparty line, revise deadline date, add open item, update stage note). Then output the **complete status doc** — all sections, current state only. Never emit partial diffs or append-only blocks to the status doc. The written version IS the complete current state.

**Size discipline**: After merging, the status doc should remain ≤ 8KB. If the result would exceed 8KB, apply these cuts before writing:
- Remove any Session Log or "Last Session Summary" block (history belongs in master brief only)
- Remove CLOSED/SUPERSEDED items from all tables (move key facts to inline text if needed)
- Remove past-due deadlines (dates before today) unless the item is still actionable
- Trim Reference Documents sections entirely

*Master brief*: Append a dated block to the relevant section only. Format:

```
**[YYYY-MM-DD]** <one-line intel fact or action>
```

---

**FULL-REWRITE mode** (regenerate structure, preserve substance):

*Status doc*: Regenerate all sections cleanly. Carry forward all current-state facts from `_status_current.md` — no context lost, only history removed. Keep Critical Driver, Stage, active Hard Deadlines, open Counterparties, and open items. Drop resolved/historical content. Target size ≤ 8KB.

*Master brief*: Append a complete new section with today's date header. Do not alter prior sections. Structure: Core narrative update → New counterparty entries → Revised thesis notes (if any) → Action items surfaced.

---

Write both to /tmp regardless of mode:

```
/tmp/<deal_id>_status_new.md
/tmp/<deal_id>_brief_new.md
```

---

### 4b-ii. Action routing — deal vs. team

After synthesizing the status doc, classify every action item as DEAL or TEAM before writing anywhere.

**DEAL action** — goes to `actions.md` only:
- Diligence tasks ("model PNGTS+GSGT combined EBITDA")
- Regulatory or legal research ("determine FERC approval required for GSGT carve-out")
- Market checks, data pulls, analytical work
- Counterparty analysis

**TEAM action** — goes to `actions.md` AND to CoS follow-ups (envelope):
- Scheduling or meeting coordination ("schedule ShoreBridge call")
- Outreach to a named person ("call Mark Mitchell re: PNGTS reserve price")
- Intro requests or external comms
- Any action with a specific hard date and named external party

For TEAM actions, emit an envelope item to the CoS dashboard immediately:

```bash
python3 -c "
import sys; sys.path.insert(0, '$HOME/cos-pipeline')
import _envelope_writer as ew
ew.append_items([{
  'content_type': 'my_action',
  'owner': '<owner>',
  'due': '<YYYY-MM-DD or empty>',
  'counterparty': '<named party>',
  'context': '<one line why>',
  'dashboard_path': 'Chief of Staff › Follow-ups',
  'content': '<verb-first action statement>'
}])
"
```

---

### 4b-iii. actions.md consolidation rules

When writing or updating `actions.md`, apply these rules to keep the list actionable:

**1. Close completed actions.** If new intel confirms an action is done (e.g. "Mark confirmed the call happened"), move it from the open table to `## Closed items`. Mark `status: done` and add a `[YYYY-MM-DD closed]` note in the action text.

**2. Group by workstream.** If 2+ open actions answer the same underlying question or belong to the same workstream, group them under a `### Workstream: <label>` header. Each workstream gets its own table header row. The parser skips section headers, so this is fully compatible with `compile-dashboard.py`.

Format:
```markdown
# Open Actions

### Workstream: GSGT Regulatory Path
| # | Action | Owner | Due | Priority | Status | Opened |
|---|--------|-------|-----|----------|--------|--------|
| W1.1 | Determine whether FERC approval required for GSGT carve-out | Yoni | 2026-05-20 | critical | open | 2026-05-12 |
| W1.2 | NH PUC approval timeline vs. PNGTS closing date | Yoni | 2026-05-20 | high | open | 2026-05-12 |

### Workstream: Platform Valuation
| # | Action | Owner | Due | Priority | Status | Opened |
|---|--------|-------|-----|----------|--------|--------|
| W2.1 | Size combined PNGTS+GSGT platform EBITDA and equity check | Yoni/Mark | 2026-05-19 | high | open | 2026-05-12 |

## Closed items
...
```

**3. Cap at ~6 open workstreams.** If more than 6 workstreams exist, move lower-priority ones to a `## Parking lot` section below Closed items. They stay visible but don't feed the health formula's critical-open count.

**4. Standalone actions** (not part of a workstream) go in an `### Other` group at the bottom of the open section.

---

### 4c. Write back to Drive

**CRITICAL:** Use ONLY `write-deal-doc` to write status and brief files. Never use the Google Drive MCP tools (`mcp__*__create_file`, `mcp__*__copy_file`, etc.) to write these docs — those tools create new files with new IDs, leaving the registered doc untouched and producing duplicate files that corrupt the folder. `write-deal-doc` always overwrites the registered doc_id in-place via `files().update()`.

```bash
cat /tmp/<deal_id>_status_new.md | python3 ~/cos-pipeline/tools/deal_extract_helpers.py write-deal-doc <deal_id> status
cat /tmp/<deal_id>_brief_new.md  | python3 ~/cos-pipeline/tools/deal_extract_helpers.py write-deal-doc <deal_id> brief
```

If the helper prints `OK wrote status for <deal_id>`, the registered doc was updated in place. If it prints `created` anywhere, something went wrong — stop and investigate before continuing.

If `--dry-run`, helpers print the intent and skip the write.

### 4d. Refresh dashboard_entry.json

After status + brief are updated, also refresh the deal's
`{deal_id}_dashboard_entry.json` so the dashboard cards stay current
without a manual `/deal-update` session.

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-deal-entry <deal_id> > /tmp/<deal_id>_entry_current.json
```

If the file prints `{}`, no entry exists yet — start from this stub:

```json
{
  "_schema_version": 1,
  "id": "<deal_id>",
  "name": "<deal_name from registry>",
  "stage": "Sourcing",
  "stage_index": 0,
  "health": 50,
  "owner": "<lead from registry>",
  "last_activity": "YYYY-MM-DD",
  "last_updated": "YYYY-MM-DD",
  "thesis": [],
  "tcip_econ": {"status_note": "Not yet formalized"},
  "tcip_edge": "",
  "key_risk": "",
  "critical_next_step": "",
  "counterparties": [],
  "contacts": [],
  "next_milestone": "",
  "next_milestone_due": "",
  "phase_capital": [],
  "workstreams": [],
  "actions": [],
  "activity_log": []
}
```

Synthesize an updated entry by **merging** the new status doc into the
current entry. Update only what the status reasonably reveals:

| Update from status | Preserve from current |
|---|---|
| `stage`, `stage_index` (parse "Stage:" or first phase header) | `phase_capital` (numbers from a deal session) |
| `next_milestone`, `next_milestone_due` (Hard Deadlines table) | `tcip_econ` (numbers) |
| `key_risk` (top tension or open question) | `thesis` pillar scores (set in deal sessions) |
| `critical_next_step` — one sentence: the single action that most unblocks the deal right now, synthesized across all open workstreams. Not the longest list; the sharpest answer. | `tcip_edge` if not stated |
| `counterparties` (rebuild from Key Names + roles) | `tagline`, `sector`, `geography`, `ticker` |
| `actions` (Open Items table → action objects with priority, status, owner, due) | `_schema_version` |
| `last_activity` = today | |
| `last_updated` = today | |
| `activity_log` — append one entry: `{date: today, summary: "Auto-sync from /deal-sync"}` | |
| `health`: nudge ±5 based on whether new info is mostly positive/negative; clamp [0,100] | |

Write the merged entry:

```bash
cat /tmp/<deal_id>_entry_new.json | python3 ~/cos-pipeline/tools/deal_extract_helpers.py write-deal-entry <deal_id>
```

The helper writes to Drive AND triggers `sync_deals_from_drive.py
--deal-id <deal_id>` to push to compiled.

If `--dry-run`, the helper prints intent and skips the Drive write.

### 4d-ii. Write actions.md from entry (non-Cholla deals)

After writing the entry, populate the local `actions.md` if it's still a
stub (no real table rows). This feeds the health-formula's 30% actions
component in `compile-dashboard.py`.

```bash
cat /tmp/<deal_id>_entry_new.json | python3 ~/cos-pipeline/tools/deal_extract_helpers.py \
  ${DRY_RUN:+--dry-run} write-actions-md <deal_id>
```

The helper skips automatically if `actions.md` already has real content
(e.g. Cholla's hand-maintained table). Safe to always run.

### 4e. Move source to _Ready/

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py move-to-ready <file_id> <drive_folder_id>
```

### 4f. Mark processed

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py mark-processed <deal_id> <file_id> success
```

### 4g. Refresh in-context state

The status and brief you just wrote are now the "current" versions for
the next file. Replace your in-memory copies before moving on.

### 4h. Failure handling

If any helper exits non-zero or you cannot generate updated content:
- `mark-processed <deal_id> <file_id> failed`
- Log one-line note (deal/file/error) — collect for the final summary
- Continue to the next file. Never abort the whole run.

---

## STEP 5 — Bump last_run for the deal

After all files for the deal are processed (success or failed):

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py update-last-run <deal_id>
```

---

## STEP 6 — Regenerate ACTIVE DEAL PIPELINE section

Skip this step if `--dry-run` OR if no deal had any new files this cycle.

For every deal in the registry, read its **current** status doc and
extract:
- Stage (look for "Stage:", "Critical Driver:", or the first section
  header that names the deal phase; fall back to `TBD` if unparseable)
- Yoni's role (lead vs. support — from the deal's registry entry or
  from a "Roles" line in the status doc)
- Mark/Nik's role

Build this exact markdown:

```
| Deal | Stage | Yoni | Mark/Nik | Status doc |
|------|-------|------|----------|------------|
| {Name} | {Stage} | {Yoni role} | {Mark or Nik role} | [link](https://docs.google.com/document/d/{status_id}/edit) |
```

Pipe it via:

```bash
cat /tmp/pipeline_section.md | python3 ~/cos-pipeline/tools/deal_extract_helpers.py regenerate-pipeline-section
```

The helper replaces only the body between the
`<!-- AUTO-GENERATED-PIPELINE-START -->` and
`<!-- AUTO-GENERATED-PIPELINE-END -->` markers in the **TCIP Firm
Context** doc. The rest of the doc is preserved verbatim.

---

## STEP 7 — Final summary and dashboard refresh

Print a clean summary:

```
DEAL-SYNC COMPLETE  (dry-run=<true|false>)
=========================================
{deal_id}: <N> processed, <M> failed   (skipped — no new files | last_run updated)
...
Pipeline section: <regenerated | skipped (no changes) | skipped (dry-run)>

ERRORS (if any):
- {deal_id}/{file_id}: {one-line error}
```

Then (unless `--dry-run`) recompile health scores and push to the live dashboard:

```bash
python3 ~/dashboards/routines/compile/compile-dashboard.py
curl -s -X POST http://localhost:7777/refresh-deals > /dev/null && echo "Dashboard updated."
```

Then write a heartbeat file so the dashboard can detect a stale deal-sync:

```bash
python3 -c "
import json, datetime
from pathlib import Path
p = Path.home() / 'dashboards/data/deal-sync-heartbeat.json'
p.write_text(json.dumps({'last_completed_at': datetime.datetime.now().isoformat(), 'dry_run': False}, indent=2))
print('Heartbeat written.')
"
```

If `--dry-run` was passed, write a dry-run heartbeat instead (does not overwrite a real-run heartbeat):

```bash
python3 -c "
import json, datetime
from pathlib import Path
p = Path.home() / 'dashboards/data/deal-sync-heartbeat.json'
# Only write if file is missing OR last run was also a dry-run
try:
    existing = json.loads(p.read_text()) if p.exists() else {}
except Exception:
    existing = {}
if existing.get('dry_run', True):
    p.write_text(json.dumps({'last_completed_at': datetime.datetime.now().isoformat(), 'dry_run': True}, indent=2))
    print('Dry-run heartbeat written.')
else:
    print('Skipped heartbeat write (last real run preserved).')
"
```

Run the appropriate block based on whether `--dry-run` was passed.

---

## RULES (non-negotiable)

- Never call the Anthropic API directly. AI work happens in this session.
- **Never use Drive MCP tools to write status/brief docs.** Use ONLY `write-deal-doc` (which calls `files().update()` on the registered doc_id). Drive MCP `create_file`/`copy_file` create new files with new IDs — this produces duplicates and leaves the registered doc stale. The only exception is `read-file` / `read-deal-doc` for reads.
- Never edit `~/dashboards/data/compiled/*.json` by hand — those are
  regenerable artifacts.
- Never delete files. The `_Ready/` move is the only "done" signal.
- On any helper failure, log + continue. Don't abort the whole run.
- Treat `user-state/` files (anywhere) as sacred — don't touch them.
- Status/brief docs are the source of truth for deal state. Don't
  invent fields. Preserve fields you don't know how to update.
