#!/usr/bin/env python3
"""Ingest a Goldman Sachs Marquee audio report as a 'Goldman Sachs Research'
podcast episode, reusing the canonical podcast_transcribe.process_episode path
(transcribe → memo → routing-v2 intel → show doc + Podcast Summaries → mark_processed).

Discovery (WHICH reports to ingest) is done in-session by the /gs-podcast-ingest
skill via the Gmail + Chrome MCP tools — that part needs an authenticated browser
and adapts to email layout, so it is intentionally NOT hard-coded here. This helper
owns the deterministic, idempotent half.

Usage:
  python3 gs_podcast_ingest.py --list-processed
  python3 gs_podcast_ingest.py --ingest \
        --url "https://d2wot7r5hbi9xl.cloudfront.net/audio/<uuid>/<name>.mp3" \
        --title "<report title>" --date 2026-06-08 --guid "gs-marquee-<report-uuid>"
  python3 gs_podcast_ingest.py --rebuild-tocs

Run with `zsh -ic` so ASSEMBLYAI_API_KEY / ANTHROPIC_API_KEY load from ~/.zshrc.
Ingest is idempotent: a guid already in processed_podcasts.json is skipped, so the
skill can be re-run safely.
"""
import argparse
import sys
from datetime import datetime, timezone

from podcast_transcribe import (
    get_services, load_json,
    process_episode, rebuild_show_toc, rebuild_summary_toc,
    DOC_INDEX_PATH, PROCESSED_PATH,
)

SHOW = "Goldman Sachs Research"


def _services():
    drive, docs = get_services()
    doc_index = load_json(DOC_INDEX_PATH)
    return drive, docs, doc_index


def list_processed():
    p = load_json(PROCESSED_PATH)
    rows = [(v.get("pub_date", "")[:10], k, v.get("title", ""))
            for k, v in p.items() if v.get("show") == SHOW]
    for d, g, t in sorted(rows, reverse=True):
        print(f"{d}  {g}  {t[:70]}")
    print(f"\n{len(rows)} '{SHOW}' episode(s) already processed.")


def ingest(url, title, date_str, guid):
    p = load_json(PROCESSED_PATH)
    if guid in p:
        print(f"SKIP (already processed): {guid}  |  {title[:60]}")
        return False
    drive, docs, doc_index = _services()
    pub = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ep = {"title": title, "audio_url": url, "guid": guid, "pub_date": pub}
    print(f"INGEST: {title[:60]}  ({date_str})")
    ok = process_episode(ep, SHOW, drive, docs, doc_index, use_batch=False)
    print(("✅ OK: " if ok else "❌ FAIL: ") + title[:60])
    return ok


def rebuild():
    drive, docs, doc_index = _services()
    processed = load_json(PROCESSED_PATH)
    if SHOW in doc_index:
        rebuild_show_toc(docs, doc_index[SHOW], SHOW, processed)
    if "__summary__" in doc_index:
        rebuild_summary_toc(docs, doc_index["__summary__"], processed)
    print("TOCs rebuilt.")


def main():
    ap = argparse.ArgumentParser(description="Ingest a GS Marquee audio report as a podcast episode.")
    ap.add_argument("--list-processed", action="store_true", help="List already-ingested GS episodes (dedup reference).")
    ap.add_argument("--ingest", action="store_true", help="Ingest one episode (requires --url --title --date --guid).")
    ap.add_argument("--rebuild-tocs", action="store_true", help="Rebuild show + summary TOCs (run once after a batch of ingests).")
    ap.add_argument("--url")
    ap.add_argument("--title")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--guid", help="Stable id, e.g. gs-marquee-<report-uuid>")
    a = ap.parse_args()

    if a.list_processed:
        list_processed()
    elif a.rebuild_tocs:
        rebuild()
    elif a.ingest:
        if not all([a.url, a.title, a.date, a.guid]):
            sys.exit("--ingest requires --url --title --date --guid")
        ingest(a.url, a.title, a.date, a.guid)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
