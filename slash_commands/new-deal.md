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
- **Deal type** — one of: `investment` (standard TCIP deal), `co-founding` (Yoni evaluating joining as co-founder/partner), `partnership-eval` (evaluating a firm/platform relationship), `other`
- Lead principal (default: from firm_context.yaml `principal.name`) — lead = who drives this deal day-to-day
- Support principal — **no default; leave blank unless explicitly named**. Do NOT default to Mark Saxe. Ask: "Is there a support principal?" and leave empty if none.
- Path to local deal documents folder (optional — press Enter to skip)
- Existing Google Drive folder ID (optional — press Enter to create new)
- Deal-specific triggers to add (options: cash_runway, regulatory_vote, disintermediation, process_deadline, relationship_tension, market_news)
- Deal-specific rules to add (options: regulated_asset, development_asset, relationship_sensitive)

**Before confirming:** check whether `deal_id` already exists in `~/cos-pipeline/tools/deal-system-data.json`:

```python
import json
with open('/Users/ygontownik/cos-pipeline/tools/deal-system-data.json') as f:
    data = json.load(f)
deals = data['deals'] if isinstance(data, dict) and 'deals' in data else data
existing = [d for d in deals if d.get('deal_id') == '{DEAL_ID}']
print(existing)
```

If a stub entry exists (fields empty or partial), warn Yoni and confirm before running the script. The script will skip updating an existing entry — you will need to manually merge the real IDs afterward (see Phase 2 note).

**Deal-type gate:** If `deal_type` is `other` (or `co-founding` / `partnership-eval` where pipeline wiring is unclear), show this warning before confirming:

> ⚠️ Deal type is `{deal_type}`. `/new-deal` wires this entity into the TCIP deal registry (deal-system-data.json, sync-state.json, drive-docs.yaml, /deal-sync). For job interviews, personal tracking, or anything that isn't a true TCIP investment deal, a plain Claude project with custom instructions is cleaner and won't pollute the deal pipeline.
>
> Proceed with **full /new-deal wiring** (registry + project), or **plain project only** (just create the Claude project — skip all registry steps)?

If Yoni chooses **plain project only**: skip Phases 2, 6, and 6b entirely. Run only Phases 3 (create project) and 5 (paste instructions). Use a simplified instruction template focused on the actual use case rather than the TCIP Level 1/2/3/4 framework.

Confirm all inputs with Yoni before proceeding.

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

**If you see** `⚠️ Could not download firm context from Drive` in the output: the script failed to write the TCIP firm context File ID into the project instructions. After Phase 5, patch it using a line-by-line approach (literal string replace is fragile due to whitespace differences):

```python
lines = open(instructions_path).readlines()
for i, line in enumerate(lines):
    if 'Step 0. Read firm context from Google Drive:' in line:
        # Next non-empty line should be "File ID: " — patch it
        for j in range(i+1, min(i+4, len(lines))):
            if 'File ID:' in lines[j] and lines[j].strip() == 'File ID:':
                lines[j] = lines[j].rstrip() + ' 1oqvRhNq-MRS9sBT-wtZxqoZiwOC5GfCQHM1K0DuC6Pk\n'
                print(f"Patched line {j}: {lines[j].rstrip()}")
                break
        break
open(instructions_path, 'w').writelines(lines)
```

Also check if `deal-system-data.json` had a pre-existing stub: if so, the script skipped updating it. Manually set the real IDs:

```python
import json
path = '/Users/ygontownik/cos-pipeline/tools/deal-system-data.json'
with open(path) as f:
    data = json.load(f)
deals = data['deals'] if isinstance(data, dict) and 'deals' in data else data
for d in deals:
    if d.get('deal_id') == '{DEAL_ID}':
        d['drive_folder_id'] = '{drive_folder_id}'
        d['status_file_id'] = '{status_id}'
        d['brief_file_id'] = '{brief_id}'
        d['lead'] = '{lead}'
        d['support'] = '{support}'  # empty string if none
with open(path, 'w') as f:
    json.dump(data if isinstance(data, dict) else deals, f, indent=2)
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

## PHASE 4 — Check Google Drive connector (likely skip)

**This account does not have the Google Drive connector enabled in Claude Projects.** The `+` button next to Files shows only: Upload from device, Add text content, GitHub.

**Expected behavior:** Skip this phase entirely. TCIP projects do not use pinned Drive files — all Drive reads happen via File IDs embedded in the project instructions (Steps 0, 1, 1b). No action needed.

**If Drive unexpectedly appears as an option** (connector was enabled): add these three docs:
- TCIP Firm Context: `https://docs.google.com/document/d/1oqvRhNq-MRS9sBT-wtZxqoZiwOC5GfCQHM1K0DuC6Pk/edit`
- Yoni Personal Context: `https://docs.google.com/document/d/1DMlnylTPI4OArDYaXVDqsS22AhbQvcwbTxJnoHp0wyA/edit`
- TCIP Deal Presentation Standards: `https://docs.google.com/document/d/1kb_Uwt6G_F-VuzLsLTyTZcO8ZNPJfFuLWre-W3FIlek/edit`

Do NOT add status, brief, or dashboard_entry files — they are read live from Drive and pinning creates stale cached copies.

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

3. In the Claude Project page, click the `+` next to "Instructions" to open the editor.
4. **Do NOT use Cmd+A / Cmd+V** — clipboard paste into the textarea does not trigger React state updates and the field will appear empty. Use JavaScript injection instead:

```javascript
// Run via javascript_tool in the browser tab
const instructions = "<SANITIZED_CONTENT_AS_JS_STRING>";
const ta = document.querySelector('textarea');
const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
setter.call(ta, instructions);
ta.dispatchEvent(new Event('input', { bubbles: true }));
`Set: ${ta.value.length} chars`;
```

To generate the JS string, run in bash:
```python
import json, subprocess
content = open(instructions_path).read()
# ... (apply replacements) ...
print(json.dumps(content.encode('ascii','replace').decode('ascii')))
```
Copy the output (including surrounding quotes) as the value for `instructions` in the JS above.

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
    status:       { doc_id: {status_id}, name: "{deal_id}_status" }
    master_brief: { doc_id: {brief_id}, name: "{deal_id}_master_brief" }
    drive_folder_id: {drive_folder_id}
    claude_context_folder_id: {claude_context_folder_id}
```

Note: status and master_brief are created as **native Google Docs** (mimeType=application/vnd.google-apps.document) by `tcip_new_deal.py`. Names have no `.md` extension. `write-deal-doc` uses the Docs API (`docs_overwrite_native`) for all subsequent writes.

**Patch the local instructions file** to replace the placeholder written by `tcip_new_deal.py`:

```python
content = open(instructions_path).read()
if 'DASHBOARD_ENTRY_FILE_ID_PLACEHOLDER' in content:
    content = content.replace('DASHBOARD_ENTRY_FILE_ID_PLACEHOLDER', dashboard_entry_file_id)
    open(instructions_path, 'w').write(content)
    print(f"Patched DASHBOARD_ENTRY_FILE_ID_PLACEHOLDER → {dashboard_entry_file_id}")
else:
    print("Placeholder not found — instructions may already be patched")
```

Record `dashboard_entry_file_id` and `claude_context_folder_id` — both
needed for project instructions in Phase 5.

**After Phase 5 (instructions paste):** Inject the dashboard entry step. Anchor on the **status File ID line** (stable) rather than Step 2's description text (which varies per deal):

```python
import subprocess

dashboard_block = (
    "\n    Step 1b. Fetch your dashboard entry from Google Drive:\n"
    f"    File ID: {dashboard_entry_file_id}\n"
    "    This is your structured deal card used by the TCIP dashboard.\n"
    "    Read it to understand current deal state (stage, health,\n"
    "    next milestone, key risk, tcip_edge).\n\n"
    "    When deal state changes during a session, output an update block:\n"
    "    ---DEAL-UPDATE---\n"
    "    { paste full updated JSON here }\n"
    "    ---END---\n"
    f"    Yoni will run: python3 ~/cos-pipeline/tools/sync_deals_from_drive.py --deal-id {deal_id}\n"
    "    to push it to the dashboard. Always output the full entry, never partial.\n"
)

content = open(instructions_path).read()
# Anchor: the line immediately after Step 1's status File ID — always stable
anchor = f"    File ID: {status_id}\n"
if anchor in content:
    content = content.replace(anchor, anchor + dashboard_block, 1)  # replace first occurrence only
    open(instructions_path, 'w').write(content)
    print("Dashboard step injected after status File ID line")
else:
    print(f"Anchor not found — search for '{status_id}' in {instructions_path}")
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

# deal-system-data.json — may be {"deals": [...]} dict OR flat list
path1 = '/Users/ygontownik/cos-pipeline/tools/deal-system-data.json'
with open(path1) as f:
    data = json.load(f)
deals = data['deals'] if isinstance(data, dict) and 'deals' in data else data
for item in deals:
    if item.get('deal_id') == DEAL_ID:
        item['project_url'] = PROJECT_URL
        break
with open(path1, 'w') as f:
    json.dump(data, f, indent=2)  # write original structure (dict or list)

# sync-state.json — dict keyed by deal_id
path2 = '/Users/ygontownik/cos-pipeline/tools/sync-state.json'
with open(path2) as f:
    ss = json.load(f)
if DEAL_ID in ss:
    ss[DEAL_ID]['project_url'] = PROJECT_URL
else:
    ss[DEAL_ID] = {'project_url': PROJECT_URL}
with open(path2, 'w') as f:
    json.dump(ss, f, indent=2)
print("Both registries updated.")
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
