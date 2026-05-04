#!/usr/bin/env python3
"""
cos_market_fetch.py — Daily market intelligence fetcher.

Reads personal.content_feeds.blogs from firm_context.yaml, fetches each RSS/Atom
feed, summarizes new items with Claude, and appends a dated Market Intelligence
entry to the daily_market doc. Runs at 6:45am so the briefing at 7:51am has content.

USAGE:
  python3 cos_market_fetch.py           # normal run (skips already-seen items)
  python3 cos_market_fetch.py --dry-run # print output, don't write to doc
  python3 cos_market_fetch.py --force   # re-fetch all items regardless of dedup
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

_HERE  = Path(__file__).parent
_CREDS = Path.home() / "credentials"
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

GOOGLE_DOCS_URL = "https://docs.googleapis.com/v1/documents"

# ── Config ────────────────────────────────────────────────────────────────────

sys.path.insert(0, str(_HERE))
import _firm_context as _fc

_CTX = _fc.load_firm_context()
_DOCS = _fc.load_drive_docs()

ANTHROPIC_API_KEY = (
    os.environ.get("ANTHROPIC_API_KEY")
    or _fc.get_keychain_secret("anthropic_api_key", _CTX)
    or ""
)

_MODEL = "claude-haiku-4-5-20251001"  # summaries are cheap; use Haiku

DEDUP_PATH = _CREDS / "processed_market_feeds.json"


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
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ── RSS parsing ───────────────────────────────────────────────────────────────

_NS = {
    "atom":    "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
}


def _text(el, *tags) -> str:
    for tag in tags:
        child = el.find(tag, _NS)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def fetch_feed(url: str) -> list[dict]:
    """Return list of {title, link, published, summary} for items in the feed."""
    headers = {"User-Agent": "cos-market-fetch/1.0 (+https://github.com/ygontownik/Dashboard)"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except Exception as e:
        log.warning(f"Could not fetch {url}: {e}")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log.warning(f"Could not parse feed {url}: {e}")
        return []

    items = []

    # RSS 2.0
    for item in root.findall(".//item"):
        title   = _text(item, "title")
        link    = _text(item, "link")
        pub     = _text(item, "pubDate")
        summary = _text(item, "description") or _text(item, "content:encoded")
        if link:
            items.append({"title": title, "link": link, "published": pub, "summary": _truncate(summary)})

    # Atom 1.0
    for entry in root.findall("atom:entry", _NS):
        title   = _text(entry, "atom:title")
        link_el = entry.find("atom:link", _NS)
        link    = (link_el.get("href") or "") if link_el is not None else ""
        pub     = _text(entry, "atom:published") or _text(entry, "atom:updated")
        summary = _text(entry, "atom:summary") or _text(entry, "atom:content")
        if link:
            items.append({"title": title, "link": link, "published": pub, "summary": _truncate(summary)})

    return items


def _truncate(text: str, limit: int = 800) -> str:
    text = (text or "").strip()
    # strip basic HTML tags
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + "…" if len(text) > limit else text


# ── Claude summarization ──────────────────────────────────────────────────────

_SYSTEM = """\
You are a chief of staff for a senior infrastructure private equity professional.
Summarize market intelligence items as tight investor-grade bullets.
Each bullet: 1-2 sentences. Lead with the investment implication or named asset/firm.
Never say "this article discusses" — just state the insight directly."""


def summarize_items(source_name: str, items: list[dict]) -> str:
    """Summarize a batch of feed items into bullet points."""
    if not items:
        return ""
    if not ANTHROPIC_API_KEY:
        # Fallback: just list titles
        return "\n".join(f"- {it['title']}" for it in items)

    block = "\n\n".join(
        f"TITLE: {it['title']}\nURL: {it['link']}\nSUMMARY: {it['summary']}"
        for it in items
    )
    prompt = (
        f"Source: {source_name}\n\n"
        f"Summarize these {len(items)} new item(s) as 1-3 investor-grade bullet(s).\n"
        f"Format: - **[Topic/Asset]:** [insight, 25-50 words]\n\n"
        f"{block}"
    )

    body = json.dumps({
        "model": _MODEL,
        "max_tokens": 512,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        return resp["content"][0]["text"].strip()
    except Exception as e:
        log.warning(f"Claude call failed for {source_name}: {e}")
        return "\n".join(f"- {it['title']}" for it in items)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch market intelligence feeds")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force",   action="store_true", help="Re-process all items")
    args = parser.parse_args()

    # Load configured sources from firm_context.yaml
    blogs: list[dict] = (
        (_CTX.get("personal") or {})
        .get("content_feeds", {})
        .get("blogs", [])
        or []
    )

    if not blogs:
        log.info("No sources configured in personal.content_feeds.blogs — nothing to do.")
        log.info("Add RSS feeds to firm_context.yaml under personal.content_feeds.blogs.")
        return

    dedup = {} if args.force else _load_dedup()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff    = datetime.now(timezone.utc) - timedelta(hours=36)

    sections: list[str] = []
    done = skipped = failed = 0

    for source in blogs:
        name = source.get("name", "Unknown")
        url  = source.get("url") or source.get("rss", "")
        if not url:
            log.warning(f"Source '{name}' has no url/rss — skipping")
            continue

        log.info(f"Fetching {name} ({url})")
        items = fetch_feed(url)
        if not items:
            failed += 1
            continue

        # Filter to new items only
        new_items = []
        for it in items:
            item_key = it["link"]
            if not args.force and item_key in dedup:
                skipped += 1
                continue
            new_items.append(it)
            dedup[item_key] = today_str

        if not new_items:
            log.info(f"  {name}: no new items")
            continue

        log.info(f"  {name}: {len(new_items)} new item(s)")
        bullets = summarize_items(name, new_items)
        if bullets:
            sections.append(f"**{name}**\n{bullets}")
        done += len(new_items)

    log.info(f"Run complete: {done} new | {skipped} skipped | {failed} failed sources")

    if not sections:
        log.info("No new content to write.")
        return

    date_label  = datetime.now().strftime("%B %d, %Y")
    time_label  = datetime.now().strftime("%H:%M")
    key_takeaway = f"Market intelligence from {len(sections)} source(s) — see bullets below."

    entry = (
        f"\n\n───────────────────────────────────────\n"
        f"{date_label} · {time_label}\n"
        f"KEY TAKEAWAY: {key_takeaway}\n\n"
        + "\n\n".join(sections)
        + "\n"
    )

    if args.dry_run:
        print("\n── DRY RUN OUTPUT ──")
        print(entry)
        print("── (not written to doc) ──")
        return

    # Write to daily_market doc
    doc_id = _DOCS.get("daily_market_update") or _DOCS.get("daily_market", "")
    if not doc_id:
        log.error("daily_market_update doc ID not found in drive-docs.yaml — cannot write.")
        sys.exit(1)

    token = get_google_token()
    if not token:
        log.error("No Google auth token — run OAuth setup first.")
        sys.exit(1)

    try:
        append_to_doc(token, doc_id, entry)
        log.info(f"Appended {len(sections)} source section(s) to daily_market doc.")
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
