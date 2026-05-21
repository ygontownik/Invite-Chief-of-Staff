---
description: Autonomous diligence data pull and thesis pressure test for any registered TCIP deal
argument-hint: "<deal_id> [--corrections-only]"
---

# /deal-diligence — Structured diligence data pull and thesis pressure test

You are executing a structured diligence data pull for TCIP (Tomac Cove Infrastructure
Partners). This is a fully autonomous run. Complete all tasks without interruption or
clarification. When you encounter ambiguity, apply the decision rules in this prompt
and log your decision in gaps.md. Never stop to ask a question. If data is unavailable,
log it as a gap and move on.

Think like a principal investor and board director. So-what first. Specifics over
generalities. Named assets and firms, not themes. Investment implications, not
descriptions. Null is correct when data is unavailable — invented precision is worse
than a gap.

All Drive I/O uses:
```
python3 ~/cos-pipeline/tools/deal_extract_helpers.py
```

---

## STEP 0 — Parse argument

`$ARGUMENTS` contains:
- `<deal_id>` (required) — e.g. `pngts`, `cholla`, `unitil`
- `--corrections-only` (optional) — skip data pulls; only run the Critical Corrections
  review and update thesis_pressure_test.md + SESSION_HANDOFF.md

Load the registry:

```bash
python3 - <<'EOF'
import json, sys
data = json.load(open('/Users/ygontownik/cos-pipeline/tools/deal-system-data.json'))
deal_id = "$DEAL_ID"
deal = next((d for d in data['deals'] if d['deal_id'] == deal_id), None)
if not deal:
    sys.exit(f"deal_id '{deal_id}' not found in registry")
print(json.dumps(deal, indent=2))
EOF
```

Hold in memory: `deal_id`, `name`, `deal_type`, `drive_folder_id`, `status_file_id`,
`brief_file_id`. You will need these in every subsequent step.

Also load the `_Diligence` subfolder ID from drive-docs.yaml if present:

```bash
python3 -c "
import yaml, os
cfg = yaml.safe_load(open(os.path.expanduser('~/dashboards/config/drive-docs.yaml')))
deal_id = '$DEAL_ID'
entry = cfg.get('deal_docs', {}).get(deal_id, {})
print('diligence_folder_id:', entry.get('diligence_folder_id', 'NOT_SET'))
print('claude_context_folder_id:', entry.get('claude_context_folder_id', 'NOT_SET'))
"
```

If `diligence_folder_id` is NOT_SET, create the `_Diligence/` subfolder under
`drive_folder_id` and register its ID in drive-docs.yaml before proceeding.

---

## STEP 1 — Drive setup (read before writing anything)

Read in this exact order. Do not proceed to Step 2 until all reads are complete.

1. Status doc (`status_file_id`) — extract: open items, named drivers, current verdicts
2. Master brief (`brief_file_id`) — extract: thesis, counterparty map, factual conflicts table
3. TCIP Firm Context (File ID: `1oqvRhNq-MRS9sBT-wtZxqoZiwOC5GfCQHM1K0DuC6Pk`)
4. Practice Patterns (File ID: `1C3z_6hnKtYZcpQM4Ffh2qN4EiVEwThNDC9NwHlt-zqY`)
5. Existing `_Diligence/` files — read each if present (look up file IDs from
   `drive-docs.yaml` under `deal_docs.<deal_id>.diligence_files`):
   - `readme` → README
   - `thesis_pressure_test` → thesis_pressure_test
   - `gaps` → gaps
   - `session_handoff` → SESSION_HANDOFF

   These are Google Docs. Read them via export:
   ```python
   import io
   from googleapiclient.http import MediaIoBaseDownload

   def read_diligence_doc(file_id):
       request = service.files().export_media(fileId=file_id, mimeType='text/plain')
       buf = io.BytesIO()
       dl = MediaIoBaseDownload(buf, request)
       done = False
       while not done:
           _, done = dl.next_chunk()
       return buf.getvalue().decode('utf-8')
   ```

After reading, hold in memory:
- Named drivers (D1 through Dn) and their current verdict status
- All open items from status doc
- All counterparty names and roles
- Any existing gap IDs (to avoid renumbering)
- Any corrections already logged in prior sessions

---

## STEP 2 — Critical Corrections

Before any data pull, open a `CRITICAL CORRECTIONS` working section.

Review every driver currently marked WEAKENED or INCONCLUSIVE in `thesis_pressure_test.md`.
For each:
- State what the prior analysis concluded
- State what evidence that conclusion rested on
- Flag if that evidence was a single source, counterparty-asserted, or inferred
- Reopen the driver if the basis was weak

Review every MEDIUM or WEAKENED row in `README.md`. Flag any that should be
re-examined against primary sources in this session.

If `--corrections-only` was passed: complete this step, update `thesis_pressure_test.md`
and `SESSION_HANDOFF.md`, then skip to STEP 6 (Output) and STEP 7 (Console Summary).

---

## STEP 3 — Data pulls (branched by deal_type)

Execute the data tasks appropriate for this deal's type. Log every failed pull as a
gap in gaps.md. Do not estimate around missing data.

---

### deal_type: pipeline

**FERC eTariff** — https://etariff.ferc.gov/TariffList.aspx
- Search by pipeline name. Download current tariff sheets.
- Extract: max tariff rates, reservation charges, commodity charges, fuel retention %
- Extract: contract summary page — shipper names, MDQ by shipper, contract tenors
- Log any rate that cannot be confirmed against a filed tariff sheet as INCONCLUSIVE

**Electronic Bulletin Boards (EBBs)**
- Find the pipeline's EBB URL from the FERC eTariff contact page or operator website
- Pull: daily capacity postings, nomination vs. confirmed volumes, scheduled quantities
- Calculate: average utilization % over trailing 12 months if daily data is available
- Log EBB URL found and data range pulled

**FERC Form 2A / VFP** — https://eqrweb.ferc.gov/EQR/controller
- Pull Volume and Financial Profile (VFP) for the pipeline
- Extract: total throughput (Dth/yr), operating revenues by category, O&M expenses
- Cross-check against counterparty-asserted EBITDA — log any gap >10%

**FERC eLibrary** — https://elibrary.ferc.gov/eLibrary/search
- Search by pipeline name. Pull last 3 years of rate case filings, settlement orders,
  capacity expansion certificates, compliance filings
- Flag any pending rate case or tariff investigation as a CRITICAL gap

---

### deal_type: power

**EIA Form 860** — https://www.eia.gov/electricity/data/eia860/
- Download current year 860 data. Filter to generator unit(s) by plant name / state.
- Extract: nameplate capacity (MW), summer/winter capacity, fuel type, online year,
  ownership, latitude/longitude

**EIA Form 923** — https://www.eia.gov/electricity/data/eia923/
- Pull trailing 2 years. Filter to same plant.
- Extract: net generation (MWh/yr), fuel consumption, capacity factor (calc from 860 MW)

**Interconnection queue** (select by market):
- ERCOT: https://www.ercot.com/gridinfo/resource — PGRR/NOGRR queue; pull position,
  MW, fuel type, expected COD, study milestone status
- MISO: https://www.misoenergy.org/planning/resource-interconnection/
- PJM: https://www.pjm.com/planning/interconnection-process

**ERCOT IPWS** (ERCOT deals only) — https://www.ercot.com/gridinfo/resource
- Pull Installed Wind and Solar Capacity Summary; generation unit detail for deal asset
- Confirm unit status (active / mothballed / retirement notice filed)

**FERC eLibrary** — search by plant name or company name
- Pull any QF filings, LGIA/SGIA, capacity market filings, PPAs if FERC-jurisdictional

---

### deal_type: utility

**SEC EDGAR** — https://www.sec.gov/cgi-bin/browse-edgar
- Pull last 3 years 10-K and last 2 quarters 10-Q for the utility holding company
- Extract: rate base, earned ROE by segment, O&M by segment, capex plan, long-term debt
- Extract: segment breakdown — electric distribution, gas distribution, transmission
- Flag any restatement or material weakness disclosure

**State PUC rate case dockets**
- Identify the relevant state commission(s) from the 10-K regulatory section
- Search docket system for open and recently-closed rate cases
- Extract: test year, requested revenue requirement, requested ROE, intervening parties,
  expected order date
- For each pending case: log as open driver or gap with ETA

Common state docket portals:
- NH PUC: https://www.puc.nh.gov/Regulatory/Dockets/
- MA DPU: https://eeaonline.eea.state.ma.us/DPU/Fileroom/
- ME PUC: https://mpuc-cms.maine.gov/CQS.Public.WebUI/
- VT PSD: https://puc.vermont.gov/electric/pending-dockets

**EIA Form 861** — https://www.eia.gov/electricity/data/eia861/
- Filter to utility by EIA utility ID (found in 10-K or EIA860)
- Extract: customers by class, MWh sales, revenues, distribution line miles

**FERC Form 1** (if transmission assets) — https://www.ferc.gov/industries-data/electric/general-information/electric-industry-forms/form-1-electric-utility-annual-report
- Pull most recent filing. Extract: transmission plant in service, depreciation, CWIP,
  transmission revenues, formula rate if applicable

---

### deal_type: midstream

**FERC Form 2** (gas) — https://www.ferc.gov/industries-data/natural-gas/overview/general-information/natural-gas-industry-forms/form-2
- Pull most recent filing for the pipeline/gathering entity
- Extract: throughput volumes, revenues by service type, O&M, plant in service

**FERC eTariff** — same as pipeline branch above
- Pull gas gathering or processing tariff sheets
- Extract: gathering rates, compression charges, fuel retention

**EIA Natural Gas** — https://www.eia.gov/naturalgas/
- EIA-914: monthly natural gas production by area — use to size basin volumes
- Natural gas weekly supply/demand for relevant hub

**State oil & gas commission** (if gathering / upstream-adjacent)
- Pull production data for wells connected to the gathering system

---

### deal_type: shipping

**MARAD** — https://www.maritime.dot.gov/
- Pull vessel registry for named vessels. Extract: vessel class, tonnage, build year,
  flag, operator, trade route authorization
- Check Jones Act waiver history if domestic trade routes involved

**SEC EDGAR** (if counterparty is public)
- Pull 10-K/Q. Extract: charter terms, counterparty names, charter rates, fleet utilization

**Baltic Exchange / Clarksons** (market context only — public data)
- Pull current charter rate indices for relevant vessel class
- Log as MEDIUM confidence (market benchmark, not deal-specific)

---

### deal_type: international_infra

No standardized public filing regime. Data pull is document-driven:

- Read all PDFs and materials already in the deal's Drive folder
- Extract: project description, CAPEX, counterparty roles, regulatory jurisdiction,
  any concession agreement terms, fund terms if LP structure
- Search for counterparty track record: prior closed deals, fund history, AUM
  (use WebSearch — DealStreetAsia, Preqin, Infrastructure Investor, company website)
- Log all unverified claims from marketing materials as INCONCLUSIVE pending
  primary source or direct counterparty confirmation

---

## STEP 4 — Gap register update

For every data point that could not be confirmed, add or update an entry in gaps.md:

```
GAP-NNN
Description: [what is missing]
Blocks: [which driver or README row this affects]
Source needed: [specific filing, database, or ask]
Resolution path: [how to get it — FERC pull / PUC docket / counterparty ask / FOIA]
Priority: CRITICAL | HIGH | MEDIUM | LOW
ETA: [YYYY-MM-DD or "TBD — depends on [event]"]
```

CRITICAL = blocks a primary deal driver verdict
HIGH = needed before IC memo
MEDIUM = useful for IC memo but not blocking
LOW = nice to have / future diligence round

Close any gap from a prior session that this run resolved. Mark it: `STATUS: CLOSED — [date] — [how resolved]`.

---

## STEP 5 — Driver verdicts and README update

For each named driver (D1 through Dn from Step 1):
- Assess verdict: CONFIRMED / WEAKENED / INCONCLUSIVE / NEW
- CONFIRMED: primary source data supports the driver's value assumption
- WEAKENED: primary source data contradicts or reduces the driver's value
- INCONCLUSIVE: insufficient primary source data; driver remains open
- NEW: driver identified during this analysis not in original thesis

Update `thesis_pressure_test.md`:
- Revised verdict for each driver
- One-sentence rationale citing the specific source that drove the change
- Revised bridge estimate if any driver moved

Update `README.md`:
- Add or revise rows for any metric confirmed or weakened in this session
- Every row: Metric / Value / Source / Confidence

---

## STEP 6 — Output: write all four files to _Diligence/ as Google Docs

All four _Diligence/ files are written as Google Docs (not plain text). This allows
them to load immediately in claude.ai project sessions via the native Drive integration.

Pattern: **delete existing + create new Google Doc** on every run. Do not attempt
in-place Docs API edits — delete/recreate is simpler and guarantees a clean state.

```python
import pickle, os, yaml
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from googleapiclient.http import MediaInMemoryUpload

token_path = os.path.expanduser('~/credentials/gdrive_token.pickle')
with open(token_path, 'rb') as f:
    creds = pickle.load(f)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
service = build('drive', 'v3', credentials=creds)

cfg_path = os.path.expanduser('~/dashboards/config/drive-docs.yaml')
cfg = yaml.safe_load(open(cfg_path))
diligence_files = cfg.get('deal_docs', {}).get(DEAL_ID, {}).get('diligence_files', {})

def write_diligence_doc(stem, display_name, content, diligence_folder_id):
    """Delete existing Google Doc (if any) and create a fresh one. Returns new file ID."""
    existing_id = diligence_files.get(stem)
    if existing_id:
        try:
            service.files().delete(fileId=existing_id).execute()
        except Exception:
            pass  # already gone — proceed

    media = MediaInMemoryUpload(content.encode('utf-8'), mimetype='text/plain')
    meta = {
        'name': display_name,
        'parents': [diligence_folder_id],
        'mimeType': 'application/vnd.google-apps.document',  # Drive converts plain text → Google Doc
    }
    result = service.files().create(body=meta, media_body=media).execute()
    new_id = result['id']

    # Update drive-docs.yaml with new file ID
    if 'deal_docs' not in cfg:
        cfg['deal_docs'] = {}
    if DEAL_ID not in cfg['deal_docs']:
        cfg['deal_docs'][DEAL_ID] = {}
    if 'diligence_files' not in cfg['deal_docs'][DEAL_ID]:
        cfg['deal_docs'][DEAL_ID]['diligence_files'] = {}
    cfg['deal_docs'][DEAL_ID]['diligence_files'][stem] = new_id
    with open(cfg_path, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

    return new_id

# Write in this order — SESSION_HANDOFF last
write_diligence_doc('readme',               'README',               readme_content,               DILIGENCE_FOLDER_ID)
write_diligence_doc('thesis_pressure_test', 'thesis_pressure_test', thesis_content,               DILIGENCE_FOLDER_ID)
write_diligence_doc('gaps',                 'gaps',                 gaps_content,                 DILIGENCE_FOLDER_ID)
write_diligence_doc('session_handoff',      'SESSION_HANDOFF',      session_handoff_content,      DILIGENCE_FOLDER_ID)
```

**File naming**: no `.md` extension — Google Docs don't use file extensions. The stem
(`readme`, `thesis_pressure_test`, `gaps`, `session_handoff`) is used as the
drive-docs.yaml key; the display name is the human-readable label in Drive.

Update SESSION_HANDOFF last (it summarizes everything written in this session).

---

## STEP 7 — Console summary on completion

Print:

```
=== /deal-diligence: <DEAL_NAME> (<deal_type>) ===

CORRECTIONS VALIDATED:
  [list any prior analysis corrections confirmed or newly identified]

DRIVER VERDICTS:
  D1 [name]: VERDICT — one-line rationale
  D2 [name]: VERDICT — one-line rationale
  ...

GAPS:
  CRITICAL: N  HIGH: N  MEDIUM: N  LOW: N
  New gaps opened: [list IDs]
  Gaps closed: [list IDs]

FILES WRITTEN:
  README.md — N rows, N HIGH confidence
  thesis_pressure_test.md — N drivers assessed
  gaps.md — N total gaps
  SESSION_HANDOFF.md — updated

RECOMMENDED PRE-NEXT-SESSION ACTIONS:
  1. [verb-first, specific, named counterparty or data source]
  2. ...
```

---

## RULES (non-negotiable)

- Never call the Anthropic API directly. All AI work happens in this session.
- Never estimate around a gap. Log it and move on.
- Never upgrade a driver verdict without a named primary source citation.
- Source hierarchy: regulatory filings > executed agreements > transcripts > marketing materials.
- "Per management" is not a source. Quote the filing and page.
- If a session ends before all tasks complete, write SESSION_HANDOFF.md with
  exactly where you stopped so the next session can resume without re-doing work.
