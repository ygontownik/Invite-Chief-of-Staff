#!/usr/bin/env python3
from __future__ import annotations
"""
cos_gmail_mini_v2.py — CoS Email Mini (Gmail + Outlook)
Cost-optimized: delta reads + Haiku triage + Sonnet escalation only

WHAT CHANGED FROM v1:
  1. DELTA READS — stores last_processed timestamp in ~/credentials/processed_emails.json
     Each run fetches ONLY emails since last run. First run defaults to last 2 hours.
     Never re-processes the same email. Cuts token consumption ~80%.

  2. TWO-PASS MODEL ROUTING:
     Pass 1 → Haiku:  classify every email (DEAL / RESEARCH / RECRUIT / ACTION / IGNORE)
     Pass 2 → Sonnet: ONLY for DEAL and RECRUIT emails that need structured write-back
     Everything else (ACTION, RESEARCH) handled entirely by Haiku.

  3. DUAL INBOX SUPPORT — Gmail and Outlook/Microsoft 365 via the same script.
     Configured by EMAIL_PROVIDER env var or firm_config.json.

  4. RESEARCH SENDER ROUTING — emails from known research senders (Capstone, Bank Street,
     Jefferies, etc.) route directly to their source doc. No Sonnet needed.

COST IMPACT:
  Before: ~$16/mo (180 runs × ~20k tokens × Sonnet)
  After:  ~$0.80/mo (180 Haiku triage + occasional Sonnet escalation only)
  Saving: ~$15/mo

SETUP:
  Gmail:   EMAIL_PROVIDER=gmail  (uses ~/credentials/gdrive_token.pickle + Gmail API)
  Outlook: EMAIL_PROVIDER=outlook (uses ~/credentials/ms_token.json + Microsoft Graph)

  firm_config.json controls:
    - which doc IDs to write to
    - which senders are research sources
    - which keywords trigger deal vs. recruit classification

USAGE:
  python cos_gmail_mini_v2.py                # normal delta run
  python cos_gmail_mini_v2.py --backfill 4h  # last 4 hours
  python cos_gmail_mini_v2.py --backfill 24h # last 24 hours
  python cos_gmail_mini_v2.py --list         # dry run, no writes
  python cos_gmail_mini_v2.py --force        # reprocess already-seen (debug)
"""

import os, sys, json, time, argparse, logging, re, base64, pickle
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.utils import parsedate_to_datetime

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    from _usage import log_usage
except Exception:
    def log_usage(*_a, **_kw): return

# ────────────────────────────────────────────────────────────────────
# PATHS & CONFIG
# ────────────────────────────────────────────────────────────────────

CREDS_DIR      = Path.home() / "credentials"
PROCESSED_FILE = CREDS_DIR / "processed_emails.json"
CONFIG_FILE    = Path.home() / "tomac-cove-pipeline" / "firm_config.json"
LOG_FILE       = Path.home() / "tomac-cove-pipeline" / "logs" / "gmail_mini.log"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)]
)
log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# DEFAULT FIRM CONFIG (overridden by firm_config.json)
# ────────────────────────────────────────────────────────────────────

# B2 (ID excision): firm_name, docs, and research_senders are no longer hardcoded.
# They MUST be supplied by firm_config.json (loaded via load_config below) — the
# script fails loud if `docs` is missing the required keys. firm_name is read
# from firm_config.json :: firm_name (or, if absent, firm_context.yaml :: firm.name).

DEFAULT_CONFIG = {
    "firm_name": "",                     # required: firm_config.json :: firm_name
    "email_provider": "gmail",           # "gmail" or "outlook"

    # Required: firm_config.json :: docs — keys followups/pipeline/people/recruiting
    "docs": {},

    # Required: firm_config.json :: research_senders — may be {} if no routing.
    "research_senders": {},

    # Keywords that signal DEAL classification in subject/sender
    "deal_keywords": [
        "cholla", "gideon", "venus", "bbeh", "black bayou",
        "pngts", "pfs", "thunderhead", "arclight", "takanock",
        "encore", "ercot", "oncor", "term sheet", "loi", "nda",
        "diligence", "ic memo", "bid", "proposal"
    ],

    # Keywords that signal RECRUIT classification
    "recruit_keywords": [
        "castleton", "related digital", "reinova", "ridgewood",
        "piper maddox", "one search", "barton partnership",
        "interview", "role", "opportunity", "resume", "cv",
        "offer", "comp", "compensation", "headhunter", "recruiter"
    ],

    # Max emails per run (safety cap)
    "max_emails_per_run": 50,

    # Default lookback on first run (hours)
    "first_run_lookback_hours": 2,
}


def _load_domain_bundle(ctx: dict) -> dict:
    """Load ~/cos-pipeline/domains/<domain>/config.yaml for the active tenant.

    Returns {} if firm_context lacks `domain`, the bundle dir is missing, or
    the YAML can't be parsed. Read-only, no side effects.
    """
    domain = (ctx or {}).get("domain")
    if not domain:
        return {}
    bundle_path = _HERE / "domains" / domain / "config.yaml"
    if not bundle_path.exists():
        return {}
    try:
        import yaml
        with open(bundle_path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        log.warning(f"Could not load domain bundle {bundle_path}: {e}")
        return {}


def load_config():
    """Load firm_config.json. Fail loud if docs / firm_name missing (B2 contract).

    Sourcing precedence:
      1. firm_config.json (per-tenant, in COS_CONFIG_DIR or ~/cos-pipeline-config-<slug>/)
      2. firm_context.yaml :: firm.name as a fallback for firm_name only
      3. drive-docs.yaml as a fallback for `docs` (keys: followups, pipeline,
         people_crm→people, recruiting, tomac_pipeline→pipeline)
    """
    # Try to source firm_config.json from the team config repo first
    try:
        sys.path.insert(0, str(_HERE))
        import _firm_context as _fc  # noqa: E402
        user_config = _fc.load_firm_config()
        ctx = _fc.load_firm_context()
        drive_docs = _fc.load_drive_docs()
    except Exception as e:
        log.warning(f"Could not load firm_context loader: {e}")
        user_config, ctx, drive_docs = {}, {}, {}

    # Legacy direct-file fallback (preserves original CONFIG_FILE behavior)
    if not user_config and CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            user_config = json.load(f)

    if not user_config:
        raise RuntimeError(
            "No firm config available. Populate firm_config.json in your "
            "tenant config repo (COS_CONFIG_DIR or ~/cos-pipeline-config-<slug>/)."
        )

    # Deep merge: user config overrides defaults
    config = {**DEFAULT_CONFIG, **user_config}

    # docs: prefer firm_config.json :: docs, fall back to drive-docs.yaml entries
    cfg_docs = user_config.get("docs", {}) or {}
    if not cfg_docs and drive_docs:
        # Map drive-docs.yaml canonical keys to gmail-mini's expected keys
        cfg_docs = {
            "followups":  drive_docs.get("followups", ""),
            "pipeline":   drive_docs.get("tomac_pipeline", ""),
            "people":     drive_docs.get("people_crm", ""),
            "recruiting": drive_docs.get("recruiting", ""),
        }
    missing = [k for k in ("followups", "pipeline", "people", "recruiting")
               if not cfg_docs.get(k)]
    if missing:
        raise RuntimeError(
            f"firm_config.json :: docs missing required key(s): {missing}. "
            f"Populate them per-tenant; legacy hardcoded fallbacks were removed in B2."
        )
    config["docs"] = cfg_docs

    # research_senders: optional; default empty dict if absent
    config["research_senders"] = user_config.get("research_senders", {}) or {}

    # firm_name: legacy field; fall back to firm_context.yaml :: firm.name
    if not config.get("firm_name"):
        config["firm_name"] = (ctx.get("firm", {}) or {}).get("name", "")
    if not config.get("firm_name"):
        raise RuntimeError(
            "firm_name is required. Set firm_config.json :: firm_name or "
            "firm_context.yaml :: firm.name."
        )

    # Keyword lists — precedence per C13 + session-4 robustness pass:
    #   1. Per-tenant firm_config.json :: deal_keywords (highest — tenant override wins)
    #   2. Domain bundle ~/cos-pipeline/domains/<domain>/config.yaml :: deal_keywords
    #   3. DEFAULT_CONFIG (lowest — hardcoded last-resort, originally tomac terms)
    # Without #2, P (real-estate tenant) would fall through to tomac's hardcoded
    # asset names (cholla, gideon, …) until they manually populated firm_config.
    domain_cfg = _load_domain_bundle(ctx)
    for keyword_field in ("deal_keywords", "recruit_keywords"):
        if keyword_field in user_config:
            config[keyword_field] = user_config[keyword_field]
        elif keyword_field in domain_cfg:
            config[keyword_field] = domain_cfg[keyword_field]
        else:
            config[keyword_field] = DEFAULT_CONFIG[keyword_field]

    # Stamp tenant-level features for downstream gating (scope A). Daemon-level
    # behavior reads these; per-user overrides apply only to dashboard tile
    # visibility (resolved server-side per request).
    tenant_features = (ctx or {}).get("features") or {}
    config["_features_job_search"] = bool(tenant_features.get("job_search", True))

    log.info(f"Loaded firm config: {config['firm_name']}")
    return config


# ────────────────────────────────────────────────────────────────────
# DELTA STATE — last processed timestamp
# ────────────────────────────────────────────────────────────────────

def load_state():
    if PROCESSED_FILE.exists():
        with open(PROCESSED_FILE) as f:
            return json.load(f)
    return {"last_processed_ts": None, "processed_ids": []}


def save_state(state):
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROCESSED_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _parse_backfill_hours(value: str) -> int:
    """Parse backfill window string to hours. Accepts '4h', '2d', '30d', etc."""
    v = value.strip().lower()
    if v.endswith("d"):
        return int(v[:-1]) * 24
    if v.endswith("h"):
        return int(v[:-1])
    return int(v)  # bare number → treat as hours


def get_fetch_since(state, config, args):
    """Return UTC datetime to fetch emails after."""
    if args.force:
        hours = _parse_backfill_hours(args.backfill) if args.backfill else 2
        return datetime.now(timezone.utc) - timedelta(hours=hours)

    if args.backfill:
        hours = _parse_backfill_hours(args.backfill)
        return datetime.now(timezone.utc) - timedelta(hours=hours)

    if state.get("last_processed_ts"):
        return datetime.fromisoformat(state["last_processed_ts"])

    # First run: default lookback
    hours = config.get("first_run_lookback_hours", 2)
    log.info(f"First run — fetching last {hours} hours")
    return datetime.now(timezone.utc) - timedelta(hours=hours)


# ────────────────────────────────────────────────────────────────────
# GMAIL ADAPTER
# ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

# Separate token file for Gmail mini — does not touch gdrive_token.pickle
# used by other pipeline scripts.
GMAIL_TOKEN_PATH = CREDS_DIR / "gmail_mini_token.pickle"

def get_gmail_service():
    """Authenticate and return Gmail API service."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_path = GMAIL_TOKEN_PATH
    creds = None

    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDS_DIR / "gdrive_credentials.json"), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)


def fetch_gmail_emails(since: datetime, config: dict, state: dict, args) -> list:
    """Fetch emails from Gmail since the given timestamp. Returns list of dicts."""
    service = get_gmail_service()

    # Build query: after: + not already processed
    since_unix = int(since.timestamp())
    query = f"after:{since_unix}"
    # Exclude automated/spam noise
    query += " -category:promotions -category:social -from:noreply -from:no-reply"

    log.info(f"Gmail query: {query}")

    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=config.get("max_emails_per_run", 50)
    ).execute()

    messages = result.get("messages", [])
    log.info(f"Gmail returned {len(messages)} messages since {since.isoformat()}")

    emails = []
    processed_ids = set(state.get("processed_ids", []))

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        if not args.force and msg_id in processed_ids:
            continue

        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        headers = {h["name"].lower(): h["value"]
                   for h in msg["payload"].get("headers", [])}

        # Extract body
        body = extract_gmail_body(msg["payload"])

        emails.append({
            "id": msg_id,
            "subject": headers.get("subject", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "body": body[:3000],  # cap at 3k chars for triage
            "source": "gmail",
        })

    return emails


def extract_gmail_body(payload: dict) -> str:
    """Recursively extract plain text body from Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

    for part in payload.get("parts", []):
        text = extract_gmail_body(part)
        if text:
            return text
    return ""


# ────────────────────────────────────────────────────────────────────
# OUTLOOK ADAPTER (Microsoft Graph)
# ────────────────────────────────────────────────────────────────────

def get_outlook_token() -> str:
    """Load Microsoft Graph access token from ~/credentials/ms_token.json."""
    token_file = CREDS_DIR / "ms_token.json"
    if not token_file.exists():
        raise FileNotFoundError(
            f"Outlook token not found at {token_file}. "
            "Run the Microsoft Graph OAuth flow first:\n"
            "  python setup_outlook_auth.py"
        )
    with open(token_file) as f:
        token_data = json.load(f)

    # Check expiry
    expires_at = token_data.get("expires_at", 0)
    if time.time() > expires_at - 60:
        log.info("Outlook token expired — refreshing...")
        token_data = refresh_outlook_token(token_data)

    return token_data["access_token"]


def refresh_outlook_token(token_data: dict) -> dict:
    """Refresh an expired Microsoft Graph token."""
    import urllib.request, urllib.parse
    tenant_id  = os.environ.get("MS_TENANT_ID", "common")
    client_id  = os.environ.get("MS_CLIENT_ID")
    client_secret = os.environ.get("MS_CLIENT_SECRET", "")

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": token_data["refresh_token"],
        "scope":         "https://graph.microsoft.com/Mail.Read offline_access",
    }).encode()

    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        new_token = json.loads(resp.read())

    new_token["expires_at"] = time.time() + new_token.get("expires_in", 3600)
    new_token["refresh_token"] = token_data.get("refresh_token",
                                                  new_token.get("refresh_token", ""))

    with open(CREDS_DIR / "ms_token.json", "w") as f:
        json.dump(new_token, f, indent=2)

    log.info("Outlook token refreshed successfully")
    return new_token


def fetch_outlook_emails(since: datetime, config: dict, state: dict, args) -> list:
    """Fetch emails from Outlook/Microsoft 365 via Graph API."""
    import urllib.request

    token = get_outlook_token()
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    # OData filter: received after since_str
    url = (
        "https://graph.microsoft.com/v1.0/me/messages"
        f"?$filter=receivedDateTime gt {since_str}"
        "&$select=id,subject,from,toRecipients,receivedDateTime,body,bodyPreview"
        f"&$top={config.get('max_emails_per_run', 50)}"
        "&$orderby=receivedDateTime desc"
    )

    headers_http = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    req = urllib.request.Request(url, headers=headers_http)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    messages = data.get("value", [])
    log.info(f"Outlook returned {len(messages)} messages since {since.isoformat()}")

    emails = []
    processed_ids = set(state.get("processed_ids", []))

    for msg in messages:
        msg_id = msg["id"]
        if not args.force and msg_id in processed_ids:
            continue

        sender = msg.get("from", {}).get("emailAddress", {})
        body_content = msg.get("body", {}).get("content", "")

        # Strip HTML if content type is HTML
        if msg.get("body", {}).get("contentType", "") == "html":
            body_content = re.sub(r"<[^>]+>", " ", body_content)
            body_content = re.sub(r"\s+", " ", body_content).strip()

        emails.append({
            "id": msg_id,
            "subject": msg.get("subject", ""),
            "from": f"{sender.get('name', '')} <{sender.get('address', '')}>",
            "to": "",
            "date": msg.get("receivedDateTime", ""),
            "body": body_content[:3000],
            "source": "outlook",
        })

    return emails


# ────────────────────────────────────────────────────────────────────
# UNIFIED EMAIL FETCHER
# ────────────────────────────────────────────────────────────────────

def fetch_emails(since: datetime, config: dict, state: dict, args) -> list:
    """Route to Gmail or Outlook based on config."""
    provider = os.environ.get("EMAIL_PROVIDER",
                               config.get("email_provider", "gmail")).lower()
    if provider == "outlook":
        return fetch_outlook_emails(since, config, state, args)
    else:
        return fetch_gmail_emails(since, config, state, args)


# ────────────────────────────────────────────────────────────────────
# RESEARCH SENDER PRE-CLASSIFICATION (zero API calls)
# ────────────────────────────────────────────────────────────────────

def extract_domain(email_str: str) -> str:
    """Extract domain from 'Name <email@domain.com>' or 'email@domain.com'."""
    match = re.search(r"[\w.+-]+@([\w.-]+)", email_str)
    return match.group(1).lower() if match else ""


def classify_by_sender(email: dict, config: dict) -> str | None:
    """
    Check if sender domain matches a known research sender.
    Returns doc_id to write to, or None if no match.
    Zero API calls — pure dict lookup.
    """
    domain = extract_domain(email.get("from", ""))
    research_senders = config.get("research_senders", {})
    return research_senders.get(domain)


def keyword_prefilter(email: dict, config: dict) -> str | None:
    """
    Fast keyword scan of subject + snippet before API call.
    Returns 'DEAL', 'RECRUIT', or None (needs Haiku triage).

    RECRUIT scan is skipped when features.job_search is OFF (scope A) — tenant
    isn't doing job search, so recruit-keyword false positives would mislabel
    real deals. RECRUIT classification is also remapped to IGNORE in the Haiku
    triage post-step.
    """
    text = (email.get("subject", "") + " " + email.get("body", "")[:500]).lower()

    for kw in config.get("deal_keywords", []):
        if kw in text:
            return "DEAL"

    if config.get("_features_job_search", True):
        for kw in config.get("recruit_keywords", []):
            if kw in text:
                return "RECRUIT"

    return None


# ────────────────────────────────────────────────────────────────────
# PASS 1: HAIKU TRIAGE
# ────────────────────────────────────────────────────────────────────

TRIAGE_SYSTEM = """You are an email triage assistant for a senior infrastructure private equity professional.
Classify each email into exactly one category. Reply with JSON only — no markdown.

Categories:
  DEAL      - relates to a specific deal, asset, counterparty, or investment opportunity
  RESEARCH  - newsletter, market update, research blast, or analyst report
  RECRUIT   - job opportunity, recruiter, headhunter, hiring firm, interview
  ACTION    - requires a follow-up, reply, or task (meeting request, intro, personal outreach)
  IGNORE    - spam, promotional, automated notification, calendar noise

JSON format:
{
  "category": "DEAL|RESEARCH|RECRUIT|ACTION|IGNORE",
  "confidence": 0.0-1.0,
  "reason": "one sentence",
  "one_liner": "max 20 words summarizing the email for a senior investor"
}"""


def _parse_json_response(text: str) -> dict:
    """Strip markdown fences and parse JSON — models sometimes wrap despite instructions."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


def haiku_triage(email: dict) -> dict:
    """
    Pass 1: Call Haiku to classify a single email.
    Returns dict with category, confidence, reason, one_liner.

    NOT MIGRATED — Option B per MIGRATION_PLAN.md: routing Haiku triage
    through the static-core cached client added a ~3× cost regression
    (~$1.60/mo → ~$4.80/mo) because the 4,234-token cached prefix dwarfs
    Haiku's natively-tiny per-call footprint. Triage doesn't need investor
    doctrine to decide DEAL vs RECRUIT vs IGNORE; original path preserved.
    """
    import anthropic

    import _secrets
    client = anthropic.Anthropic(api_key=_secrets.load_secret("ANTHROPIC_API_KEY"))

    prompt = (
        f"From: {email['from']}\n"
        f"Subject: {email['subject']}\n"
        f"Date: {email['date']}\n\n"
        f"Body (first 1500 chars):\n{email['body'][:1500]}"
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        log_usage("cos_gmail_mini_haiku", "claude-haiku-4-5-20251001", {
            "usage": {
                "input_tokens":                getattr(response.usage, "input_tokens", 0),
                "output_tokens":               getattr(response.usage, "output_tokens", 0),
                "cache_read_input_tokens":     getattr(response.usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
            }
        })
        result = _parse_json_response(response.content[0].text)
        result["category"] = result.get("category", "IGNORE").upper()
        result["confidence"] = float(result.get("confidence", 0.5))
        return result
    except Exception as e:
        log.error(f"Haiku triage failed for {email['id']}: {e}")
        return {"category": "IGNORE", "confidence": 0.0, "reason": str(e), "one_liner": ""}


# ────────────────────────────────────────────────────────────────────
# PASS 2: SONNET ENRICHMENT (DEAL / RECRUIT only, confidence >= 0.7)
# ────────────────────────────────────────────────────────────────────

DEAL_ENRICHMENT_SYSTEM = """You are a chief of staff for a senior infrastructure PE investor at Tomac Cove Infrastructure Partners.
An email has been classified as DEAL-related. Extract structured information for the deal pipeline.
Reply with JSON only — no markdown.

JSON format:
{
  "deal_name": "asset or deal name, or 'Unknown'",
  "counterparty": "firm or person name",
  "stage": "Awareness|IOI|LOI|Diligence|IC|Closed|Unknown",
  "sector": "Power|Midstream|Digital|LNG|Other",
  "action_required": true|false,
  "action_summary": "verb-first one sentence, or null",
  "priority": "High|Medium|Low",
  "summary": "2-3 sentences max for a senior investor"
}"""

RECRUIT_ENRICHMENT_SYSTEM = """You are a chief of staff for a senior infrastructure PE professional actively job searching.
An email has been classified as RECRUIT-related. Extract structured information.
Reply with JSON only — no markdown.

JSON format:
{
  "firm": "hiring firm name",
  "role": "role title or 'Unknown'",
  "recruiter": "recruiter name or firm",
  "stage": "Outreach|Screen|Interview|Offer|Closed|Unknown",
  "action_required": true|false,
  "action_summary": "verb-first one sentence, or null",
  "priority": "High|Medium|Low",
  "summary": "2-3 sentences max"
}"""

ACTION_ENRICHMENT_SYSTEM = """You are a chief of staff. An email requires a follow-up action.
Extract the action item. Reply with JSON only — no markdown.

JSON format:
{
  "action_summary": "verb-first one sentence, specific",
  "from_name": "sender name",
  "deadline": "YYYY-MM-DD or 'No deadline'",
  "priority": "High|Medium|Low",
  "context": "one sentence why this action exists"
}"""


def sonnet_enrich(email: dict, category: str) -> dict:
    """
    Pass 2: Call Sonnet to extract structured data from a high-confidence email.
    Only called for DEAL/RECRUIT with confidence >= 0.7, and ACTION.

    MIGRATED: routes through `_subscription.cached_client.complete`. The
    category-specific JSON-output system prompt is concatenated into
    user_query because the static core does not enforce a JSON contract.
    The static core's firm list and tracked-counterparty context now
    informs Sonnet's enrichment, which should improve `counterparty`
    field accuracy.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_subscription"))
    from cached_client import complete

    if category == "DEAL":
        category_system = DEAL_ENRICHMENT_SYSTEM
    elif category == "RECRUIT":
        category_system = RECRUIT_ENRICHMENT_SYSTEM
    else:
        category_system = ACTION_ENRICHMENT_SYSTEM

    user_query = (
        f"{category_system}\n\n"
        f"---\n"
        f"Email to enrich:\n"
        f"From: {email['from']}\n"
        f"Subject: {email['subject']}\n"
        f"Date: {email['date']}\n\n"
        f"Body:\n{email['body'][:2500]}"
    )

    try:
        result = complete(
            user_query=user_query,
            source_content="",
            tenant_bundle="",
            model="claude-sonnet-4-6",
            max_tokens=512,
        )
        usage = result["usage"]
        log_usage("cos_gmail_mini_sonnet", "claude-sonnet-4-6", {
            "usage": {
                "input_tokens":                getattr(usage, "input_tokens", 0),
                "output_tokens":               getattr(usage, "output_tokens", 0),
                "cache_read_input_tokens":     getattr(usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
            }
        })
        return _parse_json_response(result["text"])
    except Exception as e:
        log.error(f"Sonnet enrichment failed for {email['id']}: {e}")
        return {}


# ────────────────────────────────────────────────────────────────────
# GOOGLE DOCS WRITE-BACK
# ────────────────────────────────────────────────────────────────────

def get_docs_service():
    """Return authenticated Google Docs API service."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    # Use gmail_mini_token.pickle — it carries gmail.readonly + documents + drive.file
    token_path = GMAIL_TOKEN_PATH
    with open(token_path, "rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return build("docs", "v1", credentials=creds)


def append_to_doc(doc_id: str, text: str, heading: bool = False):
    """Append text to the end of a Google Doc."""
    docs = get_docs_service()

    # Get current doc end index
    doc = docs.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])
    end_index = content[-1].get("endIndex", 1) - 1 if content else 1

    requests = []

    if heading:
        requests.append({
            "insertText": {
                "location": {"index": end_index},
                "text": f"\n{text}\n"
            }
        })
        requests.append({
            "updateParagraphStyle": {
                "range": {
                    "startIndex": end_index + 1,
                    "endIndex": end_index + len(text) + 2
                },
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType"
            }
        })
    else:
        requests.append({
            "insertText": {
                "location": {"index": end_index},
                "text": f"\n{text}\n"
            }
        })

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests}
    ).execute()


def write_deal_update(email: dict, triage: dict, enriched: dict, config: dict, dry_run: bool = False):
    """Write DEAL STATUS UPDATE to pipeline doc."""
    doc_id = config["docs"]["pipeline"]
    date_str = datetime.now(timezone.utc).strftime("%b %d %Y")

    deal_name = enriched.get("deal_name", "Unknown")
    counterparty = enriched.get("counterparty", email.get("from", ""))
    stage = enriched.get("stage", "Unknown")
    sector = enriched.get("sector", "Unknown")
    summary = enriched.get("summary", triage.get("one_liner", ""))
    action_summary = enriched.get("action_summary", "")
    priority = enriched.get("priority", "Medium")

    text = (
        f"DEAL STATUS UPDATE — {date_str}\n"
        f"────────────────────────────────────────\n"
        f"Deal: {deal_name}\n"
        f"Counterparty: {counterparty}\n"
        f"Sector: {sector} | Stage: {stage} | Priority: {priority}\n"
        f"Subject: {email['subject']}\n"
        f"From: {email['from']}\n\n"
        f"{summary}\n"
    )
    if action_summary:
        text += f"\nACTION: {action_summary}\n"
    text += "════════════════════════════════════════\n"

    if dry_run:
        log.info(f"[DRY RUN] Would write DEAL update to pipeline doc: {deal_name}")
        print(text)
        return

    try:
        if subject_already_in_doc(doc_id, email["subject"]):
            log.info(f"DEAL already in doc (skipping duplicate): {email['subject'][:60]}")
            return
        append_to_doc(doc_id, text)
        _invalidate_doc_cache(doc_id, text)
        log.info(f"DEAL written to pipeline doc: {deal_name}")
    except Exception as e:
        log.error(f"Failed to write DEAL to doc {doc_id}: {e}")


def write_recruit_update(email: dict, triage: dict, enriched: dict, config: dict, dry_run: bool = False):
    """Write RECRUIT STATUS UPDATE to recruiting doc."""
    doc_id = config["docs"]["recruiting"]
    date_str = datetime.now(timezone.utc).strftime("%b %d %Y")

    firm = enriched.get("firm", "Unknown")
    role = enriched.get("role", "Unknown")
    recruiter = enriched.get("recruiter", email.get("from", ""))
    stage = enriched.get("stage", "Unknown")
    summary = enriched.get("summary", triage.get("one_liner", ""))
    action_summary = enriched.get("action_summary", "")
    priority = enriched.get("priority", "Medium")

    text = (
        f"RECRUIT STATUS UPDATE — {date_str}\n"
        f"────────────────────────────────────────\n"
        f"Firm: {firm}\n"
        f"Role: {role}\n"
        f"Recruiter: {recruiter}\n"
        f"Stage: {stage} | Priority: {priority}\n"
        f"Subject: {email['subject']}\n\n"
        f"{summary}\n"
    )
    if action_summary:
        text += f"\nACTION: {action_summary}\n"
    text += "════════════════════════════════════════\n"

    if dry_run:
        log.info(f"[DRY RUN] Would write RECRUIT update to recruiting doc: {firm} / {role}")
        print(text)
        return

    try:
        if subject_already_in_doc(doc_id, email["subject"]):
            log.info(f"RECRUIT already in doc (skipping duplicate): {email['subject'][:60]}")
            return
        append_to_doc(doc_id, text)
        _invalidate_doc_cache(doc_id, text)
        log.info(f"RECRUIT written to recruiting doc: {firm} / {role}")
    except Exception as e:
        log.error(f"Failed to write RECRUIT to doc {doc_id}: {e}")


def write_action_item(email: dict, triage: dict, enriched: dict, config: dict, dry_run: bool = False):
    """Write ACTION item to followups doc."""
    doc_id = config["docs"]["followups"]
    date_str = datetime.now(timezone.utc).strftime("%b %d %Y")

    if enriched:
        action_summary = enriched.get("action_summary", triage.get("one_liner", ""))
        from_name = enriched.get("from_name", email.get("from", ""))
        deadline = enriched.get("deadline", "No deadline")
        priority = enriched.get("priority", "Medium")
        context = enriched.get("context", "")
    else:
        action_summary = triage.get("one_liner", email.get("subject", ""))
        from_name = email.get("from", "")
        deadline = "No deadline"
        priority = "Medium"
        context = triage.get("reason", "")

    text = (
        f"ACTION — {date_str}\n"
        f"────────────────────────────────────────\n"
        f"Action: {action_summary}\n"
        f"From: {from_name}\n"
        f"Subject: {email['subject']}\n"
        f"Deadline: {deadline} | Priority: {priority}\n"
    )
    if context:
        text += f"Context: {context}\n"
    text += "════════════════════════════════════════\n"

    if dry_run:
        log.info(f"[DRY RUN] Would write ACTION to followups doc: {action_summary}")
        print(text)
        return

    try:
        if subject_already_in_doc(doc_id, email["subject"]):
            log.info(f"ACTION already in doc (skipping duplicate): {email['subject'][:60]}")
            return
        append_to_doc(doc_id, text)
        _invalidate_doc_cache(doc_id, text)
        log.info(f"ACTION written to followups doc: {action_summary[:60]}")
    except Exception as e:
        log.error(f"Failed to write ACTION to doc {doc_id}: {e}")


# ── Cross-user doc dedup ──────────────────────────────────────────────────────
# Problem: if Yoni and Mark are both CC'd on an email, both pipelines will
# process it and try to write to the shared Follow-ups / Pipeline / Recruiting
# doc. Fix: cache each shared doc's full text once per run; before any write,
# check if the email subject is already present. Since pipelines run on a
# staggered 2h schedule, by the time machine B runs, machine A's write is
# already in the doc and the cache catches it.
#
# The cache is per-process (module-level). Invalidated per run automatically
# since each script invocation loads a fresh cache. After a write, the cache is
# updated so in-run duplicates (rare, but possible) are also caught.

_DOC_TEXT_CACHE: dict[str, str] = {}


def _get_doc_text(doc_id: str) -> str:
    """Return lowercased full text of a Google Doc, cached per process."""
    if doc_id not in _DOC_TEXT_CACHE:
        try:
            docs = get_docs_service()
            doc = docs.documents().get(documentId=doc_id).execute()
            parts = []
            for el in doc.get("body", {}).get("content", []):
                para = el.get("paragraph")
                if not para:
                    continue
                parts.append("".join(
                    r.get("textRun", {}).get("content", "")
                    for r in para.get("elements", [])
                ))
            _DOC_TEXT_CACHE[doc_id] = "\n".join(parts).lower()
        except Exception as e:
            log.warning(f"Could not cache doc text for {doc_id}: {e}")
            _DOC_TEXT_CACHE[doc_id] = ""
    return _DOC_TEXT_CACHE[doc_id]


def _invalidate_doc_cache(doc_id: str, appended_text: str) -> None:
    """After a write, append the new text to the cache so in-run dupes are caught."""
    if doc_id in _DOC_TEXT_CACHE:
        _DOC_TEXT_CACHE[doc_id] += appended_text.lower()


def subject_already_in_doc(doc_id: str, subject: str) -> bool:
    """Return True if this email subject was already written to doc (dedup guard).

    Searches the cached full doc text for the subject as a substring — catches
    both 'Subject: Re: Cholla update' and bare title lines. Works across team
    members: if machine A wrote an entry 2h ago, machine B's cache loads fresh
    and finds the subject before writing a duplicate.
    """
    needle = subject.strip().lower()
    if not needle:
        return False
    return needle in _get_doc_text(doc_id)


def write_research(email: dict, triage: dict, doc_id: str, dry_run: bool = False):
    """Write RESEARCH email one-liner to source doc."""
    date_str = datetime.now(timezone.utc).strftime("%b %d %Y")
    one_liner = triage.get("one_liner", email.get("subject", ""))
    sender = email.get("from", "")

    text = (
        f"{date_str} — {email['subject']}\n"
        f"From: {sender}\n"
        f"{one_liner}\n"
        f"────────────────────────────────────────\n"
    )

    if dry_run:
        log.info(f"[DRY RUN] Would write RESEARCH to doc {doc_id}: {email['subject'][:60]}")
        print(text)
        return

    try:
        if subject_already_in_doc(doc_id, email["subject"]):
            log.info(f"RESEARCH already in doc (skipping duplicate): {email['subject'][:60]}")
            return
        append_to_doc(doc_id, text)
        _invalidate_doc_cache(doc_id, text)
        log.info(f"RESEARCH written to source doc {doc_id}: {email['subject'][:60]}")
    except Exception as e:
        log.error(f"Failed to write RESEARCH to doc {doc_id}: {e}")


# ────────────────────────────────────────────────────────────────────
# MAIN PROCESSING LOOP
# ────────────────────────────────────────────────────────────────────

def process_emails(emails: list, config: dict, state: dict, args) -> dict:
    """
    Process a batch of emails through the two-pass pipeline.
    Returns stats dict: done, failed, skipped, by_category.
    """
    dry_run = args.list
    stats = {
        "done": 0, "failed": 0, "skipped": 0,
        "by_category": {"DEAL": 0, "RESEARCH": 0, "RECRUIT": 0, "ACTION": 0, "IGNORE": 0}
    }
    processed_ids = state.get("processed_ids", [])

    for email in emails:
        email_id = email["id"]
        subject = email.get("subject", "(no subject)")

        try:
            # ── Step 1: Research sender pre-classification (zero API) ──
            research_doc_id = classify_by_sender(email, config)
            if research_doc_id:
                log.info(f"Research sender match: {email['from']} → doc {research_doc_id}")
                triage = {
                    "category": "RESEARCH",
                    "confidence": 1.0,
                    "reason": "Known research sender",
                    "one_liner": subject
                }
                write_research(email, triage, research_doc_id, dry_run=dry_run)
                stats["done"] += 1
                stats["by_category"]["RESEARCH"] += 1
                if not dry_run and email_id not in processed_ids:
                    processed_ids.append(email_id)
                continue

            # ── Step 2: Keyword pre-filter (zero API) ──
            kw_category = keyword_prefilter(email, config)
            if kw_category:
                log.info(f"Keyword hit [{kw_category}]: {subject[:60]}")
                # Still run Haiku for the one_liner, but we trust the keyword
                triage = haiku_triage(email)
                if triage["category"] == "IGNORE" and kw_category in ("DEAL", "RECRUIT"):
                    triage["category"] = kw_category
                    triage["confidence"] = 0.75
            else:
                # ── Step 3: Pass 1 — Haiku triage ──
                triage = haiku_triage(email)

            category   = triage["category"]
            confidence = triage["confidence"]
            one_liner  = triage.get("one_liner", "")

            # Scope A: when features.job_search is OFF, drop any RECRUIT
            # classification (Haiku doesn't know about features). Mapping to
            # IGNORE skips the Recruiting Doc write and the Sonnet escalation.
            if category == "RECRUIT" and not config.get("_features_job_search", True):
                log.info(f"RECRUIT → IGNORE (job_search feature off): {subject[:60]}")
                category = "IGNORE"
                triage["category"] = "IGNORE"

            log.info(f"[{category} {confidence:.2f}] {subject[:60]} | {one_liner[:50]}")

            if dry_run:
                print(f"  [{category} {confidence:.2f}] {subject}")
                print(f"    → {one_liner}")
                if category in ("DEAL", "RECRUIT") and confidence >= 0.7:
                    print(f"    → Would escalate to Sonnet")
                stats["by_category"][category] = stats["by_category"].get(category, 0) + 1
                stats["done"] += 1
                continue

            # ── Step 4: Route by category ──
            if category == "IGNORE":
                stats["skipped"] += 1
                stats["by_category"]["IGNORE"] += 1

            elif category == "RESEARCH":
                # Route to general followups with one_liner only
                write_action_item(email, triage, {}, config, dry_run=False)
                stats["done"] += 1
                stats["by_category"]["RESEARCH"] += 1

            elif category == "ACTION":
                # Haiku-level action — enrich with Sonnet for structured write
                enriched = sonnet_enrich(email, "ACTION")
                write_action_item(email, triage, enriched, config, dry_run=False)
                stats["done"] += 1
                stats["by_category"]["ACTION"] += 1

            elif category == "DEAL":
                if confidence >= 0.7:
                    # Pass 2: Sonnet enrichment
                    enriched = sonnet_enrich(email, "DEAL")
                    write_deal_update(email, triage, enriched, config, dry_run=False)
                else:
                    # Low-confidence deal — write as action item for manual review
                    log.info(f"Low-confidence DEAL ({confidence:.2f}) — writing as ACTION for review")
                    write_action_item(email, triage, {}, config, dry_run=False)
                stats["done"] += 1
                stats["by_category"]["DEAL"] += 1

            elif category == "RECRUIT":
                if confidence >= 0.7:
                    # Pass 2: Sonnet enrichment
                    enriched = sonnet_enrich(email, "RECRUIT")
                    write_recruit_update(email, triage, enriched, config, dry_run=False)
                else:
                    log.info(f"Low-confidence RECRUIT ({confidence:.2f}) — writing as ACTION for review")
                    write_action_item(email, triage, {}, config, dry_run=False)
                stats["done"] += 1
                stats["by_category"]["RECRUIT"] += 1

            # Track processed ID
            if email_id not in processed_ids:
                processed_ids.append(email_id)

        except Exception as e:
            log.error(f"Error processing email {email_id} ({subject[:40]}): {e}")
            stats["failed"] += 1

    state["processed_ids"] = processed_ids[-5000:]  # keep last 5000 to bound file size
    return stats


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="CoS Email Mini v2 — cost-optimized Gmail/Outlook triage pipeline"
    )
    parser.add_argument(
        "--backfill", metavar="Nh",
        help="Fetch emails from the last N hours (e.g. --backfill 4h)"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Dry run: show what would be processed, no writes to Drive"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process already-seen emails (debug/rerun)"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config()
    state  = load_state()

    if args.list:
        log.info("=== DRY RUN MODE — no Drive writes ===")

    # Determine fetch window
    since = get_fetch_since(state, config, args)
    log.info(f"Fetching emails since: {since.isoformat()}")

    # Fetch emails
    try:
        emails = fetch_emails(since, config, state, args)
    except Exception as e:
        log.error(f"Failed to fetch emails: {e}")
        sys.exit(1)

    if not emails:
        log.info("No new emails found.")
        if not args.list and not args.force:
            state["last_processed_ts"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        print("0 done | 0 failed | 0 skipped")
        return

    log.info(f"Processing {len(emails)} emails...")

    # Process batch
    stats = process_emails(emails, config, state, args)

    # Update state timestamp (only on real runs)
    if not args.list:
        state["last_processed_ts"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    # Summary
    by_cat = stats["by_category"]
    summary = (
        f"{stats['done']} done | {stats['failed']} failed | {stats['skipped']} skipped\n"
        f"  DEAL={by_cat.get('DEAL',0)} RECRUIT={by_cat.get('RECRUIT',0)} "
        f"ACTION={by_cat.get('ACTION',0)} RESEARCH={by_cat.get('RESEARCH',0)} "
        f"IGNORE={by_cat.get('IGNORE',0)}"
    )
    print(summary)
    log.info(summary)

    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
