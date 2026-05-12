#!/usr/bin/env python3
"""
Intelligence Knowledge Base Indexer

Reads all articles from the ~41 Google Docs in the NotebookLM source folder,
embeds them locally with sentence-transformers, and stores them in a ChromaDB
persistent index. No Anthropic API calls — all embedding runs on-device.

Usage:
    python3 knowledge_indexer.py [--force] [--source <tracker_key>] [--dry-run]

Env:
    COS_DATA_DIR  Base path for tenant data (default: ~/dashboards/data/)
    GOOGLE_TOKEN  Path to OAuth token (default: ~/credentials/token.json)
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── reuse generate_briefing.py infrastructure ────────────────────────────────
_BRIEF_DIR = Path('~/dashboards/routines/brief').expanduser()
sys.path.insert(0, str(_BRIEF_DIR))
from generate_briefing import (
    get_services,
    list_folder_docs,
    FOLDER_ID,
    EXCLUDE_DOC_NAMES,
    DRIVE_TO_TRACKER,
    MAX_ARTICLE_CHARS,
    HEADING_RANK,
)

# ── paths ─────────────────────────────────────────────────────────────────────
DATA_DIR   = Path(Path.home(), 'dashboards', 'data')  # overridden by COS_DATA_DIR
INDEX_DIR  = DATA_DIR / 'knowledge_index'
SIDECAR    = Path('~/credentials/processed_knowledge_articles.json').expanduser()
DEALS_DIR  = DATA_DIR / 'deals'
DEAL_REGISTRY = Path('~/cos-pipeline/tools/deal-system-data.json').expanduser()

EMBEDDING_MODEL = 'all-MiniLM-L6-v2'
COLLECTION_NAME = 'intelligence'

# ── sector tag map ────────────────────────────────────────────────────────────
SECTOR_TAG_MAP = [
    (['GS — Power', 'Jefferies — Power', 'RBN Energy', 'Texas Energy',
      'Distributed Grid', 'Electron Economics', 'Alex Lanin', 'Alex Epstein',
      'FVR', 'Energy Pipeline', 'Avanza', 'Prashant'],        ['power', 'utilities']),
    (['Jefferies — Midstream', 'RBN Energy', 'FVR', 'Energy Pipeline'],
                                                               ['midstream']),
    (['GS — Energy', 'Jefferies — Clean Energy', 'Alex Epstein'],
                                                               ['energy']),
    (['GS — Macro Market', 'Jefferies — Macro Market', 'GS — General',
      'Jefferies — General', 'Chamath', 'Bank Street', 'Foreign Office',
      'Free Press', 'Alan Dershowitz', 'Capstone DC'],        ['macro']),
    (['Data Center Fervor', 'Global Data Center Hub', 'Dirt to Data',
      'Infrastructure Research', 'a16z', 'AI Grid Insider',
      'GS — Technology', 'GS — Telecom'],                     ['digital_infra', 'data_centers']),
    (['Podcast Summaries'],                                    ['podcast', 'mixed']),
]

# ── date extraction ───────────────────────────────────────────────────────────
_DATE_RE = [
    re.compile(r'(\d{4}-\d{2}-\d{2})'),
    re.compile(r'(\d{1,2}/\d{1,2}/\d{2,4})'),
    re.compile(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4})', re.I),
]

def extract_article_date(title: str, fallback: str) -> str:
    for pat in _DATE_RE:
        m = pat.search(title)
        if m:
            raw = m.group(1)
            for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y',
                        '%B %d, %Y', '%B %d %Y', '%b %d, %Y', '%b %d %Y'):
                try:
                    return datetime.strptime(raw, fmt).strftime('%Y-%m-%d')
                except ValueError:
                    continue
    return fallback


def assign_sector_tags(source: str) -> list[str]:
    tags: set[str] = set()
    for prefixes, assigned in SECTOR_TAG_MAP:
        if any(source.startswith(p) for p in prefixes):
            tags.update(assigned)
    return sorted(tags) if tags else ['unclassified']


def make_chunk_id(doc_id: str, article_title: str, chunk_index: int = 0) -> str:
    short = doc_id[:8]
    h = hashlib.md5(article_title.encode()).hexdigest()[:8]
    return f'{short}_{h}_{chunk_index:02d}'


# ── full-doc extraction (all articles, no title filter) ───────────────────────
def extract_all_articles_from_doc(docs_svc, doc_id: str) -> list[dict]:
    """
    Extract every heading-level section from a Google Doc as a separate article.
    Mirrors extract_articles_from_doc() but returns all headings rather than
    filtering to a target list.
    """
    try:
        doc     = docs_svc.documents().get(documentId=doc_id).execute()
    except Exception as e:
        raise RuntimeError(f'Drive API error: {e}') from e

    content = doc.get('body', {}).get('content', [])
    paras   = []
    for el in content:
        para = el.get('paragraph')
        if not para:
            continue
        style = para.get('paragraphStyle', {}).get('namedStyleType', 'NORMAL_TEXT')
        text  = ''.join(
            r.get('textRun', {}).get('content', '')
            for r in para.get('elements', [])
        ).strip()
        paras.append((style, text))

    articles = []
    i = 0
    while i < len(paras):
        style, text = paras[i]
        rank = HEADING_RANK.get(style, 99)
        if rank <= 3 and text:
            body_lines, j, chars = [], i + 1, 0
            while j < len(paras):
                ns, nt = paras[j]
                next_rank = HEADING_RANK.get(ns, 99)
                if next_rank <= rank and nt:
                    break
                if nt and chars < MAX_ARTICLE_CHARS:
                    body_lines.append(nt)
                    chars += len(nt)
                j += 1
            articles.append({
                'title':   text,
                'content': '\n\n'.join(body_lines).strip(),
            })
        i += 1
    return articles


# ── deal profile builder ──────────────────────────────────────────────────────
def load_deal_profiles() -> list[dict]:
    """
    Load active deals from deal-system-data.json, enrich with deal.md frontmatter.
    Returns list of dicts with fields used for cross-reference profile building.
    """
    if not DEAL_REGISTRY.exists():
        return []
    registry = json.loads(DEAL_REGISTRY.read_text())
    deals = registry.get('deals', [])
    # Stages excluded from cross-reference — deal is no longer being pursued
    INACTIVE_STAGES = {'declined', 'passed', 'closed', 'dead', 'archived'}

    profiles = []
    for deal in deals:
        deal_id = deal.get('deal_id')
        if not deal_id:
            continue
        if deal.get('stage', '').lower() in INACTIVE_STAGES:
            continue
        deal_md_path = DEALS_DIR / deal_id / 'deal.md'
        md_data = {}
        if deal_md_path.exists():
            raw = deal_md_path.read_text()
            m = re.match(r'^---\n(.*?)\n---', raw, re.DOTALL)
            if m:
                try:
                    md_data = yaml.safe_load(m.group(1)) or {}
                except Exception:
                    pass
        thesis_labels = [
            p.get('label', '') for p in md_data.get('thesis', [])
            if isinstance(p, dict)
        ]
        profiles.append({
            'deal_id':   deal_id,
            'name':      deal.get('name', deal_id),
            'sector':    md_data.get('sector', ''),
            'geography': md_data.get('geography', ''),
            'tagline':   md_data.get('tagline', ''),
            'thesis':    ' '.join(thesis_labels),
            'stage':     deal.get('stage', ''),
        })
    return profiles


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    global DATA_DIR, INDEX_DIR, DEALS_DIR

    parser = argparse.ArgumentParser()
    parser.add_argument('--force',   action='store_true', help='Re-index everything')
    parser.add_argument('--source',  default=None,        help='Limit to one tracker key')
    parser.add_argument('--dry-run', action='store_true', help='Print what would be indexed, no writes')
    args = parser.parse_args()

    # Allow tenant path override
    import os
    if os.environ.get('COS_DATA_DIR'):
        DATA_DIR  = Path(os.environ['COS_DATA_DIR'])
        INDEX_DIR = DATA_DIR / 'knowledge_index'
        DEALS_DIR = DATA_DIR / 'deals'

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── load dedup sidecar ──
    processed: dict[str, str] = {}
    if SIDECAR.exists() and not args.force:
        try:
            processed = json.loads(SIDECAR.read_text())
        except Exception:
            processed = {}

    # ── initialise ChromaDB + embedding function ──
    if not args.dry_run:
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        print(f'Index: {INDEX_DIR}')

        print(f'Loading embedding model: {EMBEDDING_MODEL} (first run downloads ~90MB)…')
        embed_fn  = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
        db        = chromadb.PersistentClient(path=str(INDEX_DIR))
        collection = db.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=embed_fn,
            metadata={'hnsw:space': 'cosine'},
        )
        existing_count = collection.count()
        print(f'Collection "{COLLECTION_NAME}": {existing_count:,} documents already indexed')
    else:
        collection = None

    # ── connect to Google ──
    docs_svc, drive_svc = get_services()

    # ── enumerate folder ──
    folder_docs = list_folder_docs(drive_svc)
    drive_name_to_id = {
        f['name']: f['id'] for f in folder_docs
        if f['name'] not in EXCLUDE_DOC_NAMES
    }
    print(f'Folder: {len(drive_name_to_id)} eligible docs')

    # Build reverse map: tracker_key → drive_name
    tracker_to_drive = {v: k for k, v in DRIVE_TO_TRACKER.items()}

    total_indexed = total_skipped = total_errors = 0
    new_processed: dict[str, str] = {}

    for drive_name, doc_id in sorted(drive_name_to_id.items()):
        tracker_key = DRIVE_TO_TRACKER.get(drive_name, drive_name)

        if args.source and tracker_key != args.source:
            continue

        sector_tags = assign_sector_tags(tracker_key)

        print(f'\n[{tracker_key}] reading…', end=' ', flush=True)
        try:
            articles = extract_all_articles_from_doc(docs_svc, doc_id)
        except Exception as e:
            print(f'ERROR: {e}')
            total_errors += 1
            continue

        print(f'{len(articles)} articles found')

        doc_indexed = doc_skipped = 0
        for article in articles:
            title   = article['title']
            content = article['content']
            sidecar_key = f'{doc_id}::{title}'

            if not args.force and sidecar_key in processed:
                doc_skipped += 1
                total_skipped += 1
                continue

            chunk_id   = make_chunk_id(doc_id, title)
            art_date   = extract_article_date(title, today)
            now_iso    = datetime.now(timezone.utc).isoformat()

            metadata = {
                'source':           tracker_key,
                'article_title':    title,
                'article_date':     art_date,
                'doc_id':           doc_id,
                'sector_tags':      ','.join(sector_tags),   # ChromaDB metadata is flat
                'deal_tags':        '',                        # populated by cross_reference_briefing.py
                'indexed_at':       now_iso,
                'char_count':       len(content),
                'chunk_index':      0,
                'chunks_in_article': 1,
                'embedding_model':  EMBEDDING_MODEL,
            }

            if args.dry_run:
                print(f'  [DRY-RUN] would index: {title[:70]}')
            else:
                try:
                    collection.upsert(
                        ids=[chunk_id],
                        documents=[content or title],   # fall back to title if no body
                        metadatas=[metadata],
                    )
                except Exception as e:
                    print(f'  ERROR upserting {chunk_id}: {e}')
                    total_errors += 1
                    continue

            new_processed[sidecar_key] = now_iso
            doc_indexed += 1
            total_indexed += 1

        print(f'  → {doc_indexed} indexed, {doc_skipped} already seen')

    # ── persist updated sidecar ──
    if not args.dry_run and new_processed:
        merged = {**processed, **new_processed}
        SIDECAR.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
        final_count = collection.count()
        print(f'\n✓ Sidecar updated: {len(merged):,} total entries')
        print(f'✓ Index size: {final_count:,} documents')

    print(f'\nRun summary: {total_indexed} indexed | {total_skipped} skipped | {total_errors} errors')

    # ── write/refresh deal profiles cache ──
    if not args.dry_run:
        profiles = load_deal_profiles()
        if profiles:
            profiles_path = INDEX_DIR / 'deal_profiles.json'
            profiles_path.write_text(json.dumps(profiles, indent=2, ensure_ascii=False))
            print(f'✓ Deal profiles: {len(profiles)} deals written to {profiles_path}')


if __name__ == '__main__':
    main()
