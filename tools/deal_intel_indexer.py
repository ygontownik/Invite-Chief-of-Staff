#!/usr/bin/env python3
"""
Deal Intel Indexer

Reads captured entries from ~/dashboards/data/deals/<deal>/log.json for all
active deals and indexes them into a ChromaDB 'deal_intel' collection using
the same all-MiniLM-L6-v2 model as knowledge_indexer.py.

Only entries with captured=true are indexed (they've already been processed
by /deal-sync and represent confirmed deal intelligence).

Usage:
    deal_intel_indexer.py [--force] [--deal <deal_id>] [--dry-run]

Env:
    COS_DATA_DIR  Base path for tenant data (default: ~/dashboards/data/)
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR       = Path.home() / 'dashboards' / 'data'
DEALS_DIR      = DATA_DIR / 'deals'
INDEX_DIR      = DATA_DIR / 'knowledge_index'
SIDECAR        = Path('~/credentials/processed_deal_intel.json').expanduser()
DEAL_REGISTRY  = Path('~/cos-pipeline/tools/deal-system-data.json').expanduser()

EMBEDDING_MODEL = 'all-MiniLM-L6-v2'
COLLECTION_NAME = 'deal_intel'

INACTIVE_STAGES = {'declined', 'passed', 'closed', 'dead', 'archived'}


def load_active_deal_ids(target_deal: str | None = None) -> list[str]:
    """Return list of active deal IDs from the registry."""
    if not DEAL_REGISTRY.exists():
        return []
    registry = json.loads(DEAL_REGISTRY.read_text())
    deals = []
    for d in registry.get('deals', []):
        deal_id = d.get('deal_id', '')
        if not deal_id:
            continue
        if d.get('stage', '').lower() in INACTIVE_STAGES:
            continue
        if target_deal and deal_id != target_deal:
            continue
        deals.append(deal_id)
    return deals


def make_entry_id(deal_id: str, entry_id: str) -> str:
    """Stable ChromaDB document ID for a log entry."""
    return f'deal_intel::{deal_id}::{entry_id}'


def entry_to_text(entry: dict) -> str:
    """Flatten a log.json entry into a single embeddable text block.

    Supports two schemas:
      Spec schema:  title + summary + facts[] + actions[]
      Actual schema: who (title) + what (body)
    Both can coexist; spec fields take priority when present.
    """
    parts = []

    # Spec schema fields
    title   = entry.get('title', '')
    summary = entry.get('summary', '')

    # Actual schema fields (fallback)
    who  = entry.get('who', '')
    what = entry.get('what', '')

    if title:
        parts.append(title)
    elif who:
        parts.append(who)

    if summary:
        parts.append(summary)
    elif what and not title:
        # Only use 'what' as body when there's no spec-schema content
        parts.append(what)
    elif what and title:
        # If we have a title from spec but 'what' carries body content, include it
        parts.append(what)

    for fact in entry.get('facts', []):
        if fact:
            parts.append(f'- {fact}')
    for action in entry.get('actions', []):
        if action:
            parts.append(f'ACTION: {action}')

    return '\n'.join(parts).strip()


def is_indexable(entry: dict) -> bool:
    """Return True if this log entry should be indexed.

    Spec says: index entries where captured=True.
    Actual data: no 'captured' field exists; intel entries are identified by
    source='intel'.  Fall back to that when the captured flag is absent.
    """
    # Spec schema: explicit captured flag
    if 'captured' in entry:
        return entry.get('captured') is True

    # Actual schema: source type signals deal intel (not follow-ups / CRM matches)
    source = entry.get('source', '')
    return source == 'intel'


def main():
    global DATA_DIR, DEALS_DIR, INDEX_DIR

    parser = argparse.ArgumentParser()
    parser.add_argument('--force',   action='store_true', help='Re-index all captured entries')
    parser.add_argument('--deal',    default=None,        help='Limit to one deal ID')
    parser.add_argument('--dry-run', action='store_true', help='Print what would be indexed, no writes')
    args = parser.parse_args()

    import os
    if os.environ.get('COS_DATA_DIR'):
        DATA_DIR  = Path(os.environ['COS_DATA_DIR'])
        DEALS_DIR = DATA_DIR / 'deals'
        INDEX_DIR = DATA_DIR / 'knowledge_index'

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Load dedup sidecar
    processed: dict[str, str] = {}
    if SIDECAR.exists() and not args.force:
        try:
            processed = json.loads(SIDECAR.read_text())
        except Exception:
            processed = {}

    # Initialise ChromaDB
    if not args.dry_run:
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        embed_fn   = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
        db         = chromadb.PersistentClient(path=str(INDEX_DIR))
        collection = db.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=embed_fn,
            metadata={'hnsw:space': 'cosine'},
        )
        print(f'Collection "{COLLECTION_NAME}": {collection.count():,} documents already indexed')
    else:
        collection = None

    deal_ids = load_active_deal_ids(args.deal)
    print(f'Deals: {len(deal_ids)} active')

    total_indexed = total_skipped = total_errors = 0
    new_processed: dict[str, str] = {}

    for deal_id in deal_ids:
        log_path = DEALS_DIR / deal_id / 'log.json'
        if not log_path.exists():
            continue

        try:
            raw = json.loads(log_path.read_text())
        except Exception as e:
            print(f'[{deal_id}] log.json parse error: {e}')
            total_errors += 1
            continue

        # Unwrap dict wrapper {"entries": [...]} or accept flat list
        if isinstance(raw, dict):
            entries = raw.get('entries', [])
        else:
            entries = raw

        # Filter to indexable entries (captured=True or source='intel')
        captured = [e for e in entries if isinstance(e, dict) and is_indexable(e)]
        print(f'[{deal_id}] log.json: {len(entries)} entries, {len(captured)} indexable')

        for entry in captured:
            entry_id = entry.get('id', '')
            if not entry_id:
                print(f'  [{deal_id}] entry missing id, skipping: {str(entry)[:80]}')
                total_errors += 1
                continue

            sidecar_key = make_entry_id(deal_id, entry_id)
            if not args.force and sidecar_key in processed:
                total_skipped += 1
                continue

            text = entry_to_text(entry)
            if not text:
                print(f'  [{deal_id}] entry {entry_id!r} produced no embeddable text, skipping')
                total_errors += 1
                continue

            now_iso = datetime.now(timezone.utc).isoformat()
            # Title: prefer spec 'title', fall back to actual 'who'
            title_val = (entry.get('title') or entry.get('who') or '')[:200]
            metadata = {
                'deal_id':         deal_id,
                'entry_id':        entry_id,
                'entry_type':      entry.get('type') or entry.get('source') or 'intel',
                'entry_date':      entry.get('date', today),
                'title':           title_val,
                'indexed_at':      now_iso,
                'char_count':      len(text),
                'embedding_model': EMBEDDING_MODEL,
            }

            if args.dry_run:
                print(f'  [DRY-RUN] would index: {text[:70]}')
            else:
                try:
                    collection.upsert(
                        ids=[sidecar_key],
                        documents=[text],
                        metadatas=[metadata],
                    )
                except Exception as e:
                    print(f'  ERROR upserting {sidecar_key}: {e}')
                    total_errors += 1
                    continue

            new_processed[sidecar_key] = now_iso
            total_indexed += 1

    # Persist sidecar
    if not args.dry_run and new_processed:
        merged = {**processed, **new_processed}
        SIDECAR.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
        print(f'\n✓ Sidecar updated: {len(merged):,} total entries')

    print(f'\nRun summary: {total_indexed} indexed | {total_skipped} skipped | {total_errors} errors')


if __name__ == '__main__':
    main()
