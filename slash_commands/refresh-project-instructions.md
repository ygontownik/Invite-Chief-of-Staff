---
description: Refresh a deal Claude project's instructions on claude.ai with the latest Step 0a/0b/0c reference-doc block (browser-automated)
argument-hint: "[deal_id | all]"
---

# /refresh-project-instructions — automated paste of project instructions

Updates the Instructions field of a TCIP deal Claude project on
claude.ai. Replaces the existing Step 0 block with the current
Step 0a/0b/0b-2/0c (firm context, Yoni context, practice patterns,
deal presentation standards) by ID, plus the per-deal Step 1/2 lines.

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
import json, sys
data = json.load(open('/Users/ygontownik/cos-pipeline/tools/deal-system-data.json'))
DEAL = "$DEAL_ID_OR_ALL"
if DEAL == "all":
    targets = [(d["deal_id"], d["project_url"]) for d in data["deals"] if d.get("project_url")]
else:
    match = next((d for d in data["deals"] if d["deal_id"] == DEAL), None)
    if not match or not match.get("project_url"):
        sys.exit(f"deal {DEAL} not found or has no project_url")
    targets = [(DEAL, match["project_url"])]
print(json.dumps(targets))
EOF
```

Parse the result. You now have a list of `(deal_id, project_url)`.

---

## STEP 2 — Fetch instruction text from Drive

Each deal’s project instructions are stored as a Google Doc in the
deal’s `_Claude Context/` subfolder. The file ID is registered in
`~/cos-pipeline-config-tomac/drive-docs.yaml` under
`deal_docs.<deal_id>.project_instructions.doc_id`.

Run this script to fetch each deal’s instructions from Drive and
write them to `/tmp/{deal_id}_instructions.txt`:

```bash
python3 ~/cos-pipeline/tools/fetch_project_instructions.py all
```

(Pass `--deal cholla` etc. to limit to one deal.)

That script reads each doc via the Google Docs API, strips non-ASCII
characters for clipboard safety, and writes the result to
`/tmp/{deal_id}_instructions.txt`.

**If the fetch fails** for a deal (Drive error, missing doc ID),
fall back to regenerating from the hardcoded template below using
registry values from `~/cos-pipeline/tools/deal-system-data.json`:

```
================================================================
TCIP DEAL PROJECT -- {DEAL_NAME}
================================================================
You are the TCIP deal assistant for {DEAL_NAME}. Apply the firm
investor frame, six-section memo structure, and standard action-tail
rules from the Yoni Personal Context loaded below.

================================================================
SESSION START PROTOCOL -- load context
================================================================

Step 0a. Read TCIP Firm Context from Google Drive:
    File ID: 1oqvRhNq-MRS9sBT-wtZxqoZiwOC5GfCQHM1K0DuC6Pk
    Folder:  _Claude Context

Step 0b. Read Yoni Personal Context from Google Drive:
    File ID: 1DMlnylTPI4OArDYaXVDqsS22AhbQvcwbTxJnoHp0wyA
    Folder:  _Claude Context

Step 0b-2. Read TCIP Practice Patterns from Google Drive:
    File ID: 1C3z_6hnKtYZcpQM4Ffh2qN4EiVEwThNDC9NwHlt-zqY
    Folder:  _Claude Context

Step 0c. (LAZY-LOAD) Deal Presentation Standards:
    File ID: 1kb_Uwt6G_F-VuzLsLTyTZcO8ZNPJfFuLWre-W3FIlek
    DO NOT load at session start. Only for deck/pitch outputs.

Step 0d. (LAZY-LOAD) System Reference + User Manual + Skills Catalog:
    System Reference: 1TjbfPFep5xUVghBXQZbudM3gY8riXrCOm_ULQfrJYK8
    User Manual:      1d4oldIYbzdcsX8F_dXYZPUbnaLo9-sUsm071ys9NolM
    Skills Catalog:   1aMmiS_wZD3T6INQfxutJ78v6U65MYtTXkykJZ1NcRI4
    Read these when making architectural changes, when picking a skill to invoke,
    or for new-user / partner-tier onboarding context.

NEW SKILLS available in Claude Code (not in claude.ai project sessions):
    /check-system        — "is everything OK?" snapshot
    /sync-system         — flushes all canonical-source edits to derived views
    /propose-learning    — capture a new behavioral rule into LEARNINGS-LEDGER
    /pressure-test       — pressure-test a high-stakes action against accumulated context
    Full catalog: ~/dashboards/docs/MY-SKILLS.md

Step 1. Read status doc: {STATUS_FILE_ID}
Step 2. Read master brief: {BRIEF_FILE_ID}
Step 3. (LAZY-LOAD) dashboard_entry.json in folder {CLAUDE_CONTEXT_FOLDER_ID}
Step 4. Scan deal folder {DRIVE_FOLDER_ID} for new files.
```

**To edit a deal’s instructions:** open the Google Doc directly in
Drive (find via `deal_docs.<deal>.project_instructions.doc_id` in
drive-docs.yaml), edit, then re-run this skill. Changes live in
Drive — no code edits needed.

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
