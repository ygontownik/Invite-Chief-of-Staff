#!/usr/bin/env python3
"""
cos_market_fetch.py — Daily market intelligence fetcher.

Three ingestion sources (all configured in firm_context.yaml):
  1. Email senders  — searches Gmail or Outlook for emails FROM configured
                      addresses. No labeling or folder rules needed.
  2. RSS/Atom feeds — personal.content_feeds.blogs entries with a url/rss key.
  3. Websites       — personal.intelligence.websites. Auto-detects RSS feed
                      from <link> tags or common paths; falls back to scraping
                      the homepage for headline text.

Two-pass Claude synthesis:
  Pass 1 (Haiku): normalize each item — strip noise, keep named assets/firms/numbers
  Pass 2 (Sonnet): synthesize through subscriber's sector lens into KEY TAKEAWAY +
                   per-sector bullets in the exact format the briefing expects.
                   On Fridays adds a WEEKLY WRAP section.

Output appended to daily_market_update Google Doc at 6:45am, one hour before
cos_personal_briefing.py runs at 7:51am.

USAGE:
  python3 cos_market_fetch.py           # normal run
  python3 cos_market_fetch.py --dry-run # print, don't write to doc
  python3 cos_market_fetch.py --force   # ignore dedup, re-fetch everything
  python3 cos_market_fetch.py --weekly  # force weekly wrap even if not Friday
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import urllib.parse
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

GOOGLE_DOCS_URL = "https://docs.googleapis.com/v1/documents"
GMAIL_API_URL   = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH_API_URL   = "https://graph.microsoft.com/v1.0/me"
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
HAIKU_MODEL     = "claude-haiku-4-5-20251001"
SONNET_MODEL    = "claude-sonnet-4-6"
DEDUP_PATH      = _CREDS / "processed_market_feeds.json"

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

_intel   = (_CTX.get("personal") or {}).get("intelligence", {}) or {}
SECTORS  = _intel.get("sectors") or []
SENDERS  = _intel.get("research_senders") or []
WEBSITES = _intel.get("websites") or []
BLOGS    = ((_CTX.get("personal") or {}).get("content_feeds") or {}).get("blogs") or []

# ── Dedup ─────────────────────────────────────────────────────────────────────

def _load_dedup() -> dict:
    try:
        return json.loads(DEDUP_PATH.read_text()) if DEDUP_PATH.exists() else {}
    except Exception:
        return {}

def _save_dedup(d: dict) -> None:
    DEDUP_PATH.write_text(json.dumps(d, indent=2))

def _mark_seen(dedup: dict, key: str) -> None:
    dedup[key] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int = 1200) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:limit] + "…") if len(text) > limit else text

def _http_get(url: str, headers: dict = None, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "cos-market-fetch/1.0",
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

# ── Google auth ───────────────────────────────────────────────────────────────

def get_google_token() -> str | None:
    token_path = _CREDS / "token.json"
    if not token_path.exists():
        return None
    with open(token_path) as f:
        creds = json.load(f)
    if not creds.get("refresh_token"):
        return creds.get("token")
    data = urllib.parse.urlencode({
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

# ── MS Graph auth ─────────────────────────────────────────────────────────────

def get_ms_token() -> str | None:
    token_path = _CREDS / "ms_token.json"
    if not token_path.exists():
        return None
    try:
        with open(token_path) as f:
            creds = json.load(f)
        if not creds.get("refresh_token"):
            return creds.get("access_token")
        data = urllib.parse.urlencode({
            "client_id":     creds.get("client_id", ""),
            "client_secret": creds.get("client_secret", ""),
            "refresh_token": creds["refresh_token"],
            "grant_type":    "refresh_token",
            "scope":         "https://graph.microsoft.com/Mail.Read offline_access",
        }).encode()
        tenant = creds.get("tenant", "common")
        req = urllib.request.Request(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data=data, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            new = json.loads(r.read())
        creds["access_token"] = new["access_token"]
        with open(token_path, "w") as f:
            json.dump(creds, f)
        return new["access_token"]
    except Exception as e:
        log.warning(f"MS Graph token refresh failed: {e}")
        return None

# ── Email ingestion ───────────────────────────────────────────────────────────

def _decode_b64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "==").decode("utf-8", errors="replace")

def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()

def _extract_gmail_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_b64((payload.get("body") or {}).get("data", ""))
    if mime == "text/html":
        return _strip_html(_decode_b64((payload.get("body") or {}).get("data", "")))
    for part in payload.get("parts") or []:
        text = _extract_gmail_body(part)
        if text.strip():
            return text
    return ""

def fetch_gmail_by_senders(google_token: str, force: bool, dedup: dict) -> list[dict]:
    if not google_token or not SENDERS:
        return []
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
    items = []
    for sender in SENDERS:
        query = f"from:{sender} after:{cutoff_ts}"
        try:
            raw = _http_get(
                f"{GMAIL_API_URL}/messages?" + urllib.parse.urlencode({"q": query, "maxResults": 20}),
                {"Authorization": f"Bearer {google_token}"},
            )
            result = json.loads(raw)
        except Exception as e:
            log.warning(f"Gmail search for {sender} failed: {e}")
            continue

        for msg_ref in result.get("messages") or []:
            msg_id = msg_ref["id"]
            key = f"gmail:{msg_id}"
            if not force and key in dedup:
                continue
            try:
                msg_raw = _http_get(
                    f"{GMAIL_API_URL}/messages/{msg_id}?format=full",
                    {"Authorization": f"Bearer {google_token}"},
                )
                msg = json.loads(msg_raw)
            except Exception as e:
                log.warning(f"Gmail fetch {msg_id} failed: {e}")
                continue
            hdrs = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            body = _truncate(_extract_gmail_body(msg.get("payload", {})))
            if body.strip():
                items.append({"title": hdrs.get("subject", "(no subject)"),
                              "source": f"email:{sender}", "link": key, "text": body})
                _mark_seen(dedup, key)

    log.info(f"Gmail senders: {len(items)} new item(s) from {len(SENDERS)} address(es)")
    return items

def fetch_outlook_by_senders(ms_token: str, force: bool, dedup: dict) -> list[dict]:
    if not ms_token or not SENDERS:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = []
    for sender in SENDERS:
        filt = (
            f"from/emailAddress/address eq '{sender}' "
            f"and receivedDateTime ge {cutoff}"
        )
        url = (f"{GRAPH_API_URL}/messages?"
               + urllib.parse.urlencode({
                   "$filter": filt,
                   "$select": "id,subject,from,receivedDateTime,body",
                   "$top": "20",
               }))
        try:
            raw = _http_get(url, {"Authorization": f"Bearer {ms_token}"})
            result = json.loads(raw)
        except Exception as e:
            log.warning(f"Outlook search for {sender} failed: {e}")
            continue

        for msg in result.get("value") or []:
            msg_id = msg["id"]
            key = f"outlook:{msg_id}"
            if not force and key in dedup:
                continue
            body_content = (msg.get("body") or {}).get("content", "")
            body = _truncate(_strip_html(body_content))
            if body.strip():
                items.append({"title": msg.get("subject", "(no subject)"),
                              "source": f"email:{sender}", "link": key, "text": body})
                _mark_seen(dedup, key)

    log.info(f"Outlook senders: {len(items)} new item(s) from {len(SENDERS)} address(es)")
    return items

# ── RSS ingestion ─────────────────────────────────────────────────────────────

_NS = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/"}

def _xml_text(el, *tags) -> str:
    for tag in tags:
        child = el.find(tag, _NS)
        if child is not None and child.text:
            return child.text.strip()
    return ""

def _parse_feed(raw: bytes, source_name: str, force: bool, dedup: dict) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log.warning(f"Feed parse error for {source_name}: {e}")
        return []
    items = []
    for item in root.findall(".//item"):
        link = _xml_text(item, "link")
        if not link or (not force and link in dedup):
            continue
        items.append({"title": _xml_text(item, "title"), "source": source_name,
                      "link": link, "text": _truncate(_xml_text(item, "description") or _xml_text(item, "content:encoded"))})
        _mark_seen(dedup, link)
    for entry in root.findall("atom:entry", _NS):
        link_el = entry.find("atom:link", _NS)
        link = (link_el.get("href") or "") if link_el is not None else ""
        if not link or (not force and link in dedup):
            continue
        items.append({"title": _xml_text(entry, "atom:title"), "source": source_name,
                      "link": link, "text": _truncate(_xml_text(entry, "atom:summary") or _xml_text(entry, "atom:content"))})
        _mark_seen(dedup, link)
    return items

def fetch_rss_feeds(force: bool, dedup: dict) -> list[dict]:
    if not BLOGS:
        return []
    items = []
    for source in BLOGS:
        name = source.get("name", "Unknown")
        url  = source.get("url") or source.get("rss", "")
        if not url:
            continue
        try:
            raw = _http_get(url)
            batch = _parse_feed(raw, name, force, dedup)
            log.info(f"RSS {name}: {len(batch)} new item(s)")
            items.extend(batch)
        except Exception as e:
            log.warning(f"RSS fetch failed for {name}: {e}")
    return items

# ── Website ingestion (RSS auto-detect → homepage scrape) ─────────────────────

_RSS_PATHS = ["/feed", "/rss", "/feed.xml", "/atom.xml", "/rss.xml", "/feed/rss", "/rss/feed"]
_RSS_MIME  = {"application/rss+xml", "application/atom+xml", "text/xml", "application/xml"}

def _find_rss_url(homepage_url: str, html: bytes) -> str | None:
    """Try to discover an RSS feed URL from the homepage HTML."""
    # 1. Look for <link rel="alternate" type="application/rss+xml" ...>
    for m in re.finditer(
        rb'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']([^"\']+)["\'][^>]+href=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    ):
        mime, href = m.group(1).decode("utf-8", errors="replace"), m.group(2).decode("utf-8", errors="replace")
        if any(t in mime for t in ["rss", "atom", "xml"]):
            return urllib.parse.urljoin(homepage_url, href)
    # Also try href before type
    for m in re.finditer(
        rb'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    ):
        href, mime = m.group(1).decode("utf-8", errors="replace"), m.group(2).decode("utf-8", errors="replace")
        if any(t in mime for t in ["rss", "atom", "xml"]):
            return urllib.parse.urljoin(homepage_url, href)
    # 2. Try common path suffixes
    parsed = urllib.parse.urlparse(homepage_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    for path in _RSS_PATHS:
        candidate = base + path
        try:
            raw = _http_get(candidate, timeout=8)
            ET.fromstring(raw)  # valid XML?
            return candidate
        except Exception:
            continue
    return None

def _scrape_homepage(url: str, site_name: str) -> str:
    """Extract readable text from a homepage for synthesis."""
    try:
        html = _http_get(url, timeout=20).decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"Homepage scrape failed for {url}: {e}")
        return ""
    # Strip scripts, styles, nav, footer
    html = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    # Extract headings and paragraphs
    chunks = []
    for m in re.finditer(r"<(h[1-4]|p)[^>]*>(.*?)</\1>", html, re.IGNORECASE | re.DOTALL):
        text = re.sub(r"<[^>]+>", " ", m.group(2))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 30:
            chunks.append(text)
    return _truncate(" | ".join(chunks[:40]), 1500)

def fetch_websites(force: bool, dedup: dict) -> list[dict]:
    if not WEBSITES:
        return []
    items = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for site_url in WEBSITES:
        site_name = urllib.parse.urlparse(site_url).netloc.replace("www.", "")
        dedup_key = f"website:{site_url}:{today}"
        if not force and dedup_key in dedup:
            log.info(f"Website {site_name}: already fetched today")
            continue
        try:
            html = _http_get(site_url, timeout=20)
        except Exception as e:
            log.warning(f"Could not fetch {site_url}: {e}")
            continue

        # Try RSS first
        rss_url = _find_rss_url(site_url, html)
        if rss_url:
            try:
                raw = _http_get(rss_url, timeout=15)
                batch = _parse_feed(raw, site_name, force, dedup)
                log.info(f"Website {site_name}: found RSS at {rss_url}, {len(batch)} new item(s)")
                items.extend(batch)
                _mark_seen(dedup, dedup_key)
                continue
            except Exception as e:
                log.warning(f"RSS at {rss_url} failed, falling back to scrape: {e}")

        # Homepage scrape fallback
        text = _scrape_homepage(site_url, site_name)
        if text.strip():
            items.append({"title": f"{site_name} — homepage ({today})",
                          "source": site_name, "link": dedup_key, "text": text})
            _mark_seen(dedup, dedup_key)
            log.info(f"Website {site_name}: scraped homepage ({len(text)} chars)")
        else:
            log.warning(f"Website {site_name}: no usable content")

    return items

# ── Claude calls ──────────────────────────────────────────────────────────────

def _claude(model: str, system: str, prompt: str, max_tokens: int = 1024) -> str:
    if not ANTHROPIC_API_KEY:
        return "\n".join(f"- {line}" for line in prompt.splitlines()[:5])
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["content"][0]["text"].strip()

def pass1_normalize(items: list[dict]) -> list[dict]:
    """Pass 1 (Haiku): strip noise, keep investment-relevant facts."""
    normalized = []
    for it in items:
        if not it.get("text", "").strip():
            normalized.append(it)
            continue
        prompt = (
            f"Title: {it['title']}\nSource: {it['source']}\n\n{it['text']}\n\n"
            f"Extract ONLY investment-relevant facts: named companies, assets, dollar amounts, "
            f"regulatory decisions, data points. Remove: ads, navigation, promotional language, "
            f"subscription prompts. Max 250 words. Plain text."
        )
        try:
            clean = _claude(HAIKU_MODEL,
                            "Financial research assistant. Extract facts, discard noise.",
                            prompt, 350)
            normalized.append({**it, "text": clean})
        except Exception as e:
            log.warning(f"Pass 1 failed for '{it['title']}': {e}")
            normalized.append(it)
    return normalized

def pass2_synthesize(items: list[dict], is_friday: bool, weekly_items: list[dict]) -> str:
    """Pass 2 (Sonnet): sector-aware synthesis into the briefing's exact format."""
    if not items:
        return ""
    sector_str = ", ".join(SECTORS) if SECTORS else "infrastructure, energy, capital markets"
    item_blocks = "\n\n".join(
        f"[{i+1}] SOURCE: {it['source']}\nTITLE: {it['title']}\n{it['text']}"
        for i, it in enumerate(items)
    )
    sector_bullets = "\n".join(
        f"**{s}**\n[2-4 bullets. Each: named asset or firm + specific number or decision + investment implication. No vague themes.]"
        for s in (SECTORS or ["Markets"])
    )
    weekly_section = ""
    if is_friday and weekly_items:
        weekly_block = "\n\n".join(
            f"[W{i+1}] {it['source']}: {it['text'][:400]}"
            for i, it in enumerate(weekly_items)
        )
        weekly_section = f"""

After the daily section, append:

WEEKLY WRAP
Synthesize this week's items into 3-5 bullets connecting dots across sources.
Format: - **[Theme]:** [2-3 sentences, investment implication, named firms/assets]
End with: What to watch next week: [2-3 specific named catalysts or events]

This week's items:
{weekly_block}"""
    elif is_friday:
        weekly_section = "\n\nAfter the daily section, append:\nWEEKLY WRAP: Insufficient data this week for synthesis."

    prompt = f"""Synthesize market intelligence for a senior investor focused on: {sector_str}.

Today's items ({len(items)} total):
{item_blocks}

Write a Market Intelligence brief in EXACTLY this format:

KEY TAKEAWAY: [One sentence — the single most important thing today. Must name a specific company, asset, or regulatory action.]

{sector_bullets}

Sources: [comma-separated list of sources used]
{weekly_section}

RULES:
- Every bullet names a specific company, asset, dollar amount, or regulatory body — no floating assertions
- Connect dots across sources — if two say the same thing, say so
- Deprioritize items outside {sector_str}
- No bullet starts with "The" or restates the source name
- Output ONLY the brief, no preamble"""

    try:
        return _claude(SONNET_MODEL,
                       "Chief of staff synthesizing market intelligence for a senior investor.",
                       prompt, 1800)
    except Exception as e:
        log.error(f"Pass 2 failed: {e}")
        return "\n".join(f"- {it['title']} ({it['source']})" for it in items)

# ── Google Docs write ─────────────────────────────────────────────────────────

def _doc_end_index(token: str, doc_id: str) -> int:
    raw = _http_get(f"{GOOGLE_DOCS_URL}/{doc_id}", {"Authorization": f"Bearer {token}"})
    content = json.loads(raw).get("body", {}).get("content", [])
    return content[-1].get("endIndex", 1) if content else 1

def _append_to_doc(token: str, doc_id: str, text: str) -> None:
    end = _doc_end_index(token, doc_id)
    url = f"{GOOGLE_DOCS_URL}/{doc_id}:batchUpdate"
    body = json.dumps({"requests": [{"insertText": {
        "location": {"index": end - 1, "segmentId": ""}, "text": text,
    }}]}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()

def _fetch_weekly_items(google_token: str) -> list[dict]:
    """Pull this week's doc entries for Friday weekly wrap."""
    doc_id = _DOCS.get("daily_market_update") or _DOCS.get("daily_market", "")
    if not doc_id or not google_token:
        return []
    try:
        raw = _http_get(f"{GOOGLE_DOCS_URL}/{doc_id}", {"Authorization": f"Bearer {google_token}"})
        doc = json.loads(raw)
        parts = []
        for elem in doc.get("body", {}).get("content", []):
            if "paragraph" in elem:
                for pe in elem["paragraph"].get("elements", []):
                    if "textRun" in pe:
                        parts.append(pe["textRun"].get("content", ""))
        sections = "".join(parts).split("───────────────────────────────────────")
        return [{"title": f"Entry {i+1}", "source": "daily_market_doc",
                 "text": _truncate(s, 800)}
                for i, s in enumerate(sections[-7:]) if s.strip()]
    except Exception as e:
        log.warning(f"Could not fetch weekly items: {e}")
        return []

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force",   action="store_true")
    parser.add_argument("--weekly",  action="store_true")
    args = parser.parse_args()

    if not SECTORS and not SENDERS and not WEBSITES and not BLOGS:
        log.info("Nothing configured — edit firm_context.yaml :: personal.intelligence")
        return

    dedup     = {} if args.force else _load_dedup()
    is_friday = args.weekly or datetime.now().weekday() == 4

    # Auth tokens
    google_token = get_google_token()
    ms_token     = get_ms_token()

    if not google_token and not ms_token:
        log.error("No Google or Microsoft auth token found — run OAuth setup first.")
        sys.exit(1)

    # Ingest all sources
    email_items   = []
    if SENDERS:
        if google_token:
            email_items.extend(fetch_gmail_by_senders(google_token, args.force, dedup))
        if ms_token:
            email_items.extend(fetch_outlook_by_senders(ms_token, args.force, dedup))

    rss_items     = fetch_rss_feeds(args.force, dedup)
    website_items = fetch_websites(args.force, dedup)
    all_items     = email_items + rss_items + website_items

    if not all_items:
        log.info("No new items from any source.")
        _save_dedup(dedup)
        return

    log.info(f"Total: {len(all_items)} new items "
             f"({len(email_items)} email, {len(rss_items)} RSS, {len(website_items)} website)")

    # Pass 1 — normalize
    log.info("Pass 1: normalizing (Haiku)...")
    normalized = pass1_normalize(all_items)

    # Weekly context on Fridays
    weekly_items = _fetch_weekly_items(google_token) if (is_friday and google_token) else []

    # Pass 2 — synthesize
    log.info("Pass 2: synthesizing (Sonnet)...")
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
        print("\n── DRY RUN ──")
        print(entry)
        print("── (not written) ──")
        return

    # Write to doc
    doc_id = _DOCS.get("daily_market_update") or _DOCS.get("daily_market", "")
    if not doc_id:
        log.error("daily_market_update doc ID not in drive-docs.yaml")
        sys.exit(1)
    if not google_token:
        log.error("Google token required to write to doc")
        sys.exit(1)

    try:
        _append_to_doc(google_token, doc_id, entry)
        log.info(f"Written to daily_market doc.{' (includes weekly wrap)' if is_friday else ''}")
    except Exception as e:
        log.error(f"Failed to write: {e}")
        sys.exit(1)

    _save_dedup(dedup)

    # Trigger dashboard warmup
    try:
        port = os.environ.get("COS_DASHBOARD_PORT", "7777")
        req = urllib.request.Request(f"http://localhost:{port}/warmup",
                                     method="POST", headers={"Content-Length": "0"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

if __name__ == "__main__":
    main()
