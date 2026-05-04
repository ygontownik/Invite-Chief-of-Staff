#!/usr/bin/env python3
"""
cos_market_fetch.py — Daily market intelligence fetcher.

Ingests from two sources:
  1. Gmail label (personal.intelligence.gmail_label in firm_context.yaml)
     Forward any newsletter or article link to yourself and apply this label.
  2. RSS/Atom feeds (personal.content_feeds.blogs in firm_context.yaml)

Two-pass Claude synthesis:
  Pass 1 (Haiku): strip + normalize each item to a clean text block
  Pass 2 (Sonnet): synthesize across all items through the subscriber's sector
                   lens into the exact Market Intelligence format the briefing
                   prompt expects. On Fridays adds a weekly wrap section.

Output: appended to the daily_market_update Google Doc at 6:45am, one hour
before cos_personal_briefing.py runs at 7:51am.

USAGE:
  python3 cos_market_fetch.py           # normal run
  python3 cos_market_fetch.py --dry-run # print output, don't write to doc
  python3 cos_market_fetch.py --force   # re-fetch all items
  python3 cos_market_fetch.py --weekly  # force weekly wrap even if not Friday
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

_HERE    = Path(__file__).parent
_CREDS   = Path.home() / "credentials"
_LOG_DIR = _HERE / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "market_fetch.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

GOOGLE_DOCS_URL  = "https://docs.googleapis.com/v1/documents"
GMAIL_API_URL    = "https://gmail.googleapis.com/gmail/v1/users/me"
ANTHROPIC_URL    = "https://api.anthropic.com/v1/messages"
HAIKU_MODEL      = "claude-haiku-4-5-20251001"
SONNET_MODEL     = "claude-sonnet-4-6"
DEDUP_PATH       = _CREDS / "processed_market_feeds.json"

# ── Config ────────────────────────────────────────────────────────────────────

sys.path.insert(0, str(_HERE))
import _firm_context as _fc

_CTX  = _fc.load_firm_context()
_DOCS = _fc.load_drive_docs()

ANTHROPIC_API_KEY = (
    os.environ.get("ANTHROPIC_API_KEY")
    or _fc.get_keychain_secret("anthropic_api_key", _CTX)
    or ""
)

_intel        = (_CTX.get("personal") or {}).get("intelligence", {}) or {}
SECTORS       = _intel.get("sectors") or []
GMAIL_LABEL   = _intel.get("gmail_label", "Market Intel")
BLOGS         = ((_CTX.get("personal") or {}).get("content_feeds") or {}).get("blogs") or []

# ── Dedup ─────────────────────────────────────────────────────────────────────

def _load_dedup() -> dict:
    if DEDUP_PATH.exists():
        try:
            return json.loads(DEDUP_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_dedup(d: dict) -> None:
    DEDUP_PATH.write_text(json.dumps(d, indent=2))


# ── Google auth ───────────────────────────────────────────────────────────────

def get_google_token() -> str | None:
    token_path = _CREDS / "token.json"
    if not token_path.exists():
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
        return new["access_token"]
    except Exception as e:
        log.warning(f"Google token refresh failed: {e}")
        return creds.get("token")


# ── Gmail ingestion ───────────────────────────────────────────────────────────

def _gmail_get(token: str, path: str, params: dict = None) -> dict:
    url = f"{GMAIL_API_URL}/{path}"
    if params:
        import urllib.parse
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _decode_b64(s: str) -> str:
    import base64
    return base64.urlsafe_b64decode(s + "==").decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Recursively extract plaintext body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = (payload.get("body") or {}).get("data", "")
        return _decode_b64(data) if data else ""
    if mime == "text/html":
        data = (payload.get("body") or {}).get("data", "")
        raw = _decode_b64(data) if data else ""
        return re.sub(r"<[^>]+>", " ", raw)
    for part in payload.get("parts") or []:
        text = _extract_body(part)
        if text.strip():
            return text
    return ""


def fetch_gmail_items(token: str, force: bool, dedup: dict) -> list[dict]:
    """Return new emails under the Market Intel label."""
    if not GMAIL_LABEL:
        return []

    # Resolve label ID
    try:
        labels = _gmail_get(token, "labels")
        label_id = next(
            (l["id"] for l in labels.get("labels", [])
             if l.get("name", "").lower() == GMAIL_LABEL.lower()),
            None,
        )
    except Exception as e:
        log.warning(f"Gmail labels lookup failed: {e}")
        return []

    if not label_id:
        log.info(f"Gmail label '{GMAIL_LABEL}' not found — skipping Gmail ingestion.")
        return []

    # Fetch messages from last 2 days
    after_ts = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
    try:
        result = _gmail_get(token, "messages", {
            "labelIds": label_id,
            "q": f"after:{after_ts}",
            "maxResults": 50,
        })
    except Exception as e:
        log.warning(f"Gmail messages fetch failed: {e}")
        return []

    items = []
    for msg_ref in result.get("messages") or []:
        msg_id = msg_ref["id"]
        dedup_key = f"gmail:{msg_id}"
        if not force and dedup_key in dedup:
            continue
        try:
            msg = _gmail_get(token, f"messages/{msg_id}", {"format": "full"})
        except Exception as e:
            log.warning(f"Could not fetch Gmail message {msg_id}: {e}")
            continue

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "(no subject)")
        sender  = headers.get("from", "")
        body    = _extract_body(msg.get("payload", {}))
        body    = _truncate(body, 1200)

        if body.strip():
            items.append({
                "title":  subject,
                "source": sender,
                "link":   f"gmail:{msg_id}",
                "text":   body,
            })
            dedup[dedup_key] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    log.info(f"Gmail '{GMAIL_LABEL}': {len(items)} new item(s)")
    return items


# ── RSS ingestion ─────────────────────────────────────────────────────────────

_NS = {
    "atom":    "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def _xml_text(el, *tags) -> str:
    for tag in tags:
        child = el.find(tag, _NS)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def fetch_rss_items(force: bool, dedup: dict) -> list[dict]:
    """Return new items from all configured blog/RSS feeds."""
    if not BLOGS:
        return []

    all_items = []
    for source in BLOGS:
        name = source.get("name", "Unknown")
        url  = source.get("url") or source.get("rss", "")
        if not url:
            continue

        try:
            headers = {"User-Agent": "cos-market-fetch/1.0"}
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
            root = ET.fromstring(raw)
        except Exception as e:
            log.warning(f"Could not fetch/parse {name} ({url}): {e}")
            continue

        items = []
        for item in root.findall(".//item"):
            link    = _xml_text(item, "link")
            title   = _xml_text(item, "title")
            summary = _xml_text(item, "description") or _xml_text(item, "content:encoded")
            if link and (force or link not in dedup):
                items.append({"title": title, "source": name, "link": link,
                              "text": _truncate(summary)})
                dedup[link] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for entry in root.findall("atom:entry", _NS):
            link_el = entry.find("atom:link", _NS)
            link    = (link_el.get("href") or "") if link_el is not None else ""
            title   = _xml_text(entry, "atom:title")
            summary = _xml_text(entry, "atom:summary") or _xml_text(entry, "atom:content")
            if link and (force or link not in dedup):
                items.append({"title": title, "source": name, "link": link,
                              "text": _truncate(summary)})
                dedup[link] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        log.info(f"RSS {name}: {len(items)} new item(s)")
        all_items.extend(items)

    return all_items


def _truncate(text: str, limit: int = 1000) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + "…" if len(text) > limit else text


# ── Claude calls ──────────────────────────────────────────────────────────────

def _claude(model: str, system: str, prompt: str, max_tokens: int = 1024) -> str:
    if not ANTHROPIC_API_KEY:
        return prompt  # fallback: return raw input
    body = json.dumps({
        "model":      model,
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["content"][0]["text"].strip()


def pass1_normalize(items: list[dict]) -> list[dict]:
    """Pass 1 (Haiku): strip each item to a clean, investment-relevant text block."""
    normalized = []
    for it in items:
        if not it.get("text", "").strip():
            normalized.append(it)
            continue
        prompt = (
            f"Title: {it['title']}\n"
            f"Source: {it['source']}\n\n"
            f"{it['text']}\n\n"
            f"Extract ONLY the factual, investment-relevant content. "
            f"Remove: promotional language, navigation text, ads, subscription CTAs. "
            f"Keep: named companies, assets, dollar amounts, regulatory decisions, data points. "
            f"Max 300 words. Plain text only."
        )
        try:
            clean = _claude(HAIKU_MODEL, "You are a financial research assistant. Extract facts, discard noise.", prompt, 400)
            normalized.append({**it, "text": clean})
        except Exception as e:
            log.warning(f"Pass 1 failed for '{it['title']}': {e}")
            normalized.append(it)
    return normalized


def pass2_synthesize(items: list[dict], is_friday: bool, weekly_items: list[dict] = None) -> str:
    """Pass 2 (Sonnet): synthesize across all items through the subscriber's sector lens."""
    if not items:
        return ""

    sector_str = ", ".join(SECTORS) if SECTORS else "infrastructure, energy, real estate, capital markets"

    item_blocks = "\n\n".join(
        f"[{i+1}] SOURCE: {it['source']}\nTITLE: {it['title']}\n{it['text']}"
        for i, it in enumerate(items)
    )

    weekly_block = ""
    if is_friday and weekly_items:
        weekly_block = "\n\n".join(
            f"[W{i+1}] SOURCE: {it['source']}\nTITLE: {it['title']}\n{it['text']}"
            for i, it in enumerate(weekly_items)
        )

    weekly_instruction = ""
    if is_friday:
        weekly_instruction = """

After the daily brief, add a WEEKLY WRAP section:

WEEKLY WRAP
What changed this week — 3-5 bullets connecting dots across the week's items.
Format: - **[Theme]:** [2-3 sentence synthesis, investment implication, named firms/assets]
End with: What to watch next week: [2-3 specific things]""" if weekly_items else """

After the daily brief, add: WEEKLY WRAP: Insufficient data for weekly synthesis."""

    prompt = f"""You are synthesizing market intelligence for a senior investor focused on: {sector_str}.

Today's items ({len(items)} total):
{item_blocks}
{f"This week's items for the weekly wrap:{chr(10)}{weekly_block}" if weekly_block else ""}

Write a Market Intelligence brief in EXACTLY this format (no deviations):

KEY TAKEAWAY: [One sentence thesis — the single most important thing happening in the market today for this investor. Must name a specific company, asset, or regulatory action.]

{chr(10).join(f"**{s}**" + chr(10) + "[2-4 bullets. Each bullet: named asset or firm, specific number or decision, investment implication. No vague themes.]" for s in (SECTORS or ["Markets"]))}

Sources: [comma-separated list of sources used]
{weekly_instruction}

RULES:
- Every bullet names a specific company, asset, dollar amount, or regulatory body
- No bullet starts with "The" or restates the source name
- Connect dots across sources — if two sources say the same thing, say so
- Calibrate to the investor's sectors: deprioritize items outside {sector_str}
- Output only the brief. No preamble."""

    try:
        return _claude(SONNET_MODEL, "You are a chief of staff synthesizing market intelligence for a senior investor.", prompt, 1500)
    except Exception as e:
        log.error(f"Pass 2 synthesis failed: {e}")
        return "\n".join(f"- {it['title']} ({it['source']})" for it in items)


# ── Google Docs write ─────────────────────────────────────────────────────────

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
        "requests": [{"insertText": {"location": {"index": end - 1, "segmentId": ""}, "text": text}}]
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def fetch_weekly_items(token: str) -> list[dict]:
    """Pull this week's already-processed items from the daily_market doc for the weekly wrap."""
    doc_id = _DOCS.get("daily_market_update") or _DOCS.get("daily_market", "")
    if not doc_id or not token:
        return []
    try:
        url = f"{GOOGLE_DOCS_URL}/{doc_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            doc = json.loads(r.read())
        # Extract text from doc
        parts = []
        for elem in doc.get("body", {}).get("content", []):
            if "paragraph" in elem:
                for pe in elem["paragraph"].get("elements", []):
                    if "textRun" in pe:
                        parts.append(pe["textRun"].get("content", ""))
        full_text = "".join(parts)
        # Grab last 7 days worth — split by the separator we write
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        sections = full_text.split("───────────────────────────────────────")
        recent = [s for s in sections if s.strip() and any(
            datetime.now().strftime(f"%B {d:02d}") in s
            for d in range(1, 32)
        )]
        # Return as pseudo-items for the weekly wrap prompt
        return [{"title": f"Week entry {i+1}", "source": "daily_market_doc", "text": _truncate(s, 800)}
                for i, s in enumerate(recent[-7:])]
    except Exception as e:
        log.warning(f"Could not fetch weekly items from doc: {e}")
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force",   action="store_true")
    parser.add_argument("--weekly",  action="store_true", help="Force weekly wrap")
    args = parser.parse_args()

    if not SECTORS and not BLOGS and not GMAIL_LABEL:
        log.info("No sectors, sources, or Gmail label configured — nothing to do.")
        log.info("Run setup.sh step 3c or edit firm_context.yaml :: personal.intelligence")
        return

    dedup     = {} if args.force else _load_dedup()
    is_friday = args.weekly or datetime.now().weekday() == 4

    token = get_google_token()
    if not token:
        log.error("No Google auth token — run OAuth setup first.")
        sys.exit(1)

    # Ingest
    gmail_items = fetch_gmail_items(token, args.force, dedup)
    rss_items   = fetch_rss_items(args.force, dedup)
    all_items   = gmail_items + rss_items

    if not all_items:
        log.info("No new items from any source.")
        _save_dedup(dedup)
        return

    log.info(f"Total new items: {len(all_items)} ({len(gmail_items)} Gmail, {len(rss_items)} RSS)")

    # Pass 1 — normalize
    log.info("Pass 1: normalizing items (Haiku)...")
    normalized = pass1_normalize(all_items)

    # Fetch weekly context on Fridays
    weekly_items = fetch_weekly_items(token) if is_friday else []

    # Pass 2 — synthesize
    log.info("Pass 2: synthesizing market brief (Sonnet)...")
    brief = pass2_synthesize(normalized, is_friday, weekly_items)

    if not brief:
        log.warning("Synthesis produced no output.")
        return

    date_label = datetime.now().strftime("%B %d, %Y")
    time_label = datetime.now().strftime("%H:%M")
    entry = (
        f"\n\n───────────────────────────────────────\n"
        f"{date_label} · {time_label}\n\n"
        f"{brief}\n"
    )

    if args.dry_run:
        print("\n── DRY RUN OUTPUT ──")
        print(entry)
        print("── (not written to doc) ──")
        return

    # Write to daily_market doc
    doc_id = _DOCS.get("daily_market_update") or _DOCS.get("daily_market", "")
    if not doc_id:
        log.error("daily_market_update doc ID not in drive-docs.yaml — cannot write.")
        sys.exit(1)

    try:
        append_to_doc(token, doc_id, entry)
        log.info(f"Written to daily_market doc. {'(includes weekly wrap)' if is_friday else ''}")
    except Exception as e:
        log.error(f"Failed to write to doc: {e}")
        sys.exit(1)

    _save_dedup(dedup)

    # Trigger dashboard warmup
    try:
        port = os.environ.get("COS_DASHBOARD_PORT", "7777")
        req = urllib.request.Request(
            f"http://localhost:{port}/warmup", method="POST",
            headers={"Content-Length": "0"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    main()
