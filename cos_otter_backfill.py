#!/usr/bin/env python3
"""
cos_otter_backfill.py — Full backfill pass for COS transcript pipeline.

Scans all Otter AI and Call Transcript folders, processes any unprocessed
Google Doc using Claude, and writes outputs to Follow-ups, People, Recruiting,
and deal pipeline docs.

Dedup tracker: ~/credentials/processed_cos_transcripts.json
"""
# `X | None` PEP 604 annotations require Python 3.10+ at runtime; the launchd
# runner currently lands on Python 3.9 (per FutureWarning in usage-report.stderr).
# This makes annotations lazy strings so they evaluate fine on 3.9. Without it,
# every scheduled run since 2026-04-27 17:12 ET crashed with TypeError on the
# `EntityNormalizer | None` annotation at module import time.
from __future__ import annotations

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _usage import log_usage  # noqa: E402
from _entity_normalizer import EntityNormalizer  # noqa: E402

# ── Firm context (no hardcoded principal/firm references below this line) ──────
_PIPELINE_DIR = Path(os.environ.get("COS_PIPELINE_DIR", "")) or Path(__file__).parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))
import _firm_context as _fc  # noqa: E402
import _transcript_source as _ts  # noqa: E402
_CTX = _fc.load_firm_context()
_OWNERS        = _fc.owner_whitelist_str(_CTX)   # e.g. "Alice|Bob|Carol"
_SELF_EMAIL    = (_fc._principal(_CTX).get('email') or '').lower()
_DEAL_WS       = _fc.workstream_deal(_CTX)        # e.g. "Acme Capital"
_RECRUIT_WS    = _fc.workstream_recruiting(_CTX)  # e.g. "Recruiting"
_PEER_FIRMS    = _fc.peer_firms_str(_CTX)
_DOCS          = _fc.load_drive_docs()            # flat key→Drive-ID map

# Process-wide normalizer — canonical roster + phonetic dict cached across all
# transcripts in a run. Idempotent if reloaded; cheap to instantiate.
_NORMALIZER: EntityNormalizer | None = None


def _get_normalizer() -> EntityNormalizer:
    global _NORMALIZER
    if _NORMALIZER is None:
        _NORMALIZER = EntityNormalizer()
    return _NORMALIZER


def _normalize_extraction_in_place(data: dict, norm: EntityNormalizer, stats: dict) -> None:
    """Reconcile short entity strings (Who / name / firm) in extraction result.

    Mutates `data` in place. Adds counts to `stats` keys: entity_phonetic,
    entity_canonical_match, entity_unresolved_vague, entity_speaker_unresolved.

    Body-of-text fields (`what`, `content`, summaries) are NOT touched here —
    those flow through phonetic substitution applied to the transcript body
    before extraction.
    """
    stats.setdefault("entity_phonetic", 0)
    stats.setdefault("entity_canonical_match", 0)
    stats.setdefault("entity_unresolved_vague", 0)
    stats.setdefault("entity_speaker_unresolved", 0)
    stats.setdefault("entity_corrections_log", [])

    def _resolve(field_value: str) -> str:
        if not field_value or not isinstance(field_value, str):
            return field_value
        m = norm.match(field_value)
        if m.source == "phonetic":
            stats["entity_phonetic"] += 1
            stats["entity_corrections_log"].append(f'"{m.original}" → "{m.canonical}" (phonetic)')
            return m.canonical
        if m.source in ("lp", "deal", "pipeline_target") and m.confidence in ("exact", "substring"):
            if m.canonical != m.original:
                stats["entity_canonical_match"] += 1
                stats["entity_corrections_log"].append(
                    f'"{m.original}" → "{m.canonical}" ({m.source}/{m.confidence})'
                )
            return m.canonical
        if norm.is_vague(field_value):
            # Speaker N is a hard error per SKILL.md STEP 3B precondition.
            if re.match(r"^\s*speaker\s*\d+\s*$", field_value, re.I):
                stats["entity_speaker_unresolved"] += 1
            else:
                stats["entity_unresolved_vague"] += 1
            return f"[Unresolved — needs name] {field_value}"
        return field_value

    for action in data.get("action_items", []) or []:
        if isinstance(action, dict) and action.get("who"):
            action["who"] = _resolve(action["who"])
    for contact in data.get("new_contacts", []) or []:
        if isinstance(contact, dict):
            if contact.get("name"):
                contact["name"] = _resolve(contact["name"])
            if contact.get("firm"):
                contact["firm"] = _resolve(contact["firm"])
    for intel in (data.get("deal_intel") or data.get("tomac_intel") or []):  # noqa: tenant-leak — backward-compat read of old key
        if isinstance(intel, dict):
            for k in ("counterparty", "deal", "lp", "firm"):
                if intel.get(k):
                    intel[k] = _resolve(intel[k])

# ── Config ────────────────────────────────────────────────────────────────────

TOKEN_PATH           = Path.home() / "credentials/token.json"
GDRIVE_PICKLE        = Path.home() / "credentials/gdrive_token.pickle"
PIPELINE_DATA_PATH   = Path.home() / "dashboards/data/compiled/deal-pipeline-data.json"
DASHBOARD_DATA_PATH  = Path.home() / "dashboards/data/compiled/dashboard-data.json"
DEDUP_PATH      = Path.home() / "credentials/processed_cos_transcripts.json"
ROUTING_RULES_PATH = Path.home() / "dashboards/config/routing-rules.md"
# Doc IDs — loaded from ~/dashboards/config/drive-docs.yaml.
# Hardcoded fallbacks ensure the script still runs if the YAML is missing
# (e.g. on a fresh checkout before setup). Replace fallbacks with "" to
# require setup before running.
FOLLOW_UPS_DOC  = _DOCS.get("followups",      "10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY")
PEOPLE_DOC      = _DOCS.get("people_crm",     "1ZCKnZlQgKD13dLsQNxCM_nRsTjz2DVitjeUWowUur0Y")
RECRUITING_DOC  = _DOCS.get("recruiting",     "1ZnTCVoA0ID7XTDFy27yDnrEVhBqx75kaTg_QXFq4eXA")
DEAL_PIPELINE_DOC       = _DOCS.get("deal_pipeline", _DOCS.get("tomac_pipeline", "1LHorixPs8ppwSvQzGfA_B6609YZA8dSpR4rmppENzpc"))  # noqa: tenant-leak — backward-compat key
DASHBOARD_URL   = "http://localhost:7777/warmup"
CLAUDE_MODEL    = "claude-sonnet-4-6"
MEMO_MODEL      = "claude-opus-4-7"
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Legacy folder constants — used only by consolidate_transcript_siblings ─────
# The main processing loop is now config-driven via _transcript_source.py.
# These constants remain as dead-code safety-nets; they are NOT referenced by
# the scan loop. If transcript_sources is omitted from firm_context.yaml,
# _legacy_otter_sources() re-constructs equivalent sources from drive-docs.yaml
# or these same defaults.
OTTER_ROOT_FOLDER       = _DOCS.get("otter_ai",          "1zJly0cCiqsbZ3umYBXse7nYE7tUpFGOr")
OTTER_RECRUITING_FOLDER = _DOCS.get("otter_recruiting",   "1tMEGofeqzfF93YhPCyGe0dgJj8tzdRlF")
OTTER_TOMAC_FOLDER      = _DOCS.get("otter_deal", _DOCS.get("otter_tomac", "1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S"))  # noqa: tenant-leak — backward-compat key
OTTER_OTHER_FOLDER      = _DOCS.get("otter_other",        "1dt-s-D1SWaTrpIEsi0GiBAu1BCQCoPGq")
CALL_TRANSCRIPTS_FOLDER = _DOCS.get("call_transcripts",   "1jYntgSVBsW5-5rdx18TeZhHRsI9xT74p")

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".aac"}

# ── Auth ──────────────────────────────────────────────────────────────────────

def _load_routing_rules() -> str:
    """Load shared envelope routing contract from config/routing-rules.md.

    Injected as a cached Anthropic content block so this backfill and the
    _research_envelope pipeline see the same contract. If the file is
    missing, the pipeline still runs but logs a warning — the BACKFILL_PREAMBLE
    alone remains usable as a fallback.
    """
    try:
        return ROUTING_RULES_PATH.read_text()
    except Exception as e:
        print(f"  [routing] WARN — could not load {ROUTING_RULES_PATH}: {e}",
              file=sys.stderr)
        return ""


def _load_pickle_creds():
    import pickle
    with open(GDRIVE_PICKLE, "rb") as f:
        return pickle.load(f)


def _save_pickle_creds(creds):
    import pickle
    with open(GDRIVE_PICKLE, "wb") as f:
        pickle.dump(creds, f)


def refresh_token():
    """Refresh via google-auth (pickle has Drive + Docs scope)."""
    from google.auth.transport.requests import Request
    creds = _load_pickle_creds()
    creds.refresh(Request())
    _save_pickle_creds(creds)
    return creds.token


def get_token():
    creds = _load_pickle_creds()
    if creds.expired and creds.refresh_token:
        return refresh_token()
    return creds.token


# Calendar API uses a SEPARATE token at ~/credentials/token.json. The gdrive
# pickle is Drive+Docs only (widening would force a reconsent + pickle
# invalidation), so we keep the calendar.readonly scope in its own token file
# and lazy-refresh it on demand. Failures here are silent — the caller falls
# back to title-based participant extraction.
_CAL_TOKEN_PATH = Path.home() / "credentials/token.json"


def get_calendar_token():
    """Return a fresh access token for calendar.readonly, or '' on failure."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        if not _CAL_TOKEN_PATH.exists():
            return ""
        creds = Credentials.from_authorized_user_file(
            str(_CAL_TOKEN_PATH),
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _CAL_TOKEN_PATH.write_text(creds.to_json())
        return creds.token or ""
    except Exception as e:
        print(f"    ⚠️   Calendar token load failed ({type(e).__name__}: {e})", file=sys.stderr)
        return ""


# ── Drive API ─────────────────────────────────────────────────────────────────

def drive_list_folder(token, folder_id, since: str | None = None):
    """Return list of {id, name, mimeType, modifiedTime} for all files in folder.

    since: optional ISO 8601 UTC string (e.g. '2026-04-26T07:00:00Z').
    When provided, only files modified after that time are returned.
    Pass None (default) to return all files — used by --force and --backfill modes.
    """
    files = []
    page_token = None
    q = f"'{folder_id}' in parents and trashed=false"
    if since:
        q += f" and modifiedTime > '{since}'"
    while True:
        params = {
            "q": q,
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "pageSize": "100",
        }
        if page_token:
            params["pageToken"] = page_token
        url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return files


def move_file_to_folder(token, file_id, new_folder_id, old_folder_id):
    """Move a Drive file from old_folder_id to new_folder_id."""
    url = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}"
        f"?addParents={new_folder_id}&removeParents={old_folder_id}&fields=id,parents"
    )
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# Legacy category→folder map. Only used if a GoogleDriveFolderSource has no
# category_folders configured and we need a fallback for root-file moving.
_LEGACY_CATEGORY_FOLDER = {
    _DEAL_WS:    OTTER_TOMAC_FOLDER,  # noqa: tenant-leak — variable name contains legacy key, backward-compat
    _RECRUIT_WS: OTTER_RECRUITING_FOLDER,
    "Other":     OTTER_OTHER_FOLDER,
}


# ── Transcript duplicate consolidation ────────────────────────────────────────
# Otter (via Zapier) often drops the same call into Drive 2-4 times: Zapier
# double-fires, and Otter separately exports a .txt full-transcript file
# alongside the Google Doc. Result: 3-4 siblings in the folder, sometimes the
# .txt holds the richest content and the Doc conversions are thin stubs.
#
# Rule for all future Otter ingestion: one call = one Google Doc. Consolidate
# siblings before dedup-tracker check, pick the richest body, ensure Google
# Doc format, trash the rest (reversible — files go to Drive trash).

_OTTER_NOISE_PAT = re.compile(
    r"(_otter[_\s]*ai|_otter|_transcript|\(transcript\)|\.otter)",
    re.IGNORECASE,
)


def _transcript_identity_key(filename: str) -> str:
    """
    Normalize an Otter transcript filename into an identity key so that
    sibling copies (Google Doc + .txt + Zapier double-fire) collapse to the
    same key.

    Strips: extension, Otter/Zapier decorators, leading date prefix,
    decorative dashes, internal whitespace, case.
    """
    base = filename
    # strip extension
    m = _re.search(r"\.[A-Za-z0-9]{1,5}$", base)
    if m:
        base = base[: m.start()]
    # strip Otter/Zapier decorators
    base = _OTTER_NOISE_PAT.sub(" ", base)
    # strip decorative dashes (they sometimes wrap the date prefix)
    base = re.sub(r"[─\u2500]+", " ", base)
    # extract leading YYYY-MM-DD date (keep in key — don't collapse across dates)
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", base)
    date_part = date_match.group(1) if date_match else ""
    # remove the date from the body so only the title remains
    if date_part:
        base = base.replace(date_part, " ")
    # collapse any remaining hyphens used as separators into spaces
    base = re.sub(r"\s-\s", " ", base)
    # collapse whitespace, lowercase
    title_part = " ".join(base.split()).lower().strip()
    # Prepend date so 2026-04-15 and 2026-04-21 calls with same title stay distinct
    return f"{date_part}|{title_part}" if date_part else title_part


def drive_trash(token, file_id):
    """Move a Drive file to trash (reversible). Never hard-delete."""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,trashed"
    body = json.dumps({"trashed": True}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def drive_copy_as_gdoc(token, file_id, new_name, parent_folder):
    """Copy a Drive file (e.g. .txt) and convert to Google Doc via mimeType
    conversion. Returns the new Doc's id."""
    url = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}/copy"
        f"?fields=id,name,mimeType"
    )
    meta = {
        "name": new_name,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [parent_folder],
    }
    body = json.dumps(meta).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def consolidate_transcript_siblings(token, files, tracker, parent_folder):
    """
    Group files by transcript identity key. For each group with >1 file:
      1. Pick the richest-content file (largest body text).
      2. Ensure it is a Google Doc — if richest is .txt, copy-convert into
         the same folder with the canonical name.
      3. Trash all siblings (including the original .txt if it was
         copy-converted). Never hard-delete.
      4. Update `tracker` so all old IDs mark as "consolidated" and the
         canonical Doc ID inherits processed-state (if any sibling was).

    Returns a new files list with siblings collapsed to the canonical survivor
    (so the caller's downstream loop processes exactly one file per call).
    """
    # Group by identity key
    groups = {}
    for f in files:
        key = _transcript_identity_key(f["name"])
        if not key:
            continue
        groups.setdefault(key, []).append(f)

    survivors = []
    consolidated = 0
    for key, group in groups.items():
        if len(group) == 1:
            survivors.append(group[0])
            continue

        # Size each candidate by body length
        sized = []
        for f in group:
            mime = f.get("mimeType", "")
            try:
                body, _ = read_file_content(token, f["id"], mime)
                size = len(body or "")
            except Exception as e:
                print(f"    ⚠️   consolidate: could not read {f['name']}: {e}", flush=True)
                size = 0
            sized.append((size, f))

        sized.sort(key=lambda x: x[0], reverse=True)
        richest_size, richest = sized[0]

        print(
            f"    🔀  Consolidating {len(group)} siblings for '{key}' — "
            f"richest: {richest['name']} ({richest_size} bytes, {richest.get('mimeType','?')})",
            flush=True,
        )

        # Ensure survivor is a Google Doc
        survivor = richest
        richest_mime = richest.get("mimeType", "")
        if "google-apps.document" not in richest_mime:
            # Compose canonical name (strip extension, keep date+title)
            canonical_name = _re.sub(r"\.[A-Za-z0-9]{1,5}$", "", richest["name"])
            try:
                new_doc = drive_copy_as_gdoc(token, richest["id"], canonical_name, parent_folder)
                survivor = {
                    "id": new_doc["id"],
                    "name": new_doc.get("name", canonical_name),
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": richest.get("modifiedTime", ""),
                }
                print(
                    f"        → Converted richest .txt into Google Doc: {survivor['id']}",
                    flush=True,
                )
                # The original .txt becomes a sibling to trash
            except Exception as e:
                print(f"    ⚠️   consolidate: copy-as-gdoc failed: {e}", file=sys.stderr)
                survivors.append(richest)
                continue

        # Trash everyone in the group except the survivor (by id)
        any_processed = False
        for f in group:
            if f["id"] == survivor["id"]:
                continue
            # Inherit processed-state from any sibling
            if f["id"] in tracker:
                any_processed = True
                tracker[f["id"]] = {
                    **tracker[f["id"]],
                    "consolidated_into": survivor["id"],
                    "consolidated_at": datetime.now().isoformat(),
                }
            try:
                drive_trash(token, f["id"])
                print(f"        🗑  Trashed sibling: {f['name']} ({f['id']})", flush=True)
            except Exception as e:
                print(f"    ⚠️   consolidate: trash failed for {f['id']}: {e}", file=sys.stderr)

        # If any sibling was already processed, mark the survivor as processed too
        # (it inherits that state — we don't want to re-extract actions).
        if any_processed and survivor["id"] not in tracker:
            tracker[survivor["id"]] = {
                "name": survivor["name"],
                "processed_at": datetime.now().isoformat(),
                "category": "consolidated",
                "note": "Inherited processed state from trashed sibling",
            }

        survivors.append(survivor)
        consolidated += len(group) - 1

    if consolidated:
        print(f"  🔀  Consolidated {consolidated} duplicate transcript(s)", flush=True)
        save_dedup(tracker)

    return survivors

# ── Intel / conference call pre-classifier ────────────────────────────────────
# Titles matching these patterns indicate market intel calls where Yoni is a
# listener, not a participant — no action items should be generated.
# classify_title_hint() returns ("Other", True) for intel calls, (hint, False)
# otherwise. The caller overrides Claude's category and skips action extraction.

INTEL_CALL_PATTERNS = [
    # Capstone DC market calls (broker intel service)
    r"capstone\b.*(call|briefing|update|gas|power|lng|energy|dc|market)",
    # IEA, EIA, RBN, FVR, Bloomberg, S&P market briefings
    r"\b(iea|eia|rbn|fvr|bloomberg|s&p|wood mackenzie|opis)\b.*(call|briefing|update)",
    # Generic "market call", "analyst call", "expert call" from research services
    r"\b(market|analyst|expert|channel.?check)\s+(call|briefing|update)\b",
    # Conference calls / webinars the user dialed into
    r"\b(conference\s+call|webinar|earnings\s+call|investor\s+day)\b",
]

import re as _re

def classify_title_hint(title: str) -> tuple:
    """
    Returns (category_hint, is_intel_call).
    If is_intel_call=True, caller should force category='Other' and skip
    action extraction — this is a market intel call, not a direct participant call.
    """
    t = title.lower()
    # Strip the leading/trailing dash decorations Otter prepends
    t = _re.sub(r"[─\-─]+", " ", t).strip()
    for pattern in INTEL_CALL_PATTERNS:
        if _re.search(pattern, t, _re.IGNORECASE):
            return ("Other", True)
    return ("auto", False)


def clean_title_for_rename(raw_name: str, call_date: str) -> str:
    """
    Convert Otter's dash-decorated filename to 'YYYY-MM-DD Title.ext'.
    Input:  '───────Capstone - Europe Gas call───.txt'
    Output: '2026-04-21 Capstone - Europe Gas Call.txt'

    Idempotent: if raw_name already starts with YYYY-MM-DD, do not prepend again.
    Preserves the extension (never title-cases it).
    """
    # Split extension first — never title-case it.
    base = raw_name
    ext = ""
    m_ext = _re.search(r"(\.[A-Za-z0-9]{1,5})$", base)
    if m_ext:
        ext = m_ext.group(1).lower()
        base = base[: m_ext.start()]

    # Strip leading/trailing decorative dashes and whitespace
    name = _re.sub(r"^[\s─\-\u2500]+|[\s─\-\u2500]+$", "", base)
    # Normalize internal whitespace
    name = " ".join(name.split())

    # Collapse any stacked YYYY-MM-DD date prefixes (repairs historical
    # double-date leaks from the pre-idempotency version of this function).
    collapsed = _re.sub(r"^(\d{4}-\d{2}-\d{2}\s+)+", "", name)
    if collapsed != name:
        name = collapsed  # fall through so we re-attach exactly one date below

    # If already starts with a YYYY-MM-DD prefix, keep name as-is (already renamed).
    if _re.match(r"^\d{4}-\d{2}-\d{2}\s", name):
        return f"{name}{ext}"

    # Title-case only the base title
    name = name.title()
    date_str = call_date if call_date else datetime.now().strftime("%Y-%m-%d")
    return f"{date_str} {name}{ext}"


def drive_rename_file(token, file_id, new_name):
    """Rename a Drive file."""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,name"
    body = json.dumps({"name": new_name}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── Docs API ──────────────────────────────────────────────────────────────────

def gdocs_get(token, doc_id):
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def drive_download_text(token, file_id):
    """Download a plain text file from Google Drive."""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        content = r.read()
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def drive_upload_text(token, file_id, text_content):
    """Overwrite a plain text file in Drive with new content (multipart PATCH)."""
    boundary = "=====boundary====="
    meta = json.dumps({"mimeType": "text/plain"}).encode()
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
        + meta
        + f"\r\n--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n".encode()
        + text_content.encode("utf-8")
        + f"\r\n--{boundary}--".encode()
    )
    url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=multipart"
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/related; boundary={boundary}",
    }, method="PATCH")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def drive_get_meta(token, file_id):
    """Get file metadata from Drive (mimeType, name, modifiedTime)."""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,name,mimeType,modifiedTime"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── Calendar lookup for authoritative participant names ────────────────────
# When Otter transcripts land with generic "Speaker 1"/"Speaker 2" labels and
# the call title is chrome-only ("Tomac Cove Weekly Call"), we fall back to the
# Google Calendar event most likely to correspond to the transcript. Attendees
# on that event give us real participant names, which the Pass 2 prompt uses
# to map Speaker N → real person for owner attribution.
#
# The token at ~/credentials/token.json already has calendar.readonly scope.
# Failure to find a match is non-fatal — we simply fall through to title-based
# extraction (_extract_participants_from_title).

def _calendar_events_near(token, iso_time, window_minutes=120):
    """Query primary calendar for events within ±(window_minutes/2) of iso_time.

    iso_time is the transcript's approximate call time (file.modifiedTime or a
    timestamp derived from the title). Returns a list of event dicts, or []
    on any failure — calendar lookup must never block extraction.
    """
    try:
        from datetime import datetime, timedelta, timezone
        s = iso_time.replace("Z", "+00:00") if iso_time and iso_time.endswith("Z") else iso_time
        t = datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        half = timedelta(minutes=window_minutes // 2)
        params = {
            "timeMin": (t - half).isoformat(),
            "timeMax": (t + half).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "20",
        }
        url = "https://www.googleapis.com/calendar/v3/calendars/primary/events?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("items", [])
    except Exception as e:
        print(f"    ⚠️   Calendar lookup failed ({type(e).__name__}: {e})", file=sys.stderr)
        return []


_CAL_MATCH_STOPWORDS = {
    "call", "meeting", "with", "and", "the", "sync", "chat", "catch",
    "interview", "intro", "weekly", "monthly", "daily",
}


def _pick_best_calendar_match(events, transcript_title, transcript_iso_time):
    """Score calendar events against the transcript and return the best match.

    Scoring combines title token Jaccard with a time-proximity bonus. Returns
    the event dict if combined score ≥ 0.2; otherwise None. Conservative on
    purpose — false negatives are cheap (fall through to title extraction),
    false positives inject wrong names into the prompt.
    """
    if not events:
        return None
    from datetime import datetime, timezone

    def toks(s):
        s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
        return {w for w in s.split() if w and w not in _CAL_MATCH_STOPWORDS and len(w) > 2}

    tt = toks(transcript_title)
    try:
        s = transcript_iso_time.replace("Z", "+00:00") if transcript_iso_time and transcript_iso_time.endswith("Z") else transcript_iso_time
        tt_time = datetime.fromisoformat(s)
        if tt_time.tzinfo is None:
            tt_time = tt_time.replace(tzinfo=timezone.utc)
    except Exception:
        tt_time = None

    best, best_score = None, 0.0
    for ev in events:
        et = toks(ev.get("summary", ""))
        union = tt | et
        jaccard = (len(tt & et) / len(union)) if union else 0.0
        prox = 0.0
        if tt_time:
            start = ev.get("start", {}).get("dateTime") or ""
            if "T" in start:
                try:
                    et_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    delta_min = abs((tt_time - et_time).total_seconds()) / 60
                    if delta_min < 15:
                        prox = 0.30
                    elif delta_min < 45:
                        prox = 0.15
                except Exception:
                    pass
        score = jaccard + prox
        if score > best_score:
            best, best_score = ev, score
    return best if best_score >= 0.2 else None


def _participants_from_event(event, exclude_emails=None):
    """Extract participant display-names from a calendar event's attendees.

    The principal's own email (from firm_context.yaml) is filtered by default —
    the prompt handles the principal separately.
    Falls back to email local-part (prettified) when displayName is missing.
    """
    if exclude_emails is None:
        exclude_emails = (_SELF_EMAIL,) if _SELF_EMAIL else ()
    out = []
    seen = set()
    for a in event.get("attendees", []) or []:
        email = (a.get("email") or "").lower()
        if a.get("self") or email in exclude_emails:
            continue
        name = a.get("displayName") or ""
        if not name and email:
            name = email.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
        if name and name not in seen:
            out.append(name)
            seen.add(name)
    return out


def resolve_participants(token, file_id, file_name, file_modified_time=None):
    """Unified participant resolver: calendar first, title as fallback.

    Returns (participants, source) where source is 'calendar' / 'title' / 'none'.
    Never raises — any failure falls through to the next layer.
    """
    # Calendar layer (authoritative when it matches).
    # Uses a separate calendar.readonly token — the gdrive pickle is
    # Drive+Docs only, so passing its token to /calendar/v3 returns 403.
    if file_modified_time:
        try:
            cal_token = get_calendar_token()
            if not cal_token:
                raise RuntimeError("calendar token unavailable — skipping lookup")
            events = _calendar_events_near(cal_token, file_modified_time)
            best = _pick_best_calendar_match(events, file_name, file_modified_time)
            if best:
                names = _participants_from_event(best)
                if names:
                    return names, "calendar"
        except Exception as e:
            print(f"    ⚠️   Calendar resolver skipped ({type(e).__name__}: {e})", file=sys.stderr)
    # Title fallback
    names = _extract_participants_from_title(file_name)
    if names:
        return names, "title"
    return [], "none"


def doc_text(doc):
    out = ""
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    out += pe["textRun"].get("content", "")
    return out


def read_file_content(token, file_id, mime_type):
    """Read content from either a Google Doc or a plain text Drive file."""
    if "google-apps.document" in mime_type:
        doc = gdocs_get(token, file_id)
        return doc_text(doc), doc
    else:
        # Plain text or other downloadable file
        text = drive_download_text(token, file_id)
        return text, None


def last_table_row_end(doc):
    last = 0
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            text = ""
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    text += pe["textRun"].get("content", "")
            if re.match(r"^\|\s*\d+\s*\|", text):
                last = elem.get("endIndex", last)
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
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def next_row_num(fu_text):
    nums = re.findall(r"^\|\s*(\d+)\s*\|", fu_text, re.MULTILINE)
    return max((int(n) for n in nums), default=0) + 1


# ── Pipeline cross-reference ──────────────────────────────────────────────────

def load_pipeline_context():
    """Return a compact string of deal targets + portfolio companies + LPs for Claude cross-reference,
    plus a DASHBOARD PATH REFERENCE block for exact routing of action items."""
    sections = []

    active_deals   = []  # (name, stage) for path reference
    lp_targets     = []  # (name, status) for path reference
    pipeline_paths = []  # (theme_label, target_name, status) for path reference

    # Deal pipeline targets
    if PIPELINE_DATA_PATH.exists():
        try:
            data = json.loads(PIPELINE_DATA_PATH.read_text())
            lines = [f"{_DEAL_WS.upper()} DEAL PIPELINE TARGETS (flag any match in deal_intel):"]  # noqa: tenant-leak — deferred schema key
            for theme in data.get("themes", []):
                targets = theme.get("targets", [])
                if not targets:
                    continue
                theme_label = theme.get("theme", "?")
                lines.append(f"\n[{theme_label}]")
                for t in targets:
                    status = t.get("status", "?")
                    name   = t.get("name", "?")
                    owner  = t.get("owner", "")
                    cap    = t.get("cap", "")
                    owner_short = owner.split(" — ")[0][:60] if " — " in owner else owner[:60]
                    cap_short   = cap[:50] if cap else ""
                    line = f"  [{status}] {name} — {owner_short}"
                    if cap_short:
                        line += f" — {cap_short}"
                    lines.append(line)
                    pipeline_paths.append((theme_label, name, status))
            sections.append("\n".join(lines))
        except Exception:
            pass

    # Portfolio companies + LP targets
    if DASHBOARD_DATA_PATH.exists():
        try:
            dash = json.loads(DASHBOARD_DATA_PATH.read_text())
            port_lines = [f"\n{_DEAL_WS.upper()} ACTIVE DEALS (COS Dashboard › {_DEAL_WS} Deals):"]
            for item in dash.get(_DEAL_WS, dash.get("tomac", [])):  # noqa: tenant-leak — backward-compat fallback
                name  = item.get("name", "?")
                stage = item.get("stage", "?")
                contacts = item.get("contacts", "")
                if name and "Update Log" not in name and stage != "Sourcing / Auto":
                    port_lines.append(f"  [{stage}] {name} | Key contacts: {contacts[:80]}")
                    active_deals.append((name, stage))
            if len(port_lines) > 1:
                sections.append("\n".join(port_lines))

            lp_lines = [f"\n{_DEAL_WS.upper()} LP TARGETS (COS Dashboard › {_DEAL_WS} Fundraising):"]
            for lp in dash.get("lpData", []):
                name   = lp.get("name", "?")
                status = lp.get("status", "?")
                lp_lines.append(f"  [{status}] {name}")
                lp_targets.append((name, status))
            if len(lp_lines) > 1:
                sections.append("\n".join(lp_lines))
        except Exception:
            pass

    # Build dashboard path reference block
    path_lines = ["\nDASHBOARD PATH REFERENCE — use exact strings in dashboard_path fields:"]
    path_lines.append(f"\nCOS DASHBOARD — {_DEAL_WS.upper()} DEALS:")
    for name, stage in active_deals:
        path_lines.append(f"  COS › {_DEAL_WS} Deals › {name}  [{stage}]")
    if not active_deals:
        path_lines.append("  (no active deals loaded)")

    path_lines.append(f"\nCOS DASHBOARD — {_DEAL_WS.upper()} FUNDRAISING / LP TARGETS:")
    for name, status in lp_targets:
        path_lines.append(f"  COS › {_DEAL_WS} Fundraising › {name}  [{status}]")
    if not lp_targets:
        path_lines.append("  (no LP targets loaded)")

    path_lines.append("\nDEAL PIPELINE DASHBOARD — THEMES & TARGETS:")
    current_theme = None
    for theme_label, target_name, status in pipeline_paths:
        if theme_label != current_theme:
            path_lines.append(f"  [{theme_label}]")
            current_theme = theme_label
        path_lines.append(f"    Deal Pipeline › {theme_label} › {target_name}  [{status}]")

    path_lines.append("\nOTHER VALID PATHS:")
    path_lines.append("  COS › Recruiting › [firm name]")
    path_lines.append("  COS › Follow-ups  (use only when no specific deal/LP applies)")

    sections.append("\n".join(path_lines))

    # Note: peer/co-investor firms are listed in BACKFILL_PREAMBLE (cached block 1)
    # and are not duplicated here to keep the pipeline context block lean.

    return "\n\n".join(sections)


# ── Claude extraction ─────────────────────────────────────────────────────────

# ── Pass 1: Analytical memo (Sonnet) ─────────────────────────────────────────

# Header: built from firm_context.yaml (no hardcoded names below).
# Body:   static memo-format instructions (same structure for all firms).
# Memo body built from firm_context.yaml prompt_overrides (section headers + guidance).
# Universal improvements to the default sections are pushed to _firm_context.py on
# GitHub and flow to all users on git pull. Per-firm customizations live in each
# user's gitignored firm_context.yaml and are never affected by upstream updates.
_MEMO_BODY = _fc.build_memo_body(_CTX)

MEMO_PREAMBLE = _fc.build_memo_header(_CTX) + _MEMO_BODY


def extract_memo(transcript_text, title, category="auto"):
    """Pass 1: Generate six-section investor memo. Always Sonnet — structured format-constrained prose.
    Opus is reserved for Pass 2 analytical thinking (deal ideation, pipeline connections)."""
    model = CLAUDE_MODEL  # Sonnet: memo writing is format-constrained, not multi-hop inference
    today = datetime.now().strftime("%Y-%m-%d")
    dynamic = f"CALL TITLE: {title}\nDATE: {today}\n\nTRANSCRIPT:\n{transcript_text[:40000]}"
    # Auth-mode aware dispatch (codified 2026-05-05). subscription path
    # bills against Pro/Max OAuth window; api path is the legacy
    # urllib POST behavior. Cache-control blocks are honored on api,
    # ignored (silently merged) on subscription.
    import _claude_dispatch  # noqa: PLC0415
    return _claude_dispatch.call(
        task_type="cos_otter_backfill_memo",
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": MEMO_PREAMBLE,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic},
        ]}],
    ).strip()


# ── Pass 2: JSON extraction — stable preamble cached across all items ─────────

# Header: built from firm_context.yaml (no hardcoded names below).
# Body:   static extraction-task instructions with dynamic owner/workstream labels.
# Caching: BACKFILL_PREAMBLE is computed once at import time and is identical
#          across all transcripts in a single run — Anthropic prompt cache hits.
_PRINCIPAL_FIRST = _fc.principal_first_name(_CTX)

_BACKFILL_BODY = f"""

Analyze the call transcript/memo provided below and extract ALL of the following. Respond ONLY with valid JSON.

EXTRACTION TASKS:

1. category: "{_RECRUIT_WS}" (job search/recruiter/employer calls), "{_DEAL_WS}" (deal/LP/investor/partner calls), or "Other"
   If the category hint provided below is not "auto", use it unless the text clearly contradicts it.
   IMPORTANT: If this is a market briefing, broker conference call, analyst call, or any call where {_PRINCIPAL_FIRST} is a LISTENER rather than an active participant (broker market briefings, industry/policy briefings, earnings calls, etc.), set category="Other" and action_items=[] — there are no actions on calls the principal did not participate in.

2. action_items: Every action that must still happen AFTER this call — whether {_PRINCIPAL_FIRST} committed to it OR someone else committed and {_PRINCIPAL_FIRST} needs to follow up.
   CRITICAL: Capture third-party commitments that require follow-up. If someone says "I'll connect you with X" or "I'll send you Y" — create an item to follow up if not received.
   MUST capture: intro commitments (by anyone), send commitments, social invites, callbacks, explicit asks, document/email requests.
   EXCLUDE: generic "prep for call", "review notes", vague "follow up" with no named party or specific content.
   EXCLUDE COMPLETED: actions already done during/before the call.
   EXCLUDE ONE-SIDED OFFERS — CRITICAL: Actions require MUTUAL ACCEPTANCE.
     If one party OFFERS to do something ("I could ping my ex-colleague on
     the Oncor board…" / "happy to make an intro if useful" / "I can send
     that over if it'd help") and the counterparty does NOT affirmatively
     accept within the transcript ("yes please" / "that'd be great" /
     "please do" / "go ahead" / silent assent after a direct ask), it is
     NOT an action — DO NOT emit it. Offers that the counterparty brushes
     past, ignores, or pivots away from are conversational texture, not
     commitments. A one-sided offer belongs in one_line_summary context at
     most. When in doubt whether acceptance happened, EXCLUDE.
   DISTINGUISH action_type carefully:
     "new_action" — a genuine new commitment not yet tracked anywhere; WILL be written to the Follow-ups doc.
     "status_update" — the call just provided a status on something already being tracked (e.g. "proposal expected this week", "call already scheduled"). Do NOT write to Follow-ups; include in the PROCESSED header only.
   Each item: {{
     "who": "person/firm being actioned",
     "what": "verb-first specific action",
     "due": "YYYY-MM-DD",
     "owner": "{_OWNERS}|[named speaker]",
     "workstream": "Job Search|{_DEAL_WS}",
     "action_type": "new_action|status_update",
     "state": "active|waiting|watching|blocked|dormant|closed",
     "resolution_source": "(only when state=closed) cite the evidence: doc title + date, follow-up [RESOLVED] tag, etc.",
     "context": "specific context. Use one of these patterns: 'fundraising/LP discussion — [LP firm name]', 'deal diligence — [deal/asset name]', 'deal origination — [target name]', 'competitive intel — [peer firm]', 'recruiting — [firm name]'. Always name the specific entity.",
     "dashboard_path": "REQUIRED — exact path from DASHBOARD PATH REFERENCE above. NEVER leave empty. Format: 'COS › {_DEAL_WS} Deals › [deal name]', 'COS › {_DEAL_WS} Fundraising › [LP name]', 'Deal Pipeline › [theme] › [target name]'. Use 'COS › Follow-ups' only when no specific deal/LP/recruiting path applies."
   }}

   STATE FIELD GUIDE (codified 2026-05-04 — eliminates compile-time inference of state from prose):
     • "active"   → I'm doing something soon. The principal/team owns the next move.
     • "waiting"  → They owe me something. Counterparty owns the next move.
     • "watching" → Passive intel; no action expected near-term.
     • "blocked"  → Gated on a known dependency (decision, prior commitment, doc receipt). Name the gate in `what`.
     • "dormant"  → Relationship/item paused; reactivate only on fresh signal.
     • "closed"   → Done. Populate `resolution_source` with evidence.
   Pick the strongest applicable state. Default if unclear: "active" for principal-side actions, "waiting" for counterparty-side.

3. new_contacts: Every person named who should be tracked — including people mentioned but not on the call.
   Each: {{"name": "...", "firm": "...", "title": "...", "context": "one line — how they came up and why relevant", "confidence": "high|medium|low"}}

   CONFIDENCE GUIDE:
     • "high"   → Named with role/firm AND co-occurs with action verb (commit, send, schedule, intro, decided) OR principal explicitly engages with them on the call.
     • "medium" → Named clearly but only context, no action commitment.
     • "low"    → Passing mention; could be a peer/competitor reference rather than an actionable contact.
   Default: "medium". Compile uses confidence to gate auto-promotion of new firms to fundraising/deal config — `low` mentions are kept in a triage queue, not auto-added.

4. recruiting_intel: Populate whenever there is job search, firm evaluation, or role discussion — regardless of call category.
   {{"firm":"","role":"","stage":"Screening|Longlist|Shortlist|Live Process|Networking","key_dates":"","comp_intel":"","notes":""}}

5. deal_intel: Named LP or deal intelligence — populate for ANY category if the call touches a firm from the DEAL PIPELINE TARGETS or LP TARGETS above, OR if there is competitive intel, co-investment discussion, or deal origination relevant to the firm.  # noqa: tenant-leak — deferred schema key
   Each: {{
     "investor_or_firm": "",
     "status": "Active|Qualified|Hold|Long-term|Unknown",
     "key_feedback": "named entity + specific data point — be EXHAUSTIVE for LP/fundraising calls: if specific investor names were mentioned as targets or appropriate for a given strategy/gameplan, list them ALL by name with the strategy context (format: 'Firm A, Firm B, Firm C — appropriate for [strategy]; Firm D, Firm E — appropriate for [other strategy]'). Never group-summarize investor type without naming the specific firms mentioned.",
     "next_action": "",
     "intel_type": "LP/fundraising|deal intel|competitive intel|co-investor|deal origination",
     "dashboard_path": "exact path from DASHBOARD PATH REFERENCE above"
   }}

6. one_line_summary: Under 25 words. Lead with the so-what for a senior investor — name the firms, the deal angle, and the outcome.

7. call_date: Best estimate of when the call occurred. Format YYYY-MM-DD. Use document date, title clues, or content. Default to TODAY if unknown.

8. mentioned_firms: Array of every firm/organization name surfaced in the transcript — actionable AND passing references. Powers the inverse-audit ("what's mentioned but not on the dashboard?") sweep. NEVER omit firms just because they didn't generate an action_item.
   Each: {{"name": "...", "context": "one-phrase: what role did they play in the conversation?"}}

9. envelope_items: Routing-v2 items for the dashboard. Emit IN ADDITION to (not instead of) fields 2-5 above.

   STALENESS FILTER — do not emit awaiting_external items for past one-time events:
   If the item is about a conference, summit, forum, registration, RSVP, or attendance at a specific event AND the event date has already passed as of TODAY, omit the awaiting_external item entirely. The opportunity is gone. Same applies to scheduling proposals (propose times, send calendar invite) where the proposed date has clearly passed. Follow the ENVELOPE ROUTING RULES section injected below (loaded from config/routing-rules.md — the single source of truth shared with all other pipelines). For direct-interaction calls like this one, all seven content_types are permitted; apply the "{_DEAL_WS} / LP / recruiting calls" or "Intel calls" ruleset as appropriate based on whether the principal is a participant or listener.

   TIME-REFERENCE NORMALIZATION — never emit floating phrases like "next week", "early next week", "later this week", "end of the week", "end of next week" in `content` / `what` / `context`. These go silently stale once the date passes. Always materialize to "week of YYYY-MM-DD" using the call_date as the anchor (snap forward to the Monday on/after the implied target). Same rule for "tomorrow" → explicit YYYY-MM-DD, and "later today" → explicit ISO datetime. The compile layer also normalizes after the fact; doing it here prevents the stale phrasing from ever entering the pipeline. Codified in dash_corrections.md (2026-05-04).

   ACTION-DIRECTION INVERSION CHECK (rule Y2) — when the action verb is a transmission verb (`send`, `share`, `deliver`, `forward`, `provide`, `transmit`, `circulate`, `pass along`), explicitly identify which side is the sender by inspecting role context BEFORE emitting the item:
   • Investment banks / placement agents / advisors pitching deal flow to the principal → THEY send teasers/CIMs/data rooms TO the principal. Counterparty owns the action; emit `state: waiting`, `owner: external`, `counterparty: "Firm — Person"`. The principal RECEIVES; do NOT emit a my_action telling the principal to "send" what is being pitched IN.
   • Principal sponsoring a deal to LPs / co-investors / lenders → PRINCIPAL sends materials. Emit `owner: <{"| ".join(_CTX.get("owner_whitelist", ["principal"]))}>`, `state: active`.
   • Mutual exchanges (NDAs, term sheets, mark-ups, redlines passed back and forth) → emit two items, one per direction, each with the correct owner.
   • Default if unclear: emit as `state: waiting` with the counterparty as owner — better to under-attribute to the principal than fabricate a send-verb on the wrong side. The "advisor-flip" failure pattern: a fundraising advisor pitching deal flow IN was wrongly written as the principal owing the send.

   ABSOLUTE-DATE RULE (rule AB1) — every reference to a date or week in `content`, `context`, or `due` MUST be absolute YYYY-MM-DD form. Resolve relative phrasing against the transcript's date.
   • ALLOWED: `2026-05-12`, `week of 2026-05-12`, `May 12 2026`
   • FORBIDDEN: `tomorrow`, `next week`, `this Friday`, `Wed 4/29`, `Friday 5/1`, `EOD`, `early next week`, `next Monday`
   • Example: transcript dated 2026-05-04 says "by next Monday" → emit `due: 2026-05-12`, content "Deliver X by 2026-05-12".
   • Why: the item lives on the dashboard for days. Relative phrasing reads stale every day after extraction even when the action is still valid; absolute dates never go stale.

   COUNTERPARTY PLACEHOLDERS — when the firm cannot be identified from the transcript, emit `counterparty: ""` and tag the intel as `intel_type: "unattributed"`. NEVER emit a generic placeholder like "assistant", "attorneys", "Unknown", "team", a bare email address, or a person's name with no firm context. Unattributed items are routed to a review queue at compile time; placeholder firms pollute the by-firm awaiting list and make the dashboard noisier.

   STRICT RULES (enforced at write time — items failing these are rejected and counted in routingExceptions[], so missing them costs you data):
   • content_type="status_update" REQUIRES a non-empty parent_id (a short deal ticker, e.g. an uppercase abbreviation of the deal name, OR an LP slug). If you cannot identify the parent deal/LP, do NOT emit a status_update — emit it as a deal_takeaway instead (which only needs a parent_id when the parent is known).
   • content_type="lp_intel" REQUIRES parent_id (LP slug). Same rule.
   • owner field accepts ONLY: {", ".join(f'"{o}"' for o in _CTX.get("owner_whitelist", []))}, or "external". Common variant spellings (full name, nickname, alternate spelling) are normalized to the whitelist automatically — but anything outside the whitelist (e.g. a third-party firm name as owner) is rejected. When the action belongs to a counterparty, owner="external" and counterparty="Firm — Person".
   • content_type="awaiting_external" with owner="external" REQUIRES counterparty in "Firm — Person" format.

10. deal_log_entries: Array. For each ACTIVE DEAL listed in the DEAL PIPELINE TARGETS block above that this transcript SUBSTANTIVELY touches (not a passing mention), emit ONE entry per deal:
   {{
     "deal_id": "<ticker-or-slug exactly as given in the DEAL PIPELINE TARGETS block>",
     "summary": "<≤25 words: what happened on this deal — who said what, what moved, what stalled>",
     "evidence": "<≤100 chars verbatim quote from the transcript anchoring this entry>"
   }}
   Skip the deal entirely if it was only mentioned in passing (no decision, no data point, no action, no commitment). High-precision tagging — better to omit than to over-attribute. Codified 2026-05-04 (rule V1+).

RESPOND WITH THIS JSON ONLY (no markdown, no explanation):
{{"category":"...","one_line_summary":"...","call_date":"...","action_items":[{{"who":"...","what":"...","due":"...","owner":"...","workstream":"...","action_type":"new_action|status_update","state":"active|waiting|watching|blocked|dormant|closed","resolution_source":"","context":"...","dashboard_path":"..."}}],"new_contacts":[{{"name":"...","firm":"...","title":"...","context":"...","confidence":"high|medium|low"}}],"recruiting_intel":{{}},"deal_intel":[{{"investor_or_firm":"...","status":"...","key_feedback":"...","next_action":"...","intel_type":"...","dashboard_path":"..."}}],"mentioned_firms":[{{"name":"...","context":"..."}}],"envelope_items":[{{"content_type":"...","owner":"...","counterparty":"","parent_id":"","due":"","context":"...","dashboard_path":"...","content":"..."}}],"deal_log_entries":[{{"deal_id":"","summary":"","evidence":""}}]}}
"""

BACKFILL_PREAMBLE = _fc.build_backfill_header(_CTX) + _BACKFILL_BODY


def _extract_participants_from_title(title):
    """Best-effort participant-name extraction from a call title.

    Otter titles frequently encode participants as "First Last and First Last",
    "Firstname — Firm" (em-dash), "First Last + First Last", "Call with First Last",
    or "Firm / First Last". Returns a list of plausible person-name strings, or [].

    Conservative by design — it's OK to return [] if the title is chrome-only
    (e.g. "Weekly All-Hands Call"). The downstream prompt treats this as a hint,
    not a contract: the model still has to confirm by content.
    """
    if not title:
        return []
    # Strip leading/trailing separator chrome (box-drawing, hyphens, stars)
    t = re.sub(r'^[\s\W_]+|[\s\W_]+$', '', title)
    # Drop date prefix: "2026-04-21 " or "2026-04-21T..."
    t = re.sub(r'^\d{4}-\d{2}-\d{2}[T\s]+', '', t)
    # Drop leading "Call with"/"Meeting with"/"Interview with"/"Re:" scaffolding
    t = re.sub(r'^(call with|meeting with|interview with|intro with|chat with|catch[- ]up with|re:)\s+', '', t, flags=re.IGNORECASE)

    # Split on connectors commonly used for multi-party calls, including em/en dash
    parts = re.split(r'\s*(?:\band\b|\bwith\b|\bw/\b|&|\+|,|/|\||—|–|\s-\s)\s*', t, flags=re.IGNORECASE)

    # Deny-list of phrases that match the name shape but are not people.
    # Generic chrome is static; firm/deal-specific names are injected from
    # firm context at runtime so no tenant strings live in this public file.
    _NON_PERSON = {
        'Europe Gas', 'Europe Gas Call', 'Weekly Call', 'Market Blast',
        'Policy Day', 'Capstone DC', 'Capstone DC Policy Day',
        'Gas Call', 'Daily Call', 'Monthly Call',
    }
    if _DEAL_WS:
        _NON_PERSON |= {_DEAL_WS, f"{_DEAL_WS} Weekly", f"{_DEAL_WS} Weekly Call"}
    _NON_PERSON |= set(_CTX.get("call_title_chrome") or [])
    _NAME_RE = re.compile(
        r'^([A-Z][a-zA-Z\'\-]+)(\s+(?:de|van|der|da|la|le|bin|al))?'
        r'(\s+[A-Z][a-zA-Z\'\-\.]+){1,3}$'
    )
    candidates = []
    for p in parts:
        p = p.strip(' -—–·:()[]"\'')
        if not p or p in _NON_PERSON:
            continue
        # Reject fragments containing obvious non-name words
        if re.search(r'\b(Call|Meeting|Weekly|Monthly|Daily|Call\b|Partners?|Cove|Capital|Fund|Energy|Hub|Inc|LLC|LP|Company)\b', p):
            continue
        # 2-4 token person name: "First Last" / "First Middle Last" / etc.
        if _NAME_RE.match(p):
            candidates.append(p)
        # Single-token first name (only when another multi-token name is already present)
        elif re.match(r'^[A-Z][a-zA-Z\'\-]+$', p) and candidates:
            candidates.append(p)
    # Dedupe preserving order, cap at 6 to prevent runaway
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= 6:
            break
    return out


def extract_all(transcript_text, title, hint_category="auto", pipeline_context="", memo_text="",
                participants_hint=None, participants_source="title"):
    """Pass 2 extraction.

    participants_hint: optional list of real participant names. When provided
    (typically from calendar lookup), it's used directly; otherwise falls back
    to _extract_participants_from_title(title). participants_source labels the
    origin so the prompt knows how much to trust it ("calendar" is
    authoritative; "title" is a hint to confirm from content).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    # Per-transcript dynamic block.
    # When a Pass 1 memo is available, use it as the primary analytical source.
    # The raw transcript follows as a reference for specific details the memo may omit.
    if participants_hint is not None:
        participants = participants_hint
    else:
        participants = _extract_participants_from_title(title)
        participants_source = "title"
    if participants:
        trust_note = (
            "authoritative — these are the calendar attendees"
            if participants_source == "calendar"
            else "hint from title — confirm from content"
        )
        participants_line = (
            f"PARTICIPANTS ({trust_note}): {', '.join(participants)}\n"
        )
    else:
        participants_line = "PARTICIPANTS: (not resolved — infer from content)\n"
    dynamic = (
        f"CALL TITLE: {title}\n"
        f"TODAY: {today}\n"
        f"CATEGORY HINT: {hint_category}\n"
        f"{participants_line}\n"
    )
    if memo_text:
        # Cap memo at 7000 chars — preserves all six sections for most memos while
        # preventing runaway extraction on dense multi-deal TC Weekly calls.
        memo_excerpt = memo_text[:7000]
        dynamic += (
            f"INVESTOR MEMO (primary source — produced by senior analyst pass; "
            f"use this as your main input for extraction):\n{memo_excerpt}\n\n"
            f"RAW TRANSCRIPT (reference — consult for specific names, numbers, or "
            f"details not fully captured in the memo above):\n{transcript_text[:15000]}"
        )
    else:
        dynamic += f"TRANSCRIPT:\n{transcript_text[:32000]}"

    # Four-block structure:
    #  Block 0 (cached): shared routing-rules.md — single source of truth for envelope contract
    #  Block 1 (cached): stable preamble — Yoni profile, investment context, extraction rules
    #  Block 2 (cached): pipeline context — deal targets + LPs; same for all transcripts in a run
    #  Block 3 (uncached): per-transcript dynamic — title, hint, memo + transcript
    routing_rules = _load_routing_rules()
    content = []
    if routing_rules:
        content.append({
            "type": "text",
            "text": "ENVELOPE ROUTING RULES (shared contract — see config/routing-rules.md):\n\n" + routing_rules,
            "cache_control": {"type": "ephemeral"},
        })
    content.append({
        "type": "text",
        "text": BACKFILL_PREAMBLE,
        "cache_control": {"type": "ephemeral"},
    })
    if pipeline_context:
        content.append({
            "type": "text",
            "text": pipeline_context,
            "cache_control": {"type": "ephemeral"},
        })
    content.append({"type": "text", "text": dynamic})

    # Pass 2: Opus for deal calls (unknown category) (deal ideation, pipeline connections, firm right-to-win
    # angle — multi-hop inference connecting call content → ownership structures → entry paths).
    # Sonnet for Recruiting/Other (structured extraction, no deal inference needed).
    p2_model = MEMO_MODEL if hint_category not in ("Recruiting", "Other") else CLAUDE_MODEL
    # Auth-mode aware dispatch (codified 2026-05-05).
    import _claude_dispatch  # noqa: PLC0415
    raw = _claude_dispatch.call(
        task_type="cos_otter_backfill",
        model=p2_model,
        max_tokens=8192,
        messages=[{"role": "user", "content": content}],
        api_timeout=90,
    ).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ── Dedup tracker ─────────────────────────────────────────────────────────────

def load_dedup():
    if DEDUP_PATH.exists():
        return json.loads(DEDUP_PATH.read_text())
    return {}


def save_dedup(tracker):
    DEDUP_PATH.write_text(json.dumps(tracker, indent=2))


def mark_processed(tracker, file_id, title, category):
    tracker[file_id] = {
        "processed_at": datetime.now().isoformat(),
        "title": title,
        "category": category,
    }


# ── Write helpers ─────────────────────────────────────────────────────────────

def _backfill_action_dashboard_paths(actions, envelope_items, deal_intel, category):
    """Fill empty dashboard_path on action_items by cross-referencing envelope_items and deal_intel.

    Priority: (1) content-match against envelope_items, (2) first deal/LP path from
    envelope_items, (3) first deal_intel path, (4) category-based default.
    """
    if not actions:
        return

    env_items = envelope_items or []
    deal_items = deal_intel or []

    # Collect all non-empty paths from envelope items, preferring deal/LP paths
    deal_env_paths = [e.get("dashboard_path", "") for e in env_items
                      if e.get("dashboard_path") and
                      (f"{_DEAL_WS} Deals" in e.get("dashboard_path", "") or
                       f"{_DEAL_WS} Fundraising" in e.get("dashboard_path", "") or
                       _RECRUIT_WS in e.get("dashboard_path", ""))]
    deal_intel_paths = [t.get("dashboard_path", "") for t in deal_items
                         if t.get("dashboard_path")]

    best_deal_path = next(iter(deal_env_paths), "")
    best_deal_intel_path = next(iter(deal_intel_paths), "")

    if category == _RECRUIT_WS:
        fallback = best_deal_path or f"COS › {_RECRUIT_WS}"
    elif category == _DEAL_WS:
        fallback = best_deal_path or best_deal_intel_path or f"COS › {_DEAL_WS} Deals"
    else:
        fallback = best_deal_path or "COS › Follow-ups"

    for action in actions:
        if action.get("dashboard_path"):
            continue
        # Try content-match against envelope_items
        what = (action.get("what") or "").lower()
        matched = ""
        for e in env_items:
            e_path = e.get("dashboard_path", "")
            if not e_path:
                continue
            e_content = (e.get("content", "") or "").lower()
            if len(what) > 8 and (what[:25] in e_content or e_content[:25] in what):
                matched = e_path
                break
        action["dashboard_path"] = matched or fallback


def write_followups(token, actions, title, doc_link, stats):
    if not actions:
        return 0
    try:
        fu_doc  = gdocs_get(token, FOLLOW_UPS_DOC)
        fu_text = doc_text(fu_doc)
        row_num = next_row_num(fu_text)
        insert_at = last_table_row_end(fu_doc)
        new_rows = ""
        added = 0
        skipped_updates = 0
        for item in actions:
            what = item.get("what", "").strip()
            if not what:
                continue
            # Skip status updates — they reflect existing tracked items, not new tasks
            if item.get("action_type") == "status_update":
                skipped_updates += 1
                continue
            who           = item.get("who", _fc.principal_first_name(_CTX))
            due           = item.get("due", "TBD")
            ws            = item.get("workstream", _DEAL_WS)
            context       = item.get("context", "").replace("|", "/")
            dash_path     = (item.get("dashboard_path") or "COS › Follow-ups").replace("|", "/")
            new_rows += f"| {row_num} | {who} | {what} | {due} | {ws} | call — {title} | {doc_link} | {context} | {dash_path} |\n"
            row_num += 1
            added += 1
        if new_rows:
            gdocs_insert(token, FOLLOW_UPS_DOC, insert_at, new_rows)
            stats["followups_added"] += added
        suffix = f" ({skipped_updates} status updates skipped)" if skipped_updates else ""
        print(f"    ✅  Follow-ups: +{added} rows{suffix}", flush=True)
        return added
    except Exception as e:
        print(f"    ❌  Follow-ups write failed: {e}", file=sys.stderr)
        return 0


def write_people(token, contacts, title, today, stats):
    if not contacts:
        return
    try:
        pdoc = gdocs_get(token, PEOPLE_DOC)
        pcontent = pdoc.get("body", {}).get("content", [])
        pend = (pcontent[-1].get("endIndex", 2) - 1) if pcontent else 1
        ptext = f"\n\n─── From call: {title} ({today}) ───\n"
        for c in contacts:
            ptext += (
                f"\n{c.get('name','?')} / {c.get('firm','?')}"
                + (f" — {c.get('title')}" if c.get("title") else "")
                + f"\n  {c.get('context','')}\n"
            )
        gdocs_insert(token, PEOPLE_DOC, pend, ptext)
        stats["contacts_added"] += len(contacts)
        print(f"    ✅  People doc: +{len(contacts)} contacts", flush=True)
    except Exception as e:
        print(f"    ❌  People doc write failed: {e}", file=sys.stderr)


def write_recruiting(token, rec_intel, title, today):
    if not rec_intel or not rec_intel.get("firm"):
        return "No change"
    try:
        rdoc = gdocs_get(token, RECRUITING_DOC)
        rcontent = rdoc.get("body", {}).get("content", [])
        rend = (rcontent[-1].get("endIndex", 2) - 1) if rcontent else 1
        rtext = (
            f"\n\n## {rec_intel.get('firm','?')}\n"
            f"**Firm:** {rec_intel.get('firm','?')}\n"
            f"**Role:** {rec_intel.get('role','?')}\n"
            f"**Stage:** {rec_intel.get('stage','Screening')}\n"
            f"**Last action:** {today} — recorded call: {title}\n"
            f"**Next step:** {rec_intel.get('key_dates','?')}\n"
            f"**Notes:**\n"
            f"- {today}: {rec_intel.get('notes','')}\n"
            f"  Comp: {rec_intel.get('comp_intel','None surfaced')}\n"
        )
        gdocs_insert(token, RECRUITING_DOC, rend, rtext)
        print(f"    ✅  Recruiting doc: {rec_intel.get('firm')}", flush=True)
        return f"Updated: {rec_intel.get('firm')}"
    except Exception as e:
        print(f"    ❌  Recruiting doc write failed: {e}", file=sys.stderr)
        return "Error"


def write_deal_intel(token, deal_intel, title, today):
    if not deal_intel:
        return "No change"
    try:
        tdoc = gdocs_get(token, DEAL_PIPELINE_DOC)
        tcontent = tdoc.get("body", {}).get("content", [])
        tend = (tcontent[-1].get("endIndex", 2) - 1) if tcontent else 1
        ttext = (
            f"\n\n### [{today}] LP Investor Intel — {title}\n"
            f"| Investor | Status | Key feedback | Next action |\n"
            f"|---|---|---|---|\n"
        )
        for item in deal_intel:
            ttext += (
                f"| {item.get('investor_or_firm','?')} "
                f"| {item.get('status','?')} "
                f"| {item.get('key_feedback','?')} "
                f"| {item.get('next_action','?')} |\n"
            )
        gdocs_insert(token, DEAL_PIPELINE_DOC, tend, ttext)
        print(f"    ✅  {_DEAL_WS} doc: {len(deal_intel)} LP intel rows", flush=True)
        return "Updated"
    except Exception as e:
        print(f"    ❌  {_DEAL_WS} doc write failed: {e}", file=sys.stderr)
        return "Error"


def build_processing_header(category, now_str, actions, deal_intel, rec_intel,
                            n_added, n_contacts, rec_result, deal_result):
    """Return the processing summary block as a plain string."""
    new_actions    = [i for i in actions if i.get("action_type") != "status_update"]
    status_updates = [i for i in actions if i.get("action_type") == "status_update"]

    header = (
        f"╔══════════════════════════════════════════════════════════════════╗\n"
        f"PROCESSED: {now_str}  |  Category: {category}\n"
        f"Source: Otter.ai transcript / AssemblyAI call recording\n"
        f"╚══════════════════════════════════════════════════════════════════╝\n\n"
        f"NEW ACTION ITEMS ({len(new_actions)}):\n"
    )
    for item in new_actions:
        owner   = item.get("owner", item.get("who", ""))
        context = item.get("context", "")
        dpath   = item.get("dashboard_path", "")
        header += f"  → [{owner}] {item.get('what','')} — Due {item.get('due','?')}\n"
        if context:
            header += f"      Context: {context}\n"
        if dpath:
            header += f"      Dashboard: {dpath}\n"
    if not new_actions:
        header += "  None\n"

    if status_updates:
        header += f"\nSTATUS UPDATES ({len(status_updates)}) — already tracked, not added to Follow-ups:\n"
        for item in status_updates:
            dpath = item.get("dashboard_path", "")
            header += f"  ↳ {item.get('what','')} ({dpath})\n"

    if rec_intel and rec_intel.get("firm"):
        header += (
            f"\nRECRUITING INTEL:\n"
            f"  Firm: {rec_intel.get('firm','')} | Stage: {rec_intel.get('stage','')} | "
            f"Role: {rec_intel.get('role','')}\n"
            f"  {rec_intel.get('notes','')}\n"
        )
    header += "\nKEY INTEL:\n"
    for item in deal_intel:
        intel_type = item.get("intel_type", "")
        dpath      = item.get("dashboard_path", "")
        header += f"  • [{intel_type}] {item.get('investor_or_firm','')}: {item.get('key_feedback','')}\n"
        if dpath:
            header += f"      → {dpath}\n"
    if not deal_intel:
        header += "  None\n"
    header += (
        f"\nDOCS TOUCHED: Follow-ups (+{n_added} rows)"
        f" | People ({'+'+ str(n_contacts) if n_contacts else 'No change'})"
        f" | Recruiting ({rec_result})"
        f" | {_DEAL_WS} Pipeline ({deal_result})\n"
        f"══════════════════════════════════════════════════════════════════════\n\n"
    )
    return header


_STATUS_UPDATES_PATH = Path.home() / "dashboards" / "data" / "user-state" / "transcript-status-updates.json"

def persist_status_updates(actions, category, transcript_title, transcript_date):
    """Append status_update items extracted from a transcript to
    data/user-state/transcript-status-updates.json so cos_email_resolver.py can
    match them against pending followUps. Status updates are signals that work
    already happened — they're the strongest non-email resolution source.
    Each entry: {who, what, context, dashboard_path, category, transcript_title,
                 transcript_date, persisted_at}.
    Bounded to last 200 entries so the file doesn't grow unbounded.
    """
    status_updates = [i for i in actions if i.get("action_type") == "status_update"]
    if not status_updates:
        return
    try:
        existing = []
        if _STATUS_UPDATES_PATH.exists():
            existing = json.loads(_STATUS_UPDATES_PATH.read_text() or "[]")
    except Exception:
        existing = []
    now_iso = datetime.now().isoformat()
    for item in status_updates:
        existing.append({
            "who":              item.get("owner", item.get("who", "")),
            "what":             item.get("what", ""),
            "context":          item.get("context", ""),
            "dashboard_path":   item.get("dashboard_path", ""),
            "category":         category,
            "transcript_title": transcript_title,
            "transcript_date":  transcript_date,
            "persisted_at":     now_iso,
        })
    # Keep last 200 entries; drop oldest by persisted_at
    existing.sort(key=lambda x: x.get("persisted_at", ""), reverse=True)
    existing = existing[:200]
    try:
        _STATUS_UPDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATUS_UPDATES_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        print(f"    ✅  Persisted {len(status_updates)} status update(s) for resolver", flush=True)
    except Exception as e:
        print(f"    ⚠️   status-updates persist failed: {e}", file=sys.stderr)


def write_processing_header(token, doc_id, category, now_str, actions, deal_intel, rec_intel,
                            n_added, n_contacts, rec_result, deal_result,
                            is_gdoc=True, original_text="", memo_text=""):
    header = build_processing_header(
        category, now_str, actions, deal_intel, rec_intel,
        n_added, n_contacts, rec_result, deal_result,
    )
    # Append memo section directly after the PROCESSED header if available.
    # This places the full analytical memo at the top of the doc before the
    # Otter AI summary and raw transcript.
    if memo_text:
        header += (
            "INVESTOR MEMO\n"
            "────────────────────────────────────────────────────────────\n\n"
            + memo_text
            + "\n\n════════════════════════════════════════════════════════════════════\n\n"
        )
    try:
        if is_gdoc:
            gdocs_insert(token, doc_id, 1, header)
            memo_note = " + memo" if memo_text else ""
            print(f"    ✅  Processing header{memo_note} written to Google Doc", flush=True)
        else:
            # Prepend header (+ memo) to the plain text file and re-upload
            new_content = header + original_text
            drive_upload_text(token, doc_id, new_content)
            memo_note = " + memo" if memo_text else ""
            print(f"    ✅  Processing header{memo_note} prepended to plain text file", flush=True)
    except Exception as e:
        print(f"    ⚠️   Header write failed (non-critical): {e}", file=sys.stderr)


# ── Process one transcript ────────────────────────────────────────────────────

def process_transcript(token, file_id, file_name, hint_category, source_label, stats,
                       mime_type="application/vnd.google-apps.document",
                       pipeline_context="",
                       pre_loaded_text=None):
    """Process a single transcript file — memo pass + extraction + Doc writes.

    Args:
        token:            Google OAuth access token (can be None for local files)
        file_id:          Drive file ID or local path string (used as unique ID)
        file_name:        Display name of the file
        hint_category:    Category hint passed by the source ("auto", "Recruiting", etc.)
        source_label:     Human-readable source name shown in logs
        stats:            Mutable stats dict updated in-place
        mime_type:        Drive MIME type (ignored when pre_loaded_text is set)
        pipeline_context: Deal pipeline JSON injected into extraction prompt
        pre_loaded_text:  Pre-read transcript text (skips Drive read; for local files)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    is_gdoc = "google-apps.document" in mime_type and pre_loaded_text is None
    if pre_loaded_text is not None:
        # Local file — no Drive URL
        doc_link = f"file://{file_id}" if file_id.startswith("/") else file_id
    elif is_gdoc:
        doc_link = f"https://docs.google.com/document/d/{file_id}/edit"
    else:
        doc_link = f"https://drive.google.com/file/d/{file_id}/view"

    print(f"\n  → Processing: {file_name}", flush=True)

    # ── Pre-classify title ────────────────────────────────────────────────────
    # Detect market intel / conference calls from the title alone.
    # These are calls Yoni listened to (not a direct participant) —
    # no action items should be generated for them.
    _, is_intel_call = classify_title_hint(file_name)
    if is_intel_call and hint_category == "auto":
        hint_category = "Other"
        print(f"    ℹ️   Intel/conference call detected — forcing category=Other, skipping actions", flush=True)

    # Read transcript (skip for local files that passed pre_loaded_text)
    if pre_loaded_text is not None:
        text, doc_obj = pre_loaded_text, None
    else:
        try:
            text, doc_obj = read_file_content(token, file_id, mime_type)
        except Exception as e:
            print(f"    ❌  Could not read file: {e}", file=sys.stderr)
            return None

    if len(text) < 150:
        print(f"    ⚠️   Too short ({len(text)} chars) — skipping", flush=True)
        return {"category": "Other", "skipped": True}

    # ── Phonetic-correct transcript body BEFORE extraction ───────────────────
    # Otter mishears known names ("Wafra"→"Wafer", "Reinova"→"Raynova"). The
    # normalizer applies whole-word substitutions from
    # ~/credentials/phonetic_corrections.json so the LLM sees canonical names
    # in context. Short entity fields (who/firm/counterparty) are reconciled
    # against the canonical roster after extraction (see below).
    _norm = _get_normalizer()
    text, _phonetic_applied = _norm.apply_phonetic(text)
    if _phonetic_applied:
        stats.setdefault("phonetic_corrections", []).extend(_phonetic_applied)
        print(f"    🔤  Phonetic corrections applied: {_phonetic_applied}", flush=True)

    # ── Resolve participant names for Speaker N → real person mapping ────────
    # Calendar match at the file's modifiedTime is authoritative; title parsing
    # is the fallback. Result is passed into Pass 2 as the PARTICIPANTS hint.
    # Non-fatal: any failure leaves participants=None and Pass 2 falls back to
    # title-only extraction inside extract_all.
    # Skipped for local files (no Drive metadata, no calendar match possible).
    participants_hint, participants_source = [], "none"
    if pre_loaded_text is None:
        try:
            meta = drive_get_meta(token, file_id)
            mod_time = meta.get("modifiedTime") if meta else None
            participants_hint, participants_source = resolve_participants(
                token, file_id, file_name, file_modified_time=mod_time
            )
            if participants_hint:
                print(f"    ℹ️   Participants ({participants_source}): {', '.join(participants_hint)}", flush=True)
        except Exception as e:
            print(f"    ⚠️   Participant resolution skipped: {e}", file=sys.stderr)

    # ── Pass 1: Structured memo (Sonnet) ─────────────────────────────────────
    # Format-constrained six-section prose. Always Sonnet — Opus is reserved
    # for Pass 2 deal ideation and pipeline inference.
    memo_text = ""
    try:
        print(f"    ⏳  Pass 1: generating investor memo (Sonnet)…", flush=True)
        memo_text = extract_memo(text, file_name, category=hint_category)
        print(f"    ✅  Memo ready ({len(memo_text)} chars)", flush=True)
    except Exception as e:
        print(f"    ⚠️   Pass 1 memo failed — falling back to single-pass: {e}", file=sys.stderr)

    # ── Pass 2: Deal analysis + JSON extraction (Opus/Sonnet) ────────────────
    # Opus for deal calls (unknown category): multi-hop deal ideation, pipeline connections,
    # firm right-to-win angle. Sonnet for Recruiting/Other: structured extraction only.
    # Primary input is the memo when available; raw transcript provided as
    # reference for specific details the memo may not have fully captured.
    try:
        data = extract_all(
            text, file_name, hint_category, pipeline_context, memo_text=memo_text,
            participants_hint=participants_hint or None,
            participants_source=participants_source,
        )
    except Exception as e:
        print(f"    ❌  Claude extraction failed: {e}", file=sys.stderr)
        return None

    # ── Post-extraction entity reconciliation ────────────────────────────────
    # Walk action who, contact name/firm, intel counterparty/firm; reconcile
    # against canonical roster (LPs + deals + pipeline targets). Mutates `data`
    # in place. Stats include phonetic, canonical, vague, and speaker-unresolved
    # counts; corrections_log captures each replacement for the run summary.
    try:
        _normalize_extraction_in_place(data, _norm, stats)
        if stats.get("entity_speaker_unresolved", 0) > 0:
            print(
                f"    ❌  HARD ERROR — {stats['entity_speaker_unresolved']} 'Speaker N' tokens "
                f"survived extraction; STEP 3B speaker resolution failed. Aborting transcript.",
                file=sys.stderr,
            )
            return None
        ent_n = (stats.get("entity_phonetic", 0) +
                 stats.get("entity_canonical_match", 0) +
                 stats.get("entity_unresolved_vague", 0))
        if ent_n:
            print(f"    🧭  Entity reconciliation: {ent_n} adjustments", flush=True)
    except Exception as e:
        # Non-fatal: log and continue with raw extraction.
        print(f"    ⚠️   Entity reconciliation failed: {e}", file=sys.stderr)

    category    = data.get("category", "Other")
    call_date   = data.get("call_date", today)
    # Intel calls: strip any actions Claude generated (belt-and-suspenders)
    if is_intel_call:
        actions     = []
        category    = "Other"
    else:
        actions = data.get("action_items", [])
    contacts    = data.get("new_contacts", [])
    rec_intel   = data.get("recruiting_intel", {})
    deal_intel = data.get("deal_intel", [])
    summary     = data.get("one_line_summary", "")

    print(f"    Category: {category} | Actions: {len(actions)} | Contacts: {len(contacts)}", flush=True)
    if summary:
        print(f"    Summary: {summary}", flush=True)

    # Backfill any empty dashboard_path on action_items before writing
    envelope_items_pre = data.get("envelope_items", []) or []
    _backfill_action_dashboard_paths(actions, envelope_items_pre, deal_intel, category)

    # Write outputs
    n_added   = write_followups(token, actions, file_name, doc_link, stats)
    write_people(token, contacts, file_name, today, stats)
    rec_result   = "No change"
    deal_result = "No change"
    if category == "Recruiting" or (rec_intel and rec_intel.get("firm")):
        rec_result = write_recruiting(token, rec_intel, file_name, today)
    if category == _DEAL_WS or deal_intel:
        deal_result = write_deal_intel(token, deal_intel, file_name, today)

    # Write processing header + memo — Google Docs get prepend via Docs API,
    # plain text files get header prepended via Drive upload re-upload.
    # Doc structure after write: PROCESSED header → INVESTOR MEMO → Otter AI summary → transcript
    # Local files (pre_loaded_text is set) have no Drive ID — skip header write.
    if pre_loaded_text is None:
        write_processing_header(
            token, file_id, category, now_str, actions, deal_intel, rec_intel,
            n_added, len(contacts), rec_result, deal_result,
            is_gdoc=is_gdoc, original_text=text if not is_gdoc else "",
            memo_text=memo_text,
        )

    # Persist status_update items for cos_email_resolver.py — these are signals
    # that something already tracked has progressed (e.g. "FEA was delivered",
    # "I sent Mark the deck"). The resolver matches them against pending followUps.
    persist_status_updates(actions, category, file_name, today)

    # ── Route envelope items to routing-v2 arrays in dashboard-data.json ─────
    # Routing spec §4 — emit to awaitingExternal[], dealIntel[],
    # originationInbox[], themes[], and parent history[] timelines.
    # Legacy arrays (action_items, deal_intel) above are preserved for the
    # existing Google Doc writers; envelope_items is the new JSON surface.
    envelope_items = data.get("envelope_items", []) or []
    if is_intel_call:
        # Intel calls — strip any my_action / awaiting_external items defensively.
        # Prompt already instructs the model, but belt-and-suspenders.
        envelope_items = [e for e in envelope_items
                          if e.get("content_type") not in ("my_action", "awaiting_external")]

    # Deterministic post-processing: reclassify my_action → awaiting_external when
    # the action is clearly "chase a third-party commitment". Sonnet occasionally
    # misses this even with the prompt rule. Triggers on verb patterns that only
    # make sense when waiting for someone ELSE to deliver.
    _CHASE_PATTERNS = _re.compile(
        r"\b(follow\s*up\s+with|nudge|ping|chase|remind|circle\s+back\s+with|"
        r"confirm\s+(?:intro|introduction|that|whether)|"
        r"ensure\s+\w+\s+sends|when\s+\w+\s+sends)\b",
        _re.IGNORECASE,
    )
    import os as _os
    if _os.environ.get("ENVELOPE_DEBUG"):
        for a in (actions or []):
            print(f"    [act] who={(a.get('who') or '')[:25]:25s} | what={(a.get('what') or '')[:140]}")
        for e in envelope_items:
            print(f"    [env] {e.get('content_type'):20s} | owner={e.get('owner','')} | cp={e.get('counterparty','')[:30]:30s} | {(e.get('content') or '')[:120]}")
    for e in envelope_items:
        if e.get("content_type") != "my_action":
            continue
        content = e.get("content", "") or ""
        if _CHASE_PATTERNS.search(content):
            # Extract counterparty from content if possible (e.g., "follow up with Andrew Brannan")
            m = _re.search(r"follow\s*up\s+with\s+([A-Z][A-Za-z\-\.']+(?:\s+[A-Z][A-Za-z\-\.']+){0,3})",
                           content)
            cp = e.get("counterparty") or (m.group(1) if m else "")
            e["content_type"] = "awaiting_external"
            e["owner"] = "external"
            if cp and not e.get("counterparty"):
                e["counterparty"] = cp

    # Also scan legacy action_items for chase patterns — Sonnet sometimes only
    # emits these in action_items[] (doc flow), not envelope_items[]. Add a
    # shadow awaiting_external envelope item so the Awaiting External card picks it up.
    for a in (actions or []):
        what = (a.get("what") or "") + " " + (a.get("who") or "")
        if not _CHASE_PATTERNS.search(what):
            continue
        # Who committed? Try to extract from "follow up with X" or who-field
        m = _re.search(r"follow\s*up\s+with\s+([A-Z][A-Za-z\-\.']+(?:\s+[A-Z][A-Za-z\-\.']+){0,3})",
                       what)
        cp = (m.group(1) if m else (a.get("who") or "")).strip()
        shadow = {
            "content_type":   "awaiting_external",
            "owner":          "external",
            "counterparty":   cp,
            "parent_id":      "",
            "due":            a.get("due") or "",
            "context":        a.get("context") or "",
            "dashboard_path": a.get("dashboard_path") or "",
            "content":        a.get("what") or "",
        }
        # Dedupe by content equality against existing envelope_items
        already = any((e.get("content_type") == "awaiting_external"
                       and (e.get("content") or "") == shadow["content"])
                      for e in envelope_items)
        if not already:
            envelope_items.append(shadow)
    # Attach source_ref to every item
    src_ref = {
        "type":    "call",
        "title":   re.sub(r'^─+\s*|\s*─+$', '', file_name).strip(),
        "doc_url": doc_link,
        "date":    call_date,
    }
    for e in envelope_items:
        e.setdefault("source_ref", src_ref)
    if envelope_items:
        try:
            import importlib.util
            _spec = importlib.util.spec_from_file_location(
                "_envelope_writer",
                str(Path(__file__).resolve().parent / "_envelope_writer.py"),
            )
            _ew = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_ew)
            ew_summary = _ew.append_items(envelope_items)
            routed_total = sum(ew_summary.get("routed", {}).values())
            exc = ew_summary.get("exceptions", 0)
            print(f"    📬  Routing-v2: {routed_total} routed, {exc} exceptions "
                  f"→ {ew_summary.get('routed', {})}", flush=True)
        except Exception as e:
            print(f"    ⚠️   Envelope routing failed (non-critical): {e}", file=sys.stderr)

    # Sidecar tap for deal_log_entries[] — bypasses _envelope_writer
    # (parallel-session-owned) and feeds the V1+ Pass A0 lookup at
    # compile time. Soft-fails to keep extraction throughput intact
    # when the sidecar module isn't yet deployed.
    try:
        import _deal_log_sidecar as _dls
        _dle = data.get("deal_log_entries", []) or []
        if _dle:
            _n = _dls.append(_dle, src_ref, source_id=str(file_id))
            if _n:
                print(f"    🏷   Deal-log sidecar: +{_n} entries", flush=True)
    except Exception as _se:
        print(f"    ⚠️   Deal-log sidecar skipped (non-critical): {_se}",
              file=sys.stderr)

    # ── Rename file to 'YYYY-MM-DD Clean Title' ───────────────────────────────
    # Otter drops files with decorative dash-padded names. Rename on first
    # processing so the Drive folder stays readable.
    new_name = clean_title_for_rename(file_name, call_date)
    if new_name != file_name:
        try:
            drive_rename_file(token, file_id, new_name)
            print(f"    ✏️   Renamed → {new_name}", flush=True)
        except Exception as e:
            print(f"    ⚠️   Rename failed (non-critical): {e}", file=sys.stderr)

    stats["processed"] += 1
    return {"category": category, "summary": summary, "call_date": call_date}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    from datetime import timezone, timedelta
    parser = argparse.ArgumentParser(description="COS Otter/Call Transcript Backfill")
    parser.add_argument("--force", action="store_true",
                        help="Re-process files already in the dedup tracker")
    parser.add_argument("--id", dest="file_ids", metavar="FILE_ID", nargs="+",
                        help="Limit processing to specific Drive file ID(s); implies --force for those IDs")
    parser.add_argument("--days", type=int, default=3,
                        help="Only scan files modified in the last N days (default: 3)")
    parser.add_argument("--backfill", action="store_true",
                        help="Full history scan — removes the --days date limit. Use for initial setup only.")
    args = parser.parse_args()

    force      = args.force
    target_ids = set(args.file_ids) if args.file_ids else None

    # Date filter: applied to Drive folder scans so old already-processed files
    # are never listed. Disabled for --backfill, --force, and --id modes.
    if args.backfill or force or target_ids:
        since = None
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Note (2026-05-05): legacy ANTHROPIC_API_KEY guard removed —
    # _claude_dispatch.call() now enforces auth availability per
    # the active mode (subscription → CLAUDE_CODE_OAUTH_TOKEN; api →
    # ANTHROPIC_API_KEY). See cos_email_backfill.py for the same edit.

    print("=== COS Otter/Call Transcript Backfill ===", flush=True)
    print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}", flush=True)
    if since:
        print(f"Mode: incremental — files modified after {since} (last {args.days}d)", flush=True)
    elif args.backfill:
        print("Mode: --backfill (full history scan)", flush=True)
    elif force:
        print("Mode: --force (re-processing previously processed files)", flush=True)
    if target_ids:
        print(f"Mode: --id limited to {target_ids}", flush=True)
    print("", flush=True)

    # Auth
    try:
        token = refresh_token()
        print("✅  Token refreshed\n", flush=True)
    except Exception as e:
        print(f"❌  Token refresh failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Load pipeline cross-reference (injected into every Claude call)
    pipeline_context = load_pipeline_context()
    if pipeline_context:
        print(f"✅  Pipeline context loaded ({len(pipeline_context)} chars)\n", flush=True)
    else:
        print("⚠️   No pipeline context found (deal-pipeline-data.json missing)\n", flush=True)

    # Load dedup tracker
    tracker = load_dedup()
    print(f"Dedup tracker: {len(tracker)} previously processed files\n", flush=True)

    # ── Fast-exit pre-check ───────────────────────────────────────────────────
    # Before loading pipeline context (heavy) do a quick scan with the date
    # filter to count unprocessed candidates. If zero, nothing to do.
    _sources = _ts.get_transcript_sources(_CTX, _DOCS)
    if not force and not target_ids:
        _candidate_count = 0
        for _src in _sources:
            if _src.source_type == "google_drive_folder":
                for _fid, _, _ in _src.iter_folders():
                    try:
                        _files = drive_list_folder(token, _fid, since=since)
                        _candidate_count += sum(1 for f in _files if f["id"] not in tracker)
                    except Exception:
                        _candidate_count += 1  # err on the side of proceeding
            elif _src.source_type == "local_folder":
                try:
                    _local_files = _src.list_new(None, since, set(tracker.keys()))
                    _candidate_count += len(_local_files)
                except Exception:
                    _candidate_count += 1
        if _candidate_count == 0:
            print(f"PRE-CHECK: 0 unprocessed files in scan window. Nothing to do.", flush=True)
            sys.exit(0)
        print(f"PRE-CHECK: {_candidate_count} candidate file(s) found — proceeding.\n", flush=True)

    stats = {
        "processed": 0,
        "skipped_dedup": 0,
        "skipped_audio": 0,
        "skipped_already_marked": 0,
        "errors": 0,
        "followups_added": 0,
        "contacts_added": 0,
        "files": [],
    }

    # ── Process all transcript sources ────────────────────────────────────────
    # Sources come from transcript_sources in firm_context.yaml.
    # If not configured, _ts.get_transcript_sources() returns legacy Otter folders.

    print(f"── Transcript sources: {len(_sources)} configured ──────────────────────────", flush=True)
    for src_num, source in enumerate(_sources, 1):
        src_label = source.name
        print(f"\n\n── Source {src_num}/{len(_sources)}: {src_label} ({source.source_type}) ──────────────────────────", flush=True)

        # ── Google Drive folder source ─────────────────────────────────────────
        if source.source_type == "google_drive_folder":
            for folder_id, hint_cat, is_root in source.iter_folders():
                folder_label = f"{src_label} / root" if is_root else f"{src_label} / {folder_id[:8]}…"
                print(f"\n  Scanning folder: {folder_label}", flush=True)
                try:
                    files = drive_list_folder(token, folder_id, since=since)
                except Exception as e:
                    print(f"  ❌  Could not list folder {folder_id}: {e}", file=sys.stderr)
                    continue

                print(f"  Found {len(files)} files", flush=True)

                # Consolidate duplicate siblings (Zapier double-fire + .txt/.gdoc pairs).
                # Safe to run on any Drive folder — no-op when no siblings exist.
                try:
                    files = consolidate_transcript_siblings(token, files, tracker, folder_id)
                except Exception as e:
                    print(f"  ⚠️   consolidate_transcript_siblings failed: {e}", file=sys.stderr)

                for f in files:
                    fid   = f["id"]
                    fname = f["name"]
                    mime  = f.get("mimeType", "")

                    # --id filter
                    if target_ids and fid not in target_ids:
                        continue

                    # Skip audio
                    ext = Path(fname).suffix.lower()
                    if ext in AUDIO_EXTENSIONS or "audio" in mime:
                        stats["skipped_audio"] += 1
                        continue

                    # Skip unsupported formats
                    if mime and "google-apps.document" not in mime and "text" not in mime and "plain" not in mime:
                        if ext not in (".txt", ".md", ".rtf", ".vtt", ".srt"):
                            print(f"    ⚠️   Skipping unsupported format: {fname} ({mime})", flush=True)
                            continue

                    # Check dedup
                    if fid in tracker and not force and not (target_ids and fid in target_ids):
                        stats["skipped_dedup"] += 1
                        continue

                    # Check for pre-existing processing header (belt-and-suspenders,
                    # catches files processed by cos_transcript_hook.py but not yet
                    # in the dedup tracker on this machine).
                    if not force and not (target_ids and fid in target_ids):
                        try:
                            _peek, _ = read_file_content(token, fid, mime)
                            if _peek.startswith("╔══ PROCESSED:") or "PROCESSED:" in _peek[:200]:
                                print(f"    ⏭   Already has processing header: {fname}", flush=True)
                                stats["skipped_already_marked"] += 1
                                mark_processed(tracker, fid, fname, "pre-processed")
                                save_dedup(tracker)
                                continue
                        except Exception:
                            pass  # If peek fails, proceed and let process_transcript handle it

                    # Process
                    result = process_transcript(
                        token, fid, fname, hint_cat, src_label, stats,
                        mime_type=mime, pipeline_context=pipeline_context,
                    )
                    if result is None:
                        stats["errors"] += 1
                        mark_processed(tracker, fid, fname, "error")
                    elif result.get("skipped"):
                        mark_processed(tracker, fid, fname, "skipped_short")
                    else:
                        cat = result.get("category", "Other")
                        mark_processed(tracker, fid, fname, cat)
                        stats["files"].append({"name": fname, "category": cat, "source": src_label})
                        # Move file from root → category subfolder (triage routing)
                        if is_root:
                            dest = (
                                source.category_folder_for(cat)
                                or _LEGACY_CATEGORY_FOLDER.get(cat)
                            )
                            if dest:
                                try:
                                    move_file_to_folder(token, fid, dest, folder_id)
                                    print(f"    📁  Moved to {cat} folder", flush=True)
                                except Exception as e:
                                    print(f"    ⚠️   Could not move to {cat} folder: {e}", file=sys.stderr)
                    save_dedup(tracker)

        # ── Local folder source ────────────────────────────────────────────────
        elif source.source_type == "local_folder":
            try:
                local_files = source.list_new(
                    None, since, set(tracker.keys()),
                    target_ids=set(target_ids) if target_ids else None,
                )
            except Exception as e:
                print(f"  ❌  Could not list local source {src_label}: {e}", file=sys.stderr)
                continue

            print(f"  Found {len(local_files)} local file(s)", flush=True)

            for tf in local_files:
                print(f"\n  → {tf.name}", flush=True)
                try:
                    pre_text = source.download_text(tf, None)
                except Exception as e:
                    print(f"  ❌  Could not read {tf.name}: {e}", file=sys.stderr)
                    stats["errors"] += 1
                    continue

                if not pre_text or len(pre_text) < 150:
                    print(f"    ⚠️   Too short — skipping: {tf.name}", flush=True)
                    mark_processed(tracker, tf.id, tf.name, "skipped_short")
                    save_dedup(tracker)
                    continue

                result = process_transcript(
                    token, tf.id, tf.name, tf.category_hint, src_label, stats,
                    pipeline_context=pipeline_context,
                    pre_loaded_text=pre_text,
                )
                if result is None:
                    stats["errors"] += 1
                    mark_processed(tracker, tf.id, tf.name, "error")
                elif result.get("skipped"):
                    mark_processed(tracker, tf.id, tf.name, "skipped_short")
                else:
                    cat = result.get("category", "Other")
                    mark_processed(tracker, tf.id, tf.name, cat)
                    stats["files"].append({"name": tf.name, "category": cat, "source": src_label})
                save_dedup(tracker)

        else:
            print(f"  ⚠️   Unknown source type '{source.source_type}' — skipping.", file=sys.stderr)

    # ── Dashboard warmup ──────────────────────────────────────────────────────

    try:
        urllib.request.urlopen(
            urllib.request.Request(DASHBOARD_URL, method="POST"), timeout=3
        )
        print("\n✅  Dashboard warmup triggered", flush=True)
    except Exception:
        print("\n⚠️   Dashboard not running (skipped warmup)", flush=True)

    # ── Final summary ─────────────────────────────────────────────────────────

    print("\n" + "=" * 60, flush=True)
    print("RUN SUMMARY", flush=True)
    print("=" * 60, flush=True)
    print(f"{stats['processed']} transcripts processed | {stats['followups_added']} follow-ups added | {stats['contacts_added']} contacts added | dashboard pinged", flush=True)
    print(f"Skipped (already done): {stats['skipped_dedup']}", flush=True)
    print(f"Skipped (audio files):  {stats['skipped_audio']}", flush=True)
    print(f"Skipped (pre-marked):   {stats['skipped_already_marked']}", flush=True)
    print(f"Errors:                 {stats['errors']}", flush=True)
    if stats["files"]:
        print("\nProcessed files:", flush=True)
        for ff in stats["files"]:
            print(f"  [{ff['category']}] {ff['name']} ({ff['source']})", flush=True)
    else:
        print("\nNo new transcripts found to process.", flush=True)


if __name__ == "__main__":
    main()
