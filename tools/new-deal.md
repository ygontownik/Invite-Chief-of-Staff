---
description: Onboard a new deal end-to-end — Drive setup, status/brief generation, Claude Project creation, instructions paste, registry wiring
argument-hint: "[deal name] [deal id]"
---

# /new-deal — End-to-end deal onboarding

You are running the full TCIP deal onboarding flow. Complete every phase in order. Do not stop to summarize between phases — just work.

## INPUTS

Read from arguments if provided: `$ARGUMENTS` may be "Deal Name deal_id" or empty.
If empty, you will collect inputs interactively in Phase 1.

---

## PHASE 1 — Collect deal info

If $ARGUMENTS contains a deal name and ID, parse them. Otherwise ask Yoni for:
- Deal name (e.g. "Black Bayou Energy Hub")
- Deal ID (lowercase underscores, e.g. "black_bayou")
- Lead principal (default: from firm_context.yaml `principal.name`)
- Support principal (default: first team member from firm_context.yaml `team`)
- Path to local deal documents folder (optional — press Enter to skip)
- Existing Google Drive folder ID (optional — press Enter to create new)
- Deal-specific triggers to add (options: cash_runway, regulatory_vote, disintermediation, process_deadline, relationship_tension, market_news)
- Deal-specific rules to add (options: regulated_asset, development_asset, relationship_sensitive)

Confirm the collected inputs with Yoni before proceeding.

---

## PHASE 2 — Run the onboarding script

Run the script non-interactively using collected inputs:

```bash
cd ~/cos-pipeline/tools && python3 tcip_new_deal.py \
  --deal-name "{DEAL_NAME}" \
  --deal-id "{DEAL_ID}" \
  --lead "{LEAD}" \
  --support "{SUPPORT}" \
  {--docs-folder "{DOCS_FOLDER}" if provided} \
  {--drive-folder-id "{DRIVE_FOLDER_ID}" if provided} \
  {--triggers {triggers} if any} \
  {--rules {rules} if any}
```

Stream output to the terminal. Watch for:
- `✓` lines (confirm each phase completed)
- `⚠️` lines (report any warnings immediately)
- The `---BROWSER-STEP---` block at the end (capture it)

Parse from the BROWSER-STEP block:
- `deal_id`
- `deal_name`
- `instructions_path`
- `status_id`
- `brief_id`

---

## PHASE 3 — Create the Claude Project

Open the browser and navigate to https://claude.ai/projects.

1. Take a screenshot to confirm you are on the Projects page.
2. Find and click "New project" or the "+" button.
3. Wait for the project creation dialog or new project page.
4. Set the project name to exactly: `TCIP -- {deal_name}` (use the deal name from Phase 2).
5. Confirm/save the project name.
6. Take a screenshot to capture the project URL — it will look like `https://claude.ai/project/019xxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.
7. Record the project URL.

---

## PHASE 4 — Enable Google Drive connector

Within the new Project:

1. Find the Project settings (gear icon or "..." menu).
2. Navigate to integrations or knowledge sources.
3. Find Google Drive and enable it / toggle it on.
4. Confirm it shows as connected.

If you cannot find the Drive connector toggle, take a screenshot and report exactly what you see — do not guess.

---

## PHASE 5 — Paste Project instructions

1. Read the instructions file at `instructions_path` from the BROWSER-STEP block.
2. Strip all non-ASCII characters using this Python snippet:

```python
content = open(instructions_path).read()
replacements = [
    ('════════════════════════════════', '================================'),
    ('—', '--'), ('–', '-'),
    ('‘', "'"), ('’', "'"),
    ('“', '"'), ('”', '"'),
    ('→', '->'), ('×', 'x'),
]
for old, new in replacements:
    content = content.replace(old, new)
import subprocess
subprocess.run(['pbcopy'], input=content.encode('ascii', 'replace'), check=True)
```

3. In the Claude Project page, click the pencil icon next to "Instructions".
4. Select all existing text (Cmd+A) and paste (Cmd+V).
5. Click "Save instructions".
6. Verify the saved instructions preview starts with `===` separators (not garbled characters).

---

## PHASE 6 — Wire project URL to registries

Update both registry files with the project URL:

**`~/cos-pipeline/tools/deal-system-data.json`** — find the entry with matching `deal_id` and set `project_url` to the URL captured in Phase 3.

**`~/cos-pipeline/tools/sync-state.json`** — find the entry with matching `deal_id` and set `project_url` to the same URL.

Confirm both files written.

---

## PHASE 7 — Confirm and report

Output a clean summary:

```
DEAL ONBOARDING COMPLETE
========================
Deal:         {deal_name} ({deal_id})
Lead:         {lead}
Drive folder: https://drive.google.com/drive/folders/{drive_folder_id}
Status file:  {status_id}
Master brief: {brief_id}
Project URL:  {project_url}

WHAT HAPPENS NEXT
-----------------
Open the project URL and start a new chat.
Level 1 will automatically:
  - Read firm context live from Drive (Step 0)
  - Read status.md (Step 1)
  - Scan for new files (Step 2)
  - Open with a delta summary

First session will also run the FIRST SESSION SETUP block:
  - Run Critical Driver Framework
  - Fill in status.md and master_brief.md
  - Write both back to Drive
```

---

## ERROR HANDLING

- If the script fails in Phase 2: show the full error, do not proceed.
- If the Claude Project page layout is unexpected: take a screenshot, describe what you see, ask Yoni how to proceed.
- If the Drive connector toggle is missing: report it — Yoni may need to connect it manually from account settings first.
- If instructions paste produces garbled characters: re-run the encoding strip and re-paste.
- Never skip a phase and assume it worked — confirm each one before moving to the next.
