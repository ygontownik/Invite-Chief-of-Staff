#!/usr/bin/env python3
"""
NEW DEAL ONBOARDING SCRIPT
===========================
Automates ~80% of the new deal setup process.
Run this whenever the firm engages a new deal.

USAGE:
  python tcip_new_deal.py \
    --deal-name "Lakeview Wind Farm" \
    --deal-id "lakeview_wind" \
    --lead "Jane Smith" \
    --support "John Doe" \
    --docs-folder "/path/to/deal/documents"   # optional
    --drive-folder-id "EXISTING_DRIVE_FOLDER_ID"  # optional, if folder exists

  OR interactively (no flags needed):
  python tcip_new_deal.py

WHAT IT DOES AUTOMATICALLY:
  Phase 1: Creates Google Drive folder structure
  Phase 2: Reads deal documents + runs Critical Driver Framework via Claude API
  Phase 3: Generates status.md and master_brief.md, saves to Drive, records IDs
  Phase 4: Updates compile_drive_writeback.py with new file IDs
  Phase 5: Updates deal-system-data.json
  Phase 6: Creates local data folders and JSON registers
  Phase 7: Generates populated Project instructions
  Phase 8: Updates firm_context.md pipeline table in Drive
  Phase 9: Outputs BROWSER-STEP signal — Claude Code pastes instructions
           into the Claude Project and wires the Project URL automatically

WHAT REMAINS MANUAL (one-time, ~1 minute):
  - Create the Claude Project at claude.ai
  - Enable Google Drive connector in Project settings
  - Provide the Project URL to Claude Code -> rest is automatic
  NOTE: No file uploads needed -- firm context is read live from Drive

CONFIGURATION:
  Firm identity is read from firm_context.yaml (principal.name, firm.name,
  firm.short_name). Firm context Drive ID is read from drive-docs.yaml
  (docs.firm_context_doc.doc_id). See drive-docs.template.yaml.

REQUIREMENTS:
  pip install anthropic google-auth google-auth-oauthlib google-api-python-client
  credentials.json in same folder (Google Drive API OAuth credentials)
"""

import glob
import os
import shutil
import subprocess
import sys
import json
import argparse
import re
from pathlib import Path
from datetime import date
from textwrap import dedent

# ── FIRM IDENTITY (from firm_context.yaml + drive-docs.yaml) ─────────────────
# Graceful fallback — script still runs if config files are missing.
_PIPELINE_DIR = Path(__file__).parent.parent
try:
    sys.path.insert(0, str(_PIPELINE_DIR))
    import _firm_context as _fc
    _CTX             = _fc.load_firm_context()
    _DDOC            = _fc.load_drive_docs()
    FIRM_NAME        = _CTX.get("firm", {}).get("name", "the firm")
    FIRM_SHORT       = _CTX.get("firm", {}).get("short_name", "Firm")
    PRINCIPAL_NAME   = _CTX.get("principal", {}).get("name", "the lead")
    PRINCIPAL_FIRST  = PRINCIPAL_NAME.split()[0]
    _team            = _CTX.get("team", [])
    TEAM_NAMES       = [m.get("name", "") for m in _team if m.get("name")]
    # New-style ref doc location takes precedence; fall back to legacy
    # docs.firm_context_doc for tenants on the old config layout.
    FIRM_CONTEXT_DRIVE_ID = (
        _DDOC.get("reference_docs", {}).get("firm_context", {}).get("doc_id")
        or _DDOC.get("docs", {}).get("firm_context_doc", {}).get("doc_id")
        or ""
    )
except Exception:
    FIRM_NAME             = "the firm"
    FIRM_SHORT            = "Firm"
    PRINCIPAL_NAME        = "the lead"
    PRINCIPAL_FIRST       = "the lead"
    TEAM_NAMES            = []
    FIRM_CONTEXT_DRIVE_ID = ""

# ── INSTALL CHECK ─────────────────────────────────────────────────────────────

def check_requirements():
    missing = []
    try: from googleapiclient.discovery import build
    except ImportError: missing.append("google-api-python-client")
    try: from google.oauth2.credentials import Credentials
    except ImportError: missing.append("google-auth")
    if missing:
        print(f"\n❌ Missing packages: {', '.join(missing)}")
        print(f"   Run: pip install {' '.join(missing)}")
        sys.exit(1)

check_requirements()

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
SCOPES = ["https://www.googleapis.com/auth/drive"]
TODAY = date.today().isoformat()

# Paths relative to dashboard folder
COMPILE_WRITEBACK = SCRIPT_DIR / "compile_drive_writeback.py"
DEAL_SYSTEM_DATA  = SCRIPT_DIR / "deal-system-data.json"
SYNC_STATE        = SCRIPT_DIR / "sync-state.json"
DATA_DIR          = SCRIPT_DIR / "data" / "project-sync"
FIRM_CONTEXT_PATH = SCRIPT_DIR / "firm_context.md"

# Drive folder where tcip-deals-registry.json lives (TC_CONTEXT).
# The Drive Organizer reads this file at runtime to auto-expand for new deals.
TC_CONTEXT_FOLDER_ID = "1IsGHEEeWOFtJ3yDYseJlSHYBprzom1tn"
DRIVE_REGISTRY_FILE  = "tcip-deals-registry.json"

# Parent folder where new deal folders MUST be created.
# Today: TC_DEALS. Post-consolidation: 00 Tomac Cove/Active Deals/. Folder ID
# is stable under move+rename so this constant doesn't change.
TC_DEALS_FOLDER_ID   = "1vRVTYiS4wyqsHr_3iYepu2_g0biS6n3b"

# Deal model scaffolding (see scaffold_deal_model() below). Templates live in
# the private tenant config repo; deck generation is the Python+Claude pipeline
# in ~/cos-pipeline/tools/deck_base.py + ~/cos-pipeline-config-tomac/tools/
# build_deck_<deal>.py, invoked via the `tcip` alias. No OLE linking involved.
# See DRIVE-RECOMMENDATIONS.md §8 (corrected) for the workflow rationale.
DEAL_MODEL_TEMPLATE_PATH = Path(os.environ.get(
    "TCIP_MODEL_TEMPLATE",
    str(Path.home() / "cos-pipeline-config-tomac/reference_docs/TCIP_Deal_Model_Template.xlsx"),
))
DEAL_BUILDER_TEMPLATE_PATH = Path(os.environ.get(
    "TCIP_BUILDER_TEMPLATE",
    str(Path.home() / "cos-pipeline-config-tomac/tools/build_deck_fit.py"),
))
DEAL_REGISTRY_PATH = Path(os.environ.get(
    "TCIP_DEAL_REGISTRY",
    str(Path.home() / "cos-pipeline-config-tomac/config/deal_registry.json"),
))

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Drive folder subfolders to create per deal
DEAL_SUBFOLDERS = ["Documents", "Transcripts", "Filings", "Correspondence"]

# Supported document extensions for reading
DOC_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}

# ── TRIGGER AND RULES LIBRARIES ───────────────────────────────────────────────

TRIGGER_LIBRARY = {
    "cash_runway": """
Trigger D1 — CASH CLIFF APPROACHING
If today is within 30 days of the cash runway date in the status file:
→ Open every session with: "CASH CLIFF: [N] days away.
  Refi/funding status: [status]. Action needed: [action]."
""",
    "regulatory_vote": """
Trigger D2 — REGULATORY VOTE APPROACHING
If a board vote, commission ruling, or regulatory deadline is within 14 days:
→ Flag: "REGULATORY EVENT IN [N] DAYS: [event].
  Preparation status: [status]."
""",
    "disintermediation": """
Trigger D3 — DISINTERMEDIATION RISK
If any document suggests direct contact between an introducer
and the target without {FIRM_SHORT} in the loop:
→ Flag immediately: "DISINTERMEDIATION RISK DETECTED — [details]."
""",
    "process_deadline": """
Trigger D4 — PROCESS DEADLINE
If a bid deadline or bake-off date is within 7 days:
→ Flag: "PROCESS DEADLINE IN [N] DAYS. Outstanding prep: [items]."
""",
    "relationship_tension": """
Trigger D5 — RELATIONSHIP TENSION DETECTED
If any document escalates tension with a key counterparty:
→ Flag immediately and summarize the development.
→ Ask: "How do you want to handle this?"
""",
    "market_news": """
Trigger D6 — MARKET NEWS AFFECTING THESIS
If any document contains news that directly affects the critical driver:
→ Flag: "MARKET UPDATE: [summary]. Impact on thesis: [assessment]."
""",
}

RULES_LIBRARY = {
    "regulated_asset": """
- Allowed return assumptions come from filed rate cases only
- Do not estimate regulatory outcomes — find the filing
- Rate case trajectory governs the cash flow model
""",
    "development_asset": """
- Timeline assumptions = best-case unless source document says otherwise
- Regulatory approvals are probabilistic until executed documents exist
- Do not present development timeline as contracted
""",
    "relationship_sensitive": """
- Do not name {FIRM_SHORT} fees in external drafts until formalized
- Do not attribute analytical conclusions to named sources
  in external communications
- Flag any draft that could affect a key relationship
""",
}

# ── AUTH ──────────────────────────────────────────────────────────────────────

def get_drive_service():
    creds = None
    token_path = SCRIPT_DIR / "token.json"
    creds_path = SCRIPT_DIR / "credentials.json"

    if not creds_path.exists():
        print("\n❌ credentials.json not found.")
        print("   1. Go to console.cloud.google.com")
        print("   2. Enable Google Drive API")
        print("   3. Create OAuth credentials (Desktop app)")
        print(f"   4. Download as credentials.json → save to {SCRIPT_DIR}")
        sys.exit(1)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def _status_template(deal_name, deal_id, lead, support):
    support_line = f"Support: {support}" if support else "Support: —"
    return dedent(f"""\
    # {deal_name} — Status
    Deal ID: {deal_id}
    Lead: {lead} | {support_line}
    Last updated: {TODAY}

    ## Critical Driver
    *To be completed in first Claude Project session.*

    ## Hard Deadlines
    | Date | Item | Owner |
    |------|------|-------|
    | — | — | — |

    ## Open Items
    | # | Item | Owner | Due |
    |---|------|-------|-----|
    | 1 | Run Critical Driver Framework | {lead} | First session |

    ## Counterparties
    | Name | Firm | Role |
    |------|------|------|
    | — | — | — |

    ## Session Log
    | Date | Summary |
    |------|---------|
    | {TODAY} | Deal onboarded. Awaiting first session. |
    """)

def _master_brief_template(deal_name, deal_id, lead, support):
    support_line = f"Support: {support}" if support else "Support: —"
    return dedent(f"""\
    # {deal_name} — Master Brief
    Deal ID: {deal_id}
    Lead: {lead} | {support_line}
    Last updated: {TODAY}

    ## THE CORE ARGUMENT
    *To be completed in first Claude Project session.*

    ## POINTS OF CONSENSUS
    *To be completed in first Claude Project session.*

    ## POINTS OF DISAGREEMENT OR TENSION
    *To be completed in first Claude Project session.*

    ## OPEN QUESTIONS AND UNRESOLVED ISSUES
    *To be completed in first Claude Project session.*

    ## WHAT YOU WOULD NEED TO FORM A VIEW
    *To be completed in first Claude Project session.*

    ## KEY NAMES AND FIRMS
    *To be completed in first Claude Project session.*
    """)

# ── DRIVE OPERATIONS ──────────────────────────────────────────────────────────

def create_drive_folder(service, name, parent_id):
    """Create a folder in Drive. parent_id is REQUIRED (no implicit root creation).

    Hardened 2026-05-20: parent_id used to default to None, which caused new deal
    folders to land at My Drive root rather than under TC_DEALS. See DRIVE-ARCHITECTURE.md
    invariant I11.
    """
    if parent_id is None:
        raise ValueError(
            "parent_id is required — pass an explicit Drive folder ID. "
            "Creating at My Drive root is disabled to prevent registry drift."
        )
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]}
    f = service.files().create(body=meta, fields="id,name").execute()
    return f["id"]


def create_drive_text_file(service, name, content, parent_id):
    """Upload a plain-text file to Drive (NOT a Google Doc). parent_id is REQUIRED."""
    if parent_id is None:
        raise ValueError("parent_id is required for create_drive_text_file.")
    meta = {"name": name, "parents": [parent_id]}
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain", resumable=False)
    f = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
    return f["id"], f.get("webViewLink", "")


def create_drive_doc(service, title, content, parent_id):
    """Create a native Google Doc from text content. parent_id is REQUIRED.

    Produces mimeType=application/vnd.google-apps.document — readable in Drive without
    download, referenceable by claude.ai project instructions.
    """
    if parent_id is None:
        raise ValueError("parent_id is required for create_drive_doc.")
    meta = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [parent_id],
    }
    media = MediaInMemoryUpload(
        content.encode("utf-8"),
        mimetype="text/plain",  # upload as plain text; Drive converts to Google Doc
        resumable=False
    )
    f = service.files().create(
        body=meta,
        media_body=media,
        fields="id,name,webViewLink"
    ).execute()
    return f["id"], f.get("webViewLink", "")


def update_drive_doc(service, file_id, content):
    media = MediaInMemoryUpload(
        content.encode("utf-8"),
        mimetype="text/plain",
        resumable=False
    )
    service.files().update(fileId=file_id, media_body=media).execute()


def read_drive_file(service, file_id):
    try:
        content = service.files().export(
            fileId=file_id, mimeType="text/plain"
        ).execute()
        return content.decode("utf-8") if isinstance(content, bytes) else content
    except Exception:
        try:
            content = service.files().get_media(fileId=file_id).execute()
            return content.decode("utf-8") if isinstance(content, bytes) else content
        except Exception as e:
            return f"[Could not read file: {e}]"

# ── DOCUMENT READING ──────────────────────────────────────────────────────────

def read_local_docs(folder_path):
    """Read text from all supported document types in a folder."""
    docs = []
    folder = Path(folder_path)
    if not folder.exists():
        print(f"   ⚠️  Docs folder not found: {folder_path}")
        return docs

    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in DOC_EXTENSIONS:
            continue
        try:
            if f.suffix.lower() == ".txt" or f.suffix.lower() == ".md":
                content = f.read_text(encoding="utf-8", errors="ignore")
                docs.append({"name": f.name, "content": content[:8000]})
                print(f"   ✓ Read: {f.name} ({len(content):,} chars)")
            else:
                print(f"   ⚠️  Skipped binary file (convert to .txt first): {f.name}")
        except Exception as e:
            print(f"   ⚠️  Could not read {f.name}: {e}")
    return docs

# ── CLAUDE API CALLS ──────────────────────────────────────────────────────────

def run_critical_driver_framework(client, deal_name, deal_id, docs):
    """Run the critical driver analysis on deal documents."""
    print("\n   🤖 Running Critical Driver Framework via Claude API...")

    doc_text = ""
    if docs:
        for d in docs[:3]:  # Limit to first 3 docs to stay within context
            doc_text += f"\n\n--- DOCUMENT: {d['name']} ---\n{d['content'][:5000]}"
    else:
        doc_text = "[No deal documents provided — generating placeholder framework]"

    prompt = f"""You are analyzing a new infrastructure deal for {FIRM_SHORT}
({FIRM_NAME}).

Deal: {deal_name}
Deal ID: {deal_id}

Deal documents provided:
{doc_text}

Run the Critical Driver Framework:

STEP 1 — WHAT DOES THE RETURN DEPEND ON?
Identify the single variable that, if wrong, kills the return thesis.
State it in one sentence.

STEP 2 — MARKET-DETERMINED OR ASSET-SPECIFIC?
- Market-determined: driven by external supply/demand dynamics
- Asset-specific: driven by contract, permit, physical characteristic
- Or both

STEP 3 — BEAR CASE
Complete this sentence: "The thesis is wrong if..."
One paragraph. No hedging.

STEP 4 — DILIGENCE QUESTIONS
List exactly 5-10 specific, answerable questions that research
must answer to confirm or deny the critical driver.

STEP 5 — RECOMMENDATION
One paragraph on the primary diligence workstream and what
must be answered first before any capital commitment.

Output ONLY the structured block below, nothing else:

---CRITICAL-DRIVER---
deal_id: {deal_id}
critical_driver: [single sentence]
driver_type: [market-determined / asset-specific / both]
bear_case: [one paragraph]
diligence_questions:
  1. [question]
  2. [question]
  3. [question]
  4. [question]
  5. [question]
recommendation: [one paragraph]
---END---"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def generate_status_file(client, deal_name, deal_id, lead, support,
                          critical_driver_block, docs):
    """Generate initial status.md from deal info and critical driver."""
    print("   🤖 Generating status.md via Claude API...")

    prompt = f"""Generate an initial status.md file for a new deal at {FIRM_SHORT}.

Deal: {deal_name}
Deal ID: {deal_id}
Lead: {lead}
Support: {support}
Date: {TODAY}

Critical driver analysis:
{critical_driver_block}

Documents available: {', '.join(d['name'] for d in docs) if docs else 'None yet'}

Generate a complete status.md in this exact format:

# {deal_name} — Project Status
**deal_id:** {deal_id}
**generated:** {TODAY} (initial setup — populate from deal documents)
**last_session:** {TODAY}
**project_url:** null (pending — wire via /project-sync/update)

---

## Stage
[Describe current stage based on available information.
If unknown: "Early — initial engagement. Context to be built
in first full Claude session."]

---

## Hard Deadlines

| Deadline | Date | Status |
|----------|------|--------|
| [Add when known] | TBD | Pending |

---

## Key People

| Person | Role | Key facts |
|--------|------|-----------|
| {lead} | {FIRM_SHORT} lead | Active |
| {support} | {FIRM_SHORT} support | Active |
| [Add counterparties when known] | — | — |

---

## Critical Driver

[Paste the critical driver, driver type, and bear case from the
analysis above]

## Diligence Questions (open)

[List the diligence questions numbered, each as an open item]

---

## Counterparties

| Party | Role | Status | Next action |
|-------|------|--------|-------------|
| [Add when known] | — | — | — |

---

## Open Workstreams

This week:
1. Complete initial deal orientation — read all available documents
2. Confirm critical driver assessment with {lead}
3. Answer diligence question #1 (highest priority)
4. Formalize {FIRM_SHORT} economics before any external outreach

Next 2 weeks:
5. Build financial model (or review existing)
6. Identify and contact primary counterparties
7. [Add deal-specific items]

---

## Decisions Log (append only — never delete)

| Date | Decision | Rationale | Alternatives rejected |
|------|----------|-----------|----------------------|
| {TODAY} | Engaged deal | [Reason for engagement] | Passed |

---

## Last Session Summary

Date: {TODAY}
Work completed: Initial deal setup via new_deal.py script.
Critical driver framework run on available documents.
Status.md and master brief generated. Project instructions prepared.

---

## Superseded Assumptions (append only)

| Old | Correct | Source |
|-----|---------|--------|
| [None yet — add as assumptions are corrected] | — | — |

---

## Reference Documents

| Document | Location | Purpose |
|----------|----------|---------|
| [Add as documents are processed] | — | — |

Output ONLY the status.md content, no other text."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def generate_master_brief(client, deal_name, deal_id, lead, support,
                           critical_driver_block, docs):
    """Generate initial master brief from deal info."""
    print("   🤖 Generating master_brief.md via Claude API...")

    doc_summaries = ""
    if docs:
        for d in docs[:3]:
            doc_summaries += f"\n\nDocument: {d['name']}\n{d['content'][:3000]}"

    prompt = f"""Generate an initial master_brief.md for a new deal at {FIRM_SHORT}.
Use the universal 11-part structure.

Deal: {deal_name}
Deal ID: {deal_id}
Lead: {lead}
Support: {support}
Date: {TODAY}

Critical driver analysis:
{critical_driver_block}

Available documents:
{doc_summaries if doc_summaries else 'None provided yet'}

Generate using this structure. Mark sections as [TO BE POPULATED]
where information is not yet available rather than inventing facts.

# {deal_name} — Master Deal Brief
**Prepared:** {TODAY} | **By:** Claude / {FIRM_SHORT}
**Status:** Initial — populate from deal documents in first full session

---

## PART I — SITUATION SUMMARY
[2-3 paragraphs. What is the deal, who owns it, why is {FIRM_SHORT} involved,
what is the near-term catalyst. Mark unknown facts as [TBC].]

## PART II — KEY PEOPLE
[Tables for deal team, {FIRM_SHORT} team, key externals. Use what is known.]

## PART III — ASSET / DEAL DETAIL
[Asset description, key metrics, site/portfolio detail if applicable.]

## PART IV — CRITICAL DRIVER DEEP-DIVE
[This is the most important section. Based on the critical driver
analysis, write a thorough section covering:
- What the critical driver is and why it governs the thesis
- The market or asset-specific dynamics at play
- What needs to be true for the thesis to work
- What the diligence workstream should look like
- Key questions that must be answered
This section should be 3-5 paragraphs minimum.]

## PART V — VALUATION FRAMEWORK
[Initial valuation framework. Mark assumptions as [TBC] where unknown.]

## PART VI — STRATEGY
[{FIRM_SHORT}'s strategic angle, role, and fee structure. What conversations
need to happen and in what order.]

## PART VII — DEAL HISTORY TIMELINE
| Date | Event |
|------|-------|
| {TODAY} | Initial {FIRM_SHORT} engagement — deal onboarding |

## PART VIII — OPEN ITEMS
[List from diligence questions + standard open items for a new deal.]

## PART IX — KEY RISKS
[5-7 key risks based on available information and critical driver.]

## PART X — DOCUMENTS IN DRIVE
| Document | Location | Purpose |
|----------|----------|---------|
[List available documents]

## PART XI — FACTUAL CONFLICTS
[None identified yet — flag conflicts as they emerge]

Output ONLY the master brief content, no other text."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def generate_project_instructions(deal_name, deal_id, lead, support,
                                   status_file_id, brief_file_id,
                                   drive_folder_name, triggers, rules,
                                   outputs_folder_id='', session_log_file_id='',
                                   dashboard_entry_file_id='DASHBOARD_ENTRY_FILE_ID_PLACEHOLDER'):
    """Generate populated Project instructions from template."""

    trigger_text = "\n".join(TRIGGER_LIBRARY.get(t, "") for t in triggers)
    rules_text = "\n".join(RULES_LIBRARY.get(r, "") for r in rules)
    support_line = f"{support} is support." if support else ""

    return dedent(f"""
    ════════════════════════════════
    FIRST SESSION SETUP (run once, then delete this block)
    ════════════════════════════════

    This is the first session for {deal_name}. Before anything else:

    1. Read all documents uploaded to this Project.
    2. Run the Critical Driver Framework (defined in Level 3 below)
       and output the full result.
    3. Fill in status.md using the Critical Driver output:
       - Update "Critical Driver" section
       - Add any hard deadlines found
       - Add any counterparties named
       Write the updated content back to Drive:
       File ID: {status_file_id}
    4. Fill in master_brief.md using the six-section memo structure:
       Core Argument / Consensus / Tension / Open Questions /
       What You'd Need to Form a View / Key Names & Firms
       Write the updated content back to Drive:
       File ID: {brief_file_id}
    5. Confirm both files written, then proceed as normal.

    ════════════════════════════════

    You are Claude working for {PRINCIPAL_NAME} at {FIRM_NAME}
    ({FIRM_SHORT}) on the {deal_name} deal.

    {lead} is lead. {support_line}
    Do not ask {PRINCIPAL_FIRST} to re-explain the deal or his role.
    Do not proceed with any response until Level 1 is complete.

    ════════════════════════════════
    DEAL CONSTANTS (for session tooling -- do not modify)
    ════════════════════════════════
    outputs_folder_id:       {outputs_folder_id}
    session_log_file_id:     {session_log_file_id}
    dashboard_entry_file_id: {dashboard_entry_file_id}

    ════════════════════════════════
    LEVEL 1 — SESSION START
    Runs automatically, every conversation.
    ════════════════════════════════

    Step 0. Read firm context from Google Drive:
    File ID: {FIRM_CONTEXT_DRIVE_ID}
    This is the live firm context — always read from Drive,
    never from a cached or uploaded version.

    Step 1. Fetch this file from Google Drive and read it —
    this is the current deal state, updated after each session:
    File ID: {status_file_id}

    Step 2. Search the {drive_folder_name} folder in Google Drive
    for any files modified in the last 7 days that are NOT the
    status file above. For each new file found:
    - State the filename
    - State what it appears to contain (one line)
    - Flag if time-sensitive (deadline, term sheet, NDA, filing)
    Read anything that looks time-sensitive or new.

    Step 3. Run Level 2 before responding to anything.

    Step 0d. (LAZY) _Outputs/ folder and session_log.md IDs are in
    DEAL CONSTANTS above. Do NOT load at session start.
    Use them only at session end when save_session_output() runs.

    After Level 2, open every session with:
    "{deal_name} — [date]. Since last session: [delta].
     Urgent today: [what needs attention]. Resolved: [what closed]."


    ════════════════════════════════
    LEVEL 2 — DELTA DETECTION
    Runs after Level 1, every conversation.
    ════════════════════════════════

    Compare new documents against the current status file.
    For each new document:

    A. Does it resolve an open item?
       → Mark [RESOLVED date]. Never delete — keep for audit trail.

    B. Does it introduce a new deadline?
       → Add to hard deadlines. Flag if within 14 days.

    C. Does it conflict with an existing conclusion?
       → Flag: CONFLICT DETECTED: [describe].
       → Present both versions. Ask {PRINCIPAL_FIRST} which governs.
       → Never silently overwrite.

    D. Does it name a new counterparty or change existing?
       → Update counterparty table in working context.

    Then run Level 3.


    ════════════════════════════════
    LEVEL 3 — AUTO-UPDATE TRIGGERS
    Runs after Level 2. No prompt from {PRINCIPAL_FIRST} needed.
    ════════════════════════════════

    --- UNIVERSAL TRIGGERS ---

    Trigger 0 — NEW PRIMARY DEAL DOCUMENT
    When any CIM, IC memo, management presentation, financial model,
    or incoming term sheet lands in the deal folder:
    Run the Critical Driver Framework and output:

    ---CRITICAL-DRIVER---
    deal_id: {deal_id}
    document_analyzed: [filename]
    critical_driver: [single sentence]
    driver_type: [market-determined / asset-specific / both]
    bear_case: [one paragraph — The thesis is wrong if...]
    diligence_questions:
      1. [question]
      2. [question]
      3. [question]
      4. [question]
      5. [question]
    recommendation: [one paragraph]
    ---END---

    Ask: "Does this match your read? Which question is most urgent?"

    Trigger 1 — DEADLINE WITHIN 7 DAYS
    → Open with: "DEADLINE ALERT: [deadline] is [N] days away.
      Status: [status]. Action needed: [action]."

    Trigger 2 — MATERIAL ANALYTICAL CHANGE
    If new document contains new comp, rule change, executed
    contract, or new principal counterparty position:
    → Flag: "MASTER BRIEF UPDATE NEEDED: [what changed]"
    → Ask: "Want me to output the updated section now?"
    → If yes: fetch File ID {brief_file_id} and output updated
      section only.

    Trigger 3 — NEW COUNTERPARTY
    → Add to working context. Output updated counterparty table.
    → Flag if affects {FIRM_SHORT} economics or disintermediation risk.

    Trigger 4 — {FIRM_SHORT} ECONOMICS UNFORMALIZED + OUTREACH
    -> Flag: "STOP: {FIRM_SHORT} ECONOMICS NOT YET FORMALIZED.
      Do not proceed with external outreach until resolved."

    Trigger 5 — DRAFT REQUEST DETECTED
    When {PRINCIPAL_FIRST} uses: draft, write, prepare, send to, email to,
    memo on, one-pager for, update for, response to:
    State what you are drafting, source documents, purpose,
    and what must NOT be said. Then draft. Then output:

    ---DRAFT---
    deal_id: {deal_id}
    type: [email/memo/term_sheet/one_pager/lp_update]
    recipient: [name / firm]
    purpose: [one sentence]
    status: [ready_to_send / needs_review / draft_only]
    date: {TODAY}
    ---END---

    --- DEAL-SPECIFIC TRIGGERS ---
    {trigger_text}


    ════════════════════════════════
    LEVEL 4 — SESSION CLOSE
    Triggered when {PRINCIPAL_FIRST} says "session close".
    ════════════════════════════════

    Output these blocks in order. No other text.

    BLOCK 1 — DASHBOARD SYNC:
    ---SESSION-CLOSE---
    deal_id: {deal_id}
    last_session_date: YYYY-MM-DD
    session_summary: [2-3 sentences]
    open_items_delta: [new or resolved, or "none"]
    status_change: [any change to stage/facts/deadlines, or "none"]
    activities:
      - [what was worked on]
    decisions:
      - decision: [what was decided]
        rationale: [why]
        rejected: [alternatives not chosen, or "none"]
    pending_drafts:
      - type: [type]
        recipient: [who]
        purpose: [what it needs to accomplish]
        priority: [this_week / this_month]
    research_tasks:
      - topic: [what to research]
        driver: [which critical driver it relates to]
        questions: [specific questions]
        priority: [urgent / standard]
    critical_driver_update: [change to assessment, or "none"]
    ---END---

    BLOCK 2 — UPDATED STATUS FILE:
    Complete updated status.md incorporating this session.
    Label: "UPDATED STATUS FILE — save to Drive
    File ID: {status_file_id}"

    BLOCK 3 — MASTER BRIEF DELTA (only if Trigger 2 fired):
    Updated sections only.
    Label: "MASTER BRIEF DELTA — paste into {deal_id}_master_brief.md
    File ID: {brief_file_id}"

    BLOCK 4 — SESSION NARRATIVE:
    One paragraph per major topic: question, analysis, conclusion,
    what was rejected and why, key counterparty quotes.
    Label: "SESSION NARRATIVE — append to master brief Part VII"

    BLOCK 5 -- SESSION OUTPUT SAVE (automatic, no prompt needed):
    If this session produced a named deliverable (jsx, html, analysis,
    memo, brief, transcript_summary, research_report, or deal_update),
    emit one block per deliverable:

    ---SESSION-OUTPUT---
    deal: <deal_id>
    date: YYYY-MM-DD
    type: <type>
    title: <title>
    description: <description (<=25 words)>
    artifact: <filename.ext>
    ---END-SESSION-OUTPUT---

    Valid types: jsx | html | analysis | memo | brief | transcript_summary |
                 research_report | deal_update

    The capture pipeline reads this block every 4h and updates
    session_log.md + dashboard_entry.json in Drive automatically.
    Do NOT attempt Drive file uploads -- large files cannot be
    reliably transmitted via claude.ai Drive tools.

    Execute automatically. Emit for every named deliverable.
    Multiple blocks fine (one per deliverable).


    ════════════════════════════════
    ANALYTICAL RULES
    ════════════════════════════════

    Universal:
    - Do not redo prior analytical work — build on it
    - Flag document conflicts explicitly — never overwrite silently
    - Always state which document a fact comes from
    - {FIRM_SHORT} economics must be formalized before any external outreach
    - Do not reverse established conclusions without flagging
    - Timeline assumptions = best-case unless source says otherwise

    Deal-specific:
    {rules_text}


    ════════════════════════════════
    DRAFTING RULES
    ════════════════════════════════

    Universal:
    - Never reference {FIRM_SHORT} fees in external drafts until formalized
    - Never attribute conclusions to named sources externally
    - Never commit to development-stage timelines as contracted
    - Always ask: "Who else might read this?" before finalizing

    Deal-specific:
    {rules_text}
    """).strip()

# ── LOCAL FILE OPERATIONS ─────────────────────────────────────────────────────

def update_compile_writeback(deal_id, file_id):
    """Add new deal to compile_drive_writeback.py."""
    if not COMPILE_WRITEBACK.exists():
        print(f"   ⚠️  {COMPILE_WRITEBACK} not found — skipping")
        return

    content = COMPILE_WRITEBACK.read_text()
    marker = "    # ADD NEW DEAL HERE:"

    if deal_id in content:
        print(f"   ⚠️  {deal_id} already in compile_drive_writeback.py — skipping")
        return

    if marker in content:
        new_line = f"    '{deal_id}': '{file_id}',\n    {marker}"
        content = content.replace(marker, new_line)
        COMPILE_WRITEBACK.write_text(content)
        print(f"   ✓ Added {deal_id} to compile_drive_writeback.py")
    else:
        print(f"   ⚠️  Could not find insertion marker in compile_drive_writeback.py")
        print(f"       Add manually: '{deal_id}': '{file_id}'")


def update_deal_system_data(deal_id, deal_name, lead, support,
                             drive_folder_id, status_file_id, brief_file_id,
                             outputs_folder_id='', session_log_file_id='',
                             transcripts_folder_id='', organizer_aliases=None):
    """Add new deal entry to deal-system-data.json."""
    if not DEAL_SYSTEM_DATA.exists():
        data = {"deals": []}
    else:
        data = json.loads(DEAL_SYSTEM_DATA.read_text())

    if organizer_aliases is None:
        organizer_aliases = _default_aliases(deal_id, deal_name)

    # 2026-05-21 (L0049/DR1 fix): If a same-deal_id entry already exists, it's
    # almost certainly a stub from a prior failed /new-deal run whose Drive
    # folder is now orphaned. Overwriting it with the IDs from THIS run is
    # the correct behavior — the alternative (silently skipping) leaves the
    # registry pointed at orphan Drive folders while THIS run creates a new
    # set of folders that never get registered. We preserve any user-set
    # fields the stub may have (organizer_aliases, project_url) — write-once,
    # not regen-from-scratch.
    existing_idx = None
    existing = None
    for i, d in enumerate(data.get("deals", [])):
        if d.get("deal_id") == deal_id:
            existing_idx = i
            existing = d
            break

    new_deal = {
        "deal_id": deal_id,
        "name": deal_name,
        "stage": (existing or {}).get("stage", "early"),
        "lead": lead,
        "support": support,
        "organizer_aliases": (existing or {}).get("organizer_aliases", organizer_aliases),
        "drive_folder_id": drive_folder_id,
        "transcripts_folder_id": transcripts_folder_id,
        "status_file_id": status_file_id,
        "brief_file_id": brief_file_id,
        "outputs_folder_id": outputs_folder_id,
        "session_log_file_id": session_log_file_id,
        "project_url": (existing or {}).get("project_url"),
        "created": (existing or {}).get("created", TODAY),
        "last_session": TODAY,
    }

    if existing_idx is not None:
        # Capture orphan IDs in a sibling field so cleanup can find them.
        orphan_ids = {
            k: existing.get(k) for k in (
                "drive_folder_id", "status_file_id", "brief_file_id",
                "outputs_folder_id", "session_log_file_id", "transcripts_folder_id"
            ) if existing.get(k) and existing.get(k) != locals().get(k)
        }
        if orphan_ids:
            new_deal["_orphan_ids_pending_cleanup"] = orphan_ids
            print(f"   ⚠️  {deal_id} stub existed — overwriting with new IDs. "
                  f"Orphan IDs flagged for cleanup: {list(orphan_ids.keys())}")
        data["deals"][existing_idx] = new_deal
        print(f"   ✓ Overwrote {deal_id} in deal-system-data.json")
    else:
        data.setdefault("deals", []).append(new_deal)
        print(f"   ✓ Added {deal_id} to deal-system-data.json")

    DEAL_SYSTEM_DATA.write_text(json.dumps(data, indent=2))


def update_sync_state(deal_id, deal_name):
    """Add new deal to sync-state.json."""
    if not SYNC_STATE.exists():
        state = {}
    else:
        state = json.loads(SYNC_STATE.read_text())

    if deal_id not in state:
        state[deal_id] = {
            "project_url": None,
            "last_session_date": TODAY,
            "session_summary": "Initial setup",
        }
        SYNC_STATE.write_text(json.dumps(state, indent=2))
        print(f"   ✓ Added {deal_id} to sync-state.json")


def update_firm_context_pipeline(service, new_deal_id=None, new_deal_name=None,
                                  new_lead=None, new_support=None, new_stage="Early"):
    """Upsert a deal row in the ACTIVE DEAL PIPELINE table in tcip_firm_context.md.
    Existing rows are preserved. Only the new/updated deal row is added or refreshed."""
    import io as _io, re as _re
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

    if not new_deal_id:
        print("   ⚠️  No deal_id provided — skipping firm context update")
        return

    _principal_first = PRINCIPAL_NAME.split()[0] if PRINCIPAL_NAME else ""
    _team_first = TEAM_NAMES[0].split()[0] if TEAM_NAMES else ""
    yoni_role = "Lead" if new_lead and _principal_first and _principal_first in new_lead else (
        "Support" if new_support and _principal_first and _principal_first in new_support else "—")
    mark_role = "Lead" if new_lead and _team_first and _team_first in new_lead else (
        "Support" if new_support and _team_first and _team_first in new_support else "—")
    new_row = f"| **{new_deal_name}** | {new_stage} | {yoni_role} | {mark_role} | `{new_deal_id}_context.md` |"

    # Download current file from Drive
    try:
        buf = _io.BytesIO()
        dl = service.files().get_media(fileId=FIRM_CONTEXT_DRIVE_ID)
        MediaIoBaseDownload(buf, dl).next_chunk()
        content = buf.getvalue().decode("utf-8")
    except Exception as e:
        print(f"   ⚠️  Could not download firm context from Drive: {e}")
        return

    # Find the pipeline table block
    table_match = _re.search(
        r"(## ACTIVE DEAL PIPELINE.*?\n\|[-| ]+\|)(.*?)(?=\n## |\Z)",
        content, flags=_re.DOTALL
    )
    if not table_match:
        print("   ⚠️  Could not find pipeline table in firm context — skipping")
        return

    header = table_match.group(1)
    body   = table_match.group(2)      # existing data rows as a block
    rows   = [r for r in body.split("\n") if r.strip().startswith("|")]

    # Check if deal already has a row (match on deal_id anywhere in the row)
    found = False
    updated_rows = []
    for row in rows:
        if f"`{new_deal_id}_context.md`" in row or f"**{new_deal_name}**" in row:
            updated_rows.append(new_row)   # replace with updated row
            found = True
        else:
            updated_rows.append(row)
    if not found:
        updated_rows.append(new_row)       # append new deal

    new_table_block = header + "\n" + "\n".join(updated_rows) + "\n"

    new_content = (
        content[: table_match.start()]
        + new_table_block
        + content[table_match.end() :]
    )

    # Update Last updated line
    new_content = _re.sub(
        r"\*\*Last updated:\*\* .*",
        f"**Last updated:** {TODAY}",
        new_content,
    )

    # Upload back to Drive
    try:
        media = MediaIoBaseUpload(
            _io.BytesIO(new_content.encode()), mimetype="text/plain"
        )
        service.files().update(fileId=FIRM_CONTEXT_DRIVE_ID, media_body=media).execute()
        action = "updated" if found else "added"
        print(f"   ✓ firm_context.md — deal row {action} in Drive")
    except Exception as e:
        print(f"   ⚠️  Could not upload firm context to Drive: {e}")
        return

    # Also update local copy if it exists
    for lpath in [Path.home() / "Downloads" / "files" / "firm_context.md", FIRM_CONTEXT_PATH]:
        if lpath.exists():
            lpath.write_text(new_content, encoding="utf-8")
            print(f"   ✓ Local copy updated: {lpath}")


def create_local_data_folders(deal_id):
    """Create local data/project-sync/[deal_id]/ folder and JSON files."""
    deal_dir = DATA_DIR / deal_id
    deal_dir.mkdir(parents=True, exist_ok=True)

    for fname in ["decisions_log.json", "draft_queue.json", "research_queue.json"]:
        fpath = deal_dir / fname
        if not fpath.exists():
            fpath.write_text("[]")

    print(f"   ✓ Created local data folders: {deal_dir}")


def save_instructions_locally(deal_id, instructions):
    """Save generated Project instructions to local file for reference."""
    out = DATA_DIR / deal_id / "project_instructions.txt"
    out.write_text(instructions)
    print(f"   ✓ Saved Project instructions to {out}")
    return out

# ── CLAUDE CODE ANALYSIS (CLAUDE MAX) ────────────────────────────────────────

def _find_claude_bin():
    pattern = str(Path.home() / 'Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude')
    candidates = sorted(glob.glob(pattern))
    if candidates:
        return candidates[-1]
    return shutil.which('claude')

def _extract_between(text, start_marker, end_marker):
    start_idx = text.find(start_marker)
    end_idx   = text.find(end_marker)
    if start_idx == -1 or end_idx == -1:
        return None
    return text[start_idx + len(start_marker):end_idx].strip()

def run_analysis_via_claude_code(deal_name, deal_id, lead, support, docs):
    """Run Critical Driver Framework + generate .md content via Claude Code CLI.
    Uses Claude Max quota — no API spend. Returns (status_content, brief_content)
    or (None, None) if claude binary not found or times out."""

    claude_bin = _find_claude_bin()
    if not claude_bin:
        print("   ⚠️  claude CLI not found — skipping AI analysis.")
        print("      Templates written to Drive. Run first session to fill them in.")
        return None, None

    firm_context = ""
    if FIRM_CONTEXT_PATH.exists():
        firm_context = FIRM_CONTEXT_PATH.read_text(encoding="utf-8")

    docs_section = ""
    if docs:
        docs_section = "\n\nDEAL DOCUMENTS PROVIDED:\n"
        for d in docs:
            docs_section += f"\n--- {d['name']} ---\n{d['content']}\n"
    else:
        docs_section = ("\n\nNo local deal documents provided. "
                        "Generate framework based on deal name and firm context only. "
                        "Mark all sections as preliminary.")

    support_line = f"Support: {support}" if support else "Support: none assigned"

    prompt = f"""You are onboarding a new infrastructure deal for {FIRM_SHORT} ({FIRM_NAME}).

Deal: {deal_name}
Deal ID: {deal_id}
Lead: {lead}
{support_line}

FIRM CONTEXT:
{firm_context}
{docs_section}

Complete all three tasks in order. Be specific — use named assets, firms, dollar amounts, and dates wherever the documents support them. No generic placeholders.

TASK 1 — CRITICAL DRIVER FRAMEWORK
Output the full Critical Driver analysis for {deal_name}.

Format:
critical_driver: [single sentence — the one thing that determines whether this deal works]
return_driver: [primary return mechanism — yield, spread, multiple, IRR]
key_risk: [single biggest risk to the thesis]
diligence_priority: [most important thing to verify first]
right_to_win: [why {FIRM_SHORT} specifically, not a larger fund]

TASK 2 — STATUS.MD
Generate the complete status.md content. Wrap it exactly like this:

===STATUS_START===
# {deal_name} — Status
Deal ID: {deal_id}
Lead: {lead} | {support_line}
Last updated: {TODAY}

## Critical Driver
[paste critical_driver from Task 1]

## Hard Deadlines
| Date | Item | Owner |
|------|------|-------|
[fill from documents or mark as TBD]

## Open Items
| # | Item | Owner | Due |
|---|------|-------|-----|
| 1 | Complete diligence priority | {lead} | TBD |

## Counterparties
| Name | Firm | Role |
|------|------|------|
[fill from documents]

## Session Log
| Date | Summary |
|------|---------|
| {TODAY} | Deal onboarded via {FIRM_SHORT} system. Critical Driver complete. |
===STATUS_END===

TASK 3 — MASTER_BRIEF.MD
Generate the complete master_brief.md using the six-section memo structure. Wrap it exactly like this:

===BRIEF_START===
# {deal_name} — Master Brief
Deal ID: {deal_id}
Lead: {lead} | {support_line}
Last updated: {TODAY}

## THE CORE ARGUMENT
[1-2 paragraphs. Lead with the investment thesis.]

## POINTS OF CONSENSUS
[Bullet points. Named facts with conviction.]

## POINTS OF DISAGREEMENT OR TENSION
[Bullet points. Where the thesis could break.]

## OPEN QUESTIONS AND UNRESOLVED ISSUES
[Bullet points. What is unknown or pending.]

## WHAT YOU WOULD NEED TO FORM A VIEW
[Bullet points. Specific diligence, data, expert calls needed.]

## KEY NAMES AND FIRMS
[Every person and organization named — one line each.]
===BRIEF_END===
"""

    print("   Running Claude Code (Claude Max quota — no API cost)...")
    try:
        result = subprocess.run(
            [claude_bin, '--dangerously-skip-permissions', '--print', prompt],
            capture_output=True, text=True, timeout=300,
        )
        output = result.stdout or ""
        if result.returncode != 0:
            print(f"   ⚠️  Claude Code exited {result.returncode}. Using templates.")
            if result.stderr:
                print(f"      {result.stderr[:300]}")
            return None, None

        status_content = _extract_between(output, "===STATUS_START===", "===STATUS_END===")
        brief_content  = _extract_between(output, "===BRIEF_START===",  "===BRIEF_END===")

        if not status_content or not brief_content:
            print("   ⚠️  Could not parse Claude output — marker not found. Using templates.")
            return None, None

        return status_content, brief_content

    except subprocess.TimeoutExpired:
        print("   ⚠️  Claude Code timed out (5 min). Using templates.")
        return None, None
    except Exception as e:
        print(f"   ⚠️  Claude Code error: {e}. Using templates.")
        return None, None

# ── DRIVE REGISTRY ────────────────────────────────────────────────────────────

def _default_aliases(deal_id, deal_name):
    """Derive filename-matching aliases from deal_id and deal_name.

    These go into organizer_aliases in deal-system-data.json and are read
    by the Drive Organizer at runtime so new deals are auto-handled without
    any manual script edits.
    """
    aliases = [deal_id]
    # Add dot-separated version of name words (e.g. "black_bayou" → "black.bayou")
    words = [w.lower() for w in re.findall(r'[a-zA-Z]+', deal_name) if len(w) > 1]
    compound = '.'.join(words)
    if compound and compound != deal_id and compound not in aliases:
        aliases.append(compound)
    # Add underscore variant if different (e.g. "align_infra")
    underscore = '_'.join(words)
    if underscore and underscore != deal_id and underscore not in aliases:
        aliases.append(underscore)
    # Deduplicate preserving order
    seen = set()
    return [a for a in aliases if not (a in seen or seen.add(a))]


def upload_drive_registry(service, data_path=None):
    """Rebuild tcip-deals-registry.json from deal-system-data.json and
    upload/update it in Drive TC_CONTEXT folder.

    Called automatically at end of new deal onboarding.
    Also callable standalone: python tcip_new_deal.py --rebuild-registry
    """
    if data_path is None:
        data_path = DEAL_SYSTEM_DATA
    if not data_path.exists():
        print(f"   ⚠️  {data_path} not found — skipping registry upload")
        return

    data = json.loads(data_path.read_text())
    registry_deals = []
    for d in data.get("deals", []):
        if d.get("stage") in ("inactive", "closed"):
            continue
        # Display name: strip parenthetical (e.g. "Cholla / Venus" → "Cholla")
        display_name = re.sub(r'\s*/\s*.*', '', d.get("name", d["deal_id"])).strip()
        aliases = d.get("organizer_aliases")
        if not aliases:
            aliases = _default_aliases(d["deal_id"], d.get("name", d["deal_id"]))
        registry_deals.append({
            "deal_id":              d["deal_id"],
            "display_name":         display_name,
            "aliases":              aliases,
            "root_folder_id":       d.get("drive_folder_id", ""),
            "transcripts_folder_id":d.get("transcripts_folder_id", ""),
            "outputs_folder_id":    d.get("outputs_folder_id", ""),
        })

    registry = {"updated": TODAY, "deals": registry_deals}
    registry_json = json.dumps(registry, indent=2)

    # Find existing file or create new
    results = service.files().list(
        q=(f"name='{DRIVE_REGISTRY_FILE}' and "
           f"'{TC_CONTEXT_FOLDER_ID}' in parents and trashed=false"),
        fields="files(id)"
    ).execute()
    media = MediaInMemoryUpload(registry_json.encode("utf-8"),
                                mimetype="application/json",
                                resumable=False)
    if results.get("files"):
        file_id = results["files"][0]["id"]
        service.files().update(fileId=file_id, media_body=media).execute()
        print(f"   ✓ Updated {DRIVE_REGISTRY_FILE} in Drive TC_CONTEXT "
              f"({len(registry_deals)} deals)")
    else:
        meta = {"name": DRIVE_REGISTRY_FILE,
                "parents": [TC_CONTEXT_FOLDER_ID]}
        f = service.files().create(body=meta, media_body=media,
                                   fields="id").execute()
        print(f"   ✓ Created {DRIVE_REGISTRY_FILE} in Drive TC_CONTEXT "
              f"— {f['id']} ({len(registry_deals)} deals)")


# ── DEAL MODEL SCAFFOLD (XLSX + builder script + registry entry) ─────────────
#
# Architecture (per ~/cos-pipeline-config-tomac/config/deal_registry.json _meta):
#   - Model:    XLSX with named ranges (Inputs / Engine / Outputs tabs)
#   - Deck:     GENERATED, not stored — deck_base.py + build_deck_<deal>.py
#               rebuild the PPTX from the model each time `tcip rebuild` runs
#   - Refresh:  `tcip "natural language"` → update_deck.py → Claude → rebuild
#
# So this function does NOT clone any PPTX template and does NOT patch any OLE
# link. The model→deck connection is the Python pipeline above; the deck file
# only exists after the first `tcip --deal <deal_id> rebuild`.
#
# What it DOES do (matching deal_registry.json _meta.new_deal_instructions):
#   1. Copy TCIP_Deal_Model_Template.xlsx → ~/dashboards/data/deals/<deal_id>/profit-model.xlsx
#      (local — read by compile-dashboard.py)
#   2. Copy build_deck_fit.py → build_deck_<deal_id>.py
#      (stub — deal-specific customization lands here via `tcip` later)
#   3. Upload one archive copy of the model to Drive _Outputs/ as
#      <deal_id>_Model_v1.xlsx
#   4. Add an entry to deal_registry.json so `tcip --deal <deal_id> rebuild`
#      finds the right model + script
#
# Earlier iterations of this function tried to clone a linked PPTX+XLSX pair
# from Drive _Claude Context and patch the .rels XML — based on a misread of
# DRIVE-RECOMMENDATIONS.md §8 (now corrected in that doc's header).


def _drive_upload_binary(service, local_path, parent_id, mimetype, name=None):
    """Upload local_path to Drive under parent_id with given mimetype. Returns (id, webViewLink)."""
    from googleapiclient.http import MediaFileUpload
    local_path = Path(local_path)
    meta = {"name": name or local_path.name, "parents": [parent_id]}
    media = MediaFileUpload(str(local_path), mimetype=mimetype, resumable=False)
    f = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
    return f["id"], f.get("webViewLink", "")


def _register_deal_in_registry(deal_id, model_path, pptx_path, script_path,
                                aliases=None, registry_path=None):
    """Add or update a deal entry in deal_registry.json. Idempotent.

    Skipped (with a printed note) if the registry file doesn't exist — the
    public-repo install may not have the private tenant registry yet.
    """
    registry_path = Path(registry_path) if registry_path else DEAL_REGISTRY_PATH
    if not registry_path.exists():
        print(f"   ℹ  Registry not found at {registry_path}; skipping registration.")
        print(f"      (This is normal for a public-repo install without the private tenant config.)")
        return False
    reg = json.loads(registry_path.read_text())
    aliases = aliases or [deal_id]
    reg[deal_id] = {
        "model":   str(model_path),
        "pptx":    str(pptx_path),
        "script":  str(script_path),
        "aliases": aliases,
    }
    registry_path.write_text(json.dumps(reg, indent=2) + "\n")
    print(f"   ✓ Registered '{deal_id}' in {registry_path.name}")
    return True


def scaffold_deal_model(deal_id, deal_name, outputs_folder_id, drive_service,
                        upload=True, mirror_local=True, register=True,
                        aliases=None,
                        model_template=None,
                        builder_template=None):
    """Scaffold a new deal's model + builder script + registry entry.

    Per deal_registry.json _meta.new_deal_instructions:
      1. Copy TCIP_Deal_Model_Template.xlsx → <deal_id>_Model_v1.xlsx
      2. Populate Inputs tab + build Engine/Outputs (manual, follow-up)
      3. Copy build_deck_fit.py → build_deck_<deal_id>.py
      4. Register paths in deal_registry.json
      5. Run: tcip --deal <deal_id> rebuild

    No PPTX template is cloned — the deck is generated on demand.

    Args:
        deal_id:           slug used in filenames and registry keys.
        deal_name:         human-readable (used only for log lines).
        outputs_folder_id: Drive _Outputs/ folder ID for the deal (where the
                           archive XLSX copy lands). Pass any string when
                           upload=False.
        drive_service:     authed Drive v3 service (or None when upload=False).
        upload:            if False, skip Drive upload (smoke-test path).
        mirror_local:      if True, drop the model at
                           ~/dashboards/data/deals/<deal_id>/profit-model.xlsx
                           so compile-dashboard.py picks it up.
        register:          if True, add an entry to deal_registry.json.
        aliases:           list of registry aliases (defaults to [deal_id]).
        model_template:    override DEAL_MODEL_TEMPLATE_PATH (test injection).
        builder_template:  override DEAL_BUILDER_TEMPLATE_PATH (test injection).

    Returns a dict with paths, new Drive ID (if uploaded), and registration status.
    """
    import shutil

    model_template = Path(model_template) if model_template else DEAL_MODEL_TEMPLATE_PATH
    builder_template = Path(builder_template) if builder_template else DEAL_BUILDER_TEMPLATE_PATH

    if not model_template.exists():
        raise FileNotFoundError(
            f"Model template not found: {model_template}. "
            f"Set TCIP_MODEL_TEMPLATE env var or check the path in deal_registry.json _meta."
        )
    if not builder_template.exists():
        raise FileNotFoundError(
            f"Builder template not found: {builder_template}. "
            f"Set TCIP_BUILDER_TEMPLATE env var or check the path in deal_registry.json _meta."
        )

    new_model_name   = f"{deal_id}_Model_v1.xlsx"
    new_deck_name    = f"{deal_id}_Deck_v1.pptx"        # generated later by `tcip rebuild`
    new_builder_name = f"build_deck_{deal_id}.py"

    builder_dest    = builder_template.parent / new_builder_name
    local_data_dir  = Path.home() / "dashboards" / "data" / "deals" / deal_id
    local_mirror    = local_data_dir / "profit-model.xlsx"
    local_pptx_out  = local_data_dir / new_deck_name    # `tcip rebuild` writes here

    result = {
        "deal_id":          deal_id,
        "deal_name":        deal_name,
        "model_template":   str(model_template),
        "builder_template": str(builder_template),
        "new_model_name":   new_model_name,
        "new_deck_name":    new_deck_name,
        "new_builder_name": new_builder_name,
        "builder_dest":     str(builder_dest),
        "local_mirror":     None,
        "local_pptx_out":   str(local_pptx_out),
        "model_drive_id":   None,
        "model_drive_url":  None,
        "builder_copied":   False,
        "registered":       False,
        "uploaded":         False,
    }

    # 1. Copy the builder script (always — it's local; idempotent).
    if builder_dest.exists():
        print(f"   ℹ  Builder script already exists: {builder_dest.name} (leaving in place)")
    else:
        shutil.copy(builder_template, builder_dest)
        print(f"   ✓ Copied builder template → {builder_dest.name}")
        print(f"     Next: customize FIT_INPUT_NAMES, _fit_compute_fn, slide builders for {deal_id}")
        print(f"           (or run: tcip \"build out the {deal_id} deck per the model\")")
        result["builder_copied"] = True

    # 2. Mirror XLSX locally for compile-dashboard.py + tcip rebuild.
    if mirror_local:
        local_data_dir.mkdir(parents=True, exist_ok=True)
        if local_mirror.exists():
            print(f"   ℹ  Local model mirror already exists: {local_mirror}")
        else:
            shutil.copy(model_template, local_mirror)
            print(f"   ✓ Local model mirror → {local_mirror}")
        result["local_mirror"] = str(local_mirror)

    # 3. Upload an archive copy to Drive _Outputs/ — bracketed by coordination
    #    lock to serialize against /deal-sync and sync_registry.py.
    if upload:
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            from coordination import lock as _coordination_lock
        except Exception as e:
            raise RuntimeError(f"Could not import coordination lock helper: {e}")
        holder = f"tcip_new_deal.py:scaffold_deal_model:{deal_id}"
        # Stage a per-deal-named copy so Drive shows the new filename.
        upload_tmp = local_data_dir / new_model_name
        if not upload_tmp.exists():
            shutil.copy(model_template, upload_tmp)
        with _coordination_lock("drive-docs.yaml", holder=holder,
                                ttl_seconds=300, timeout_seconds=120):
            xlsx_id, xlsx_url = _drive_upload_binary(
                drive_service, upload_tmp, outputs_folder_id, XLSX_MIME, name=new_model_name
            )
            print(f"   ✓ Uploaded XLSX → {xlsx_id}")
        result["model_drive_id"]  = xlsx_id
        result["model_drive_url"] = xlsx_url
        result["uploaded"]        = True
    else:
        print(f"   ℹ  upload=False — skipping Drive upload (smoke test)")

    # 4. Register in deal_registry.json so `tcip --deal <deal_id>` resolves.
    if register:
        result["registered"] = _register_deal_in_registry(
            deal_id,
            model_path=local_mirror,
            pptx_path=local_pptx_out,
            script_path=builder_dest,
            aliases=aliases or [deal_id],
        )

    return result


# ── INTERACTIVE INPUT ─────────────────────────────────────────────────────────

def prompt_interactive():
    print("\n" + "═" * 60)
    print("  {FIRM_SHORT} NEW DEAL SETUP")
    print("═" * 60)

    deal_name = input("\nDeal name (e.g. 'Lakeview Wind Farm'): ").strip()
    deal_id = input("Deal ID (e.g. 'black_bayou', lowercase, underscores): ").strip()
    _default_lead = PRINCIPAL_NAME
    _default_support = TEAM_NAMES[0] if TEAM_NAMES else ""
    lead = input(f"Lead principal [{_default_lead}]: ").strip() or _default_lead
    support_default = _default_support if lead != _default_support else (TEAM_NAMES[1] if len(TEAM_NAMES) > 1 else "")
    support = input(f"Support principal [{support_default}]: ").strip() or support_default
    docs_folder = input("Path to deal documents folder (or press Enter to skip): ").strip()
    drive_folder_id = input("Existing Drive folder ID (or press Enter to create new): ").strip()

    print("\nTriggers to add (comma-separated, or press Enter for none):")
    print("  Options: cash_runway, regulatory_vote, disintermediation,")
    print("           process_deadline, relationship_tension, market_news")
    triggers_input = input("Triggers: ").strip()
    triggers = [t.strip() for t in triggers_input.split(",") if t.strip()] if triggers_input else []

    print("\nRules to add (comma-separated, or press Enter for none):")
    print("  Options: regulated_asset, development_asset, relationship_sensitive")
    rules_input = input("Rules: ").strip()
    rules = [r.strip() for r in rules_input.split(",") if r.strip()] if rules_input else []

    return {
        "deal_name": deal_name,
        "deal_id": deal_id,
        "lead": lead,
        "support": support,
        "docs_folder": docs_folder or None,
        "drive_folder_id": drive_folder_id or None,
        "triggers": triggers,
        "rules": rules,
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=f"{FIRM_SHORT} New Deal Setup")
    parser.add_argument("--deal-name", help="Full deal name")
    parser.add_argument("--deal-id", help="Short deal ID (lowercase, underscores)")
    parser.add_argument("--lead", help="Lead principal")
    parser.add_argument("--support", help="Support principal")
    parser.add_argument("--docs-folder", help="Path to deal documents")
    parser.add_argument("--drive-folder-id", help="Existing Drive folder ID")
    parser.add_argument("--triggers", nargs="*", default=[])
    parser.add_argument("--rules", nargs="*", default=[])
    parser.add_argument("--rebuild-registry", action="store_true",
                        help="Rebuild and upload tcip-deals-registry.json from "
                             "deal-system-data.json without creating a new deal")
    parser.add_argument("--smoke-test-scaffold", action="store_true",
                        help="Run scaffold_deal_model() with deal_id='test_deal' "
                             "and upload=False, register=False; verifies template "
                             "copy + builder clone + local mirror plumbing, then "
                             "prints the result and exits without writing Drive or registry.")
    args = parser.parse_args()

    # Standalone registry rebuild — no deal creation needed
    if args.rebuild_registry:
        print("📡 Connecting to Google Drive...")
        drive = get_drive_service()
        print("\n─────────────────────────────────")
        print("Rebuilding Drive Registry")
        print("─────────────────────────────────")
        upload_drive_registry(drive)
        print("\nDone. Drive Organizer will auto-load all deals on next run.")
        return

    # Standalone scaffold smoke test — no Drive write, no registry change.
    if args.smoke_test_scaffold:
        print("\n─────────────────────────────────")
        print("SMOKE TEST — scaffold_deal_model(deal_id='test_deal', upload=False, register=False)")
        print("─────────────────────────────────")
        result = scaffold_deal_model(
            deal_id="test_deal",
            deal_name="Test Deal (smoke)",
            outputs_folder_id="UNUSED_NO_UPLOAD",
            drive_service=None,
            upload=False,
            mirror_local=True,
            register=False,
        )
        print("\n─── RESULT ──────────────────────")
        print(json.dumps(result, indent=2))
        if not (Path(result["local_mirror"] or "/nonexistent").exists()
                and Path(result["builder_dest"]).exists()):
            print("\n⚠️  Smoke test produced expected paths but files are missing on disk.")
            sys.exit(2)
        print("\n✅ Smoke test passed: model template + builder script in place.")
        print(f"   To clean up: rm -r ~/dashboards/data/deals/test_deal "
              f"&& rm {result['builder_dest']}")
        return

    # Get inputs
    if args.deal_name and args.deal_id:
        config = {
            "deal_name": args.deal_name,
            "deal_id": args.deal_id,
            "lead": args.lead or PRINCIPAL_NAME,
            "support": args.support or "",
            "docs_folder": args.docs_folder,
            "drive_folder_id": args.drive_folder_id,
            "triggers": args.triggers,
            "rules": args.rules,
        }
    else:
        config = prompt_interactive()

    deal_name = config["deal_name"]
    deal_id = config["deal_id"]
    lead = config["lead"]
    support = config["support"]
    docs_folder = config.get("docs_folder")
    existing_drive_folder_id = config.get("drive_folder_id")
    triggers = config.get("triggers", [])
    rules = config.get("rules", [])

    print(f"\n{'═' * 60}")
    print(f"  Setting up: {deal_name} ({deal_id})")
    print(f"  Lead: {lead} | Support: {support}")
    print(f"{'═' * 60}\n")

    # Init services
    print("📡 Connecting to Google Drive...")
    drive = get_drive_service()

    # ── PHASE 1: DRIVE FOLDER STRUCTURE ──────────────────────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 1 — Google Drive Setup")
    print("─────────────────────────────────")

    if existing_drive_folder_id:
        drive_folder_id = existing_drive_folder_id
        drive_folder_name = deal_name
        print(f"   ✓ Using existing folder: {drive_folder_id}")
    else:
        print(f"   Creating folder: {deal_name} (under TC_DEALS)")
        drive_folder_id = create_drive_folder(drive, deal_name, parent_id=TC_DEALS_FOLDER_ID)
        drive_folder_name = deal_name
        print(f"   ✓ Created: {drive_folder_id}")

    subfolder_ids = {}
    for subfolder in DEAL_SUBFOLDERS:
        sfid = create_drive_folder(drive, subfolder, drive_folder_id)
        subfolder_ids[subfolder] = sfid
        print(f"   ✓ Subfolder: {subfolder}/")
    transcripts_folder_id = subfolder_ids.get("Transcripts", "")

    # _Outputs/ — session deliverables go here, one Google Doc per output
    outputs_folder_id = create_drive_folder(drive, "_Outputs", drive_folder_id)
    print(f"   ✓ Subfolder: _Outputs/ — {outputs_folder_id}")

    log_header = (
        f"# {deal_name} — Session Output Log\n\n"
        f"| Date | Type | Title | Description | File |\n"
        f"|------|------|-------|-------------|------|\n"
    )
    session_log_file_id, _ = create_drive_text_file(
        drive, "session_log.md", log_header, outputs_folder_id
    )
    print(f"   ✓ session_log.md — {session_log_file_id}")

    # ── PHASE 2: CREATE TEMPLATE .md FILES IN DRIVE ──────────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 2 — Drive .md File Setup")
    print("─────────────────────────────────")

    status_content = _status_template(deal_name, deal_id, lead, support)
    master_content = _master_brief_template(deal_name, deal_id, lead, support)

    status_title = f"{deal_id}_status.md"
    brief_title  = f"{deal_id}_master_brief.md"

    status_id, status_url = create_drive_doc(drive, status_title, status_content, drive_folder_id)
    brief_id, brief_url   = create_drive_doc(drive, brief_title, master_content, drive_folder_id)

    print(f"   ✓ Status file created — ID: {status_id}")
    print(f"     URL: {status_url}")
    print(f"   ✓ Master brief created — ID: {brief_id}")
    print(f"     URL: {brief_url}")

    # ── PHASE 3: AI ANALYSIS VIA CLAUDE CODE (CLAUDE MAX) ────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 3 — AI Analysis (Claude Max)")
    print("─────────────────────────────────")

    docs = []
    if docs_folder:
        docs = read_local_docs(docs_folder)
        print(f"   ✓ Loaded {len(docs)} local document(s)")

    ai_status, ai_brief = run_analysis_via_claude_code(
        deal_name, deal_id, lead, support, docs
    )

    if ai_status:
        update_drive_doc(drive, status_id, ai_status)
        print("   ✓ status.md filled with Critical Driver analysis")
    else:
        print("   ℹ  status.md written as template — fill in first Claude Project session")

    if ai_brief:
        update_drive_doc(drive, brief_id, ai_brief)
        print("   ✓ master_brief.md filled with six-section memo")
    else:
        print("   ℹ  master_brief.md written as template — fill in first Claude Project session")

    # ── PHASE 4: DEAL MODEL + BUILDER SCAFFOLD ───────────────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 4 — Deal Model + Builder Scaffold")
    print("─────────────────────────────────")
    deal_model = None
    try:
        deal_model = scaffold_deal_model(
            deal_id=deal_id,
            deal_name=deal_name,
            outputs_folder_id=outputs_folder_id,
            drive_service=drive,
            upload=True,
            mirror_local=True,
            register=True,
        )
        print(f"   ✓ Next:  tcip --deal {deal_id} rebuild   "
              f"(once Inputs tab is populated + builder customized)")
    except Exception as e:
        print(f"   ⚠️  scaffold_deal_model failed: {e}")
        print(f"      Deal onboarding continues; model can be scaffolded manually later.")

    # ── PHASE 5: UPDATE COMPILE SCRIPT ───────────────────────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 5 — Compile Script Update")
    print("─────────────────────────────────")
    update_compile_writeback(deal_id, status_id)

    # ── PHASE 6: UPDATE DEAL SYSTEM DATA ────────────────────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 6 — Deal Registry Update")
    print("─────────────────────────────────")
    update_deal_system_data(
        deal_id, deal_name, lead, support,
        drive_folder_id, status_id, brief_id,
        outputs_folder_id=outputs_folder_id,
        session_log_file_id=session_log_file_id,
        transcripts_folder_id=transcripts_folder_id,
    )
    update_sync_state(deal_id, deal_name)
    # Upload Drive registry so the Drive Organizer picks up this deal
    # automatically on its next morning run — no Apps Script edits needed.
    upload_drive_registry(drive)

    # ── PHASE 7: LOCAL DATA FOLDERS ──────────────────────────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 7 — Local Data Folders")
    print("─────────────────────────────────")
    create_local_data_folders(deal_id)

    # ── PHASE 8: GENERATE PROJECT INSTRUCTIONS ───────────────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 8 — Project Instructions")
    print("─────────────────────────────────")
    instructions = generate_project_instructions(
        deal_name, deal_id, lead, support,
        status_id, brief_id,
        drive_folder_name, triggers, rules,
        outputs_folder_id=outputs_folder_id,
        session_log_file_id=session_log_file_id,
    )
    instructions_path = save_instructions_locally(deal_id, instructions)

    # ── PHASE 9: UPDATE FIRM CONTEXT ─────────────────────────────────────────
    print("\n─────────────────────────────────")
    print("PHASE 9 — Firm Context Update")
    print("─────────────────────────────────")
    update_firm_context_pipeline(
        drive,
        new_deal_id=deal_id,
        new_deal_name=deal_name,
        new_lead=lead,
        new_support=support,
        new_stage="Early",
    )

    # ── PHASE 10: COMPLETION + BROWSER-STEP SIGNAL ───────────────────────────
    print(f"\n{'=' * 60}")
    print("  AUTOMATED SETUP COMPLETE")
    print(f"{'=' * 60}")
    print(f"""
DEAL: {deal_name} ({deal_id})

FILE IDs (permanent -- never change):
  Status:          {status_id}
  Master brief:    {brief_id}
  Drive folder:    https://drive.google.com/drive/folders/{drive_folder_id}
  Outputs folder:  {outputs_folder_id}
  session_log.md:  {session_log_file_id}

REMAINING MANUAL STEPS (~1 minute)
-------------------------------------
STEP 1 -- Create the Claude Project
  -> Go to: https://claude.ai
  -> Click Projects -> New Project
  -> Name it exactly: {FIRM_SHORT} -- {deal_name}
  -> Enable the Google Drive connector in Project settings
  -> Copy the Project URL when created

STEP 2 -- Give the Project URL to Claude Code
  Claude Code will automatically:
  - Paste the Project instructions (firm context read live
    from Drive -- no file upload needed)
  - Wire the Project URL to the dashboard registry
  - Confirm setup is complete

-------------------------------------
""")

    # Machine-readable signal for Claude Code to pick up
    print(f"""---BROWSER-STEP---
deal_id: {deal_id}
deal_name: {deal_name}
instructions_path: {instructions_path}
status_id: {status_id}
brief_id: {brief_id}
outputs_folder_id: {outputs_folder_id}
session_log_file_id: {session_log_file_id}
action: paste_project_instructions_and_wire_url
awaiting: project_url
---END---""")


if __name__ == "__main__":
    main()
