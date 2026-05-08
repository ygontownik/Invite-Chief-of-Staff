#!/usr/bin/env python3
"""
meeting_prep.py — pre-meeting intelligence brief

Pulls today's (or a specified date's) Google Calendar events, cross-references
call transcript history and the deal pipeline, then generates a six-section
Claude brief per external meeting.

Usage:
    python meeting_prep.py                        # today's meetings
    python meeting_prep.py --date 2026-05-08     # specific date
    python meeting_prep.py --title "Stonepeak"   # search by title substring
    python meeting_prep.py --dry-run             # show events, no Claude call
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────────
CREDENTIALS_DIR        = Path.home() / "credentials"
GCAL_TOKEN_PATH        = CREDENTIALS_DIR / "gcal_token.json"
GCAL_CREDS_PATH        = CREDENTIALS_DIR / "client_secret.json"
TRANSCRIPT_TRACKER     = CREDENTIALS_DIR / "processed_cos_transcripts.json"
PIPELINE_JSON          = Path.home() / "dashboards/data/compiled/deal-pipeline-data.json"

GCAL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
]

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── calendar ───────────────────────────────────────────────────────────────────

def _get_gcal_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if GCAL_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GCAL_TOKEN_PATH), GCAL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GCAL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    if not creds or not creds.valid:
        raise RuntimeError(
            "GCal token not found or invalid. Run call_scheduler.py to authenticate."
        )
    return build("calendar", "v3", credentials=creds)


def fetch_events_for_date(target_date: datetime.date) -> list[dict]:
    """Return all GCal events for target_date with full attendee data."""
    try:
        svc = _get_gcal_service()
    except Exception as e:
        print(f"[warn] GCal unavailable: {e}")
        return []

    tz_str = "America/New_York"
    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         0, 0, 0).astimezone().isoformat()
    day_end   = datetime(target_date.year, target_date.month, target_date.day,
                         23, 59, 59).astimezone().isoformat()

    events = []
    try:
        cal_list = svc.calendarList().list().execute()
        for cal in cal_list.get("items", []):
            result = svc.events().list(
                calendarId=cal["id"],
                timeMin=day_start,
                timeMax=day_end,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            ).execute()
            for ev in result.get("items", []):
                normalized = _normalize(ev)
                if normalized:
                    events.append(normalized)
    except Exception as e:
        print(f"[warn] Error fetching calendar: {e}")

    # deduplicate by event id
    seen = set()
    unique = []
    for ev in events:
        if ev["id"] not in seen:
            seen.add(ev["id"])
            unique.append(ev)
    return sorted(unique, key=lambda e: e["start"])


def _normalize(ev: dict) -> dict | None:
    start_raw = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
    if "T" not in str(start_raw):
        return None  # skip all-day events
    end_raw = ev.get("end", {}).get("dateTime") or ""

    attendees = []
    for a in ev.get("attendees", []):
        email = a.get("email", "")
        name  = a.get("displayName", "")
        if not email:
            continue
        attendees.append({"email": email, "name": name, "status": a.get("responseStatus", "")})

    return {
        "id":        ev.get("id", ""),
        "title":     ev.get("summary", "Untitled"),
        "start":     start_raw,
        "end":       end_raw,
        "location":  ev.get("location", ""),
        "attendees": attendees,
        "description": ev.get("description", ""),
    }


# ── transcript history ──────────────────────────────────────────────────────────

def _load_transcript_tracker() -> dict:
    if not TRANSCRIPT_TRACKER.exists():
        return {}
    try:
        return json.loads(TRANSCRIPT_TRACKER.read_text())
    except Exception:
        return {}


def find_transcript_history(firm_tokens: list[str], tracker: dict) -> list[dict]:
    """Return transcript entries whose title contains any of the firm tokens."""
    hits = []
    tokens_lower = [t.lower() for t in firm_tokens if len(t) > 3]
    for file_id, meta in tracker.items():
        title = meta.get("title", "").lower()
        if any(tok in title for tok in tokens_lower):
            hits.append({
                "title":        meta.get("title", ""),
                "category":     meta.get("category", ""),
                "processed_at": meta.get("processed_at", ""),
                "file_id":      file_id,
            })
    hits.sort(key=lambda x: x["processed_at"], reverse=True)
    return hits[:5]  # most recent 5


# ── deal pipeline ───────────────────────────────────────────────────────────────

def _load_pipeline() -> dict:
    if not PIPELINE_JSON.exists():
        return {}
    try:
        return json.loads(PIPELINE_JSON.read_text())
    except Exception:
        return {}


def find_pipeline_match(firm_tokens: list[str], pipeline: dict) -> dict | None:
    """Return the first pipeline target whose name matches a firm token."""
    tokens_lower = [t.lower() for t in firm_tokens if len(t) > 3]
    for theme in pipeline.get("themes", []):
        for target in theme.get("targets", []):
            name = target.get("name", "").lower()
            if any(tok in name or name in tok for tok in tokens_lower):
                return {
                    "name":   target.get("name"),
                    "theme":  theme.get("theme"),
                    "status": target.get("status"),
                    "score":  target.get("score"),
                    "cap":    target.get("cap"),
                    "loc":    target.get("loc"),
                    "question": target.get("question"),
                }
    return None


# ── firm extraction ─────────────────────────────────────────────────────────────

_MY_DOMAINS_CACHE = None
def _my_domains() -> set:
    """Set of "internal" email domains used to filter internal-only meetings.
    Derived from principal.email's domain plus principal.internal_domains in
    firm_context.yaml. Always includes gmail.com so personal-Gmail principals
    don't classify themselves as external."""
    global _MY_DOMAINS_CACHE
    if _MY_DOMAINS_CACHE is not None:
        return _MY_DOMAINS_CACHE
    domains = {"gmail.com"}
    try:
        my_email = _load_my_email()
        if my_email and "@" in my_email:
            domains.add(my_email.split("@", 1)[1].lower())
    except Exception:
        pass
    _MY_DOMAINS_CACHE = domains
    return domains
_STOP_WORDS = {
    "call", "meeting", "sync", "catch", "up", "intro", "follow",
    "with", "and", "the", "for", "re", "via", "zoom", "teams", "meet",
    "discussion", "conversation", "connect", "check", "in",
}


def extract_firm_tokens(event: dict, my_email: str) -> list[str]:
    """Extract candidate firm/person names from event title and attendee domains."""
    tokens = []

    # From title: split on common separators, drop stop words
    title_parts = re.split(r"[-|/\\\(\):,@]|\s+", event["title"])
    for part in title_parts:
        part = part.strip()
        if len(part) > 3 and part.lower() not in _STOP_WORDS:
            tokens.append(part)

    # From external attendee email domains
    my_domain = my_email.split("@")[-1] if "@" in my_email else ""
    for att in event.get("attendees", []):
        dom = att["email"].split("@")[-1] if "@" in att["email"] else ""
        if dom and dom not in _my_domains() and dom != my_domain:
            # company name = domain without TLD
            company = dom.rsplit(".", 1)[0]
            if len(company) > 3:
                tokens.append(company)
        # also add display name words
        for word in re.split(r"\s+", att.get("name", "")):
            if len(word) > 3 and word.lower() not in _STOP_WORDS:
                tokens.append(word)

    # deduplicate preserving order
    seen = set()
    out = []
    for t in tokens:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            out.append(t)
    return out


def external_attendees(event: dict, my_email: str) -> list[dict]:
    """Return attendees not from my own email/domain."""
    my_domain = my_email.split("@")[-1] if "@" in my_email else ""
    return [
        a for a in event.get("attendees", [])
        if my_email not in a["email"]
        and a["email"].split("@")[-1] not in _my_domains()
        and a["email"].split("@")[-1] != my_domain
    ]


# ── Claude brief generation ─────────────────────────────────────────────────────

_SYSTEM = """\
You are an expert meeting-preparation assistant for a senior infrastructure private equity professional.
Generate a concise pre-meeting brief in the exact six-section structure below.
Be specific. Use named assets, deal sizes, and firm names from the context provided.
If the context lacks specifics, flag what would be needed to form a view.
"""

_USER_TEMPLATE = """\
MEETING: {title}
TIME: {start}
EXTERNAL ATTENDEES: {attendees}

TRANSCRIPT HISTORY (prior calls with this firm/person):
{transcript_history}

DEAL PIPELINE MATCH:
{pipeline_match}

Generate a pre-meeting brief using this exact structure:
1. THE CORE ARGUMENT — what is the strategic purpose of this meeting? What outcome matters?
2. POINTS OF CONSENSUS — what do we already agree on or expect to agree on? (from history)
3. POINTS OF DISAGREEMENT OR TENSION — where is there friction, uncertainty, or unresolved negotiation?
4. OPEN QUESTIONS AND UNRESOLVED ISSUES — what is still unclear going into this meeting?
5. WHAT YOU WOULD NEED TO FORM A VIEW — what information should you try to extract in this meeting?
6. KEY NAMES AND FIRMS — list every person and firm that is relevant, one line each.

Keep each section tight: 3-5 bullets or 2 short paragraphs max.
"""


def generate_brief(event: dict, transcripts: list[dict], pipeline_match: dict | None) -> str:
    import anthropic

    att_lines = "\n".join(
        f"  - {a['name'] or a['email']} <{a['email']}>"
        for a in event.get("attendees", [])
    ) or "  (none listed)"

    if transcripts:
        hist_lines = "\n".join(
            f"  - [{t['processed_at'][:10]}] {t['title']} ({t['category']})"
            for t in transcripts
        )
    else:
        hist_lines = "  No prior call transcripts found for this firm."

    if pipeline_match:
        pm_lines = (
            f"  Name: {pipeline_match['name']}\n"
            f"  Theme: {pipeline_match['theme']}\n"
            f"  Status: {pipeline_match['status']} | Score: {pipeline_match['score']}\n"
            f"  Cap: {pipeline_match['cap']} | Location: {pipeline_match['loc']}\n"
            f"  Key question: {pipeline_match['question']}"
        )
    else:
        pm_lines = "  No matching deal pipeline target found."

    prompt = _USER_TEMPLATE.format(
        title=event["title"],
        start=event["start"],
        attendees=att_lines,
        transcript_history=hist_lines,
        pipeline_match=pm_lines,
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── main ────────────────────────────────────────────────────────────────────────

def _load_my_email() -> str:
    """Load principal email from firm_context.yaml."""
    candidates = []
    env = os.environ.get("COS_CONFIG_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    for cand in sorted(Path.home().glob("cos-pipeline-config-*")):
        candidates.append(cand)
    candidates.append(Path.home() / "cos-pipeline-config")
    for d in candidates:
        p = d / "firm_context.yaml"
        if p.exists():
            try:
                import yaml
                data = yaml.safe_load(p.read_text()) or {}
                email = (data.get("principal") or {}).get("email", "")
                if email:
                    return email
            except Exception:
                pass
    return ""


def main():
    parser = argparse.ArgumentParser(description="Pre-meeting intelligence brief")
    parser.add_argument("--date",    default=None, help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--title",   default=None, help="Filter events by title substring")
    parser.add_argument("--dry-run", action="store_true", help="Show events only, no Claude call")
    args = parser.parse_args()

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date: {args.date}")
            sys.exit(1)
    else:
        target_date = datetime.now().date()

    my_email = _load_my_email()

    print(f"Fetching calendar for {target_date}...")
    events = fetch_events_for_date(target_date)

    if args.title:
        events = [e for e in events if args.title.lower() in e["title"].lower()]

    if not events:
        print("No events found.")
        return

    # Filter to events with at least one external attendee (or all if attendee data unavailable)
    meetings = []
    for ev in events:
        ext = external_attendees(ev, my_email)
        if ext or not ev.get("attendees"):
            meetings.append(ev)

    if not meetings:
        print(f"No external meetings found on {target_date}.")
        return

    tracker  = _load_transcript_tracker()
    pipeline = _load_pipeline()

    print(f"\nFound {len(meetings)} external meeting(s) on {target_date}\n")
    print("=" * 70)

    for ev in meetings:
        title = ev["title"]
        start = ev["start"][:16].replace("T", " ")
        print(f"\n### {title}  ({start})")

        firm_tokens = extract_firm_tokens(ev, my_email)
        transcripts = find_transcript_history(firm_tokens, tracker)
        pm_match    = find_pipeline_match(firm_tokens, pipeline)

        print(f"  Firm tokens: {firm_tokens[:6]}")
        print(f"  Transcript hits: {len(transcripts)}")
        print(f"  Pipeline match: {pm_match['name'] if pm_match else 'none'}")

        if args.dry_run:
            continue

        if not ANTHROPIC_API_KEY:
            print("[error] ANTHROPIC_API_KEY not set.")
            continue

        print("\nGenerating brief...\n")
        try:
            brief = generate_brief(ev, transcripts, pm_match)
        except Exception as e:
            print(f"[error] Claude call failed: {e}")
            continue

        print(brief)
        print("\n" + "─" * 70)


if __name__ == "__main__":
    main()
