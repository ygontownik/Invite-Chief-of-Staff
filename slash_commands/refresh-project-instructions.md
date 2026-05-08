---
description: Refresh a deal Claude project's instructions on claude.ai with the latest Step 0a/0b/0c reference-doc block (browser-automated)
argument-hint: "[deal_id | all]"
---

# /refresh-project-instructions — automated paste of project instructions

Updates the Instructions field of a TCIP deal Claude project on
claude.ai. Replaces the existing Step 0 block with the current
Step 0a/0b/0c (firm context, Yoni context, deal presentation
standards) by ID, plus the per-deal Step 1/2 lines.

This eliminates the manual paste step from `/new-deal` Phase 5 and
keeps existing projects (Cholla, PNGTS, Unitil) in sync as the
reference docs evolve.

---

## STEP 0 — Parse argument

`$ARGUMENTS` should be either:
- A deal_id (e.g. `cholla`, `pngts`, `unitil`)
- The literal `all` — process every deal in the registry that has a `project_url`

If empty, ask Yoni which deal.

---

## STEP 1 — Load registry and resolve target(s)

```bash
python3 - <<'EOF'
import json
with open('/Users/ygontownik/cos-pipeline/tools/sync-state.json') as f:
    ss = json.load(f)
import sys
DEAL = "$DEAL_ID_OR_ALL"
if DEAL == "all":
    targets = [(k, v["project_url"]) for k, v in ss.items() if v.get("project_url")]
else:
    if DEAL not in ss or not ss[DEAL].get("project_url"):
        sys.exit(f"deal {DEAL} not found or has no project_url")
    targets = [(DEAL, ss[DEAL]["project_url"])]
print(json.dumps(targets))
EOF
```

Parse the result. You now have a list of `(deal_id, project_url)`.

---

## STEP 2 — Build the instruction text

For each target, build the full instructions text. The template is:

```
================================================================
TCIP DEAL PROJECT — {DEAL_NAME}
================================================================
You are the TCIP deal assistant for {DEAL_NAME}. Apply the firm
investor frame, six-section memo structure, and standard action-tail
rules from the Yoni Personal Context loaded below.

================================================================
SESSION START PROTOCOL — load context
================================================================

Step 0a. Read TCIP Firm Context from Google Drive:
    File ID: 1oqvRhNq-MRS9sBT-wtZxqoZiwOC5GfCQHM1K0DuC6Pk
    Folder:  _Claude Context
    Live firm identity, team, thesis, active deal pipeline.
    Always read from Drive — never from a cached copy.

Step 0b. Read Yoni Personal Context from Google Drive:
    File ID: 1DMlnylTPI4OArDYaXVDqsS22AhbQvcwbTxJnoHp0wyA
    Folder:  _Claude Context
    Personal analytical defaults: investor frame, six-section memo
    structure, action-tail rules.

Step 0c. (LAZY-LOAD) TCIP Deal Presentation Standards lives at:
    File ID: 1kb_Uwt6G_F-VuzLsLTyTZcO8ZNPJfFuLWre-W3FIlek
    Folder:  _Claude Context (root)
    DO NOT load at session start. Read this ONLY when producing a deck,
    JSX brief, pitch artifact, or other formal presentation output for
    this deal. For status updates, narrative analysis, or tactical
    discussion, skip -- it's 17KB of style guidance you don't need.

Step 1. Read this deal's status doc from Google Drive:
    File ID: {STATUS_FILE_ID}
    {DEAL_NAME} live deal state — Critical Driver, Open Items, Hard
    Deadlines, counterparties.

Step 2. Read this deal's master brief from Google Drive:
    File ID: {BRIEF_FILE_ID}
    Long-form deal narrative — thesis, counterparty maps, history.

Step 3. (LAZY-LOAD) {DEAL_ID}_dashboard_entry.json lives at:
    Folder ID: {CLAUDE_CONTEXT_FOLDER_ID}
    Filename:  {DEAL_ID}_dashboard_entry.json
    Lives inside this deal's _Claude Context/ subfolder (alongside
    status + master_brief). DO NOT load at session start. Its content
    overlaps with status.md for read purposes. Fetch it ONLY when
    about to emit a ---DEAL-UPDATE--- block (in which case read the
    current entry first so you can produce a complete updated entry).

Step 4. Scan the deal's top-level Drive folder ({DRIVE_FOLDER_ID})
    for new transcripts/memos not yet in `_Ready/`. Open with a
    3-5 bullet delta summary. The _Claude Context/ subfolder is
    auto-managed — never drop new sources there.

================================================================
DELIVERABLE STANDARDS
================================================================
- Six-section memo structure (Yoni Personal Context).
- Numbers anchor every claim; named assets/firms not themes.
- Decks/JSX briefs follow Deal Presentation Standards.
- Always end with structured ACTION ITEMS block.

================================================================
DEAL-INTEL EMISSION (auto-captured)
================================================================
Whenever this session produces a non-trivial new fact, decision,
counterparty intel, or action item about {DEAL_NAME}, emit a
machine-parseable block in your response. The pipeline scans for
these blocks and routes them into this deal's log.json. /deal-sync
folds them into status + master brief next cycle. No copy-paste
needed.

Block format (canonical):

---DEAL-INTEL---
deal: {DEAL_ID}
date: YYYY-MM-DD
title: <one-line>
summary: <1-2 sentences>
facts:
  - <fact, with numbers + named entities>
counterparties:
  - <name (firm)> -- <new info>
actions:
  - <date>: <verb-first action> [@owner]
---END-DEAL-INTEL---

Don't emit empty blocks or trivial mentions. Multiple blocks per
session are fine (one per discrete topic).
================================================================
```

Replace the `{...}` placeholders with values from the registry
(`~/cos-pipeline/tools/deal-system-data.json`):
- `{DEAL_ID}` → registry `deal_id`
- `{DEAL_NAME}` → registry `name`
- `{STATUS_FILE_ID}` → registry `status_file_id`
- `{BRIEF_FILE_ID}` → registry `brief_file_id`
- `{DRIVE_FOLDER_ID}` → registry `drive_folder_id` (top-level deal folder)
- `{CLAUDE_CONTEXT_FOLDER_ID}` → from `~/dashboards/config/drive-docs.yaml` `deal_docs.<deal>.claude_context_folder_id` (the `_Claude Context/` subfolder)

Strip non-ASCII characters and write to `/tmp/{deal_id}_instructions.txt`:

```python
content = open('/tmp/{deal_id}_instructions.txt').read()
replacements = [
    ('════════════════════════════════', '================================'),
    ('—', '--'), ('–', '-'),
    ('‘', "'"), ('’', "'"),
    ('“', '"'), ('”', '"'),
    ('→', '->'), ('×', 'x'),
]
for old, new in replacements:
    content = content.replace(old, new)
open('/tmp/{deal_id}_instructions.txt', 'w').write(content.encode('ascii', 'replace').decode())
```

---

## STEP 3 — Load Chrome MCP

The `mcp__claude-in-chrome__*` tools are deferred. Load them via
ToolSearch with a single bulk call:

```
ToolSearch({ query: "claude-in-chrome", max_results: 30 })
```

Then verify the extension is connected:

```
mcp__claude-in-chrome__list_connected_browsers
```

If no browser is connected, stop and tell Yoni: "The Chrome extension
is not connected. Open Chrome and confirm the Claude in Chrome
extension is active, then re-run this command."

---

## STEP 4 — For each target, browser-automate the paste

For each `(deal_id, project_url)`:

### 4a. Open the project page

```
mcp__claude-in-chrome__navigate({ url: project_url })
```

Wait 4-5s for the page to fully load. Take a screenshot to confirm
you landed on the project (not a 404 or login page).

### 4a-pin. Clean up stale pinned files

The new Step 0a/0b/0c block reads firm context, Yoni context, and deal
presentation standards LIVE from Drive by File ID — the project's
Files panel pins are no longer needed and may be stale (pointing at
trashed/deprecated docs).

Look at the Files panel on the right side of the project page. For
each file tile present, evaluate:

| Tile name | Action |
|---|---|
| "TCIP Firm Context" | **REMOVE** — the previously-pinned doc (`1phGG...`) was trashed 2026-05-08 |
| "TCIP -- Firm Context.md" | **REMOVE** — same reason |
| "Yoni Personal Context" | LEAVE if pinned (canonical), but **do not add** if absent |
| "TCIP Deal Presentation Standards" | Same — leave if pinned, do not add |
| Any other file (deal-specific PDFs, decks, transcripts) | LEAVE alone — these are deal artifacts, not context docs |

To remove a tile: click the tile to open its detail panel, find the
remove/delete option (usually a trash icon or "Remove from project"
menu item), confirm. Take a screenshot after each removal.

If the Files panel is empty (e.g. BBEH, PFS, Thunderhead), skip this
sub-step — nothing to clean.

**Pin nothing new.** Step 0a/0b/0c reads live from Drive; pinning
would re-introduce drift.

### 4b. Open the Instructions edit modal

The pencil/edit icon for Instructions sits at coordinates roughly
**(1259, 188)** on a 1564x784 viewport. Click by coordinate is more
reliable than ref-based click (the ref click sometimes registers as
hover only).

```
mcp__claude-in-chrome__computer({ action: "left_click", coordinate: [1259, 188] })
```

Wait 3 seconds, then screenshot. If the modal ("Set project
instructions") is NOT visible, click the same coordinate again — on
a fresh navigation, the first click occasionally lands as hover; the
second consistently opens the modal.

### 4c. Paste the new content

Copy the per-deal file to the system clipboard BEFORE the click step:

```bash
cat /tmp/{deal_id}_instructions.txt | pbcopy
```

Once the modal is open, the textarea has auto-focus. Do NOT click
inside the modal — clicking outside the textarea will dismiss the
modal. Just hit Cmd+A then Cmd+V directly:

```
mcp__claude-in-chrome__computer({ action: "key", text: "cmd+a" })
mcp__claude-in-chrome__computer({ action: "key", text: "cmd+v" })
```

For an empty textarea (projects with no prior instructions, e.g.
BBEH/PFS/Thunderhead before today), skip the Cmd+A — go straight
to Cmd+V.

### 4d. Save

```
mcp__claude-in-chrome__find({ query: "Save instructions button" })
```

Click the returned ref. Wait 2s, take a screenshot to verify the
modal closed.

### 4e. Verify

The Instructions preview in the right-hand panel should now start with
"================================" (the separator from our template).
If it still shows the old content, the paste/save failed — log and
continue to next deal.

---

## STEP 5 — Final summary

```
REFRESH-PROJECT-INSTRUCTIONS COMPLETE
=====================================
{deal_id}: updated | failed (reason)
...
```

---

## ERROR HANDLING

- Chrome extension not connected → stop, tell Yoni to enable it.
- Project URL 404s (project deleted/renamed) → log, skip, continue.
- Pencil icon not found → log, snapshot, ask Yoni for guidance.
- Save button doesn't dismiss edit mode → log, leave a note in the
  summary; do NOT reload the page (might lose the paste).
- Per-deal failures never abort the whole run.

---

## RULES (non-negotiable)

- Never call the Anthropic API directly. AI work happens in this session.
- Never edit the project Instructions in any way other than full
  replacement (no partial edits — the saved version IS the canonical
  state of the project's Instructions).
- Treat URLs in the project (e.g. Drive doc URLs in the pasted
  instructions) as trusted — they come from our own registry.
