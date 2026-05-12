#!/usr/bin/env python3
"""
Intelligence Knowledge Base Query

Retrieves relevant article chunks from the local ChromaDB index for a given
natural-language query. All embedding runs on-device — no Anthropic API calls.

Output is formatted for direct paste into a Claude Code session for in-session
synthesis (Rule CC1).

Usage:
    knowledge_query.py "<query>" [--deal <deal_id>] [--source <tracker_key>]
                                  [--since YYYY-MM-DD] [--top-k 10]
                                  [--format text|json]

Examples:
    knowledge_query.py "PJM interconnection queue delays"
    knowledge_query.py "ERCOT land prices" --deal cholla --top-k 5
    knowledge_query.py "LNG FID projects" --since 2026-03-01
    knowledge_query.py "data center water" --format json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR       = Path(os.environ.get('COS_DATA_DIR', Path.home() / 'dashboards' / 'data'))
INDEX_DIR      = DATA_DIR / 'knowledge_index'
EMBEDDING_MODEL = 'all-MiniLM-L6-v2'
COLLECTION_NAME = 'intelligence'

# Module-level ChromaDB client cache — avoids duplicate PersistentClient
# instantiation on the same path (SQLite lock risk).
_chroma_clients: dict[str, object] = {}


def _get_chroma_client() -> object:
    """Return a cached PersistentClient for INDEX_DIR. Creates on first call."""
    import chromadb
    key = str(INDEX_DIR)
    if key not in _chroma_clients:
        _chroma_clients[key] = chromadb.PersistentClient(path=key)
    return _chroma_clients[key]


def _snippet(text: str, max_chars: int = 400) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(' ', 1)[0] + '…'


def query(
    query_text: str,
    deal_id: str | None = None,
    source: str | None = None,
    since: str | None = None,
    top_k: int = 10,
) -> list[dict]:
    """
    Query the knowledge base. Returns list of chunk dicts sorted by similarity.
    """
    if not INDEX_DIR.exists():
        print('ERROR: Knowledge index not found. Run knowledge_indexer.py first.',
              file=sys.stderr)
        sys.exit(1)

    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    embed_fn   = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
    db         = _get_chroma_client()
    collection = db.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embed_fn,
        metadata={'hnsw:space': 'cosine'},
    )

    if collection.count() == 0:
        print('ERROR: Index is empty. Run knowledge_indexer.py --force first.',
              file=sys.stderr)
        sys.exit(1)

    # Build where filter
    where: dict | None = None
    filters = []

    if deal_id:
        # deal_tags is a comma-separated string in metadata
        filters.append({'deal_tags': {'$contains': deal_id}})
    if source:
        filters.append({'source': {'$eq': source}})
    if since:
        filters.append({'article_date': {'$gte': since}})

    if len(filters) > 1:
        where = {'$and': filters}
    elif len(filters) == 1:
        where = filters[0]

    results = collection.query(
        query_texts=[query_text],
        n_results=min(top_k, collection.count()),
        where=where,
        include=['documents', 'metadatas', 'distances'],
    )

    chunks = []
    docs      = results['documents'][0]
    metas     = results['metadatas'][0]
    distances = results['distances'][0]

    for doc, meta, dist in zip(docs, metas, distances):
        similarity = round(1 - dist, 4)   # cosine distance → similarity
        chunks.append({
            'source':        meta.get('source', ''),
            'article_title': meta.get('article_title', ''),
            'article_date':  meta.get('article_date', ''),
            'doc_id':        meta.get('doc_id', ''),
            'sector_tags':   meta.get('sector_tags', '').split(','),
            'deal_tags':     [t for t in meta.get('deal_tags', '').split(',') if t],
            'similarity':    similarity,
            'snippet':       _snippet(doc),
            'full_content':  doc,
            'indexed_at':    meta.get('indexed_at', ''),
        })

    return sorted(chunks, key=lambda c: c['similarity'], reverse=True)


def query_deal_intel(
    query_text: str,
    deal_id: str | None = None,
    since: str | None = None,
    top_k: int = 10,
) -> list[dict]:
    """
    Query the deal_intel collection (log.json captured facts per deal).
    Returns list of chunk dicts sorted by descending similarity.
    Uses the same PersistentClient path as query() but targets the 'deal_intel' collection.
    """
    if not INDEX_DIR.exists():
        print('ERROR: Knowledge index not found. Run deal_intel_indexer.py first.',
              file=sys.stderr)
        sys.exit(1)

    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    embed_fn = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
    db = _get_chroma_client()

    try:
        collection = db.get_collection(
            name='deal_intel',
            embedding_function=embed_fn,
        )
    except Exception:
        print('ERROR: deal_intel collection not found. Run deal_intel_indexer.py first.',
              file=sys.stderr)
        sys.exit(1)

    if collection.count() == 0:
        return []

    # Build where filter
    filters = []
    if deal_id:
        filters.append({'deal_id': {'$eq': deal_id}})
    if since:
        filters.append({'entry_date': {'$gte': since}})

    where: dict | None = None
    if len(filters) > 1:
        where = {'$and': filters}
    elif len(filters) == 1:
        where = filters[0]

    results = collection.query(
        query_texts=[query_text],
        n_results=min(top_k, collection.count()),
        where=where,
        include=['documents', 'metadatas', 'distances'],
    )

    chunks = []
    for doc, meta, dist in zip(
        results['documents'][0],
        results['metadatas'][0],
        results['distances'][0],
    ):
        chunks.append({
            'deal_id':      meta.get('deal_id', ''),
            'entry_id':     meta.get('entry_id', ''),
            'entry_type':   meta.get('entry_type', ''),
            'entry_date':   meta.get('entry_date', ''),
            'title':        meta.get('title', ''),
            'similarity':   round(1 - dist, 4),
            'snippet':      _snippet(doc),
            'full_content': doc,
        })

    return sorted(chunks, key=lambda c: c['similarity'], reverse=True)


def format_text(chunks: list[dict], query_text: str) -> str:
    if not chunks:
        return f'No results found for: "{query_text}"'

    top_sim = chunks[0]['similarity']
    lines = [
        f'=== KNOWLEDGE BASE QUERY: "{query_text}" ===',
        f'Returned {len(chunks)} chunks (top similarity {top_sim:.2f})',
        '',
    ]
    for c in chunks:
        doc_id    = c.get('doc_id', '')
        source    = c.get('source') or c.get('deal_id', '')
        art_date  = c.get('article_date') or c.get('entry_date', '')
        art_title = c.get('article_title') or c.get('title', '')
        drive_url = f'https://docs.google.com/document/d/{doc_id}' if doc_id else ''
        header = f'[{source} | {art_date} | sim={c["similarity"]:.2f}]'
        lines.append(header)
        lines.append(f'Title: {art_title}')
        lines.append(c['snippet'])
        if drive_url:
            lines.append(f'Doc: {drive_url}')
        lines.append('')

    # Stale index warning
    profile_path = INDEX_DIR / 'deal_profiles.json'
    if profile_path.exists():
        mtime = datetime.fromtimestamp(profile_path.stat().st_mtime, tz=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
        if age_hours > 36:
            lines.append(f'⚠ Index last updated {age_hours:.0f}h ago — run knowledge_indexer.py')

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Query the intelligence knowledge base')
    parser.add_argument('query',          help='Natural language query')
    parser.add_argument('--deal',  '-d',  default=None, help='Filter by deal_id')
    parser.add_argument('--source', '-s', default=None, help='Filter by tracker key')
    parser.add_argument('--since',        default=None, help='Filter by article_date >= YYYY-MM-DD')
    parser.add_argument('--top-k', '-k',  type=int, default=10, help='Number of results')
    parser.add_argument('--format', '-f', choices=['text', 'json'], default='text')
    parser.add_argument('--collection', '-c', choices=['intelligence', 'deal_intel'],
                        default='intelligence',
                        help='Which collection to query (default: intelligence)')
    args = parser.parse_args()

    if args.collection == 'deal_intel':
        chunks = query_deal_intel(
            query_text=args.query,
            deal_id=args.deal,
            since=args.since,
            top_k=args.top_k,
        )
    else:
        chunks = query(
            query_text=args.query,
            deal_id=args.deal,
            source=args.source,
            since=args.since,
            top_k=args.top_k,
        )

    if args.format == 'json':
        print(json.dumps(chunks, indent=2, ensure_ascii=False))
    else:
        print(format_text(chunks, args.query))


if __name__ == '__main__':
    main()
