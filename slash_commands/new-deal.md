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
- Lead principal (default: from firm_context.yaml `principal.name`) — lead = who drives this deal day-to-day, not necessarily the firm principal
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

**If you see** `⚠️ Could not download firm context from Drive` in the output: the script failed to write the TCIP firm context File ID into the project instructions. After Phase 5, patch it manually:

```python
content = open(instructions_path).read()
content = content.replace(
    'Step 0. Read firm context from Google Drive:\n    File ID: \n',
    'Step 0. Read firm context from Google Drive:\n    File ID: 1oqvRhNq-MRS9sBT-wtZxqoZiwOC5GfCQHM1K0DuC6Pk\n'
)
open(instructions_path, 'w').write(content)
```

Then re-run the ASCII-strip and re-paste the instructions in Phase 5.

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

## PHASE 4 — Confirm Google Drive connector + add firm context

Within the new Project, click the `+` button next to "Files" and confirm "Drive" appears as an option. If it does, the connector is active.

**Add TCIP context docs to Project Files** (every TCIP deal project gets all three):

1. Click `+` → Drive
2. Paste each URL in turn:
   - TCIP Firm Context: `https://docs.google.com/document/d/1oqvRhNq-MRS9sBT-wtZxqoZiwOC5GfCQHM1K0DuC6Pk/edit`
   - Yoni Personal Context: `https://docs.google.com/document/d/1DMlnylTPI4OArDYaXVDqsS22AhbQvcwbTxJnoHp0wyA/edit`
   - TCIP Deal Presentation Standards: `https://docs.google.com/document/d/1kb_Uwt6G_F-VuzLsLTyTZcO8ZNPJfFuLWre-W3FIlek/edit`
3. Confirm all three appear in the Files section.

All three live in the `_Claude Context` Drive folder
(`1fReGbo5FikVDYUZ06BKzHKhV-A9QRItR`) and are mirrored to git in
`~/cos-pipeline-config-tomac/` by the dashboard Stop hook.

Do NOT add the status, brief, or dashboard_entry files to the Project's Files section. They are read live from Drive via File IDs in the project instructions. Pinning them creates a stale cached copy that diverges from the live version.

If the Drive option is missing entirely, take a screenshot and report — Yoni may need to connect Drive from account settings first.

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

## PHASE 6 — Create _Claude Context/ subfolder + upload dashboard entry

Every deal folder must contain a `_Claude Context/` subfolder with the
three AI-readable artifacts: `{deal_id}_status.md`, `{deal_id}_master_brief.md`,
`{deal_id}_dashboard_entry.json`. Source files (transcripts, decks, etc.)
live at the deal-folder root; `_Claude Context/` holds only the curated
deal state.

```python
import os, pickle, json
from googleapiclient.discovery import build

with open(os.path.expanduser('~/credentials/gdrive_token.pickle'), 'rb') as f:
    creds = pickle.load(f)
svc = build('drive', 'v3', credentials=creds)

# Create _Claude Context/ inside the deal folder
ctx = svc.files().create(
    body={'name': '_Claude Context',
          'mimeType': 'application/vnd.google-apps.folder',
          'parents': [drive_folder_id]},
    fields='id'
).execute()
claude_context_folder_id = ctx['id']
print(f'claude_context_folder_id: {claude_context_folder_id}')

# Move existing status + brief docs (created by tcip_new_deal.py at the
# deal-folder root) into _Claude Context/
for fid in (status_id, brief_id):
    f = svc.files().get(fileId=fid, fields='parents').execute()
    prev = ','.join(f.get('parents', []))
    svc.files().update(fileId=fid, addParents=claude_context_folder_id,
                      removeParents=prev, fields='id').execute()
```

Then create `{deal_id}_dashboard_entry.json` and upload it directly into
`_Claude Context/`. Schema (from `~/cos-pipeline/data-tomac/compiled/deal-system-data.json`):

```json
{
  "_schema_version": 1,
  "_last_synced_from_drive": "YYYY-MM-DD",
  "id": "{deal_id}",
  "name": "{deal_name}",
  "ticker": "{TICKER}",
  "sector": "",
  "geography": "",
  "tagline": "",
  "owner": "{lead}",
  "last_updated": "YYYY-MM-DD",
  "stage": "Sourcing",
  "stage_index": 0,
  "health": 50,
  "last_activity": "YYYY-MM-DD",
  "phase_capital": [],
  "tcip_econ": {"status_note": "Not yet formalized"},
  "thesis": [],
  "tcip_edge": "",
  "key_risk": "",
  "counterparties": [],
  "contacts": [],
  "next_milestone": "",
  "next_milestone_due": "",
  "workstreams": [],
  "actions": [],
  "activity_log": []
}
```

Upload to the deal's `_Claude Context/` subfolder (NOT the deal-folder root):

```python
fname = f'{deal_id}_dashboard_entry.json'
# write entry to /tmp/{fname} first, then:
media = MediaFileUpload(f'/tmp/{fname}', mimetype='application/json')
result = svc.files().create(
    body={'name': fname, 'parents': [claude_context_folder_id]},
    media_body=media, fields='id'
).execute()
dashboard_entry_file_id = result['id']
print(f'dashboard_entry_file_id: {dashboard_entry_file_id}')
```

Then add the new deal entry to `~/dashboards/config/drive-docs.yaml`
under `deal_docs:` with all four IDs:

```yaml
deal_docs:
  {deal_id}:
    status:       { doc_id: {status_id}, name: "{deal_id}_status.md" }
    master_brief: { doc_id: {brief_id}, name: "{deal_id}_master_brief.md" }
    drive_folder_id: {drive_folder_id}
    claude_context_folder_id: {claude_context_folder_id}
```

Record `dashboard_entry_file_id` and `claude_context_folder_id` — both
needed for project instructions in Phase 5.

**After Phase 5 (instructions paste):** Add a Step block to the instructions for reading/updating the dashboard entry. Use the Python injection approach from the PNGTS onboarding — find "Step 1." anchor and insert after it. The block text is:

```
    Step 2. Fetch your dashboard entry from Google Drive:
    File ID: {dashboard_entry_file_id}
    This is your structured deal card used by the TCIP dashboard.
    Read it to understand current deal state (stage, health, thesis scores,
    next milestone, key risk, tcip_edge).

    When deal state changes during a session, output an update block:
    ---DEAL-UPDATE---
    { paste full updated JSON here }
    ---END---
    Yoni will run: python3 ~/cos-pipeline/tools/sync_deals_from_drive.py --deal-id {deal_id}
    to push it to the dashboard. Always output the full entry, never partial.
```

Re-sanitize and re-paste instructions after injection.

---

## PHASE 6b — Wire project URL to registries

Update both registry files with the project URL:

**`~/cos-pipeline/tools/deal-system-data.json`** — this is a JSON array. Find the object with matching `deal_id` and set `project_url`.

**`~/cos-pipeline/tools/sync-state.json`** — this is a JSON object keyed by `deal_id` (e.g. `{"pngts": {...}, "cholla": {...}}`). Access via `data[deal_id]["project_url"]`, not by iterating a list.

Use this Python snippet to handle both structures correctly:

```python
import json

PROJECT_URL = "{project_url}"
DEAL_ID = "{deal_id}"

# deal-system-data.json — list of objects
with open('~/cos-pipeline/tools/deal-system-data.json') as f:
    data = json.load(f)
for item in data:
    if item.get('deal_id') == DEAL_ID:
        item['project_url'] = PROJECT_URL
        break
with open('~/cos-pipeline/tools/deal-system-data.json', 'w') as f:
    json.dump(data, f, indent=2)

# sync-state.json — dict keyed by deal_id
with open('~/cos-pipeline/tools/sync-state.json') as f:
    ss = json.load(f)
ss[DEAL_ID]['project_url'] = PROJECT_URL
with open('~/cos-pipeline/tools/sync-state.json', 'w') as f:
    json.dump(ss, f, indent=2)
```

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
