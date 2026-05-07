#!/usr/bin/env python3
"""
cos_transcript_hook.py — post-transcription action extractor
Reads a new call transcript from Google Drive, extracts follow-ups via
Claude Haiku (~$0.003/call), appends them to the COS follow-ups doc,
and triggers a dashboard warmup.

Called non-blocking (Popen) from call_recorder.py after each transcript
is written to Drive. Runs in ~5s, no latency impact on the recorder.

Usage:
    python3 cos_transcript_hook.py --doc-id DOC_ID --title "Call Title" --category auto
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _usage import log_usage  # noqa: E402

# ── Firm context (no hardcoded principal/firm references below this line) ──────
# Sibling pipeline dir is prepended to sys.path for shared firm/context modules
# when this hook runs from a different cwd. Path is derived, not tenant-coded.
_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))
import _firm_context as _fc  # noqa: E402
import _secrets  # noqa: E402
_CTX = _fc.load_firm_context()
_OWNERS          = _fc.owner_whitelist_str(_CTX)   # e.g. "<owner1>|<owner2>"
_DEAL_WS         = _fc.workstream_deal(_CTX)        # workstream label from firm_context
_PEER_FIRMS      = _fc.peer_firms_str(_CTX)
_PRINCIPAL_FIRST = _fc.principal_first_name(_CTX)
_DOCS            = _fc.load_drive_docs()

# ── Entity normalizer (non-fatal — hook must not block recording flow) ─────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _entity_normalizer import EntityNormalizer as _EN  # noqa: E402
    _NORMALIZER = _EN()

    def _normalize_hook_extraction(data: dict, norm: "_EN") -> int:
        """Resolve entity strings in hook extraction. Returns number of adjustments."""
        n = 0

        def _resolve(v: str) -> str:
            nonlocal n
            if not v or not isinstance(v, str):
                return v
            m = norm.match(v)
            if m.source == "phonetic" or (
                m.source in ("lp", "deal", "pipeline_target") and
                m.confidence in ("exact", "substring") and m.canonical != m.original
            ):
                n += 1
                return m.canonical
            if norm.is_vague(v):
                n += 1
                return f"[Unresolved — needs name] {v}"
            return v

        for action in data.get("action_items", []) or []:
            if isinstance(action, dict) and action.get("who"):
                action["who"] = _resolve(action["who"])
        for contact in data.get("new_contacts", []) or []:
            if isinstance(contact, dict):
                if contact.get("name"): contact["name"] = _resolve(contact["name"])
                if contact.get("firm"): contact["firm"] = _resolve(contact["firm"])
        for item in (data.get("deal_intel") or data.get("tomac_intel") or []):  # noqa: tenant-leak — backward-compat read of old key
            if isinstance(item, dict) and item.get("investor_or_firm"):
                item["investor_or_firm"] = _resolve(item["investor_or_firm"])
        for item in data.get("lp_updates", []) or []:
            if isinstance(item, dict) and item.get("lp_name"):
                item["lp_name"] = _resolve(item["lp_name"])
        return n

except Exception:
    _NORMALIZER = None  # type: ignore[assignment]
    def _normalize_hook_extraction(*_a, **_kw) -> int: return 0  # type: ignore[misc]


# Doc IDs — loaded from ~/dashboards/config/drive-docs.yaml.
# Hardcoded fallbacks ensure the script still runs if the YAML is missing.
FOLLOW_UPS_DOC      = _DOCS.get("followups",      "10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY")
PEOPLE_DOC          = _DOCS.get("people_crm",     "1ZCKnZlQgKD13dLsQNxCM_nRsTjz2DVitjeUWowUur0Y")
RECRUITING_DOC      = _DOCS.get("recruiting",     "1ZnTCVoA0ID7XTDFy27yDnrEVhBqx75kaTg_QXFq4eXA")
DEAL_PIPELINE_DOC   = _DOCS.get("deal_pipeline", _DOCS.get("tomac_pipeline", "1LHorixPs8ppwSvQzGfA_B6609YZA8dSpR4rmppENzpc"))  # noqa: tenant-leak
# Transcripts inbox — Zapier/call_recorder drops files here; Drive organizer
# routes them to deal folders only AFTER this hook moves them to _Ready/.
TRANSCRIPTS_FOLDER  = _DOCS.get("call_transcripts", "1B7UgpFCElgyZMLbq1yrf-N7PsB-UA4SE")
TOKEN_PATH          = Path.home() / "credentials/gcal_token.json"
PIPELINE_DATA_PATH  = Path.home() / "dashboards/data/compiled/deal-pipeline-data.json"
DASHBOARD_DATA_PATH = Path.home() / "dashboards/data/compiled/dashboard-data.json"
DEAL_SYSTEM_PATH    = Path.home() / "dashboards/data/compiled/deal-system-data.json"
DASHBOARD_URL       = "http://localhost:7777/warmup"
CLAUDE_MODEL        = "claude-sonnet-4-6"
# Resolves through keychain (Mac default) then env-var fallback per BOOTSTRAP_PLAN #2.
ANTHROPIC_KEY       = _secrets.load_secret("ANTHROPIC_API_KEY", "")


# ── Google OAuth ──────────────────────────────────────────────────────────────

def get_calendar_attendees(token):
    """Best-effort: return attendee names from calendar events in the past 2h.
    Fails silently — the hook continues without attendee context if this errors."""
    try:
        from datetime import timezone, timedelta
        now = datetime.now(timezone.utc)
        t_min = (now - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        t_max = (now + timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
        url = (
            "https://www.googleapis.com/calendar/v3/calendars/primary/events"
            f"?timeMin={urllib.parse.quote(t_min)}"
            f"&timeMax={urllib.parse.quote(t_max)}"
            "&singleEvents=true&orderBy=startTime&maxResults=5"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            events = json.loads(r.read()).get("items", [])
        names, seen = [], set()
        for event in events:
            for att in event.get("attendees", []):
                name = att.get("displayName") or att.get("email", "").split("@")[0]
                if name and name not in seen:
                    names.append(name)
                    seen.add(name)
        return names
    except Exception:
        return []


def refresh_token():
    with open(TOKEN_PATH) as f:
        creds = json.load(f)
    data = urllib.parse.urlencode({
        "client_id":     creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request(creds["token_uri"], data=data, method="POST")
    with urllib.request.urlopen(req) as r:
        creds["token"] = json.loads(r.read())["access_token"]
    with open(TOKEN_PATH, "w") as f:
        json.dump(creds, f)
    return creds["token"]


# ── Google Docs helpers ───────────────────────────────────────────────────────

def gdocs_get(token, doc_id):
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def doc_text(doc):
    out = ""
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    out += pe["textRun"].get("content", "")
    return out


def last_table_row_end(doc):
    """Return the endIndex of the last '| N | ...' paragraph in the doc."""
    last = 0
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            text = ""
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    text += pe["textRun"].get("content", "")
            if re.match(r"^\|\s*\d+\s*\|", text):
                last = elem.get("endIndex", last)
    # Fall back to end of doc minus sentinel
    if not last:
        content = doc.get("body", {}).get("content", [])
        last = (content[-1].get("endIndex", 2) - 1) if content else 1
    return last


def gdocs_insert(token, doc_id, index, text):
    batch = {"requests": [
        {"insertText": {"location": {"index": index, "segmentId": ""}, "text": text}}
    ]}
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate"
    data = json.dumps(batch).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def drive_get_or_create_folder(token, name, parent_id):
    """Return the ID of a subfolder, creating it if it doesn't exist."""
    safe = urllib.parse.quote(name.replace("'", r"\'"))
    q = urllib.parse.quote(
        f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
        f" and '{parent_id}' in parents and trashed=false"
    )
    url = f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id)&pageSize=5"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        files = json.loads(r.read()).get("files", [])
    if files:
        return files[0]["id"]
    body = json.dumps({
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }).encode()
    req = urllib.request.Request(
        "https://www.googleapis.com/drive/v3/files?fields=id",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["id"]


def drive_move(token, file_id, new_parent_id, old_parent_id):
    """Move a Drive file from old_parent_id to new_parent_id."""
    url = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}"
        f"?addParents={new_parent_id}&removeParents={old_parent_id}&fields=id"
    )
    req = urllib.request.Request(
        url, data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def next_row_num(fu_text):
    nums = re.findall(r"^\|\s*(\d+)\s*\|", fu_text, re.MULTILINE)
    return max((int(n) for n in nums), default=0) + 1


# ── Dashboard path reference (dynamic, loaded at call time) ──────────────────

def build_dashboard_path_reference():
    """Build a DASHBOARD PATH REFERENCE string from live dashboard data.
    All sections are loaded dynamically at call time — no hardcoded deal names."""
    active_deals   = []  # active portfolio (deal-system-data.json)
    lp_targets     = []
    pipeline_paths = []  # Deal Ideas scored targets (deal-pipeline-data.json)

    # Active TCIP deal portfolio
    if DEAL_SYSTEM_PATH.exists():
        try:
            ds = json.loads(DEAL_SYSTEM_PATH.read_text())
            for deal in ds.get("deals", []):
                name = deal.get("name", "?")
                if name:
                    active_deals.append((name, deal.get("stage", "?")))
        except Exception:
            pass

    # Fallback to dashboard-data.json tomac section if deal-system not available
    if not active_deals and DASHBOARD_DATA_PATH.exists():
        try:
            dash = json.loads(DASHBOARD_DATA_PATH.read_text())
            for item in dash.get(_DEAL_WS, dash.get("tomac", [])):  # noqa: tenant-leak — backward-compat read
                name = item.get("name", "?")
                if name and "Update Log" not in name:
                    active_deals.append((name, item.get("stage", "?")))
        except Exception:
            pass

    # LP targets
    if DASHBOARD_DATA_PATH.exists():
        try:
            dash = json.loads(DASHBOARD_DATA_PATH.read_text())
            for lp in dash.get("lpData", []):
                lp_targets.append((lp.get("name", "?"), lp.get("status", "?")))
        except Exception:
            pass

    # Deal Ideas pipeline (scored targets by theme)
    if PIPELINE_DATA_PATH.exists():
        try:
            data = json.loads(PIPELINE_DATA_PATH.read_text())
            for theme in data.get("themes", []):
                theme_label = theme.get("theme", "?")
                for t in theme.get("targets", []):
                    pipeline_paths.append((theme_label, t.get("name", "?"), t.get("status", "?")))
        except Exception:
            pass

    lines = ["DASHBOARD PATH REFERENCE — use exact strings in dashboard_path fields:"]

    lines.append(f"\nCOS DASHBOARD — ACTIVE {_DEAL_WS.upper()} DEALS (deal portfolio, COS tab):")
    for name, stage in active_deals:
        lines.append(f"  COS › {_DEAL_WS} Deals › {name}  [{stage}]")
    if not active_deals:
        lines.append("  (none loaded)")

    lines.append("\nCOS DASHBOARD — LP / FUNDRAISING TARGETS:")
    for name, status in lp_targets:
        lines.append(f"  COS › {_DEAL_WS} Fundraising › {name}  [{status}]")
    if not lp_targets:
        lines.append("  (none loaded)")

    lines.append("\nDEAL IDEAS DASHBOARD (/deals/) — THEMES & SCORED TARGETS:")
    lines.append("  Cross-reference: if the call mentions any asset, geography, sector, or")
    lines.append("  counterparty that touches a theme or target below, flag it in deal_intel")  # noqa: tenant-leak — deferred schema migration
    lines.append("  using the Deal Pipeline path. This is how call intel flows into the pipeline.")
    current_theme = None
    for theme_label, target_name, status in pipeline_paths:
        if theme_label != current_theme:
            lines.append(f"  [{theme_label}]")
            current_theme = theme_label
        lines.append(f"    Deal Pipeline › {theme_label} › {target_name}  [{status}]")
    if not pipeline_paths:
        lines.append("  (none loaded)")

    lines.append("\nOTHER VALID PATHS:")
    lines.append("  COS › Recruiting › [firm name]")
    lines.append("  COS › Follow-ups  (use only when no specific deal/LP applies)")

    return "\n".join(lines)


# ── Claude Haiku extraction ───────────────────────────────────────────────────

# Stable preamble — cached via Anthropic prompt caching. ~1.5KB, identical
# across all calls, so repeated calls within 5 min hit cache at ~10% input cost.
# The trailing call-title / today / transcript go in a second (uncached) block.
# Header: built from firm_context.yaml (no hardcoded names below).
_p = _fc._principal(_CTX)
_f = _fc._firm(_CTX)
_dl = _fc._deal_lead(_CTX)
_team = _CTX.get("team", [])
_p_name = _p.get("name", "Principal")
_dl_name = _dl.get("name", "co-founder")
_focus_list = _p.get("investment_focus", [])
_focus_sectors = ", ".join(
    s.split("(")[0].strip() for s in (_focus_list if isinstance(_focus_list, list) else [_focus_list])
)
_team_str = ", ".join(
    [f"{_p_name} (co-founder)"] +
    [f"{m['name']} ({m['role']})" for m in _team if m.get("name") != _p_name]
)

_EXTRACTION_BODY = f"""

Analyze the call transcript/memo provided below and extract ALL of the following. Respond ONLY with valid JSON.

EXTRACTION TASKS:

0. speakers: For each speaker label in the transcript (Speaker 1, Speaker 2, Unknown Speaker, etc.), identify who they are.
   PRIMARY source: the KNOWN ATTENDEES list injected with this call — cross-reference attendee names against speaker behavior and context.
   SECONDARY: context clues — names spoken aloud, roles described, things said.

   STANDING RULE — {_p_name} is likely on Zoom, Teams, Otter.ai, and desktop recordings but may not speak (listening-only calls happen). Identify his speaker label if he speaks using these markers:
   - addressed by name ("{_PRINCIPAL_FIRST}, what do you think?")
   - speaks about infrastructure PE from a principal/investor perspective: {_focus_sectors}
   - references his own background: {_p.get("background", "")}, co-founding {_f.get("name", "the firm")} with {_dl_name}
   - personally owns certain counterparty relationships (named in active deal contexts)
   - discusses deal structure, investment returns, LP fundraising, or firm strategy from a GP/co-founder frame

   Do not force {_PRINCIPAL_FIRST} as a speaker if no label matches those markers. Use "Unknown" when genuinely unresolvable.
   Each: {{"label": "Speaker 1", "name": "{_dl_name}", "role": "{_dl.get("role", "co-founder")}", "evidence": "addressed as '{_dl_name.split()[0]}' at 00:33; KNOWN ATTENDEES list includes {_dl_name}"}}

1. category: "{_fc.workstream_recruiting(_CTX)}" (job search/recruiter/employer calls), "{_DEAL_WS}" (deal/LP/investor/partner calls), or "Other"

2. action_items: Every action that must still happen AFTER this call.
   MUST capture: social invites, send commitments, intro commitments, callbacks, explicit asks, document requests. Also third-party commitments requiring follow-up.
   EXCLUDE: generic "prep for call", "review notes", vague "follow up" with no specific content. EXCLUDE COMPLETED actions.
   DISTINGUISH action_type:
     "new_action" — genuine new commitment not yet tracked; WILL be written to Follow-ups.
     "status_update" — status on something already tracked. Do NOT write to Follow-ups.
   Use RESOLVED speaker names ({", ".join(_CTX.get("owner_whitelist", [_p_name]))}) in owner — never "Speaker 1".
   Each item: {{
     "who": "person/firm being actioned",
     "what": "verb-first specific action",
     "due": "YYYY-MM-DD",
     "owner": "{_OWNERS}|[resolved name]",
     "workstream": "Job Search|{_DEAL_WS}",
     "action_type": "new_action|status_update",
     "context": "specific context using these patterns: 'fundraising/LP — [LP firm name]', 'deal diligence — [deal/asset name]', 'deal origination — [target name]'. Always name the specific entity.",
     "dashboard_path": "exact path from DASHBOARD PATH REFERENCE injected below"
   }}

3. deal_updates: For every named deal, asset, acquisition target, or new investment signal discussed where the call contained material new information — status changes, capital structure developments, counterparty moves, valuation data, new opportunity signals. Not limited to deals already in the pipeline; include new ideas surfaced in the call. One block per deal. Omit only if a deal was a passing mention with no material information.
   Each: {{
     "deal_name": "name of the deal, asset, or opportunity",
     "status_change": "one sentence: what specifically changed or what new signal surfaced",
     "key_developments": ["named entity + specific data point — format: '[Counterparty] [action/milestone] (validated by {_PRINCIPAL_FIRST})' or similar — always include named firm and concrete fact", "..."],
     "owner": "{"|".join(_CTX.get("owner_whitelist", [_p_name]))}|shared",
     "next_step": "specific next milestone",
     "dashboard_directive": "explicit instruction: what to update where — format: 'Update [DEAL_TICKER] status note in COS › {_DEAL_WS} Deals › [deal name]; [what to add]'"
   }}

4. lp_updates: For each LP, investor, fundraising advisor, or capital source where the call surfaced actionable intel or a new outreach. One block per LP.
   Each: {{
     "lp_name": "John Hancock",
     "contact_name": "Eddie Dunn",
     "status": "Outreach initiated|Warm intro pending|Exploratory call|Active|Hold|Unknown",
     "owner": "{_OWNERS}",
     "context": "specific: fund history, relationship, mandate fit, e.g. 'Eddie led Phoenix Tower, Diamond Comm (Fund 1); {_PRINCIPAL_FIRST} on Duquesne board with JH team'",
     "next_action": "verb-first specific action",
     "dashboard_directive": "e.g. 'Add to LP tracker; Stage = Outreach initiated; owner = {_CTX.get("owner_whitelist", ["Principal"])[-1]}'"
   }}

5. new_contacts: People mentioned who should be tracked.
   Each: {{"name": "...", "firm": "...", "title": "...", "context": "one line"}}

6. recruiting_intel (only if {_fc.workstream_recruiting(_CTX)}): {{"firm":"","role":"","stage":"Screening|Longlist|Shortlist|Live Process","key_dates":"","comp_intel":"","notes":""}}

7. deal_intel: Named deal and LP intelligence. For substantive {_DEAL_WS} calls, aim for 5+ items. Include: deal structure details, competitive dynamics, counterparty motivations, LP mandate fit, precedent transactions, new asset angles, fundraising strategy advice, market observations with investment implications.  # noqa: tenant-leak — deferred schema migration
   CROSS-REFERENCE: scan against the DEAL IDEAS DASHBOARD paths injected below. If something discussed (asset type, geography, sector, counterparty) connects to a named pipeline theme or target, include that item with the matching dashboard_path. This is how call intel flows into the deal pipeline.
   Each: {{
     "investor_or_firm": "",
     "status": "Active|Qualified|Hold|Long-term|Unknown",
     "key_feedback": "named entity + specific data point + investment implication — be detailed",
     "next_action": "",
     "intel_type": "LP/fundraising|deal intel|competitive intel|co-investor|deal origination|fundraising strategy",
     "dashboard_path": "exact path from DASHBOARD PATH REFERENCE below — use Deal Pipeline path if this touches a pipeline theme/target"
   }}

8. one_line_summary: Under 25 words. Lead with the so-what for a senior investor.

KEY COMPETITOR / CO-INVESTOR FIRMS — flag any mention in deal_intel:  # noqa: tenant-leak — deferred schema key
{_PEER_FIRMS}.

RESPOND WITH THIS JSON ONLY (no markdown, no explanation):
{{"category":"...","one_line_summary":"...","speakers":[{{"label":"...","name":"...","role":"...","evidence":"..."}}],"action_items":[{{"who":"...","what":"...","due":"...","owner":"...","workstream":"...","action_type":"new_action|status_update","context":"...","dashboard_path":"..."}}],"deal_updates":[{{"deal_name":"...","status_change":"...","key_developments":["..."],"owner":"...","next_step":"...","dashboard_directive":"..."}}],"lp_updates":[{{"lp_name":"...","contact_name":"...","status":"...","owner":"...","context":"...","next_action":"...","dashboard_directive":"..."}}],"new_contacts":[...],"recruiting_intel":{{}},"deal_intel":[{{"investor_or_firm":"...","status":"...","key_feedback":"...","next_action":"...","intel_type":"...","dashboard_path":"..."}}]}}  # noqa: tenant-leak — deal_intel is a deferred schema migration
"""

EXTRACTION_PREAMBLE = _fc.build_extraction_header(_CTX) + _EXTRACTION_BODY


def extract_all(transcript_text, title, category, attendees=None):
    today = datetime.now().strftime("%Y-%m-%d")
    attendee_line = ""
    if attendees:
        attendee_line = f"KNOWN ATTENDEES (from calendar invite — use to resolve Speaker labels): {', '.join(attendees)}\n"
    dynamic = (
        f"CALL TITLE: {title}\n"
        f"TODAY: {today}\n"
        f"{attendee_line}\n"
        f"TRANSCRIPT:\n{transcript_text[:30000]}"
    )

    dashboard_paths = build_dashboard_path_reference()

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 3000,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": EXTRACTION_PREAMBLE,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dashboard_paths,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": dynamic},
            ],
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key":          ANTHROPIC_KEY,
            "anthropic-version":  "2023-06-01",
            "anthropic-beta":     "prompt-caching-2024-07-31",
            "content-type":       "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read())
    log_usage("cos_transcript_hook", CLAUDE_MODEL, resp)
    raw = resp["content"][0]["text"].strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ── Warmup ────────────────────────────────────────────────────────────────────

def warmup():
    try:
        urllib.request.urlopen(
            urllib.request.Request(DASHBOARD_URL, method="POST"), timeout=3
        )
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc-id",   required=True,  help="Google Doc ID of the transcript")
    parser.add_argument("--title",    required=True,  help="Human-readable call title")
    parser.add_argument("--category", default="auto", help="deal / recruiting / other / auto")
    args = parser.parse_args()

    print(f"[hook] Starting COS extraction: {args.title}", flush=True)

    if not ANTHROPIC_KEY:
        print("[hook] ANTHROPIC_API_KEY not set — skipping.", file=sys.stderr)
        warmup(); return

    try:
        token = refresh_token()
    except Exception as e:
        print(f"[hook] Token refresh failed: {e}", file=sys.stderr)
        return

    # Read transcript doc
    try:
        tdoc  = gdocs_get(token, args.doc_id)
        ttext = doc_text(tdoc)
    except Exception as e:
        print(f"[hook] Could not read transcript: {e}", file=sys.stderr)
        warmup(); return

    if len(ttext) < 200:
        print("[hook] Transcript too short — skipping.")
        warmup(); return

    # Best-effort calendar attendee lookup (fails silently)
    attendees = get_calendar_attendees(token)
    if attendees:
        print(f"[hook] Calendar attendees: {', '.join(attendees)}", flush=True)

    if _NORMALIZER is not None:
        try:
            ttext, _phon = _NORMALIZER.apply_phonetic(ttext)
            if _phon:
                print(f"[hook] 🔤  Phonetic corrections: {_phon}", flush=True)
        except Exception as _pe:
            print(f"[hook] ⚠️  Phonetic normalization failed: {_pe}", file=sys.stderr)

    # Full COS extraction
    try:
        data = extract_all(ttext, args.title, args.category, attendees=attendees)
    except Exception as e:
        print(f"[hook] Extraction failed: {e}", file=sys.stderr)
        warmup(); return

    if _NORMALIZER is not None:
        try:
            _n_adj = _normalize_hook_extraction(data, _NORMALIZER)
            if _n_adj:
                print(f"[hook] 🧭  Entity reconciliation: {_n_adj} adjustments", flush=True)
            # Speaker N warning — soft (hook can't abort recording flow)
            unresolved_speakers = [
                a for a in (data.get("action_items", []) or [])
                if isinstance(a, dict) and re.match(r"^\s*speaker\s*\d+\s*$", a.get("who", ""), re.I)
            ]
            if unresolved_speakers:
                print(
                    f"[hook] ⚠️  {len(unresolved_speakers)} action item(s) still assigned to "
                    f"'Speaker N' — calendar attendee lookup may have failed.",
                    file=sys.stderr,
                )
        except Exception as _ne:
            print(f"[hook] ⚠️  Entity reconciliation failed: {_ne}", file=sys.stderr)

    category    = data.get("category", "Other")
    actions     = data.get("action_items", [])
    contacts    = data.get("new_contacts", [])
    rec_intel   = data.get("recruiting_intel", {})
    deal_intel  = data.get("deal_intel") or data.get("tomac_intel") or []  # noqa: tenant-leak — backward-compat read of old key
    speakers    = data.get("speakers", [])
    deal_upd    = data.get("deal_updates", [])
    lp_upd      = data.get("lp_updates", [])
    today       = datetime.now().strftime("%Y-%m-%d")
    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc_link    = f"https://docs.google.com/document/d/{args.doc_id}/edit"

    print(f"[hook] Category: {category} | Actions: {len(actions)} | Contacts: {len(contacts)}", flush=True)

    added = 0

    # ── Follow-ups table ──────────────────────────────────────────────────────
    new_actions    = [i for i in actions if i.get("action_type") != "status_update"]
    status_updates = [i for i in actions if i.get("action_type") == "status_update"]
    if new_actions:
        try:
            fu_doc    = gdocs_get(token, FOLLOW_UPS_DOC)
            fu_text   = doc_text(fu_doc)
            row_num   = next_row_num(fu_text)
            insert_at = last_table_row_end(fu_doc)
            new_rows  = ""
            for item in new_actions:
                what = item.get("what", "").strip()
                if not what:
                    continue
                who       = item.get("who", _PRINCIPAL_FIRST)
                due       = item.get("due", "TBD")
                ws        = item.get("workstream", _DEAL_WS)
                context   = item.get("context", "").replace("|", "/")
                dash_path = item.get("dashboard_path", "COS › Follow-ups").replace("|", "/")
                new_rows += f"| {row_num} | {who} | {what} | {due} | {ws} | call — {args.title} | {doc_link} | {context} | {dash_path} |\n"
                row_num += 1
                added   += 1
            if new_rows:
                gdocs_insert(token, FOLLOW_UPS_DOC, insert_at, new_rows)
            suffix = f" ({len(status_updates)} status updates skipped)" if status_updates else ""
            print(f"[hook] ✅  Follow-ups: +{added} rows{suffix}", flush=True)
        except Exception as e:
            print(f"[hook] Follow-ups write failed: {e}", file=sys.stderr)

    # ── People doc ────────────────────────────────────────────────────────────
    if contacts:
        try:
            pdoc     = gdocs_get(token, PEOPLE_DOC)
            pcontent = pdoc.get("body", {}).get("content", [])
            pend     = (pcontent[-1].get("endIndex", 2) - 1) if pcontent else 1
            ptext    = f"\n\n─── From call: {args.title} ({today}) ───\n"
            for c in contacts:
                ptext += (
                    f"\n{c.get('name','?')} / {c.get('firm','?')}"
                    + (f" — {c.get('title')}" if c.get("title") else "")
                    + f"\n  {c.get('context','')}\n"
                )
            gdocs_insert(token, PEOPLE_DOC, pend, ptext)
            print(f"[hook] ✅  People doc: +{len(contacts)} contacts", flush=True)
        except Exception as e:
            print(f"[hook] People doc write failed: {e}", file=sys.stderr)

    # ── Recruiting doc ────────────────────────────────────────────────────────
    if category == "Recruiting" and rec_intel and rec_intel.get("firm"):
        try:
            rdoc     = gdocs_get(token, RECRUITING_DOC)
            rcontent = rdoc.get("body", {}).get("content", [])
            rend     = (rcontent[-1].get("endIndex", 2) - 1) if rcontent else 1
            rtext    = (
                f"\n\n## {rec_intel.get('firm','?')}\n"
                f"**Firm:** {rec_intel.get('firm','?')}\n"
                f"**Role:** {rec_intel.get('role','?')}\n"
                f"**Stage:** {rec_intel.get('stage','Screening')}\n"
                f"**Last action:** {today} — recorded call: {args.title}\n"
                f"**Next step:** {rec_intel.get('key_dates','?')}\n"
                f"**Notes:**\n"
                f"- {today}: {rec_intel.get('notes','')}\n"
                f"  Comp: {rec_intel.get('comp_intel','None surfaced')}\n"
            )
            gdocs_insert(token, RECRUITING_DOC, rend, rtext)
            print(f"[hook] ✅  Recruiting doc: {rec_intel.get('firm')}", flush=True)
        except Exception as e:
            print(f"[hook] Recruiting doc write failed: {e}", file=sys.stderr)

    # ── Deal Pipeline doc ─────────────────────────────────────────────────────
    if category == _DEAL_WS and deal_intel:  # noqa: tenant-leak — deferred schema migration
        try:
            tdoc2    = gdocs_get(token, DEAL_PIPELINE_DOC)
            tcontent = tdoc2.get("body", {}).get("content", [])
            tend     = (tcontent[-1].get("endIndex", 2) - 1) if tcontent else 1
            ttext2   = (
                f"\n\n### [{today}] LP Investor Intel — {args.title}\n"
                f"| Investor | Status | Key feedback | Next action |\n"
                f"|---|---|---|---|\n"
            )
            for item in deal_intel:
                ttext2 += (
                    f"| {item.get('investor_or_firm','?')} "
                    f"| {item.get('status','?')} "
                    f"| {item.get('key_feedback','?')} "
                    f"| {item.get('next_action','?')} |\n"
                )
            gdocs_insert(token, DEAL_PIPELINE_DOC, tend, ttext2)
            print(f"[hook] ✅  {_DEAL_WS} doc: {len(deal_intel)} LP intel rows", flush=True)
        except Exception as e:
            print(f"[hook] {_DEAL_WS} doc write failed: {e}", file=sys.stderr)

    # ── Processing header on call doc ─────────────────────────────────────────
    try:
        header = (
            f"╔══════════════════════════════════════════════════════════════════╗\n"
            f"PROCESSED: {now_str}  |  Category: {category}\n"
            f"Source: call_recorder.py (BlackHole / Twilio)\n"
            f"╚══════════════════════════════════════════════════════════════════╝\n\n"
        )

        # SPEAKERS
        if speakers:
            header += "SPEAKERS:\n"
            for sp in speakers:
                name = sp.get("name", "Unknown")
                role = sp.get("role", "")
                ev   = sp.get("evidence", "")
                header += f"  {sp.get('label','?')} = {name}"
                if role:
                    header += f" — {role}"
                if ev:
                    header += f" ({ev})"
                header += "\n"
            header += "\n"

        # DEAL UPDATES (deal-workstream calls only)
        if deal_upd:
            header += "DEAL UPDATES\n────────────────────────────────────────────────────────────────\n"
            for du in deal_upd:
                header += f"{du.get('deal_name', '?').upper()}"
                if du.get("owner"):
                    header += f" — [{du['owner']} lead]"
                header += "\n"
                if du.get("status_change"):
                    header += f"  Status: {du['status_change']}\n"
                for dev in du.get("key_developments", []):
                    header += f"  • {dev}\n"
                if du.get("next_step"):
                    header += f"  Next: {du['next_step']}\n"
                if du.get("dashboard_directive"):
                    header += f"  Dashboard: {du['dashboard_directive']}\n"
                header += "\n"

        # LP / FUNDRAISING PIPELINE (deal-workstream calls only)
        if lp_upd:
            header += "LP / FUNDRAISING PIPELINE\n────────────────────────────────────────────────────────────────\n"
            for lp in lp_upd:
                name    = lp.get("lp_name", "?")
                contact = lp.get("contact_name", "")
                owner   = lp.get("owner", "")
                status  = lp.get("status", "")
                header += f"  • {name}"
                if contact:
                    header += f" / {contact}"
                if status:
                    header += f" — {status}"
                if owner:
                    header += f" [{owner}]"
                header += "\n"
                if lp.get("context"):
                    header += f"    Intel: {lp['context']}\n"
                if lp.get("next_action"):
                    header += f"    Next: {lp['next_action']}\n"
                if lp.get("dashboard_directive"):
                    header += f"    Dashboard: {lp['dashboard_directive']}\n"
            header += "\n"

        # ACTION ITEMS grouped by owner — order from firm_context.yaml owner_whitelist (reversed so principal appears last)
        owners_order = list(reversed(_CTX.get("owner_whitelist", [_PRINCIPAL_FIRST])))
        by_owner: dict = {}
        for item in new_actions:
            o = item.get("owner", "Other")
            by_owner.setdefault(o, []).append(item)
        for o in list(by_owner):
            if o not in owners_order:
                owners_order.append(o)

        if new_actions:
            header += "KEY ACTION ITEMS:\n"
            for o in owners_order:
                items = by_owner.get(o, [])
                if not items:
                    continue
                header += f"\n  [{o}]\n"
                for item in items:
                    context = item.get("context", "")
                    dpath   = item.get("dashboard_path", "")
                    header += f"  → {item.get('what', '')} — Due {item.get('due', '?')}\n"
                    if context:
                        header += f"      Context: {context}\n"
                    if dpath:
                        header += f"      Dashboard: {dpath}\n"
        else:
            header += "KEY ACTION ITEMS:\n  None\n"

        if status_updates:
            header += f"\nSTATUS UPDATES ({len(status_updates)}) — already tracked, not added to Follow-ups:\n"
            for item in status_updates:
                dpath = item.get("dashboard_path", "")
                header += f"  ↳ {item.get('what', '')} ({dpath})\n"

        header += "\nKEY INTEL:\n"
        for item in deal_intel:
            intel_type = item.get("intel_type", "")
            dpath      = item.get("dashboard_path", "")
            header += f"  • [{intel_type}] {item.get('investor_or_firm', '')}: {item.get('key_feedback', '')}\n"
            if dpath:
                header += f"      → {dpath}\n"
        if not deal_intel:
            header += "  None\n"

        header += (
            f"\nDOCS TOUCHED: Follow-ups (+{added} rows)"
            f" | People ({'+'+ str(len(contacts)) if contacts else 'No change'})"
            f" | Recruiting ({'Updated: ' + rec_intel.get('firm','') if category=='Recruiting' and rec_intel.get('firm') else 'No change'})"
            f" | {_DEAL_WS} Pipeline ({'Updated' if category==_DEAL_WS and deal_intel else 'No change'})\n"
            f"══════════════════════════════════════════════════════════════════════\n\n"
        )
        gdocs_insert(token, args.doc_id, 1, header)
        print("[hook] ✅  Processing header written to call doc", flush=True)
    except Exception as e:
        print(f"[hook] Header write failed (non-critical): {e}", file=sys.stderr)

    # Move transcript to _Ready/ so Drive organizer knows overlay is complete
    # and can safely route the file to the correct deal's Transcripts folder.
    try:
        ready_id = drive_get_or_create_folder(token, "_Ready", TRANSCRIPTS_FOLDER)
        drive_move(token, args.doc_id, ready_id, TRANSCRIPTS_FOLDER)
        print("[hook] ✅  Moved to Transcripts/_Ready/ — ready for Drive organizer", flush=True)
    except Exception as e:
        print(f"[hook] ⚠️  Could not move to _Ready/ (non-critical): {e}", file=sys.stderr)

    warmup()
    print(f"[hook] Done. {added} follow-ups | {len(contacts)} contacts | dashboard pinged.", flush=True)


if __name__ == "__main__":
    main()
