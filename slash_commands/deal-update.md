---
description: Push a deal state update from a TCIP project session to Drive + dashboard
argument-hint: "[deal_id]"
---

# /deal-update — Push deal state to Drive and dashboard

After a TCIP deal project session that produced a `---DEAL-UPDATE---` block:

## STEP 1 — Get the deal ID

If $ARGUMENTS is provided, use it as the deal_id.
Otherwise ask: "Which deal? (cholla / pngts / unitil / ...)"

## STEP 2 — Read the JSON from clipboard

Tell the user: "Copy the full JSON from inside the ---DEAL-UPDATE--- block (just the JSON, not the fences), then press Enter."

Wait for confirmation.

## STEP 3 — Run the write script

```bash
python3 ~/dashboards/scripts/write-deal-entry.py --deal-id {deal_id}
```

The script will:
1. Read JSON from clipboard
2. Strip any ---DEAL-UPDATE--- fences if present
3. Validate the JSON
4. Write to the deal's Drive folder
5. Run sync_deals_from_drive.py to pull to local
6. Warm up the dashboard

## STEP 4 — Confirm

Report: deal updated, Drive file written, dashboard warmed.
If the script errors, show the full output and diagnose.

## STEP 5 — Strategic-update detection (Jane substrate)

Review the deal update content. If it includes any of the following, also
update the deal's `decision_state_jane` Drive Doc:
- A strategic frame change (new thesis, changed risk/return view)
- A new deal-killer identified
- A sequencing dependency shift (what must happen before what changed)
- A "what would change my view" change

When detected:

**At minimum:** bump the `**Last updated:** YYYY-MM-DD` line to today's date
(2026-05-25 or run `date +%Y-%m-%d` to get current date).

**Ideally:** update the relevant bullet(s) in the Decision State with the new
strategic frame content.

**How to do the update (per EP1 — setContent only, never recreate):**

1. Get the `decision_state_jane_file_id` for this deal:
   ```bash
   python3 -c "
   import json
   d = json.load(open('/Users/ygontownik/dashboards/data/compiled/deal-system-data.json'))
   deal = next((x for x in d['deals'] if x.get('id') == '{deal_id}'), None)
   print(deal.get('decision_state_jane_file_id', 'MISSING') if deal else 'DEAL NOT FOUND')
   "
   ```

2. Read the current local mirror at
   `~/dashboards/data/deals/{deal_id}/decision_state_jane.md`

3. Edit the content in memory (bump Last updated, revise relevant bullets)

4. Write back via Drive API — use the same `set_doc_content` helper that
   `bootstrap_jane_drive.py` uses (setContent on the registered fileId):
   ```python
   import pickle
   from googleapiclient.discovery import build
   from googleapiclient.http import MediaInMemoryUpload

   with open('/Users/ygontownik/credentials/gdrive_token.pickle', 'rb') as f:
       creds = pickle.load(f)
   svc = build('drive', 'v3', credentials=creds)
   media = MediaInMemoryUpload(content.encode('utf-8'), mimetype='text/plain')
   svc.files().update(fileId='{decision_state_jane_file_id}', media_body=media).execute()
   ```

5. Refresh the local mirror:
   ```bash
   # Write updated content to local mirror
   # (same content just setContent'd to Drive)
   ```

If the strategic update is minor (routine status update, no frame change):
skip this step. When in doubt, update Last updated — it keeps the staleness
check (check_decision_state.py) from flagging the deal unnecessarily.
