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

**What does NOT belong in the status doc** — these always route elsewhere, regardless of size budget:

| Content type | Goes to | Why not status |
|---|---|---|
| Pending drafts (email/one-pager/memo to a named recipient) | `actions.md` as a `### Workstream: Pending Drafts` table; if dated + named external party, also CoS Follow-ups envelope (TEAM action) | Status describes deal state; drafts are owner-tasked work product |
| Session deliverables / artifacts (PPTX, PDF, deck filenames) | Master brief "### Session deliverables" subsection of the dated incremental block | Status is current-state; artifacts are session-bound history |
| Decisions ledger ("decided X, rejected Y") | Master brief dated incremental block | Status doesn't carry rejected alternatives or rationale text |
| Session summaries / what-was-worked-on prose | Master brief dated incremental block | Repeats activity already in log.json + master brief |
| Research-task questions (the questions themselves, not the task) | Master brief; the action goes to `actions.md` | Questions are exploration; status carries only the action |

Status carries only: Critical Driver, Stage (one line), Structure Summary (terse), Hard Deadlines (open only), Counterparties (active), Open Items (titles + owner + due — no rationale prose). Everything else is master_brief or actions.md material.

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

### 4b-iv. Output 3 — jane_brief.md (Jane portfolio-layer substrate) + master_brief.md Jane Header

**Part A — master_brief.md Jane Header**

Every master_brief.md you write or update MUST START with a structured Jane Header
(before all other content). Sonnet extracts these fields from log.json entries,
deal.md counterparties, and recent transcripts. No manual authoring required —
extract from existing inputs.

```markdown
**[Jane Header — auto-generated <YYYY-MM-DD>]**
- **Frame:** <1 sentence — strategic shape of this deal right now. Be specific. Examples:
  "FIT shows breadth of deal flow to Brian Becker, not single-deal-gated";
  "PNGTS bid positioning depends on BlackRock/MSIP committee outcome (Doug-leaving context)."
  If insufficient signal: "(insufficient signal — flag for Yoni)">
- **Relationship holders:** <named TCIP person who owns each major counterparty relationship.
  Examples: "Mark Saxe owns Brian Becker, Mark Mitchell, Garden Investments; Yoni owns Gideon
  Powell, Sasha (GridFree)". Extract from log.json who field + deal.md contacts.
  If insufficient signal: "(insufficient signal — flag for Yoni)">
- **Deal-killers:** <conditions that would end engagement. Extract from log entries mentioning
  blockers, non-negotiables, walk-away conditions. Examples: "Baazov non-negotiables on FIT
  GP-stake economics permanently rejected"; "BlackRock committee blocks PNGTS reserve price".
  If insufficient signal: "(insufficient signal — flag for Yoni)">
- **Cross-deal links:** <if this deal connects to another deal via shared actor or structure,
  name them. Examples: "FIT and BBEH share Brian Becker counterparty"; "PNGTS GSGT carve-out
  creates regulatory dependency". If no cross-deal links identified: "None identified.">

---

(existing master_brief content follows)
```

**Jane Header extraction rules:**
- Extract from: log.json entries (who, what, source fields) + deal.md counterparties
  array + any decision_state_jane.md content if still present locally.
- If a field cannot be extracted with confidence, write `(insufficient signal — flag for Yoni)`
  rather than invent or fabricate.
- Absolute dates only (YYYY-MM-DD) per Rule AB1 — no relative references.
- Update the date in `[Jane Header — auto-generated <YYYY-MM-DD>]` to today's date on every write.
- The header must be the FIRST content in the file, before any other sections.

---

**Part B — jane_brief.md**

For each deal you process this run, ALSO generate Jane Brief content and
write it to `~/dashboards/data/deals/<slug>/jane_brief.md` AND its
registered Drive Doc via `write-jane-brief`.

**Skip this deal's jane_brief if:** zero new log entries since last brief AND
the existing jane_brief.md already exists and is not a placeholder. Leave it
untouched.

**Content structure** (Jane's portfolio critic parses these fields by heading —
follow EXACTLY, heading names are machine-parsed):

```markdown
# Jane Brief — <Deal Name>
_Generated <YYYY-MM-DD> by /deal-sync. Reflects log through entry <date>._

## Strategic frame
<1-2 sentences. What this deal IS strategically RIGHT NOW. Derived from
master_brief.md Jane Header (Frame field) + recent log entries.
master_brief.md is the authoritative per-deal context maintained by /deal-sync —
contains current state, relationship holders, sequencing, open items, deal-killers.
Apply it as the lens. If a DEAL-INTEL block with type=strategic_frame_override
exists in recent log, that overrides — quote it verbatim and cite the entry.>

## Proposed next action
<1 sentence. The single most important next strategic move for this deal.
NOT merely the curated action from actions.md — your synthesized read of
what Yoni should actually be working on right now.
If the curated action matches your read: "Curated action matches: <curated>".
Otherwise propose the strategic reframe.>

## Why (citations only — no vibes)
<2-4 bullets. Each MUST cite a specific log entry (date + snippet) or a
line from master_brief.md. NO narrative prose. NO "I notice that".
Every claim traces to a deterministic source. If you can't cite it, drop it.>
- log.json:<slug> entry <YYYY-MM-DD>: "<verbatim snippet ≤120 chars>"
- master_brief.md: "<exact line from the master brief>"

## Open threads (last 14d, max 5)
<Distinct counterparty-or-topic threads with movement in last 14 days.>
- <thread label> — first <YYYY-MM-DD>, last <YYYY-MM-DD>, <N> entries

## Blockers (max 3)
<Items waiting on external action, with age.>
- <what> — awaiting <whom> — since <YYYY-MM-DD> (<N>d)

## Last material signal
<YYYY-MM-DD> — <one-line summary of the most consequential recent log entry>

## Upcoming calls (next 7d)
<Calendar events involving this deal's counterparties OR with the deal name
in the title, dated within the next 7 days (relative to today, the date you
are running). Drives Jane's "prepare before this meeting" suggestions.
If no upcoming calls are known, write: "No upcoming calls identified.">
- YYYY-MM-DD HH:MM — <event title>, attendees: <list or "unknown">
```

**Constraints:**
- Target 1-2KB per brief. Tight. Jane reads 9 of these per run; don't bloat.
- Citations are MANDATORY in the Why section. No citation, no claim.
- Absolute dates only (YYYY-MM-DD) — no "last week", "yesterday", etc. (Rule AB1)

**Workflow for each deal:**

```bash
# 1. Write content to temp file
cat > /tmp/jane_brief_<deal_id>.md << 'EOF'
<generated content>
EOF

# 2. Invoke the helper (EP1 — setContent on registered ID, never recreate)
python3 ~/cos-pipeline/tools/deal_extract_helpers.py write-jane-brief \
  --deal-id <deal_id> --content-file /tmp/jane_brief_<deal_id>.md
```

If `--dry-run` is set, pass `--dry-run` before the sub-command:
```bash
python3 ~/cos-pipeline/tools/deal_extract_helpers.py --dry-run write-jane-brief \
  --deal-id <deal_id> --content-file /tmp/jane_brief_<deal_id>.md
```

If the helper prints a `WARNING:` line about missing `jane_brief_file_id`,
log it in your summary and continue with other deals — do not abort.

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
