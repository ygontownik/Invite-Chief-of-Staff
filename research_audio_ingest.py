#!/usr/bin/env python3
"""Ingest a third-party research-provider audio item (a Goldman Sachs Marquee
audio report, a Capstone webinar replay, an expert-call replay, etc.) as a
podcast-style episode under a PER-PROVIDER show, reusing the canonical
podcast_transcribe.process_episode path (transcribe → memo → routing-v2 intel →
show doc + Podcast Summaries → mark_processed).

Each provider is its own show (e.g. "Goldman Sachs Research", "Capstone"),
get_or_create_doc auto-creates "<show> Transcripts" on first ingest and registers
it in ~/credentials/podcast_doc_index.json. rebuild_summary_toc (patched 2026-06-11)
includes feedless manual shows, so they roll into Podcast Summaries like every
RSS show.

Discovery + audio-URL extraction (WHICH items, and the playable media URL) is done
in-session by the ingest skills via Gmail + Chrome MCP — those steps need an
authenticated browser and adapt to email/page layout, so they are not hard-coded
here. This helper owns the deterministic, idempotent half.

Usage:
  python3 research_audio_ingest.py --list-processed [--show "Capstone"]
  python3 research_audio_ingest.py --ingest --show "Capstone" \
        --url "<public audio/video URL>" \
        --title "<title>" --date 2026-06-10 --guid "capstone-<event-id>"
  python3 research_audio_ingest.py --rebuild-tocs --show "Capstone"

Run with `zsh -ic` so ASSEMBLYAI_API_KEY / ANTHROPIC_API_KEY load from ~/.zshrc.
Ingest is idempotent: a guid already in processed_podcasts.json is skipped.
"""
import argparse
import sys
from datetime import datetime, timezone

from podcast_transcribe import (
    get_services, load_json,
    process_episode, rebuild_show_toc, rebuild_summary_toc,
    DOC_INDEX_PATH, PROCESSED_PATH,
)

DEFAULT_SHOW = "Goldman Sachs Research"


def _services():
    drive, docs = get_services()
    doc_index = load_json(DOC_INDEX_PATH)
    return drive, docs, doc_index


def list_processed(show=None):
    p = load_json(PROCESSED_PATH)
    rows = [(v.get("pub_date", "")[:10], v.get("show", ""), k, v.get("title", ""))
            for k, v in p.items() if v.get("show")]
    if show:
        rows = [r for r in rows if r[1] == show]
    for d, sh, g, t in sorted(rows, reverse=True):
        print(f"{d}  [{sh}]  {g}  {t[:60]}")
    label = f"'{show}'" if show else "all provider"
    print(f"\n{len(rows)} {label} episode(s) already processed.")


def ingest(show, url, title, date_str, guid):
    p = load_json(PROCESSED_PATH)
    if guid in p:
        print(f"SKIP (already processed): {guid}  |  {title[:60]}")
        return False
    drive, docs, doc_index = _services()
    pub = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ep = {"title": title, "audio_url": url, "guid": guid, "pub_date": pub}
    print(f"INGEST [{show}]: {title[:60]}  ({date_str})")
    ok = process_episode(ep, show, drive, docs, doc_index, use_batch=False)
    print(("✅ OK: " if ok else "❌ FAIL: ") + title[:60])
    return ok


def rebuild(show):
    drive, docs, doc_index = _services()
    processed = load_json(PROCESSED_PATH)
    if show in doc_index:
        rebuild_show_toc(docs, doc_index[show], show, processed)
    if "__summary__" in doc_index:
        rebuild_summary_toc(docs, doc_index["__summary__"], processed)
    print(f"TOCs rebuilt for show '{show}'.")


def main():
    ap = argparse.ArgumentParser(description="Ingest a research-provider audio item as a per-provider podcast episode.")
    ap.add_argument("--list-processed", action="store_true", help="List already-ingested episodes (optionally for one --show).")
    ap.add_argument("--ingest", action="store_true", help="Ingest one episode (requires --show --url --title --date --guid).")
    ap.add_argument("--rebuild-tocs", action="store_true", help="Rebuild show + summary TOCs (run once after a batch).")
    ap.add_argument("--show", default=None, help='Provider show name, e.g. "Goldman Sachs Research" or "Capstone".')
    ap.add_argument("--url")
    ap.add_argument("--title")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--guid", help="Stable id, e.g. gs-marquee-<uuid> or capstone-<event-id>")
    a = ap.parse_args()

    if a.list_processed:
        list_processed(a.show)
    elif a.rebuild_tocs:
        rebuild(a.show or DEFAULT_SHOW)
    elif a.ingest:
        show = a.show or DEFAULT_SHOW
        if not all([a.url, a.title, a.date, a.guid]):
            sys.exit("--ingest requires --url --title --date --guid (and --show)")
        ingest(show, a.url, a.title, a.date, a.guid)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
