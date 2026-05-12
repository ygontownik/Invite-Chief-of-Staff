#!/usr/bin/env python3
"""
Deal Cross-Reference Engine for Daily Briefing

Reads today's briefing_sources_{date}.json, embeds each article locally with
sentence-transformers, and scores against active deal profiles. High-relevance
matches are emitted as ---DEAL-INTEL--- blocks into
/tmp/deal_intel_blocks_{date}.txt for intel_capture.py to route to log.json.

Also writes /tmp/cross_ref_summary_{date}.json for the weekly digest writer.

No Anthropic API calls — all embedding runs on-device (Rule CC1).

Usage:
    python3 cross_reference_briefing.py --date YYYY-MM-DD [--threshold 0.55] [--dry-run]
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR       = Path.home() / 'dashboards' / 'data'
PROFILES_PATH  = DATA_DIR / 'knowledge_index' / 'deal_profiles.json'
EMBEDDING_MODEL = 'all-MiniLM-L6-v2'
DEFAULT_THRESHOLD = 0.35   # MiniLM scores on finance/infra text are lower than general text; tune via --threshold


def _clean_content(text: str) -> str:
    """Strip Substack/email boilerplate from article content."""
    # Normalize vertical tabs and other whitespace variants to newline
    text = text.replace('\x0b', '\n').replace('\r', '\n')
    lines = text.split('\n')
    clean = []
    for line in lines:
        s = line.strip()
        # Skip URL-heavy lines, Substack redirects, email headers, blank date headers
        if not s:
            continue
        if s.startswith('http') or 'substack.com/redirect' in s:
            continue
        if re.match(r'^Date:\s+\w+ \d{1,2},? \d{4}$', s):
            continue
        if s.startswith('View this post on the web'):
            continue
        if re.match(r'^[A-Z]\.\s*(REPORT HEADER|EXECUTIVE SUMMARY|$)', s):
            continue
        # Newsletter footers
        if re.search(r'subscribe for free|thanks for reading|unsubscribe|manage preferences', s, re.I):
            continue
        # Skip lines that are almost entirely a URL
        url_chars = len(re.findall(r'https?://\S+', s))
        if len(s) < 80 and url_chars > 0:
            continue
        clean.append(s)
    return '\n'.join(clean)


def _first_sentences(text: str, n: int = 2) -> str:
    """Return roughly the first n sentences from cleaned text."""
    text = _clean_content(text).strip()
    if not text:
        return ''
    parts = re.split(r'(?<=[.!?])\s+', text)
    return ' '.join(parts[:n]).strip()


def _key_facts(text: str, max_facts: int = 3) -> list[str]:
    """Extract substantive facts from article content."""
    text = _clean_content(text)
    facts = []
    for line in text.split('\n'):
        line = line.strip().lstrip('•–—-• ').strip()
        # Skip short lines, pure headers, and URL-containing lines
        if len(line) < 40 or line.endswith(':') or 'http' in line:
            continue
        facts.append(line[:200])
        if len(facts) >= max_facts:
            break
    return facts


def build_deal_intel_block(deal_id: str, deal_name: str, source: str,
                            article_title: str, content: str,
                            similarity: float, today: str) -> str:
    summary = _first_sentences(content, 2)
    if not summary:
        summary = article_title
    # Trim to ~250 chars
    if len(summary) > 250:
        summary = summary[:247] + '…'

    facts = _key_facts(content)
    facts_yaml = '\n'.join(f'  - {f}' for f in facts) if facts else '  - (see source)'

    return (
        f'---DEAL-INTEL---\n'
        f'deal: {deal_id}\n'
        f'date: {today}\n'
        f'title: {source} — {article_title[:80]}\n'
        f'summary: {summary}\n'
        f'facts:\n{facts_yaml}\n'
        f'counterparties: []\n'
        f'actions: []\n'
        f'---END-DEAL-INTEL---'
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date',      default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--threshold', type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument('--dry-run',   action='store_true')
    args = parser.parse_args()

    today = args.date
    sources_file = Path(f'/tmp/briefing_sources_{today}.json')
    blocks_file  = Path(f'/tmp/deal_intel_blocks_{today}.txt')
    summary_file = Path(f'/tmp/cross_ref_summary_{today}.json')

    if not sources_file.exists():
        print(f'ERROR: {sources_file} not found — run generate_briefing.py first',
              file=sys.stderr)
        sys.exit(1)

    if not PROFILES_PATH.exists():
        print(f'ERROR: {PROFILES_PATH} not found — run knowledge_indexer.py first',
              file=sys.stderr)
        sys.exit(1)

    # ── load inputs ──
    sources_data = json.loads(sources_file.read_text())
    all_sources  = sources_data.get('sources', [])

    profiles = json.loads(PROFILES_PATH.read_text())
    # Filter to deals with meaningful profile text
    active_profiles = []
    for p in profiles:
        profile_text = ' '.join(filter(None, [
            p.get('name', ''),
            p.get('sector', ''),
            p.get('geography', ''),
            p.get('tagline', ''),
            p.get('thesis', ''),
        ])).strip()
        if profile_text:
            active_profiles.append({**p, '_profile_text': profile_text})

    if not active_profiles:
        print('No active deal profiles found — nothing to cross-reference')
        blocks_file.write_text('')
        sys.exit(0)

    # Flatten all articles
    articles = []
    for src in all_sources:
        source_name = src['source']
        for art in src.get('articles', []):
            content = art.get('content', '').strip()
            articles.append({
                'source':  source_name,
                'title':   art.get('title', ''),
                'content': content,
                'text':    f"{art.get('title', '')}\n{content[:800]}",  # embedding input
            })

    if not articles:
        print('No articles in sources file')
        blocks_file.write_text('')
        sys.exit(0)

    print(f'Cross-referencing {len(articles)} articles × {len(active_profiles)} deals '
          f'(threshold={args.threshold})')

    # ── embed ──
    from sentence_transformers import SentenceTransformer, util

    model = SentenceTransformer(EMBEDDING_MODEL)

    article_texts = [a['text'] for a in articles]
    deal_texts    = [p['_profile_text'] for p in active_profiles]

    article_vecs = model.encode(article_texts, convert_to_tensor=True, show_progress_bar=False)
    deal_vecs    = model.encode(deal_texts,    convert_to_tensor=True, show_progress_bar=False)

    # similarity matrix: [n_articles × n_deals]
    scores = util.cos_sim(article_vecs, deal_vecs).cpu().numpy()

    # ── collect matches ──
    # cross_ref[deal_id] = [{article, sim}, ...]
    cross_ref: dict[str, list[dict]] = {p['deal_id']: [] for p in active_profiles}

    for a_idx, article in enumerate(articles):
        for d_idx, profile in enumerate(active_profiles):
            sim = float(scores[a_idx, d_idx])
            if sim >= args.threshold:
                cross_ref[profile['deal_id']].append({
                    'source':        article['source'],
                    'article_title': article['title'],
                    'content':       article['content'],
                    'similarity':    round(sim, 4),
                })

    # Sort each deal's matches by similarity desc
    for deal_id in cross_ref:
        cross_ref[deal_id].sort(key=lambda x: x['similarity'], reverse=True)

    # ── emit blocks ──
    blocks = []
    summary_out: dict[str, list[dict]] = {}

    for profile in active_profiles:
        deal_id   = profile['deal_id']
        deal_name = profile.get('name', deal_id)
        matches   = cross_ref.get(deal_id, [])

        if not matches:
            continue

        summary_out[deal_id] = []
        print(f'\n  [{deal_id}] {len(matches)} relevant article(s):')

        for m in matches:
            sim_str = f'{m["similarity"]:.2f}'
            print(f'    sim={sim_str}  [{m["source"]}] {m["article_title"][:60]}')

            block = build_deal_intel_block(
                deal_id=deal_id,
                deal_name=deal_name,
                source=m['source'],
                article_title=m['article_title'],
                content=m['content'],
                similarity=m['similarity'],
                today=today,
            )
            blocks.append(block)
            summary_out[deal_id].append({
                'source':        m['source'],
                'article_title': m['article_title'],
                'similarity':    m['similarity'],
            })

    total_matches = sum(len(v) for v in summary_out.values())
    print(f'\nTotal matches: {total_matches} across {len(summary_out)} deals')

    if args.dry_run:
        print('\n[DRY-RUN] Blocks that would be emitted:')
        for b in blocks:
            print(b)
            print()
        sys.exit(0)

    # ── write output files ──
    blocks_file.write_text('\n\n'.join(blocks) + ('\n' if blocks else ''))
    summary_file.write_text(json.dumps({
        'date':    today,
        'deals':   summary_out,
        'threshold': args.threshold,
    }, indent=2, ensure_ascii=False))

    print(f'\n✓ Blocks → {blocks_file}')
    print(f'✓ Summary → {summary_file}')

    if not blocks:
        print('No matches above threshold — blocks file is empty (briefing continues normally)')


if __name__ == '__main__':
    main()
