#!/usr/bin/env python3
"""
cos_personal_briefing.py — Daily 7:51am Chief of Staff personal briefing

Replaces the cos-personal-briefing SKILL. ONE Sonnet call with a cached
system prompt replaces a multi-turn agent session.

WHAT IT DOES:
  1. Reads 5 Google Docs (follow-ups, recruiting, tomac-pipeline,
     market-update, briefing-log tail for Captured Overnight context)
  2. Calls Claude Sonnet once to generate the full structured briefing
  3. Appends the briefing to the Personal Briefing Log doc
  4. Triggers dashboard cache warmup

USAGE:
  python3 cos_personal_briefing.py           # normal run
  python3 cos_personal_briefing.py --dry-run # generate but don't append
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_CREDS = Path.home() / "credentials"
_LOG_DIR = Path.home() / "tomac-cove-pipeline" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "personal_briefing.log"),
    ],
)
log = logging.getLogger("cos_briefing")

sys.path.insert(0, str(_HERE))
try:
    from _usage import log_usage
except Exception:
    def log_usage(*_a, **_kw): return

# ── Constants ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL      = "https://api.anthropic.com/v1/messages"
MODEL              = "claude-sonnet-4-6"
MAX_TOKENS         = 2048
DASHBOARD_WARMUP   = "http://localhost:7777/warmup"
GOOGLE_DOCS_URL    = "https://docs.googleapis.com/v1/documents"

DOC_FOLLOWUPS      = "10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY"
DOC_RECRUITING     = "1ZnTCVoA0ID7XTDFy27yDnrEVhBqx75kaTg_QXFq4eXA"
DOC_TOMAC_PIPELINE = "1LHorixPs8ppwSvQzGfA_B6609YZA8dSpR4rmppENzpc"
DOC_MARKET_UPDATE  = "1UZ1t4bhgzll5VcAuP3Mj1CyYb-4xjgmbUK1xg6oUS_k"
DOC_BRIEFING_LOG   = "14wE3L6ZRsjhhx2psRKbaHS5i0kgEoteWYZusqETiAZ0"

# ── Google auth ───────────────────────────────────────────────────────────────

def get_google_token() -> str | None:
    token_path = _CREDS / "token.json"
    if not token_path.exists():
        log.warning(f"No Google token at {token_path}")
        return None
    with open(token_path) as f:
        creds = json.load(f)
    if not creds.get("refresh_token"):
        return creds.get("token")
    import urllib.parse as _up
    data = _up.urlencode({
        "client_id":     creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type":    "refresh_token",
    }).encode()
    try:
        req = urllib.request.Request(creds["token_uri"], data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            new = json.loads(r.read())
        creds["token"] = new["access_token"]
        with open(token_path, "w") as f:
            json.dump(creds, f)
        return creds["token"]
    except Exception as e:
        log.warning(f"Google token refresh failed: {e}")
        return creds.get("token")

# ── Google Docs helpers ───────────────────────────────────────────────────────

def _doc_text(doc: dict, char_limit: int = 0) -> str:
    """Extract plain text from a Google Docs document object."""
    parts = []
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    parts.append(pe["textRun"].get("content", ""))
    text = "".join(parts)
    return text[:char_limit] if char_limit else text


def fetch_doc(token: str, doc_id: str, char_limit: int = 30000) -> str:
    url = f"{GOOGLE_DOCS_URL}/{doc_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            doc = json.loads(r.read())
        return _doc_text(doc, char_limit)
    except Exception as e:
        log.warning(f"Could not read doc {doc_id}: {e}")
        return f"[could not read doc {doc_id}: {e}]"


def doc_end_index(token: str, doc_id: str) -> int:
    url = f"{GOOGLE_DOCS_URL}/{doc_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        doc = json.loads(r.read())
    content = doc.get("body", {}).get("content", [])
    return content[-1].get("endIndex", 1) if content else 1


def append_to_doc(token: str, doc_id: str, text: str) -> None:
    end = doc_end_index(token, doc_id)
    url = f"{GOOGLE_DOCS_URL}/{doc_id}:batchUpdate"
    body = json.dumps({
        "requests": [{
            "insertText": {
                "location": {"index": end - 1, "segmentId": ""},
                "text": text,
            }
        }]
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()

# ── System prompt (static — will be prompt-cached) ────────────────────────────

_SYSTEM = """You are the Chief of Staff AI generating the daily personal briefing for Yoni Gontownik — senior infrastructure PE professional co-founding Tomac Cove Infrastructure Partners with Mark Saxe.

You will be given the current content of four Google Docs and the tail of the Personal Briefing Log. Generate the full briefing with EXACTLY this structure and nothing else:

## Personal Briefing — {TODAY} ({DAY_OF_WEEK})

### Today's Priorities
All follow-up rows with due date = today, sorted by urgency (explicitly promised > inferred).
Format each: **[Person/Firm]** — [what] _(source: email/call/calendar)_
If none: "No urgent items due today."

### Coming Up (Next 3 Days)
Follow-up rows due in the next 3 calendar days (not today). Same format.
If none: "Nothing due in the next 3 days."

### Recruiting Pipeline
For each active opportunity (stage ≠ Closed), one line:
**[Firm]** · [Role] · [Stage] → [Next step] _(deadline if set)_
If none active: "No active recruiting opportunities."

### Tomac Cove
Active deals (stage ≠ Closed/Pass), one line each:
**[Company]** · [Stage] → [Next step] _(Mark's status if noted)_
If none: "No active deals."

### Market Intelligence
From the Daily Market Update doc, find the most recent entry (today or yesterday).
- Write the **KEY TAKEAWAY** verbatim (one sentence)
- Write 3–5 most investment-relevant bullets from sections 1–3 (Digital Infra/Energy, Regulatory, Capital Flows/Dealmaking). Prioritize: named assets, dollar amounts, named firms, regulatory decisions with hard dates.
- Format each bullet: `- **[Section header]:** [first sentence of insight, 30–60 words max]`
If no entry from the last 48h exists: "No new market update today."
This section is parsed by the dashboard fetch script — make it specific, not thematic.

### Captured Overnight
One paragraph (3–5 sentences) summarizing what the capture pipeline added since yesterday, based on the most recent Capture Summary entry in the Personal Briefing Log tail.
If no Capture Summary found: "No capture summary available."

RULES:
- Replace {TODAY} and {DAY_OF_WEEK} with the actual date and day provided in the user message.
- Output ONLY the briefing markdown. No preamble, no closing remarks.
- Be specific: named people, firms, dates, deal stages. Never vague summaries.
- Today's Priorities and Coming Up draw exclusively from the Follow-ups doc — do not invent items."""

# ── Claude call ───────────────────────────────────────────────────────────────

def call_claude(user_message: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    body = json.dumps({
        "model":      MODEL,
        "max_tokens": MAX_TOKENS,
        "system": [
            {
                "type":          "text",
                "text":          _SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_message}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={
            "x-api-key":        ANTHROPIC_API_KEY,
            "anthropic-version":"2023-06-01",
            "anthropic-beta":   "prompt-caching-1",
            "content-type":     "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read())
    log_usage("cos_personal_briefing", MODEL, resp)
    return resp["content"][0]["text"].strip()

# ── Dashboard warmup ──────────────────────────────────────────────────────────

def trigger_warmup() -> None:
    try:
        urllib.request.urlopen(
            urllib.request.Request(DASHBOARD_WARMUP, method="POST"),
            timeout=5,
        ).read()
        log.info("Dashboard warmup triggered")
    except Exception as e:
        log.warning(f"Warmup failed (non-fatal): {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate briefing but don't append to log or warmup")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    today_str    = now.strftime("%B %-d, %Y")
    day_of_week  = now.strftime("%A")

    log.info("═══ cos_personal_briefing starting ═══")

    token = get_google_token()
    if not token:
        log.error("No Google token — cannot read docs")
        return 1

    # Fetch all source docs in parallel-ish (sequential is fine, they're fast)
    log.info("Fetching source docs...")
    followups   = fetch_doc(token, DOC_FOLLOWUPS,      char_limit=25000)
    recruiting  = fetch_doc(token, DOC_RECRUITING,     char_limit=15000)
    pipeline    = fetch_doc(token, DOC_TOMAC_PIPELINE, char_limit=15000)
    market      = fetch_doc(token, DOC_MARKET_UPDATE,  char_limit=10000)
    briefing_tail = fetch_doc(token, DOC_BRIEFING_LOG, char_limit=50000)
    # Only send the tail (last ~8000 chars) to find the most recent Capture Summary
    briefing_tail = briefing_tail[-8000:]
    log.info(f"  follow-ups: {len(followups)} chars")
    log.info(f"  recruiting: {len(recruiting)} chars")
    log.info(f"  pipeline:   {len(pipeline)} chars")
    log.info(f"  market:     {len(market)} chars")

    user_message = f"""TODAY: {today_str} ({day_of_week})

=== FOLLOW-UPS DOC ===
{followups}

=== RECRUITING PIPELINE DOC ===
{recruiting}

=== TOMAC COVE DEAL PIPELINE DOC ===
{pipeline}

=== DAILY MARKET UPDATE DOC ===
{market}

=== PERSONAL BRIEFING LOG (tail — for Captured Overnight) ===
{briefing_tail}

Generate the briefing now."""

    log.info("Calling Claude...")
    try:
        briefing = call_claude(user_message)
    except Exception as e:
        log.error(f"Claude call failed: {e}")
        return 1

    log.info(f"Briefing generated ({len(briefing)} chars)")

    if args.dry_run:
        log.info("(dry-run) — briefing not appended. Output:")
        print(briefing)
        return 0

    # Append to Personal Briefing Log with a separator
    append_text = f"\n\n{'═' * 60}\n\n{briefing}\n"
    try:
        append_to_doc(token, DOC_BRIEFING_LOG, append_text)
        log.info("Appended to Personal Briefing Log")
    except Exception as e:
        log.error(f"Failed to append to briefing log: {e}")
        return 1

    trigger_warmup()
    log.info("═══ cos_personal_briefing complete ═══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
