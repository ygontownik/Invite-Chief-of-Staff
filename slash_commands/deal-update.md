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
