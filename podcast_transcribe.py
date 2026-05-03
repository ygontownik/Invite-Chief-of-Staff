#!/usr/bin/env python3
"""
Podcast Transcriber — Production Script
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Transcribes podcast episodes via AssemblyAI, generates structured analytical
memos via Claude, and writes to Google Docs with live-hyperlink TOCs.

DOCUMENT STRUCTURE
  ├── [Show] Transcripts  (one per show)
  │     TABLE OF CONTENTS
  │       Show Name
  │         Apr 09 2026 — Episode Title           ← hyperlink to heading below
  │                       One-sentence summary.
  │         Apr 02 2026 — Episode Title
  │                       One-sentence summary.
  │     ──────────────────────────────────────────
  │     HEADING_1: Episode Title  (Apr 09 2026)   ← anchor
  │       Structured analytical memo
  │       ── separator ──
  │       Full diarized transcript
  │     HEADING_1: Episode Title  (Apr 02 2026)
  │       ...
  │
  └── Podcast Summaries
        TABLE OF CONTENTS
          Catalyst
            Apr 09 2026 — Episode Title           ← hyperlink
                          One-sentence summary.
          Open Circuit
            ...
        ──────────────────────────────────────────
        HEADING_1: Catalyst
          HEADING_2: Episode Title  (Apr 09 2026) ← anchor
            Structured analytical memo
          HEADING_2: Episode Title  (...)
        HEADING_1: Open Circuit
          ...

MEMO SECTIONS (per episode):
  THE CORE ARGUMENT
  POINTS OF CONSENSUS
  POINTS OF DISAGREEMENT OR TENSION
  OPEN QUESTIONS AND UNRESOLVED ISSUES
  WHAT YOU WOULD NEED TO FORM A VIEW
  KEY NAMES AND FIRMS

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMANDS:
  python podcast_transcribe.py               # daily — new episodes only
  python podcast_transcribe.py --backfill    # first run — last 28 days
  python podcast_transcribe.py --list        # show feed status, no transcription
  python podcast_transcribe.py --show "Catalyst"
  python podcast_transcribe.py --url <mp3>   # one-off URL
  python podcast_transcribe.py --force       # re-process already-done episodes

SETUP (one-time):
  pip install feedparser requests anthropic \
      google-auth google-auth-oauthlib google-api-python-client
  export ASSEMBLYAI_API_KEY="..."   # add to ~/.zshrc
  export ANTHROPIC_API_KEY="..."    # add to ~/.zshrc
  # Place OAuth client JSON at ~/credentials/gdrive_credentials.json
  # (Google Cloud Console → APIs & Services → Credentials → Desktop app)
  # Delete ~/credentials/gdrive_token.pickle if it exists — re-auth needed
  # for expanded scopes: drive.file + documents
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import json
import os
import pickle
import re
import sys
import time
import traceback
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _usage import log_usage  # noqa: E402

# ── Firm context and config ────────────────────────────────────────────────────
_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import _firm_context as _fc  # noqa: E402
import _secrets  # noqa: E402
_CTX      = _fc.load_firm_context()
_FIRM_CFG = _fc.load_firm_config()

# ── Config ────────────────────────────────────────────────────────────────────

def _load_env_from_zshrc():
    """Load API keys from ~/.zshrc if not already in environment."""
    zshrc = os.path.expanduser("~/.zshrc")
    if not os.path.exists(zshrc):
        return
    with open(zshrc) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and not os.environ.get(key):
                    os.environ[key] = val

_load_env_from_zshrc()

# Resolves through keychain (Mac default) then env-var fallback per BOOTSTRAP_PLAN #2.
ASSEMBLYAI_API_KEY = _secrets.load_secret("ASSEMBLYAI_API_KEY", "")
ANTHROPIC_API_KEY  = _secrets.load_secret("ANTHROPIC_API_KEY", "")

# GDRIVE_FOLDER_ID — folder where per-show transcript Docs live.
# SUMMARY_GDRIVE_FOLDER_ID — folder where the aggregate Podcast Summaries Doc lives.
# Both load from firm_config.json with fallback to legacy hardcoded IDs.
GDRIVE_FOLDER_ID         = _FIRM_CFG.get("podcast_transcripts_folder_id", "1ARc4Xes4gZocphJhC9bk8egfXqiz_3BC")
SUMMARY_GDRIVE_FOLDER_ID = _FIRM_CFG.get("podcast_summary_folder_id",     "15cTUBvS63edtT5pM-k8LpacUOSQujSVF")
PROCESSED_PATH   = os.path.expanduser("~/credentials/processed_podcasts.json")
DOC_INDEX_PATH   = os.path.expanduser("~/credentials/podcast_doc_index.json")
CREDS_PATH       = os.path.expanduser("~/credentials/gdrive_credentials.json")
TOKEN_PATH       = os.path.expanduser("~/credentials/gdrive_token.pickle")

# Podcast feeds — priority order:
#   1. personal.content_feeds.podcasts in firm_context.yaml  (per-person)
#   2. podcast_feeds in firm_config.json                     (firm-level override)
#   3. Built-in defaults                                     (fallback)
#
# personal.content_feeds.podcasts is a list of {name, rss} dicts.
# Convert to the {name: rss_url} dict format used throughout this script.
def _load_feeds() -> dict:
    # 1. Personal feeds from firm_context.yaml
    personal_podcasts = (
        _CTX.get("personal", {})
            .get("content_feeds", {})
            .get("podcasts", [])
    )
    if personal_podcasts:
        return {p["name"]: p["rss"] for p in personal_podcasts if p.get("name") and p.get("rss")}

    # 2. Firm-level override from firm_config.json
    if _FIRM_CFG.get("podcast_feeds"):
        return _FIRM_CFG["podcast_feeds"]

    # 3. B5 (ID excision): no fallback. The legacy hardcoded RSS list
    #    (Catalyst / Open Circuit / Energy Capital / Infrastructure Investor /
    #    Energy Gang) was tenant-specific. New tenants must populate their feeds
    #    in firm_context.yaml :: personal.content_feeds.podcasts or
    #    firm_config.json :: podcast_feeds. Returning {} causes the script to
    #    have no episodes to process — caller logs a warning at the call site.
    import sys as _sys
    print(
        "[podcast_transcribe] WARNING: no podcast feeds configured. "
        "Populate firm_context.yaml :: personal.content_feeds.podcasts or "
        "firm_config.json :: podcast_feeds.",
        file=_sys.stderr,
    )
    return {}

FEEDS = _load_feeds()

SUMMARY_DOC_NAME = "Podcast Summaries"
BACKFILL_DAYS    = 28
MAX_EPISODES     = 30
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
]

# Sentinel text marking the end of the TOC block in each doc
TOC_END_MARKER = "——— END OF TABLE OF CONTENTS ———"


# ── Persistence ───────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def mark_processed(guid: str, show: str, title: str, doc_url: str,
                    one_liner: str = "", pub_date_str: str = "",
                    show_heading_id: str = "", summary_heading_id: str = ""):
    p = load_json(PROCESSED_PATH)
    p[guid] = {
        "show":               show,
        "title":              title,
        "transcribed":        datetime.now().isoformat(),
        "doc_url":            doc_url,
        "one_liner":          one_liner,
        "pub_date":           pub_date_str,
        "show_heading_id":    show_heading_id,
        "summary_heading_id": summary_heading_id,
    }
    save_json(PROCESSED_PATH, p)


# ── RSS ───────────────────────────────────────────────────────────────────────

def parse_pub_date(entry) -> datetime | None:
    for field in ("published", "updated"):
        raw = entry.get(field)
        if raw:
            try:
                return parsedate_to_datetime(raw)
            except Exception:
                pass
    return None


def get_episodes(rss_url: str, since: datetime | None = None) -> list:
    feed = feedparser.parse(rss_url)
    if feed.bozo and not feed.entries:
        print(f"  ⚠️  Feed warning: {feed.bozo_exception}")
    episodes = []
    for entry in feed.entries[:MAX_EPISODES]:
        audio_url = None
        for link in entry.get("links", []):
            if link.get("type", "").startswith("audio"):
                audio_url = link["href"]
                break
        if not audio_url:
            for enc in entry.get("enclosures", []):
                if "audio" in enc.get("type", ""):
                    audio_url = enc["href"]
                    break
        pub_date = parse_pub_date(entry)
        if pub_date and pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)
        if since and pub_date and pub_date < since:
            continue
        episodes.append({
            "title":     entry.get("title", "Unknown"),
            "published": entry.get("published", ""),
            "pub_date":  pub_date,
            "audio_url": audio_url,
            "guid":      entry.get("id", entry.get("link", "")),
        })
    return episodes


# ── AssemblyAI ────────────────────────────────────────────────────────────────

def transcribe_audio(audio_url: str) -> dict:
    if not ASSEMBLYAI_API_KEY:
        sys.exit("❌  ASSEMBLYAI_API_KEY not set.")
    headers = {"authorization": ASSEMBLYAI_API_KEY, "content-type": "application/json"}
    base    = "https://api.assemblyai.com/v2"

    print(f"    → Submitting to AssemblyAI…")
    resp = requests.post(
        f"{base}/transcript",
        json={"audio_url": audio_url, "speaker_labels": True, "speech_models": ["universal-2"]},
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    tid = resp.json()["id"]
    print(f"    → ID: {tid}")

    for attempt in range(240):
        time.sleep(10)
        r    = requests.get(f"{base}/transcript/{tid}", headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if attempt % 6 == 0:
            print(f"    → {data['status']} ({attempt * 10}s)")
        if data["status"] == "completed":
            return data
        if data["status"] == "error":
            raise RuntimeError(f"AssemblyAI error: {data.get('error')}")
    raise RuntimeError("AssemblyAI timed out after 40 min.")


# ── Claude analytical memo ────────────────────────────────────────────────────

# Split into stable (cached) + dynamic parts. The stable preamble — role,
# formatting rules, section definitions — is identical across every episode
# and is eligible for Anthropic prompt caching. When a nightly batch fires
# several episodes back-to-back, every call after the first gets the
# preamble at ~10% of normal input cost.
#
# Structurally, the transcript now goes at the END of the prompt (was
# previously in the middle). Moving it is a semantic no-op — the model
# still sees the same content — and it creates a clean cache boundary.

MEMO_PREAMBLE = """\
You are a senior infrastructure private equity analyst. You have just listened \
to a podcast episode and read the full transcript. Your job is to produce a \
structured analytical memo — the kind a managing director would read in 3-5 \
minutes before deciding whether to dig deeper.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL FORMATTING RULES — follow exactly:
- DO NOT use any markdown. No #, ##, ###, **, *, -, ___, ---, or bullet symbols.
- Use plain dashes (•) for bullet points, typed literally.
- Section headings must appear exactly as shown below — plain text, no symbols.
- No horizontal rules, no bold markers, no italics markers.
- Output plain text only — it will be inserted directly into a Google Doc.

Write the memo using EXACTLY these seven sections with these EXACT headings \
(plain text, no markdown):

ONE-SENTENCE SUMMARY
A single crisp sentence (max 25 words) capturing the episode's core point.

THE CORE ARGUMENT
One to two paragraphs. Central thesis. Lead with the so-what. \
Named assets, MW, firm names, deal sizes where they exist in the transcript.

POINTS OF CONSENSUS
Bullet points (use •). What participants clearly agreed on. Attribute by name.

POINTS OF DISAGREEMENT OR TENSION
Bullet points (use •). Pushback, hedging, conspicuous vagueness.

OPEN QUESTIONS AND UNRESOLVED ISSUES
Bullet points (use •). Explicit uncertainty, missing data, pending decisions, \
regulatory and timing dependencies.

WHAT YOU WOULD NEED TO FORM A VIEW
Bullet points (use •). Specific data, diligence questions, market checks, \
or expert conversations needed before acting as a senior infra PE investor.

KEY NAMES AND FIRMS
Every person and organization named. One line each. \
Format: Name / Firm — context.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

MEMO_DYNAMIC_TEMPLATE = """\
Podcast: {show}
Episode: {title}
Date: {date}

Full transcript:
{transcript}
"""


def clean_memo(text: str) -> str:
    """Strip any markdown that leaks through from Claude output."""
    lines = []
    for line in text.splitlines():
        # Strip heading markers (# ## ###)
        line = re.sub(r'^#{1,6}\s+', '', line)
        # Strip bold/italic markers
        line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
        line = re.sub(r'\*(.+?)\*', r'\1', line)
        line = re.sub(r'__(.+?)__', r'\1', line)
        line = re.sub(r'_(.+?)_', r'\1', line)
        # Skip horizontal rules
        if re.match(r'^[-=━*]{3,}\s*$', line.strip()):
            continue
        lines.append(line)
    return '\n'.join(lines)


def generate_memo(show: str, title: str,
                  pub_date: datetime | None,
                  transcript_text: str) -> tuple[str, str]:
    """
    Returns (full_memo, one_sentence_summary).
    Parses the ONE-SENTENCE SUMMARY section out of the memo.
    """
    if not ANTHROPIC_API_KEY:
        fallback = "(memo skipped — ANTHROPIC_API_KEY not set)"
        return fallback, "No summary available."

    date_str = pub_date.strftime("%Y-%m-%d") if pub_date else "unknown"
    dynamic  = MEMO_DYNAMIC_TEMPLATE.format(
        show=show, title=title, date=date_str,
        transcript=transcript_text[:40000],
    )

    headers = {
        "x-api-key":           ANTHROPIC_API_KEY,
        "anthropic-version":   "2023-06-01",
        "anthropic-beta":      "prompt-caching-2024-07-31",
        "content-type":        "application/json",
    }
    body = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": 2048,
        "messages":   [{
            "role": "user",
            "content": [
                {"type": "text", "text": MEMO_PREAMBLE,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": dynamic},
            ],
        }],
    }

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=body, timeout=90,
        )
        r.raise_for_status()
        resp_json = r.json()
        log_usage("podcast_transcribe", body["model"], resp_json)
        full_memo = clean_memo(resp_json["content"][0]["text"].strip())
    except Exception as e:
        fallback = f"(memo generation failed: {e})"
        return fallback, "Summary unavailable."

    # Extract one-sentence summary from the memo
    one_liner = _extract_one_liner(full_memo)
    return full_memo, one_liner


def _extract_one_liner(memo: str) -> str:
    """Pull the ONE-SENTENCE SUMMARY line out of the memo text."""
    lines  = memo.splitlines()
    in_sec = False
    for line in lines:
        stripped = line.strip()
        if stripped.upper() == "ONE-SENTENCE SUMMARY":
            in_sec = True
            continue
        if in_sec:
            if stripped and not stripped.upper().startswith("THE CORE"):
                return stripped
    # Fallback: first non-empty line
    for line in lines:
        if line.strip():
            return line.strip()[:200]
    return "No summary available."


# ── Transcript formatter ──────────────────────────────────────────────────────

def format_transcript_block(data: dict, show: str, title: str,
                              pub_date: datetime | None) -> str:
    pub_str = pub_date.strftime("%Y-%m-%d") if pub_date else "unknown date"
    lines   = [
        f"Show: {show}   |   Published: {pub_str}   |   "
        f"Transcribed: {datetime.now().strftime('%Y-%m-%d %H:%M')}   |   "
        f"Duration: {round(data.get('audio_duration', 0) / 60, 1)} min",
        "",
        "FULL TRANSCRIPT",
        "─" * 40,
        "",
    ]
    utterances = data.get("utterances", [])
    if not utterances:
        lines.append(data.get("text", "(no transcript text returned)"))
    else:
        for utt in utterances:
            spk   = utt.get("speaker", "?")
            text  = utt.get("text", "").strip()
            start = utt.get("start", 0) // 1000
            ts    = f"[{start // 60}:{start % 60:02d}]"
            lines.append(f"Speaker {spk} {ts}:  {text}")
            lines.append("")
    return "\n".join(lines)


# ── Google auth ───────────────────────────────────────────────────────────────

def get_services():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_PATH):
                sys.exit(
                    f"❌  {CREDS_PATH} not found.\n"
                    "    Download OAuth 2.0 Desktop credentials from Google Cloud Console\n"
                    "    (APIs & Services → Credentials → Create → OAuth client ID → Desktop app)\n"
                    "    Save to ~/credentials/gdrive_credentials.json\n"
                    "    Then delete ~/credentials/gdrive_token.pickle if it exists."
                )
            flow  = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    drive = build("drive", "v3", credentials=creds)
    docs  = build("docs",  "v1", credentials=creds)
    return drive, docs


# ── Google Docs low-level helpers ─────────────────────────────────────────────

def create_gdoc(drive_svc, name: str, folder_id: str = GDRIVE_FOLDER_ID) -> str:
    meta = {
        "name":     name,
        "mimeType": "application/vnd.google-apps.document",
        "parents":  [folder_id],
    }
    return drive_svc.files().create(body=meta, fields="id").execute()["id"]


def get_or_create_doc(drive_svc, doc_index: dict, key: str, doc_name: str,
                      folder_id: str = GDRIVE_FOLDER_ID) -> str:
    if key in doc_index:
        return doc_index[key]
    print(f"    → Creating Google Doc: '{doc_name}'")
    doc_id = create_gdoc(drive_svc, doc_name, folder_id)
    doc_index[key] = doc_id
    save_json(DOC_INDEX_PATH, doc_index)
    return doc_id


def batch_update(docs_svc, doc_id: str, reqs: list):
    if not reqs:
        return
    for attempt in range(5):
        try:
            docs_svc.documents().batchUpdate(
                documentId=doc_id, body={"requests": reqs}
            ).execute()
            return
        except Exception as e:
            if "429" in str(e) or "RATE_LIMIT_EXCEEDED" in str(e):
                wait = 15 * (2 ** attempt)
                time.sleep(wait)
            else:
                raise
    # Final attempt — let it raise
    docs_svc.documents().batchUpdate(
        documentId=doc_id, body={"requests": reqs}
    ).execute()


def get_doc(docs_svc, doc_id: str) -> dict:
    return docs_svc.documents().get(documentId=doc_id).execute()


def get_content(docs_svc, doc_id: str) -> list:
    return get_doc(docs_svc, doc_id).get("body", {}).get("content", [])


def get_end_index(content: list) -> int:
    return max(1, content[-1].get("endIndex", 2) - 1) if content else 1


def para_text(el: dict) -> str:
    para = el.get("paragraph", {})
    return "".join(
        r.get("textRun", {}).get("content", "")
        for r in para.get("elements", [])
    ).strip()


def para_style(el: dict) -> str:
    return el.get("paragraph", {}).get("paragraphStyle", {}).get("namedStyleType", "")


def find_text_index(content: list, target: str) -> int | None:
    """Return the startIndex of the first paragraph whose text contains target."""
    for el in content:
        if target in para_text(el):
            return el.get("startIndex")
    return None


STYLE_FONT_SIZES = {
    "HEADING_1":    {"pt": 16, "bold": True},
    "HEADING_2":    {"pt": 13, "bold": True},
    "HEADING_3":    {"pt": 11, "bold": True},
    "NORMAL_TEXT":  {"pt": 10, "bold": False},
}

def apply_para_style(docs_svc, doc_id: str, content: list,
                     target_text: str, style: str, nth: str = "last",
                     min_start: int = 0, max_end: int = 0):
    matches = [el for el in content if para_text(el) == target_text]
    if min_start or max_end:
        matches = [
            el for el in matches
            if el.get("startIndex", 0) >= min_start
            and (not max_end or el.get("endIndex", 0) <= max_end)
        ]
    if not matches:
        return
    el    = matches[-1] if nth == "last" else matches[0]
    start = el.get("startIndex", 1)
    end   = el.get("endIndex",   start + len(target_text) + 1)
    batch_update(docs_svc, doc_id, [{
        "updateParagraphStyle": {
            "range":          {"startIndex": start, "endIndex": end},
            "paragraphStyle": {"namedStyleType": style},
            "fields":         "namedStyleType",
        }
    }])
    # Apply explicit font size and weight
    fmt = STYLE_FONT_SIZES.get(style)
    if fmt:
        batch_update(docs_svc, doc_id, [{
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end - 1},
                "textStyle": {
                    "fontSize":   {"magnitude": fmt["pt"], "unit": "PT"},
                    "bold":       fmt["bold"],
                    "weightedFontFamily": {"fontFamily": "Arial"},
                },
                "fields": "fontSize,bold,weightedFontFamily",
            }
        }])


# ── Bookmark & hyperlink helpers ──────────────────────────────────────────────

def make_bookmark_id(show: str, title: str, pub_date: datetime | None) -> str:
    """Create a stable, filesystem-safe bookmark ID for an episode."""
    date_str = pub_date.strftime("%Y%m%d") if pub_date else "00000000"
    raw      = f"{show}_{title}_{date_str}"
    # Keep only alphanumeric + underscore, max 100 chars
    safe     = re.sub(r"[^a-zA-Z0-9_]", "_", raw)[:100]
    return safe


def get_heading_id(docs_svc, doc_id: str, heading_text: str) -> str | None:
    """Return the headingId Google Docs assigns to a paragraph styled as a heading.
    Returns the LAST match so that re-runs always capture the most recently written entry."""
    content = get_content(docs_svc, doc_id)
    last_hid = None
    for el in content:
        if para_text(el) == heading_text:
            ps = el.get("paragraph", {}).get("paragraphStyle", {})
            hid = ps.get("headingId")
            if hid:
                last_hid = hid
    return last_hid


def insert_toc_entry_with_link(docs_svc, doc_id: str,
                                insert_index: int,
                                date_label: str,
                                title: str,
                                one_liner: str,
                                named_range_id: str):
    """
    Insert a TOC entry at insert_index:
      "MMM DD YYYY — Episode Title\n"   ← hyperlink to named range
      "               One-sentence summary.\n"
    """
    prefix    = f"{date_label} — "
    entry_line = prefix + title + "\n"
    summary_line = "                " + one_liner + "\n"

    # Insert both lines as plain text first
    batch_update(docs_svc, doc_id, [{
        "insertText": {
            "location": {"index": insert_index},
            "text":     entry_line + summary_line,
        }
    }])

    # Apply hyperlink to "date — title" portion only
    link_start = insert_index
    link_end   = insert_index + len(entry_line) - 1  # exclude newline

    # headingId is assigned by Google Docs when the paragraph is styled as a heading
    batch_update(docs_svc, doc_id, [{
        "updateTextStyle": {
            "range": {"startIndex": link_start, "endIndex": link_end},
            "textStyle": {
                "link":          {"headingId": named_range_id},
                "foregroundColor": {
                    "color": {"rgbColor": {"red": 0.067, "green": 0.396, "blue": 0.753}}
                },
                "underline": True,
            },
            "fields": "link,foregroundColor,underline",
        }
    }])


def style_summary_line_italic(docs_svc, doc_id: str,
                               insert_index: int, entry_line: str, summary_line: str,
                               font_size: int = 10):
    """Make the one-liner summary line italic and grey."""
    summary_start = insert_index + len(entry_line)
    summary_end   = summary_start + len(summary_line) - 1
    batch_update(docs_svc, doc_id, [{
        "updateTextStyle": {
            "range": {"startIndex": summary_start, "endIndex": summary_end},
            "textStyle": {
                "italic": True,
                "fontSize": {"magnitude": font_size, "unit": "PT"},
                "foregroundColor": {
                    "color": {"rgbColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}
                },
            },
            "fields": "italic,fontSize,foregroundColor",
        }
    }])


# ── TOC rebuild (runs after all episodes processed) ───────────────────────────

def _clear_toc_block(docs_svc, doc_id: str):
    """Delete the existing TOC block (from index 1 through end of TOC_END_MARKER line)."""
    content = get_content(docs_svc, doc_id)
    toc_end_idx = None
    for el in content:
        if TOC_END_MARKER in para_text(el):
            toc_end_idx = el.get("endIndex")
            break
    if toc_end_idx and toc_end_idx > 1:
        batch_update(docs_svc, doc_id, [{
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": toc_end_idx}
            }
        }])


def _write_toc_block(docs_svc, doc_id: str, toc_text: str):
    """Insert a fresh TOC block at the top of the doc and style it."""
    full = "TABLE OF CONTENTS\n" + toc_text + TOC_END_MARKER + "\n\n"
    batch_update(docs_svc, doc_id, [
        {"insertText": {"location": {"index": 1}, "text": full}}
    ])
    # Style the header
    content = get_content(docs_svc, doc_id)
    apply_para_style(docs_svc, doc_id, content, "TABLE OF CONTENTS", "HEADING_1", nth="first")
    # Set TOC body text to 8pt Arial (everything between header and end marker)
    toc_end_idx = None
    content2 = get_content(docs_svc, doc_id)
    for el in content2:
        if TOC_END_MARKER in para_text(el):
            toc_end_idx = el.get("startIndex")
            break
    # Find where TOC body starts (right after TABLE OF CONTENTS heading)
    toc_header_end = None
    for el in content2:
        if para_text(el) == "TABLE OF CONTENTS":
            toc_header_end = el.get("endIndex")
            break
    if toc_header_end and toc_end_idx and toc_end_idx > toc_header_end:
        batch_update(docs_svc, doc_id, [{
            "updateTextStyle": {
                "range": {"startIndex": toc_header_end, "endIndex": toc_end_idx},
                "textStyle": {
                    "fontSize": {"magnitude": 8, "unit": "PT"},
                    "bold": False,
                    "weightedFontFamily": {"fontFamily": "Arial"},
                },
                "fields": "fontSize,bold,weightedFontFamily",
            }
        }])


def _valid_heading_ids(content: list) -> set:
    """Return the set of headingIds that actually exist in this document."""
    ids = set()
    for el in content:
        hid = el.get("paragraph", {}).get("paragraphStyle", {}).get("headingId")
        if hid:
            ids.add(hid)
    return ids


def _apply_toc_links(docs_svc, doc_id: str,
                     entries: list[tuple[str, str, str]]):
    """
    Apply hyperlinks to TOC entry lines.
    entries: list of (entry_text_without_newline, one_liner, heading_id)
    """
    content = get_content(docs_svc, doc_id)
    valid_ids = _valid_heading_ids(content)
    for entry_text, one_liner, heading_id in entries:
        if not heading_id or heading_id not in valid_ids:
            continue
        for el in content:
            t = para_text(el)
            if entry_text[:40] in t and t.startswith(entry_text[:20]):
                start = el.get("startIndex", 1)
                end   = el.get("endIndex", start + len(entry_text)) - 1
                batch_update(docs_svc, doc_id, [{
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "textStyle": {
                            "link": {"headingId": heading_id},
                            "foregroundColor": {
                                "color": {"rgbColor": {"red": 0.067, "green": 0.396, "blue": 0.753}}
                            },
                            "underline": True,
                            "fontSize": {"magnitude": 8, "unit": "PT"},
                        },
                        "fields": "link,foregroundColor,underline,fontSize",
                    }
                }])
                break


def rebuild_show_toc(docs_svc, doc_id: str, show_name: str, processed: dict):
    """
    Rebuild the TOC for a per-show transcript doc.
    Groups episodes reverse-chronologically with clickable titles and one-liner.
    """
    # Collect episodes for this show from processed metadata, sorted newest first
    eps = [v for v in processed.values()
           if v.get("show") == show_name and v.get("show_heading_id")]
    eps.sort(key=lambda x: x.get("pub_date", ""), reverse=True)

    toc_lines = []
    link_entries = []
    for ep in eps:
        pub = ep.get("pub_date", "")[:10]
        try:
            dt = datetime.fromisoformat(pub)
            date_label = dt.strftime("%b %d %Y")
        except Exception:
            date_label = pub
        title     = ep["title"]
        one_liner = ep.get("one_liner", "")
        hid       = ep.get("show_heading_id", "")
        entry     = f"{date_label} — {title}"
        toc_lines.append(entry)
        if one_liner:
            indent = " " * 4
            toc_lines.append(f"{indent}{one_liner}")
        toc_lines.append("")
        link_entries.append((entry, one_liner, hid))

    if not toc_lines:
        return

    _clear_toc_block(docs_svc, doc_id)
    _write_toc_block(docs_svc, doc_id, "\n".join(toc_lines) + "\n")
    _apply_toc_links(docs_svc, doc_id, link_entries)
    print(f"    → TOC rebuilt: {len(eps)} episode(s)")


def rebuild_summary_toc(docs_svc, doc_id: str, processed: dict):
    """
    Rebuild the consolidated TOC for the Podcast Summaries doc.
    Shows ordered by most recent episode date (newest show first).
    Episodes within each show: reverse-chrono.
    Small font, clickable links, one-liner per episode.
    """
    toc_lines    = []
    link_entries = []

    # Order shows by their most recent episode date, newest first
    def show_latest(show_name):
        eps = [v for v in processed.values() if v.get("show") == show_name]
        if not eps:
            return ""
        return max(v.get("pub_date", "") for v in eps)

    ordered_shows = sorted(FEEDS.keys(), key=show_latest, reverse=True)

    for show_name in ordered_shows:
        eps = [v for v in processed.values()
               if v.get("show") == show_name and v.get("summary_heading_id")]
        if not eps:
            continue
        eps.sort(key=lambda x: x.get("pub_date", ""), reverse=True)

        toc_lines.append(f"{show_name}")  # show subheading
        for ep in eps:
            pub = ep.get("pub_date", "")[:10]
            try:
                dt = datetime.fromisoformat(pub)
                date_label = dt.strftime("%b %d %Y")
            except Exception:
                date_label = pub
            title     = ep["title"]
            one_liner = ep.get("one_liner", "")
            hid       = ep.get("summary_heading_id", "")
            indent    = "    "
            entry     = f"{indent}{date_label} — {title}"
            toc_lines.append(entry)
            if one_liner:
                toc_lines.append(f"{indent}    {one_liner}")
            link_entries.append((entry.strip(), one_liner, hid))
        toc_lines.append("")  # blank line between shows

    if not toc_lines:
        return

    _clear_toc_block(docs_svc, doc_id)
    _write_toc_block(docs_svc, doc_id, "\n".join(toc_lines) + "\n")

    # Bold the show subheadings
    content = get_content(docs_svc, doc_id)
    for show_name in ordered_shows:
        for el in content:
            if para_text(el) == show_name:
                start = el.get("startIndex", 1)
                end   = el.get("endIndex", start + len(show_name)) - 1
                batch_update(docs_svc, doc_id, [{
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "textStyle": {
                            "bold": True,
                            "fontSize": {"magnitude": 9, "unit": "PT"},
                        },
                        "fields": "bold,fontSize",
                    }
                }])
                break

    _apply_toc_links(docs_svc, doc_id, link_entries)
    print(f"    → Summary TOC rebuilt: {sum(1 for v in processed.values() if v.get('summary_heading_id'))} episode(s)")




def apply_normal_text_style(docs_svc, doc_id: str, start: int, end: int):
    """Apply 10pt Arial NORMAL_TEXT style to a range of body text."""
    if end <= start:
        return
    batch_update(docs_svc, doc_id, [
        {
            "updateParagraphStyle": {
                "range":          {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields":         "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {
                    "fontSize":   {"magnitude": 10, "unit": "PT"},
                    "bold":       False,
                    "italic":     False,
                    "weightedFontFamily": {"fontFamily": "Arial"},
                },
                "fields": "fontSize,bold,italic,weightedFontFamily",
            }
        }
    ])


def get_toc_end_marker_index(content: list) -> int | None:
    """Return the startIndex of the TOC_END_MARKER paragraph."""
    for el in content:
        if TOC_END_MARKER in para_text(el):
            return el.get("startIndex")
    return None


# ── Per-show transcript doc ───────────────────────────────────────────────────

def prepend_episode_to_show_doc(docs_svc, doc_id: str,
                                 show: str, title: str,
                                 pub_date: datetime | None,
                                 memo: str,
                                 one_liner: str,
                                 transcript_block: str) -> str:
    """
    Prepend episode (memo + transcript) after the TOC block.
    Returns the bookmark_id used for TOC hyperlinking.
    """
    pub_str     = pub_date.strftime("%b %d %Y") if pub_date else "Unknown"
    ep_heading  = f"{title}  ({pub_str})"
    bookmark_id = make_bookmark_id(show, title, pub_date)
    separator   = "\n" + "─" * 60 + "\n\n"
    section_end = "\n" + "═" * 60 + "\n\n"

    full_block = (
        f"{ep_heading}\n"
        f"{memo}\n"
        f"{separator}"
        f"{transcript_block}"
        f"{section_end}"
    )

    # Find insertion point: right after TOC_END_MARKER
    content    = get_content(docs_svc, doc_id)
    insert_idx = get_end_index(content)
    for el in content:
        if TOC_END_MARKER in para_text(el):
            insert_idx = el.get("endIndex", insert_idx)
            break

    batch_update(docs_svc, doc_id, [
        {"insertText": {"location": {"index": insert_idx}, "text": full_block}}
    ])

    # Apply NORMAL_TEXT style to the entire inserted block first (resets any inherited styles)
    block_end = insert_idx + len(full_block)
    apply_normal_text_style(docs_svc, doc_id, insert_idx, block_end)

    # Style episode heading as HEADING_1 — Google Docs assigns a headingId automatically
    content3 = get_content(docs_svc, doc_id)
    apply_para_style(docs_svc, doc_id, content3, ep_heading, "HEADING_1")

    # Retrieve the headingId for TOC hyperlinking
    heading_id = get_heading_id(docs_svc, doc_id, ep_heading) or bookmark_id

    # Style memo section headers as HEADING_3 — constrain to the newly inserted block
    # so duplicate header names in older episodes are not affected.
    block_end_approx = insert_idx + len(full_block)
    memo_headers = [
        "ONE-SENTENCE SUMMARY",
        "THE CORE ARGUMENT",
        "POINTS OF CONSENSUS",
        "POINTS OF DISAGREEMENT OR TENSION",
        "OPEN QUESTIONS AND UNRESOLVED ISSUES",
        "WHAT YOU WOULD NEED TO FORM A VIEW",
        "KEY NAMES AND FIRMS",
        "FULL TRANSCRIPT",
    ]
    for header in memo_headers:
        c = get_content(docs_svc, doc_id)
        apply_para_style(docs_svc, doc_id, c, header, "HEADING_3",
                         nth="first", min_start=insert_idx, max_end=block_end_approx)

    print(f"    → Episode prepended to show doc.")
    return heading_id


# ── Aggregated summary doc ────────────────────────────────────────────────────

def append_memo_to_summary_doc(docs_svc, doc_id: str,
                                show: str, title: str,
                                pub_date: datetime | None,
                                memo: str,
                                one_liner: str) -> str:
    """
    Append memo to the aggregated summary doc under the correct show section.
    Returns bookmark_id for TOC hyperlinking.
    """
    pub_str     = pub_date.strftime("%b %d %Y") if pub_date else "Unknown"
    ep_heading  = f"{title}  ({pub_str})"
    bookmark_id = make_bookmark_id(show + "_summary", title, pub_date)
    separator   = "\n" + "─" * 60 + "\n\n"

    content      = get_content(docs_svc, doc_id)
    show_el      = next(
        (el for el in content
         if para_text(el) == show and para_style(el) == "HEADING_1"),
        None
    )

    if show_el is None:
        # Create show section at end of doc (after TOC block)
        marker_idx = get_toc_end_marker_index(content)
        end_idx    = get_end_index(content)
        insert_pos = end_idx

        insert_text = f"\n{show}\n{ep_heading}\n{memo}\n{separator}"
        block_end = insert_pos + len(insert_text)
        batch_update(docs_svc, doc_id, [
            {"insertText": {"location": {"index": insert_pos}, "text": insert_text}}
        ])
        apply_normal_text_style(docs_svc, doc_id, insert_pos, block_end)
        content2 = get_content(docs_svc, doc_id)
        apply_para_style(docs_svc, doc_id, content2, show,       "HEADING_1")
        content3 = get_content(docs_svc, doc_id)
        apply_para_style(docs_svc, doc_id, content3, ep_heading, "HEADING_2")
    else:
        # Append new episode at end of show's section (before next HEADING_1)
        insert_pos = get_end_index(content)
        for el in content:
            s = el.get("startIndex", 0)
            if (s > show_el.get("startIndex", 0)
                    and para_style(el) == "HEADING_1"
                    and para_text(el) != show):
                insert_pos = el.get("startIndex", insert_pos)
                break

        ep_block = f"{ep_heading}\n{memo}\n{separator}"
        block_end = insert_pos + len(ep_block)
        batch_update(docs_svc, doc_id, [
            {"insertText": {"location": {"index": insert_pos}, "text": ep_block}}
        ])
        apply_normal_text_style(docs_svc, doc_id, insert_pos, block_end)
        content2 = get_content(docs_svc, doc_id)
        apply_para_style(docs_svc, doc_id, content2, ep_heading, "HEADING_2")

    # Style memo section headers as HEADING_3 — constrain to the newly inserted block
    # so duplicate header names in earlier episodes are not affected.
    memo_headers = [
        "ONE-SENTENCE SUMMARY",
        "THE CORE ARGUMENT",
        "POINTS OF CONSENSUS",
        "POINTS OF DISAGREEMENT OR TENSION",
        "OPEN QUESTIONS AND UNRESOLVED ISSUES",
        "WHAT YOU WOULD NEED TO FORM A VIEW",
        "KEY NAMES AND FIRMS",
    ]
    for header in memo_headers:
        c = get_content(docs_svc, doc_id)
        apply_para_style(docs_svc, doc_id, c, header, "HEADING_3",
                         nth="first", min_start=insert_pos, max_end=block_end)

    # Retrieve the headingId for TOC hyperlinking (assigned after HEADING_2 style applied)
    heading_id = get_heading_id(docs_svc, doc_id, ep_heading) or bookmark_id

    print(f"    → Memo appended to summary doc.")
    return heading_id


# ── Core episode processor ────────────────────────────────────────────────────

def process_episode(ep: dict, show_name: str,
                     drive_svc, docs_svc, doc_index: dict) -> bool:
    title     = ep["title"]
    audio_url = ep.get("audio_url")
    guid      = ep.get("guid", audio_url)
    pub_date  = ep.get("pub_date")

    if not audio_url:
        print(f"  ⚠️  Skipping '{title}' — no audio URL.")
        return False

    print(f"\n  ▶  {show_name}  |  {title}")
    print(f"     {ep.get('published', '')[:16]}")

    # 1. Transcribe
    try:
        aai_data = transcribe_audio(audio_url)
    except Exception as e:
        print(f"  ❌  Transcription failed: {e}")
        return False

    transcript_block = format_transcript_block(aai_data, show_name, title, pub_date)

    # 2. Generate memo + one-liner
    print(f"    → Generating analytical memo…")
    raw_text         = aai_data.get("text", "") or transcript_block
    memo, one_liner  = generate_memo(show_name, title, pub_date, raw_text)

    # 2b. Routing-v2 (Phase 2): emit envelope items from the memo.
    # Podcasts are intel sources — no my_action / awaiting_external / status_update.
    try:
        import importlib.util as _ilu
        from pathlib import Path as _P
        _p = _P(__file__).parent / "_research_envelope.py"
        _spec = _ilu.spec_from_file_location("_research_envelope", _p)
        _re_mod = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_re_mod)
        _re_mod.extract_and_route(
            title=f"{show_name} — {title}"[:100],
            markdown=memo or "",
            source_type="podcast",
            date=pub_date.strftime("%Y-%m-%d") if pub_date else "",
        )
    except Exception as _env_err:
        print(f"    [routing-v2] skipped: {_env_err}")

    # 3. Get/create docs
    show_doc_id    = get_or_create_doc(
        drive_svc, doc_index, show_name, f"{show_name} Transcripts"
    )
    summary_doc_id = get_or_create_doc(
        drive_svc, doc_index, "__summary__", SUMMARY_DOC_NAME, SUMMARY_GDRIVE_FOLDER_ID
    )

    # 4. Write episode to show doc (memo + transcript, newest first)
    try:
        show_bookmark_id = prepend_episode_to_show_doc(
            docs_svc, show_doc_id,
            show_name, title, pub_date,
            memo, one_liner, transcript_block,
        )
    except Exception as e:
        print(f"  ❌  Failed writing show doc: {e}")
        traceback.print_exc()
        return False

    # 6. Write memo to summary doc
    try:
        summary_bookmark_id = append_memo_to_summary_doc(
            docs_svc, summary_doc_id,
            show_name, title, pub_date,
            memo, one_liner,
        )
    except Exception as e:
        print(f"  ❌  Failed writing summary doc: {e}")
        traceback.print_exc()
        return False

    # 7. Mark complete — store heading IDs for TOC rebuild at end of run
    pub_date_str = pub_date.strftime("%Y-%m-%d") if pub_date else ""
    show_url = f"https://docs.google.com/document/d/{show_doc_id}/edit"
    mark_processed(guid, show_name, title, show_url,
                   one_liner=one_liner,
                   pub_date_str=pub_date_str,
                   show_heading_id=show_bookmark_id or "",
                   summary_heading_id=summary_bookmark_id or "")

    mins = round(aai_data.get("audio_duration", 0) / 60, 1)
    print(f"  ✅  {mins} min  |  ~${round(mins * 0.009, 2)} AssemblyAI cost")
    print(f"     Transcript → https://docs.google.com/document/d/{show_doc_id}/edit")
    print(f"     Summaries  → https://docs.google.com/document/d/{summary_doc_id}/edit")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Podcast transcriber → Google Docs (memos + transcripts + live TOC)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"First-run: pull last {BACKFILL_DAYS} days from all feeds")
    parser.add_argument("--list",     action="store_true",
                        help="Show feed status only — no transcription")
    parser.add_argument("--force",    action="store_true",
                        help="Re-process already-transcribed episodes")
    parser.add_argument("--show",     choices=list(FEEDS.keys()),
                        help="Limit to one show")
    parser.add_argument("--url",      help="Transcribe a single MP3 URL (bypasses dedup)")
    args = parser.parse_args()

    processed    = load_json(PROCESSED_PATH)
    doc_index    = load_json(DOC_INDEX_PATH)
    feeds_to_run = {args.show: FEEDS[args.show]} if args.show else FEEDS

    since = (
        datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
        if args.backfill else
        datetime.now(timezone.utc) - timedelta(days=2)
    )

    # ── List mode ──
    if args.list:
        for show_name, rss_url in feeds_to_run.items():
            eps = get_episodes(rss_url)
            print(f"\n📻  {show_name}")
            for i, ep in enumerate(eps):
                status = "✅" if ep["guid"] in processed else "🆕"
                print(f"  [{i+1}] {ep.get('published','')[:16]}  {status}  {ep['title'][:70]}")
        print(f"\n{len(processed)} total processed")
        print(f"\nDoc index:\n{json.dumps(doc_index, indent=2)}")
        return

    # ── One-off URL ──
    if args.url:
        drive_svc, docs_svc = get_services()
        process_episode(
            {"title": "Manual Episode", "audio_url": args.url,
             "guid": args.url, "pub_date": None},
            "Manual", drive_svc, docs_svc, doc_index,
        )
        return

    # ── Main transcription run ──
    mode = f"BACKFILL (last {BACKFILL_DAYS} days)" if args.backfill else "DAILY CHECK"
    print(f"\n{'='*60}")
    print(f"  Podcast Transcriber — {mode}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    print("\n🔑  Authenticating Google…")
    drive_svc, docs_svc = get_services()
    print("    ✅  Authenticated.")

    total_new = total_done = total_failed = 0

    for show_name, rss_url in feeds_to_run.items():
        print(f"\n{'─'*50}")
        print(f"📻  {show_name}")

        try:
            episodes = get_episodes(rss_url, since=since)
        except Exception as e:
            print(f"  ❌  Feed fetch failed: {e}")
            continue

        new_eps = [ep for ep in episodes
                   if args.force or ep["guid"] not in processed]
        skipped = len(episodes) - len(new_eps)

        if skipped:
            print(f"  ↩️   {skipped} already transcribed.")
        if not new_eps:
            print(f"  ✅  Nothing new.")
            continue

        print(f"  🆕  {len(new_eps)} new episode(s).")
        total_new += len(new_eps)

        for ep in new_eps:
            ok           = process_episode(ep, show_name, drive_svc, docs_svc, doc_index)
            total_done   += int(ok)
            total_failed += int(not ok)

    print(f"\n{'='*60}")
    print(f"  {total_done} transcribed  |  {total_failed} failed  |  {total_new} total new")
    print(f"{'='*60}\n")

    # Rebuild all TOCs only when new episodes were processed
    if total_new > 0 and doc_index:
        processed_final = load_json(PROCESSED_PATH)
        print("📋  Rebuilding TOCs…")

        # Per-show transcript docs
        for show_name in FEEDS.keys():
            show_key = show_name
            if show_key in doc_index:
                try:
                    rebuild_show_toc(docs_svc, doc_index[show_key], show_name, processed_final)
                except Exception as e:
                    print(f"  ⚠️  {show_name} TOC failed: {e}")
                time.sleep(3)

        # Consolidated summary doc — sleep to avoid rate limit after per-show TOC writes
        time.sleep(5)
        if "__summary__" in doc_index:
            try:
                rebuild_summary_toc(docs_svc, doc_index["__summary__"], processed_final)
            except Exception as e:
                print(f"  ⚠️  Summary TOC failed: {e}")

        print("    ✅  TOCs rebuilt.")

    # Trigger dashboard warmup so new episodes appear immediately
    if total_new > 0:
        try:
            import urllib.request as _ur
            _ur.urlopen(_ur.Request("http://localhost:7777/warmup", method="POST"), timeout=2)
            print("    ✅  Dashboard warmup triggered.")
        except Exception:
            pass

    if doc_index:
        print("📄  Google Docs:")
        for key, doc_id in doc_index.items():
            label = SUMMARY_DOC_NAME if key == "__summary__" else f"{key} Transcripts"
            print(f"    {label}")
            print(f"    https://docs.google.com/document/d/{doc_id}/edit")


if __name__ == "__main__":
    main()
