#!/usr/bin/env python3
"""webinar_watch.py — scheduled live-capture for website-link webinars (Capstone, …).

Third-party research webinars (Capstone via Cvent "Attendee Hub", etc.) arrive as
EMAIL invites — never on the calendar, and the join link isn't a Zoom/Teams/Meet URL
— so the calendar-driven call_scheduler never arms a recording. This watcher closes
that gap WITHOUT editing the live scheduler:

  scan Gmail for allowlisted-provider webinar invites
    → parse title + start/end + join link
    → POST a signed "meeting" to the running call_scheduler (localhost:8765/schedule)
    → call_scheduler arms call_recorder (BlackHole system-audio) at the start time

NOTE: capture is system-audio — you must JOIN and play the webinar at its time
(click Capstone's "Join Event"); the scheduler only arms the recording window.

Run on a cadence via launchd (e.g. every 3h). State (dedup): ~/credentials/processed_webinars.json

Usage:
  python3 webinar_watch.py --scan [--days 30]    # dry-run: parse + print, no side effects
  python3 webinar_watch.py --arm  [--days 30]    # scan + feed new webinars to the scheduler
  python3 webinar_watch.py --list                # show already-armed webinars
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_gmail_mini_v2 import get_gmail_service  # reuse the token.json Gmail auth

CREDS = Path.home() / "credentials"
STATE_PATH = CREDS / "processed_webinars.json"
SCHEDULER_URL = "http://127.0.0.1:8765/schedule"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEFAULT_DURATION_MIN = 90  # webinars run ~60-90 min; over-capture is harmless

# provider email domain → per-provider show (matches research_audio_ingest --show)
PROVIDERS = {
    "capstonedc.com": {"show": "Capstone", "slug": "capstone"},
}

# US timezone words → fixed UTC offset (DST-aware-ish: assume daylight for Mar-Nov).
# Capstone is DC/Eastern. Good enough for arming a recording window.
_TZ_OFFSETS = {"eastern": -4, "et": -4, "edt": -4, "est": -5,
               "central": -5, "ct": -5, "cdt": -5, "cst": -6,
               "mountain": -6, "mt": -6, "pacific": -7, "pt": -7, "pdt": -7, "pst": -8}


def load_state():
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(s):
    STATE_PATH.write_text(json.dumps(s, indent=2))


def _b64_decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _message_body(msg):
    """Flatten a Gmail message payload to text (prefer text/plain, fall back to html)."""
    def walk(part):
        out = ""
        mt = part.get("mimeType", "")
        body = part.get("body", {})
        if body.get("data"):
            out += _b64_decode(body["data"])
        for p in part.get("parts", []) or []:
            out += "\n" + walk(p)
        return out
    return walk(msg.get("payload", {}))


def _parse_dt_from_subject(subject):
    """Capstone subject: 'Capstone Event: <Title> - June 10, 2026, 1:00 PM Eastern Time'."""
    m = re.search(r"-\s*([A-Z][a-z]+ \d{1,2},? \d{4}),?\s*(\d{1,2}:\d{2}\s*[AP]M)\s*([A-Za-z ]*)?", subject)
    if not m:
        return None
    date_s, time_s, tz_s = m.group(1), m.group(2), (m.group(3) or "").strip().lower()
    try:
        dt = datetime.strptime(f"{date_s.replace(',', '')} {time_s.upper().replace(' ', '')}",
                               "%B %d %Y %I:%M%p")
    except ValueError:
        return None
    # Resolve timezone word → offset → store as naive-local? The scheduler parses ISO.
    tz_word = next((w for w in _TZ_OFFSETS if w in tz_s), "eastern")
    offset = _TZ_OFFSETS[tz_word]
    # Return an ISO string WITH offset so the scheduler's _parse_dt gets an aware dt.
    sign = "+" if offset >= 0 else "-"
    return dt.strftime(f"%Y-%m-%dT%H:%M:00{sign}{abs(offset):02d}:00")


def _clean_title(subject):
    t = re.sub(r"^\s*Capstone Event:\s*", "", subject, flags=re.I)
    t = re.sub(r"\s*-\s*[A-Z][a-z]+ \d{1,2},? \d{4}.*$", "", t)  # strip trailing date/time
    t = re.sub(r"\s*:\s*Registration Confirmed\s*$", "", t, flags=re.I)
    return t.strip()


def _find_join_url(body):
    # Cvent Attendee Hub / generic join link
    for pat in [r"https?://[^\s\"'<>]*cvent[^\s\"'<>]*",
                r"https?://[^\s\"'<>]*attendee[^\s\"'<>]*",
                r"https?://[^\s\"'<>]*(?:webinar|event|join)[^\s\"'<>]*"]:
        m = re.search(pat, body, re.I)
        if m:
            return m.group(0)
    return ""


def scan(days):
    svc = get_gmail_service()
    found = []
    for domain, meta in PROVIDERS.items():
        q = f'from:{domain} (webinar OR "join event" OR event OR registration) newer_than:{days}d'
        res = svc.users().messages().list(userId="me", q=q, maxResults=25).execute()
        for ref in res.get("messages", []):
            msg = svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            subject = headers.get("subject", "")
            if "webinar" not in subject.lower() and "event" not in subject.lower():
                continue
            start_iso = _parse_dt_from_subject(subject)
            if not start_iso:
                continue
            title = _clean_title(subject)
            body = _message_body(msg)
            join_url = _find_join_url(body)
            start_dt = datetime.fromisoformat(start_iso)
            end_iso = (start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)).isoformat()
            guid = f"{meta['slug']}-" + hashlib.sha1(f"{title}|{start_iso[:10]}".encode()).hexdigest()[:12]
            found.append({
                "id": guid, "provider_show": meta["show"],
                # Recording title carries the provider so the transcript is
                # attributable to its per-provider show (Part C routing).
                "title": f"{meta['show']} — {title}",
                "start": start_iso, "end": end_iso, "join_url": join_url,
                "has_video": True, "source": "webinar",
            })
    # de-dup within this scan by guid (multiple emails per event: invite + confirmation)
    uniq = {f["id"]: f for f in found}
    return list(uniq.values())


def feed_scheduler(meeting):
    body = json.dumps(meeting).encode()
    if not WEBHOOK_SECRET:
        return False, "WEBHOOK_SECRET not set"
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(SCHEDULER_URL, data=body,
                                 headers={"Content-Type": "application/json", "X-Signature": sig})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200, f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)


def cmd_scan(days):
    items = scan(days)
    print(f"Parsed {len(items)} webinar invite(s) (last {days}d):\n")
    for m in items:
        print(f"  [{m['provider_show']}] {m['title']}")
        print(f"      start={m['start']}  end={m['end']}")
        print(f"      join={m['join_url'][:90] or '(none found)'}")
        print(f"      guid={m['id']}\n")


def cmd_arm(days):
    state = load_state()
    items = scan(days)
    armed = 0
    now = datetime.now().astimezone()
    for m in items:
        if m["id"] in state:
            continue
        if datetime.fromisoformat(m["end"]) < now:
            state[m["id"]] = {"title": m["title"], "skipped": "past", "at": now.isoformat()}
            continue
        ok, info = feed_scheduler(m)
        print(f"  {'✅ armed' if ok else '❌ ' + info}: {m['title']}  ({m['start']})")
        if ok:
            state[m["id"]] = {"title": m["title"], "show": m["provider_show"],
                              "start": m["start"], "armed_at": now.isoformat()}
            armed += 1
    save_state(state)
    print(f"\nArmed {armed} new webinar(s).")


def cmd_list():
    state = load_state()
    for guid, v in sorted(state.items(), key=lambda kv: kv[1].get("start", ""), reverse=True):
        flag = v.get("skipped", "armed")
        print(f"  {v.get('start','?')}  [{flag}]  {v.get('title','')[:60]}")
    print(f"\n{len(state)} webinar(s) tracked.")


def main():
    ap = argparse.ArgumentParser(description="Watch Gmail for provider webinar invites and arm live recordings.")
    ap.add_argument("--scan", action="store_true", help="Dry-run: parse + print, no side effects.")
    ap.add_argument("--arm", action="store_true", help="Scan + feed new webinars to the scheduler.")
    ap.add_argument("--list", action="store_true", help="List already-armed webinars.")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    if a.scan:
        cmd_scan(a.days)
    elif a.arm:
        cmd_arm(a.days)
    elif a.list:
        cmd_list()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
