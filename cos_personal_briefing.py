#!/usr/bin/env python3
"""
cos_personal_briefing.py — Daily 7:51am Chief of Staff personal briefing

Replaces the cos-personal-briefing SKILL. ONE Sonnet call via
_subscription.cached_client replaces a multi-turn agent session.

WHAT IT DOES:
  1. Reads 5 Google Docs (follow-ups, recruiting, tomac-pipeline,
     market-update, briefing-log tail for Captured Overnight context)
  2. Calls Claude Sonnet once via cached_client — investor identity +
     Tomac bundle ride the cached system blocks; briefing structural
     template moves into user_query (Option B pattern)
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

import _firm_context as _fc  # noqa: E402
import _secrets  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = _secrets.load_secret("ANTHROPIC_API_KEY", "")
DASHBOARD_WARMUP   = "http://localhost:7777/warmup"
GOOGLE_DOCS_URL    = "https://docs.googleapis.com/v1/documents"

# ── Drive doc IDs (B1: ID excision) ───────────────────────────────────────────
# Loaded from drive-docs.yaml in the firm config repo via _fc.load_drive_docs().
# Required keys: followups, recruiting, tomac_pipeline (deal pipeline narrative),
# daily_market_update, briefing_log. Fail loud if any are missing — no silent
# fallback to legacy hardcoded IDs.
_DOCS = _fc.load_drive_docs()

def _require_doc(key: str) -> str:
    val = _DOCS.get(key, "")
    if not val:
        raise RuntimeError(
            f"drive-docs.yaml missing required doc id '{key}'. "
            f"Populate ~/cos-pipeline-config-<slug>/drive-docs.yaml or "
            f"set $COS_CONFIG_DIR to your tenant config directory."
        )
    return val

DOC_FOLLOWUPS      = _require_doc("followups")
DOC_RECRUITING     = _require_doc("recruiting")
DOC_TOMAC_PIPELINE = _require_doc("tomac_pipeline")
DOC_MARKET_UPDATE  = _require_doc("daily_market_update")
DOC_BRIEFING_LOG   = _require_doc("briefing_log")

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

# ── Briefing format prompt (loaded lazily, tenant-specific) ───────────────────
# Identity and investor frame live in the cached static core (system_prompt_v1.md).
# This function returns only the structural template unique to this routine.

def _build_briefing_format_prompt(today_str: str, day_of_week: str) -> str:
    ctx = _fc.load_firm_context()
    f   = ctx.get("firm", {}) or {}
    f_name = f.get("name", "the firm")
    dl = _fc._deal_lead(ctx)
    dl_first = (dl.get("name", "Deal lead") or "Deal lead").split()[0]
    deal_section = (
        ctx.get("workstream_categories", {}).get("deal")
        or f.get("short_name")
        or f_name
    )
    return f"""Generate the daily personal briefing for {today_str} ({day_of_week}).

Use EXACTLY this structure and nothing else:

## Personal Briefing — {today_str} ({day_of_week})

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

### {deal_section}
Active deals (stage ≠ Closed/Pass), one line each:
**[Company]** · [Stage] → [Next step] _({dl_first}'s status if noted)_
If none: "No active deals."

### Market Intelligence
From the Daily Market Update doc, find the most recent entry (today or yesterday).
- Write the **KEY TAKEAWAY** line verbatim (one sentence)
- Write the sector bullets verbatim — preserve the **bold sector headers** and bullet text exactly as written. Do not reformat or summarize.
- If a WEEKLY WRAP section is present (Fridays), include it verbatim after the daily bullets.
If no entry from the last 48h exists: "No new market update today."
This section is parsed by the dashboard — copy it exactly from the doc, do not paraphrase.

### Podcast Intelligence
From the Personal Briefing Log, find any podcast episode memos added in the last 48 hours.
For each episode found, write one entry:
**[Show Name] — "[Episode Title]"**
- THE CORE ARGUMENT: [one sentence verbatim or close paraphrase]
- KEY INVESTMENT ANGLE: [the single most actionable insight — named asset, firm, deal structure, or regulatory position. 1-2 sentences.]
- NAMES: [comma-separated list of named people and firms from that episode]
If no podcast memos in last 48h: "No new episodes processed."

### Captured Overnight
One paragraph (3–5 sentences) covering what else the capture pipeline added since yesterday — emails, calls, non-podcast captures. Exclude podcasts (covered above).
If no Capture Summary found: "No capture summary available."

RULES:
- Output ONLY the briefing markdown. No preamble, no closing remarks.
- Be specific: named people, firms, dates, deal stages. Never vague summaries.
- Today's Priorities and Coming Up draw exclusively from the Follow-ups doc — do not invent items.
- Podcast Intelligence draws exclusively from the Briefing Log — do not invent episodes."""


# ── Claude call via cached_client ─────────────────────────────────────────────

def call_claude(format_prompt: str, source_content: str,
                auth_mode: str = None, tenant: str = "tomac") -> str:
    if auth_mode == "subscription":
        import _model_router as mr  # noqa: PLC0415
        user_message = f"{format_prompt}\n\n{source_content}" if source_content else format_prompt
        result = mr.call_claude(
            task_type="cos-personal-briefing",
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            mode="subscription",
            tenant=tenant,
        )
        return result["text"]
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    sys.path.insert(0, str(_HERE / "_subscription"))
    from cached_client import complete  # noqa: PLC0415
    result = complete(
        user_query="",
        source_content=source_content,
        tenant_bundle="",
        routine_prompt=format_prompt,   # third cache breakpoint: stable briefing template
        model="claude-sonnet-4-6",
        max_tokens=2048,
    )
    usage = result["usage"]
    log_usage("cos_personal_briefing", "claude-sonnet-4-6", {
        "usage": {
            "input_tokens":                getattr(usage, "input_tokens", 0),
            "output_tokens":               getattr(usage, "output_tokens", 0),
            "cache_read_input_tokens":     getattr(usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        }
    })
    return result["text"].strip()

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

    format_prompt = _build_briefing_format_prompt(today_str, day_of_week)
    source_content = f"""=== FOLLOW-UPS DOC ===
{followups}

=== RECRUITING PIPELINE DOC ===
{recruiting}

=== TOMAC COVE DEAL PIPELINE DOC ===
{pipeline}

=== DAILY MARKET UPDATE DOC ===
{market}

=== PERSONAL BRIEFING LOG (tail — for Captured Overnight) ===
{briefing_tail}"""

    fc = _fc.load_firm_context()
    _auth_mode = fc.get("auth_mode")
    _tenant = fc.get("tenant_slug") or "tomac"
    log.info(f"Calling Claude (auth_mode={_auth_mode!r}, tenant={_tenant!r})...")
    try:
        briefing = call_claude(format_prompt, source_content,
                               auth_mode=_auth_mode, tenant=_tenant)
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
