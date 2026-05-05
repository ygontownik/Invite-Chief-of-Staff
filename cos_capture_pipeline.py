#!/usr/bin/env python3
"""
cos_capture_pipeline.py — Daily 7:22am COS capture + reconciliation

Replaces the cos-capture-pipeline Claude Code SKILL with a portable Python
script. Same behavior, no Claude Code dependency, supports Gmail OR Outlook
via the email provider abstraction.

WHAT IT DOES (same as the SKILL it replaces):
  PART A — CAPTURE (new info from the last ~24h)
    - Scan inbox (Gmail or Outlook) for new emails since yesterday 7am
    - Scan Drive folders (Otter, Call Recordings, deal correspondence) for new files
    - Scan Calendar for next 14 days
    - For each email needing a reply: draft it in the principal's voice
      (tone/sign-off from firm_context.yaml["draft_voice"])
    - Extract action items, deal updates, new contacts

  PART B — RECONCILIATION (sync existing follow-ups against new evidence)
    - REMOVE rows resolved by Gmail/Calendar/transcript evidence
    - ESCALATE rows that became urgent
    - UPDATE rows whose scope/timing changed
    - ENRICH rows with latest context

  PART C — WRITE (single batched call)
    - All doc updates pipe through cos_batch_write.py (existing)
    - Drafts created via the email provider
    - Dashboard warmup triggered automatically

ARCHITECTURE:
  Python orchestrates fetches and writes; ONE Anthropic call (Claude Sonnet)
  does the SKILL's reasoning. The full SKILL ruleset is encoded in the system
  prompt below — built dynamically from firm_context.yaml so it runs unchanged
  for any firm.

USAGE:
  python3 cos_capture_pipeline.py                    # normal daily run
  python3 cos_capture_pipeline.py --since 24h        # custom lookback
  python3 cos_capture_pipeline.py --dry-run          # no writes, no drafts
  python3 cos_capture_pipeline.py --no-drafts        # writes but skip drafts
  python3 cos_capture_pipeline.py --provider outlook # override email_provider

SCHEDULED:
  LaunchAgent com.tomaccove.cos-capture-pipeline → bash runner
  → calls this script at 7:22 AM M-F directly (no Claude Code SKILL wrapper).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Paths and config ─────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_CREDS = Path.home() / "credentials"

# B3 (ID excision): per-tenant log path. Slug comes from
# firm_context.yaml :: tenant_slug (or firm.short_name lowercased as fallback).
# Defaults to "tomac" for backwards compatibility. The directory
# ~/cos-pipeline/logs-<slug>/ replaces the legacy ~/tomac-cove-pipeline/logs.
# setup.py's --check command will offer to symlink the legacy path forward.
def _resolve_log_dir() -> Path:
    try:
        sys.path.insert(0, str(_HERE))
        import _firm_context as _fc_local  # noqa: E402
        ctx = _fc_local.load_firm_context()
    except Exception:
        ctx = {}
    slug = (
        ctx.get("tenant_slug")
        or (ctx.get("firm", {}) or {}).get("short_name", "")
        or "tomac"
    )
    slug = str(slug).strip().lower().replace(" ", "-") or "tomac"
    return Path.home() / "cos-pipeline" / f"logs-{slug}"

_LOG_DIR = _resolve_log_dir()
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "capture_pipeline.log"),
    ],
)
log = logging.getLogger("cos_capture")

sys.path.insert(0, str(_HERE))

import _firm_context as _fc  # noqa: E402
import _secrets  # noqa: E402
try:
    from _usage import log_usage
except Exception:
    def log_usage(*_a, **_kw): return
from _email_provider import (  # noqa: E402
    DraftHandle,
    EmailMessage,
    EmailProviderError,
    get_email_provider,
)


# ── Constants ─────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = _secrets.load_secret("ANTHROPIC_API_KEY", "")
MAX_TOKENS = 8192
DASHBOARD_WARMUP_URL = "http://localhost:7777/warmup"


# ── System prompt builder (replaces the SKILL.md ruleset) ────────────────────

def build_system_prompt(ctx: dict) -> str:
    """
    Render the SKILL's full ruleset as a system prompt, parameterized by
    firm_context.yaml. The structure mirrors the SKILL's PART A / PART B /
    quality standards / draft voice exactly — only firm identity is templated.
    """
    p = ctx.get("principal", {})
    f = ctx.get("firm", {})
    dl = _fc._deal_lead(ctx)
    p_name = p.get("name", "Principal")
    p_first = p_name.split()[0]
    f_name = f.get("name", "the firm")
    f_short = f.get("short_name", "")
    deal_ws = _fc.workstream_deal(ctx)
    recruit_ws = _fc.workstream_recruiting(ctx)
    owners = _fc.owner_whitelist_str(ctx)

    voice = ctx.get("draft_voice", {}) or {}
    voice_tone = voice.get("tone", "professional, concise, warm but not flowery")
    voice_signoff = voice.get(
        "preferred_signoff", f"Best,\n{p_first}"
    ).replace("[principal_first_name]", p_first)
    voice_greeting = voice.get("default_greeting", "Hi [first_name],")
    voice_brevity = voice.get(
        "brevity", "2-4 sentences for routine replies; up to 8 for substantive responses"
    )
    voice_always = voice.get("always_include", []) or []
    voice_never = voice.get("never_include", []) or []
    voice_context_lines = voice.get("context_to_include_in_replies", []) or []

    team_str = ", ".join(f"{m['name']} ({m['role']})" for m in ctx.get("team", []))

    # The full ruleset, templated
    parts = [
        f"You are the Chief of Staff AI for {p_name} — {p.get('role', 'senior investor')}, "
        f"co-founding {f_name} ({f_short}) with {dl.get('name', 'co-founder') if dl else ''}.",
        f"\nTEAM: {team_str}.",
        f"\nYour job is the daily 7:22am capture + reconciliation pass over inbox, "
        f"calendar, Drive, and the Follow-ups doc. Output a single JSON spec the "
        f"Python wrapper will execute.\n",

        "═" * 60,
        "PART A — CAPTURE NEW INFORMATION",
        "═" * 60,

        f"\nA1. Inbox (last 24h)\n"
        f"For every email in the input data set:\n"
        f"  - Extract new commitments, follow-ups, meeting requests, deadlines.\n"
        f"  - Identify new contacts to add to the People doc.\n"
        f"  - DRAFT REPLIES: when an email requires a reply from {p_first} and "
        f"    they haven't responded yet, compose a contextually appropriate draft\n"
        f"    following the DRAFT VOICE rules below. Output it in the 'drafts' array.",

        "\nA2. Calendar (next 14 days)\n"
        "Cross-check upcoming events against existing follow-up rows.\n"
        "DO NOT create generic 'prep for call' items — only specific prep with named deliverables.\n"
        "DELETE on sight: 'Send notes', 'Send action items', 'Email summary' from any recurring "
        "internal call (Weekly Sync, Daily Standup, etc.).",

        "\nA3. Drive (Otter / Call Recordings / Deal Correspondence)\n"
        "For each new transcript/file: extract commitments, deal stage changes, new contacts. "
        "Apply the read strategy — find the summary section first, don't read full transcripts blindly.\n"
        "Speaker rules:\n"
        f"  - 'I will…' from {p_first} → owner='{p_first}'\n"
        f"  - 'Mark/David said they will…' → note in pipeline doc + add tracking follow-up\n"
        f"  - 'Speaker N' must be resolved by context before generating items.",

        "═" * 60,
        "ACTION ITEM QUALITY STANDARD",
        "═" * 60,

        "\nValid action items either:\n"
        "  1. Have a direct link to a pre-prepared artifact (Gmail/Outlook draft, Drive doc,\n"
        "     payment portal, calendar invite), OR\n"
        "  2. State a specific named deliverable (not a category of work).\n",

        "DELETE on sight (these are NOT action items):\n"
        "  - 'Prep for call with X' — no specific deliverable\n"
        "  - 'Review agenda for [meeting]' — calendar handles this\n"
        "  - 'Follow up generally with X' — needs a specific reason\n"
        "  - 'Check in with [person]' — unless a specific question or deliverable\n"
        "  - 'Review notes from weekly call' — not an action\n"
        "  - Any 'Send notes / Send recap / Email summary / Distribute action items'\n"
        "    from ANY recurring weekly/biweekly internal call. DELETE.",

        "\nTwo-party rule: every follow-up must involve a clear action between\n"
        f"{p_first} (or {dl.get('name', '').split()[0] if dl else 'a teammate'}) and a SPECIFIC NAMED external party.\n"
        "Reject if Who is just '{p_first}' alone, counterparty is unnamed, or it's purely internal.",

        f"\nACTION-DIRECTION INVERSION CHECK (rule Y2) — when the action verb is a\n"
        f"transmission verb (`send`, `share`, `forward`, `deliver`, `provide`,\n"
        f"`transmit`, `circulate`, `pass along`, `intro`, `ping`, `schedule`,\n"
        f"`follow up`, `return call`, `attach`), explicitly identify which side\n"
        f"is the SENDER from the email's From:/To: headers and role context\n"
        f"BEFORE emitting the follow-up. Do NOT default to {p_first} just because\n"
        f"their name appears in the thread.\n"
        f"  - Investment banks / placement agents / fundraising advisors / brokers\n"
        f"    pitching deal flow or capital INTO {p_first} → THEY send teasers /\n"
        f"    CIMs / data rooms / decks / term sheets TO {p_first}. The counterparty\n"
        f"    owes the action; emit owner='external' with counterparty='Firm — Person'.\n"
        f"    NEVER emit a follow-up telling {p_first} to 'send' what is being\n"
        f"    pitched IN (failure mode: 'Astris sent us the teaser' written as\n"
        f"    '{p_first} to send teaser to Astris' — owner is Astris, NOT {p_first}).\n"
        f"  - {p_first} sponsoring a deal OUT to LPs / co-investors / lenders →\n"
        f"    {p_first} sends materials. Emit owner='{p_first}'.\n"
        f"  - Mutual exchanges (NDAs, term sheets, redlines passed back and forth)\n"
        f"    → emit two follow-ups, one per direction, each with the correct owner.\n"
        f"  - Default if unclear: attribute the send to the counterparty (owner=\n"
        f"    'external'). Better to under-attribute to {p_first} than fabricate\n"
        f"    a send-verb on the wrong side.",

        f"\nABSOLUTE-DATE RULE (rule AB1) — every reference to a date or week in\n"
        f"the `what` / `context` / `linked_to` text MUST be an absolute form.\n"
        f"Resolve relative phrasing against TODAY (provided in the user message)\n"
        f"or, when emitting a row from a specific email/transcript, against\n"
        f"that source's date.\n"
        f"  - ALLOWED: '2026-05-12', 'week of 2026-05-12', 'May 12'\n"
        f"  - FORBIDDEN: 'tomorrow', 'next week', 'this Friday', 'Wed 4/29',\n"
        f"    'Friday 5/1', 'EOD', 'early next week', 'next Monday'\n"
        f"  Example: email dated 2026-05-04 says 'send by tomorrow EOD' →\n"
        f"    emit `what` as 'Send X by 2026-05-05 EOD' (or just '2026-05-05').\n"
        f"  Example: 'confirm Wed 4/29 morning intro call' (year inferred from\n"
        f"    email date) → emit 'confirm 2026-04-29 morning intro call'.\n"
        f"  Why: the row lives on the dashboard for days; relative phrasing\n"
        f"  reads stale every day after extraction even when the action is\n"
        f"  still valid. Absolute dates never go stale.",

        "═" * 60,
        "PART B — RECONCILIATION",
        "═" * 60,

        "\nGo through every existing follow-up row and decide one of:\n"
        "  REMOVE — only with positive evidence of completion:\n"
        "    - Gmail/Outlook reply confirms it was done\n"
        "    - Calendar event for it is in the past + a confirming email/transcript exists\n"
        "    - Recent transcript explicitly says 'handled'\n"
        "    - SCHEDULING RESOLUTION: if follow-up is about scheduling X, and a calendar\n"
        "      event with X now exists → REMOVE (the scheduled event IS the evidence).\n"
        "    Past due date alone is NEVER reason to remove — overdue items stay.\n",

        "  ESCALATE — move due date to today if:\n"
        "    - Calendar event for this item is TODAY or TOMORROW\n"
        "    - A reply created a firm new deadline\n"
        "    - The counterparty followed up and is waiting.\n",

        "  UPDATE — refine What/Due if scope or timing changed in new email/transcript.\n",
        "  ENRICH — append latest context inline (one concise line) for key items.\n",

        "═" * 60,
        "DRAFT VOICE",
        "═" * 60,

        f"\nWhen drafting an email reply on behalf of {p_name}:\n",
        f"  TONE: {voice_tone}",
        f"  GREETING: {voice_greeting}  (replace [first_name] with recipient's first name)",
        f"  BREVITY: {voice_brevity}",
        f"  SIGN-OFF: {voice_signoff}",
        "\n  ALWAYS:",
        *[f"    - {line}" for line in voice_always],
        "\n  NEVER:",
        *[f"    - {line}" for line in voice_never],
        "\n  CONTEXT TO LEAN ON:",
        *[f"    - {line}" for line in voice_context_lines],

        "═" * 60,
        "OUTPUT FORMAT",
        "═" * 60,

        "\nReturn ONE JSON object with this exact shape:\n",
        "{",
        '  "follow_ups_to_add": [',
        '    {"who":"...", "what":"...", "due":"YYYY-MM-DD", '
        f'"workstream":"Job Search|{deal_ws}|Other", '
        '"linked_to":"<URL or empty>", "context":"..."},',
        "    ...",
        "  ],",
        '  "follow_ups_to_remove": [',
        '    {"row_num": <int>, "reason": "evidence summary"}',
        "  ],",
        '  "follow_ups_to_update": [',
        '    {"row_num": <int>, "what":"<new>", "due":"<new>", "reason": "..."}',
        "  ],",
        '  "drafts": [',
        '    {"in_reply_to_message_id":"<provider id>", '
        '"in_reply_to_thread_id":"<provider thread id>", '
        '"to":["<email>"], "cc":[], "subject":"Re: ...", "body_text":"..."}',
        "  ],",
        '  "deal_updates": [',
        '    {"deal_name":"...", "status_change":"...", "next_step":"..."}',
        "  ],",
        '  "new_contacts": [',
        '    {"name":"...", "firm":"...", "title":"...", "context":"..."}',
        "  ],",
        '  "log_summary": "1-3 sentence summary of what changed this run."',
        "}",

        f'\nOwners must be one of: {owners}, or "external".',
        "Return ONLY the JSON. No prose, no markdown fences.",
    ]

    return "\n".join(parts)


# ── Anthropic call via cached_client ─────────────────────────────────────────

def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("{") or text.startswith("["): return text
    if "```" in text:
        parts = text.split("```")
        for i in range(len(parts) - 1, 0, -1):
            block = parts[i]
            if block.startswith("json\n"): block = block[5:]
            block = block.strip()
            if block.startswith("{") or block.startswith("["): return block
    return text


def call_claude(system_prompt: str, user_payload: str, ctx: dict = None) -> dict:
    """Single Sonnet call — returns parsed JSON output."""
    if ctx and ctx.get("auth_mode") == "subscription":
        import _model_router as mr  # noqa: PLC0415
        slug = (
            ctx.get("tenant_slug")
            or (ctx.get("firm", {}) or {}).get("short_name", "").lower().replace(" ", "-")
            or "tomac"
        )
        result = mr.call_claude(
            task_type="cos-capture-pipeline",
            system=system_prompt,
            messages=[{"role": "user", "content": user_payload}],
            mode="subscription",
            tenant=slug,
            extract_json=True,
        )
        return json.loads(_extract_json(result["text"]))

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    sys.path.insert(0, str(_HERE / "_subscription"))
    from cached_client import complete  # noqa: PLC0415
    result = complete(
        user_query="",
        source_content=user_payload,
        tenant_bundle="",
        routine_prompt=system_prompt,   # third cache breakpoint: stable ruleset+schema
        model="claude-sonnet-4-6",
        max_tokens=MAX_TOKENS,
    )
    usage = result["usage"]
    log_usage("cos_capture_pipeline", "claude-sonnet-4-6", {
        "usage": {
            "input_tokens":                getattr(usage, "input_tokens", 0),
            "output_tokens":               getattr(usage, "output_tokens", 0),
            "cache_read_input_tokens":     getattr(usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        }
    })
    text = result["text"].strip()
    # Strip optional code-fence
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return json.loads(text)


# ── Drive / Calendar fetch helpers ────────────────────────────────────────────

def fetch_followups_doc(token: str, doc_id: str) -> str:
    """Read the Follow-ups doc as plain text, with row indices."""
    if not doc_id:
        return ""
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            doc = json.loads(r.read())
    except Exception as e:
        log.warning(f"Could not read followups doc {doc_id}: {e}")
        return ""
    text = ""
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    text += pe["textRun"].get("content", "")
    return text[:30000]  # cap for context window


def fetch_calendar_events(token: str, lookback_days: int = 7, lookahead_days: int = 14) -> list[dict]:
    """Fetch upcoming and recent calendar events via the user's primary calendar."""
    now = datetime.now(timezone.utc)
    t_min = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_max = (now + timedelta(days=lookahead_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    import urllib.parse as _up
    url = (
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        f"?timeMin={_up.quote(t_min)}&timeMax={_up.quote(t_max)}"
        "&singleEvents=true&orderBy=startTime&maxResults=100"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        log.warning(f"Calendar fetch failed: {e}")
        return []
    events = []
    for ev in data.get("items", []):
        events.append({
            "title": ev.get("summary", ""),
            "start": (ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")),
            "end":   (ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date", "")),
            "attendees": [a.get("displayName") or a.get("email", "") for a in ev.get("attendees", [])],
        })
    return events


def get_google_token() -> Optional[str]:
    """Refresh and return the Drive/Docs/Calendar OAuth token (token.json format)."""
    token_path = _CREDS / "token.json"
    if not token_path.exists():
        log.warning(f"No Google OAuth token at {token_path}")
        return None
    with open(token_path) as f:
        creds = json.load(f)
    if not creds.get("refresh_token"):
        return creds.get("token")
    import urllib.parse as _up
    data = _up.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
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


# ── Email assembly ────────────────────────────────────────────────────────────

def serialize_emails_for_prompt(emails: list[EmailMessage]) -> str:
    """Compact email rendering for the Claude payload — keeps tokens reasonable."""
    out = []
    for m in emails[:50]:  # cap for context window
        out.append(
            f"--- EMAIL ---\n"
            f"id: {m.id}\n"
            f"thread: {m.thread_id}\n"
            f"from: {m.sender}\n"
            f"to: {', '.join(str(r) for r in m.recipients)}\n"
            f"subject: {m.subject}\n"
            f"received: {m.received_at.isoformat() if m.received_at else 'n/a'}\n"
            f"snippet: {m.snippet[:300]}\n"
            f"body_excerpt: {(m.body_text or '')[:1500]}\n"
        )
    return "\n".join(out)


# ── Write phase ───────────────────────────────────────────────────────────────

def write_via_batch_script(spec: dict, dry_run: bool = False) -> bool:
    """Pipe the JSON spec into cos_batch_write.py (existing script)."""
    if dry_run:
        log.info("(dry-run) Would write spec: %s", json.dumps(spec, indent=2)[:1000])
        return True
    batch_script = _HERE / "cos_batch_write.py"
    if not batch_script.exists():
        log.error(f"cos_batch_write.py not found at {batch_script}")
        return False
    try:
        proc = subprocess.run(
            ["python3", str(batch_script)],
            input=json.dumps(spec).encode(),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            log.error(f"cos_batch_write failed: {proc.stderr.decode()}")
            return False
        log.info(f"Batch write completed: {proc.stdout.decode().strip()[:200]}")
        return True
    except Exception as e:
        log.error(f"Batch write error: {e}")
        return False


def create_drafts(provider, drafts: list[dict], dry_run: bool = False) -> list[DraftHandle]:
    """Create email drafts via the provider."""
    handles = []
    for d in drafts:
        if dry_run:
            log.info(f"(dry-run) Would draft: to={d.get('to')} subj={d.get('subject', '')[:60]}")
            continue
        try:
            handle = provider.create_draft(
                to=d.get("to", []),
                subject=d.get("subject", ""),
                body_text=d.get("body_text", ""),
                cc=d.get("cc"),
                in_reply_to_message_id=d.get("in_reply_to_message_id"),
                in_reply_to_thread_id=d.get("in_reply_to_thread_id"),
            )
            handles.append(handle)
            log.info(f"Draft created: {handle.id} → {handle.web_url}")
        except EmailProviderError as e:
            log.error(f"Draft failed for {d.get('to')}: {e}")
    return handles


def trigger_warmup() -> None:
    try:
        urllib.request.urlopen(
            urllib.request.Request(DASHBOARD_WARMUP_URL, method="POST"),
            timeout=5,
        ).read()
        log.info("Dashboard warmup triggered")
    except Exception as e:
        log.warning(f"Warmup trigger failed (non-fatal): {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since",     default="24h",
                        help="Lookback window: 24h, 4h, 7d, etc. Default: 24h")
    parser.add_argument("--dry-run",   action="store_true", help="No writes, no drafts")
    parser.add_argument("--no-drafts", action="store_true", help="Writes but skip drafts")
    parser.add_argument("--provider",  default=None,
                        help="Override email_provider from firm_config.json")
    args = parser.parse_args()

    log.info("═══ cos_capture_pipeline starting ═══")

    # Load configs
    try:
        ctx = _fc.load_firm_context()
    except Exception as e:
        log.error(f"Could not load firm_context.yaml: {e}")
        return 1

    cfg_path = _HERE / "firm_config.json"
    cfg = json.load(open(cfg_path)) if cfg_path.exists() else {}
    provider_name = args.provider or cfg.get("email_provider", "gmail")
    log.info(f"Email provider: {provider_name}")

    # Parse --since
    n = int(args.since[:-1] or "24")
    unit = args.since[-1].lower() if args.since else "h"
    delta = timedelta(hours=n) if unit == "h" else timedelta(days=n)
    since = datetime.now(timezone.utc) - delta

    # Authorize email provider
    try:
        provider = get_email_provider(provider_name)
        provider.authorize()
    except Exception as e:
        log.error(f"Email provider auth failed: {e}")
        return 1

    # Round 1: data collection
    log.info(f"Fetching emails since {since.isoformat()}...")
    try:
        emails = provider.search_inbox(since=since, max_results=50)
    except EmailProviderError as e:
        log.error(f"Inbox search failed: {e}")
        return 1
    log.info(f"  {len(emails)} emails")

    google_token = get_google_token()

    log.info("Fetching follow-ups doc + calendar...")
    docs = _fc.load_drive_docs()
    followups_doc_id = docs.get("followups", "")
    followups_text = fetch_followups_doc(google_token, followups_doc_id) if google_token else ""
    log.info(f"  followups doc: {len(followups_text)} chars")

    calendar = fetch_calendar_events(google_token) if google_token else []
    log.info(f"  calendar events: {len(calendar)}")

    # Round 2: fetch full thread bodies for emails that look like they need a reply
    full_threads = {}
    for m in emails[:20]:  # cap for token budget
        if m.is_unread or "?" in m.snippet:
            try:
                full_threads[m.thread_id] = provider.get_thread(m.thread_id)
            except EmailProviderError:
                continue

    # Round 3: build prompt + call Claude
    system_prompt = build_system_prompt(ctx)
    user_payload = (
        f"TODAY: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"=== EMAILS (last {args.since}, {len(emails)} messages) ===\n"
        f"{serialize_emails_for_prompt(emails)}\n\n"
        f"=== UPCOMING CALENDAR (next 14 days) ===\n"
        f"{json.dumps(calendar[:50], indent=2)}\n\n"
        f"=== EXISTING FOLLOW-UPS DOC (current state) ===\n"
        f"{followups_text}\n\n"
        f"Apply the SKILL ruleset. Return the JSON spec."
    )

    log.info(f"Calling Claude (system={len(system_prompt)} chars, user={len(user_payload)} chars)...")
    try:
        result = call_claude(system_prompt, user_payload, ctx=ctx)
    except Exception as e:
        log.error(f"Claude call failed: {e}")
        return 1

    log.info("Claude responded:")
    log.info(f"  follow_ups_to_add:    {len(result.get('follow_ups_to_add', []))}")
    log.info(f"  follow_ups_to_remove: {len(result.get('follow_ups_to_remove', []))}")
    log.info(f"  follow_ups_to_update: {len(result.get('follow_ups_to_update', []))}")
    log.info(f"  drafts:               {len(result.get('drafts', []))}")
    log.info(f"  deal_updates:         {len(result.get('deal_updates', []))}")
    log.info(f"  new_contacts:         {len(result.get('new_contacts', []))}")

    # Round 4: writes
    if not args.no_drafts:
        create_drafts(provider, result.get("drafts", []), dry_run=args.dry_run)
    write_via_batch_script(result, dry_run=args.dry_run)

    if not args.dry_run:
        trigger_warmup()

    log.info(f"Summary: {result.get('log_summary', '(no summary)')}")
    log.info("═══ cos_capture_pipeline complete ═══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
