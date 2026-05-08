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

Once, per deal, before processing files:

```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-deal-doc <deal_id> status > /tmp/<deal_id>_status_current.md
python3 ~/cos-pipeline/tools/deal_extract_helpers.py read-deal-doc <deal_id> brief  > /tmp/<deal_id>_brief_current.md
```

Read both files into your context.

If a deal has no fully populated status/brief yet (e.g. it was just
scaffolded — a stub), use `pngts_status.md` and `pngts_master_brief.md`
as your structural template.

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

Using the file's content + the current status + the current brief,
produce **two** updated documents:

**Updated status doc** — concise, structured, follow Yoni Personal
Context six-section memo structure. Keep stable: Critical Driver,
Stage, Health, Next Milestone, Open Questions. Update what changed.

**Updated master brief doc** — long-form deal narrative. Append new
intel to the appropriate section (counterparties, thesis, key risks,
TCIP edge, action log). Don't rewrite history; add context.

Write both to /tmp:

```
/tmp/<deal_id>_status_new.md
/tmp/<deal_id>_brief_new.md
```

### 4c. Write back to Drive

```bash
cat /tmp/<deal_id>_status_new.md | python3 ~/cos-pipeline/tools/deal_extract_helpers.py write-deal-doc <deal_id> status
cat /tmp/<deal_id>_brief_new.md  | python3 ~/cos-pipeline/tools/deal_extract_helpers.py write-deal-doc <deal_id> brief
```

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
| `counterparties` (rebuild from Key Names + roles) | `tcip_edge` if not stated |
| `actions` (Open Items table → action objects with priority, status, owner, due) | `tagline`, `sector`, `geography`, `ticker` |
| `last_activity` = today | `_schema_version` |
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

## STEP 7 — Final summary

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

---

## RULES (non-negotiable)

- Never call the Anthropic API directly. AI work happens in this session.
- Never edit `~/dashboards/data/compiled/*.json` by hand — those are
  regenerable artifacts.
- Never delete files. The `_Ready/` move is the only "done" signal.
- On any helper failure, log + continue. Don't abort the whole run.
- Treat `user-state/` files (anywhere) as sacred — don't touch them.
- Status/brief docs are the source of truth for deal state. Don't
  invent fields. Preserve fields you don't know how to update.
