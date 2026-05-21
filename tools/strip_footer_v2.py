#!/usr/bin/env python3
"""
Strip email footer boilerplate from Google Docs (NotebookLM sources).

Strategy: The docs store each email as a single large paragraph (thousands of chars).
The footer is embedded within these paragraphs. We locate the footer start position
within each paragraph's text runs and delete from there to end of the paragraph.

Footer patterns:
- Substack feeds: footer begins at \x0b\x0b\x0b\x0bUnsubscribe https://substack.com/...
  We cut just before this segment (keeping the 2 preceding \x0b chars as paragraph spacing)
- Bank Street: footer begins at \x0b\x0b\x0b\x0b© Copyright 2026...
  We cut just before this segment
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path.home() / 'credentials'))

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_PATH = Path('~/credentials/token.json').expanduser()
creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
docs_svc = build('docs', 'v1', credentials=creds)

# Footer anchor patterns — we delete from (and including) these markers to end of paragraph
# Each is a regex; we find the FIRST match and delete from there to end of para
FOOTER_ANCHORS = [
    # Substack unsubscribe block — always \x0b\x0b\x0b\x0b then Unsubscribe https://substack.com
    re.compile(r'\x0b{2,}Unsubscribe https://substack\.com/', re.I),
    # Bank Street copyright block
    re.compile(r'\x0b{2,}©\s*Copyright\s+\d{4}', re.I),
    # Generic: \x0b\x0b\x0b\x0b followed by "You're receiving this"
    re.compile(r'\x0b{2,}You.?re receiving this', re.I),
    # Generic: \x0b\x0b\x0b\x0b followed by "This email was sent"
    re.compile(r'\x0b{2,}This email was sent', re.I),
    # Substack: "if you would like to opt out" — captures the lead-in sentence too
    # We already have the Unsubscribe URL pattern above for substack; this is backup
]

# Minimum paragraph length to consider (skip short separator paragraphs)
MIN_PARA_LEN = 500


def get_doc_paragraphs_with_elements(doc_id):
    """
    Return list of dicts:
    {
        'para_start': int,   # startIndex of paragraph in doc
        'para_end': int,     # endIndex of paragraph in doc
        'full_text': str,    # concatenated text of all text runs
        'elements': list,    # raw paragraph elements (for index calculation)
    }
    """
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    body_content = doc.get('body', {}).get('content', [])

    result = []
    for el in body_content:
        if 'paragraph' not in el:
            continue
        para = el['paragraph']
        para_start = el.get('startIndex', 0)
        para_end = el.get('endIndex', 0)

        elements = para.get('elements', [])
        full_text = ''
        for pe in elements:
            tr = pe.get('textRun')
            if tr:
                full_text += tr.get('content', '')

        result.append({
            'para_start': para_start,
            'para_end': para_end,
            'full_text': full_text,
            'elements': elements,
        })

    return doc, result


def char_offset_to_doc_index(para_start, elements, char_offset):
    """
    Convert a character offset within para's full_text to an absolute doc index.
    Elements contain startIndex/endIndex in doc coordinates.
    """
    cumulative = 0
    for pe in elements:
        tr = pe.get('textRun')
        if not tr:
            continue
        content = tr.get('content', '')
        content_len = len(content)
        el_start = pe.get('startIndex', para_start + cumulative)

        if char_offset < cumulative + content_len:
            # The target is within this element
            offset_within_element = char_offset - cumulative
            return el_start + offset_within_element
        cumulative += content_len

    # If char_offset is at or beyond the end, return para_end - 1 (before the \n)
    return para_start + cumulative


def find_footer_start(full_text):
    """Return character offset where footer begins, or None if not found."""
    for pattern in FOOTER_ANCHORS:
        m = pattern.search(full_text)
        if m:
            # We want to keep 1 \x0b (paragraph gap), so start deletion at match.start() + 1
            # But actually we want to remove the extra \x0b chars + the footer
            # Keep at most 1 trailing \x0b before the cut
            cut_pos = m.start()
            # Walk back to keep exactly 1 \x0b before the cut if it was preceded by content
            # The pattern starts with \x0b{2,}, so cut_pos is at the first \x0b
            # We keep 1 for paragraph spacing, delete from cut_pos+1
            return cut_pos + 1  # keep one \x0b, delete the rest

    return None


def process_doc(name, doc_id, dry_run=False):
    print(f"\n{'='*60}")
    print(f"Doc: {name}  [{doc_id}]")

    try:
        doc, paragraphs = get_doc_paragraphs_with_elements(doc_id)
    except Exception as e:
        print(f"  ERROR fetching doc: {e}")
        return {'name': name, 'found': 0, 'removed': 0, 'chars_removed': 0, 'error': str(e)}

    total_chars = sum(len(p['full_text']) for p in paragraphs)
    print(f"  Total paragraphs: {len(paragraphs)}, total chars: {total_chars:,}")

    # Find all footer ranges to delete (in doc index space)
    delete_ranges = []  # list of (start_doc_idx, end_doc_idx, preview)

    for para in paragraphs:
        full_text = para['full_text']
        if len(full_text) < MIN_PARA_LEN:
            continue  # skip short paragraphs

        footer_char_offset = find_footer_start(full_text)
        if footer_char_offset is None:
            continue

        # Convert char offset to doc index
        delete_start = char_offset_to_doc_index(
            para['para_start'], para['elements'], footer_char_offset
        )
        # Delete to end of paragraph content (para_end - 1 to preserve the paragraph break \n)
        # para_end is exclusive in the Docs API; the last char is usually \n
        delete_end = para['para_end'] - 1  # keep the trailing \n to preserve paragraph structure

        if delete_end <= delete_start:
            continue

        chars_to_remove = delete_end - delete_start
        preview = repr(full_text[footer_char_offset:footer_char_offset+100])
        delete_ranges.append((delete_start, delete_end, chars_to_remove, preview))

    print(f"  Footer blocks found: {len(delete_ranges)}")
    total_chars_to_remove = sum(r[2] for r in delete_ranges)
    print(f"  Total chars to remove: {total_chars_to_remove:,}")

    if not delete_ranges:
        print("  Nothing to delete.")
        return {'name': name, 'found': 0, 'removed': 0, 'chars_removed': 0}

    # Show first 5 samples
    print("  Sample footer starts (first 5):")
    for start, end, chars, preview in delete_ranges[:5]:
        print(f"    doc[{start}:{end}] ({chars} chars) → {preview}")

    if dry_run:
        print("  [DRY RUN] No changes made.")
        return {'name': name, 'found': len(delete_ranges), 'removed': 0, 'chars_removed': 0}

    # Delete in reverse order to preserve index integrity
    sorted_ranges = sorted(delete_ranges, key=lambda x: x[0], reverse=True)

    requests = []
    for start, end, _, _ in sorted_ranges:
        requests.append({
            'deleteContentRange': {
                'range': {
                    'startIndex': start,
                    'endIndex': end,
                }
            }
        })

    # Chunk into batches of 400 (API limit)
    CHUNK_SIZE = 400
    total_batches = (len(requests) + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(f"  Sending {len(requests)} delete requests in {total_batches} batch(es)...")

    for i in range(0, len(requests), CHUNK_SIZE):
        chunk = requests[i:i+CHUNK_SIZE]
        try:
            docs_svc.documents().batchUpdate(
                documentId=doc_id,
                body={'requests': chunk}
            ).execute()
            print(f"  Batch {i//CHUNK_SIZE + 1}/{total_batches} done ({len(chunk)} ops)")
        except Exception as e:
            print(f"  ERROR in batch {i//CHUNK_SIZE + 1}: {e}")
            return {'name': name, 'found': len(delete_ranges), 'removed': 0,
                    'chars_removed': 0, 'error': str(e)}

    # Verify: re-fetch and count
    try:
        _, paragraphs_after = get_doc_paragraphs_with_elements(doc_id)
        after_chars = sum(len(p['full_text']) for p in paragraphs_after)
        print(f"  Chars before: {total_chars:,} → after: {after_chars:,} "
              f"(removed {total_chars - after_chars:,})")
    except Exception as e:
        print(f"  Could not re-verify: {e}")
        after_chars = None

    return {
        'name': name,
        'found': len(delete_ranges),
        'removed': len(delete_ranges),
        'chars_removed': total_chars - (after_chars or 0),
    }


ALL_DOCS = [
    # Primary targets
    ("Bank Street Group LLC",         "1LoeiC6Z6xFXnnCelm7j6MgkduUOvrEcgYhQizfy-NJc"),
    ("a16z",                          "1fkH1X6HQw-ruogp54Zq77SjgcD2kvptc-CDcHsxxdiY"),
    ("Suhail Y Tayeb / Dirt to Data", "1AXiVPtbOvylG8VhuHaORd_0RPdGyA3hVB0IK-zHSFN8"),
    ("Global Data Center Hub",        "12x2JwmnXymIyJvgbYiiL5YlVXpBCYduXOrPtOufuz8U"),
    # Additional docs
    ("Energy Pipeline (Gemini)",      "1olCXFTHX0tv3Bqb29x02s7Oa7aryW1SNNFJQQtndvmQ"),
    ("FVR Energy Finance",            "1Jg_-LamIsKVKXBrWlZICZGrQTkOoXBeAT2U2rNldLoA"),
    ("GS Energy",                     "1NGKZXv0MgkbBXXQRPQ2Kiq1AgKg88v5bmyPlpz4Pi9c"),
    ("GS Macro Market",               "19wGIr8UoxiRuL2jEFOp-aGLA4sHj_rGP5DeILWDyKCQ"),
    ("RBN Energy Daily Archive",      "1N6mqhMJn1IJP-5EwByYccEb0uaoBeUDXKRNT8BUbfW4"),
]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    if args.dry_run:
        print("*** DRY RUN — no changes will be made ***\n")

    results = []
    for name, doc_id in ALL_DOCS:
        r = process_doc(name, doc_id, dry_run=args.dry_run)
        results.append(r)

    print(f"\n{'='*60}")
    print("FINAL SUMMARY")
    print(f"{'='*60}")
    grand_blocks = 0
    grand_chars = 0
    for r in results:
        action = "removed" if not args.dry_run else "found"
        blocks = r.get('removed' if not args.dry_run else 'found', 0)
        chars = r.get('chars_removed', 0)
        err = f" [ERROR: {r['error']}]" if 'error' in r else ""
        print(f"  {r['name']:<38} {blocks:>3} footer blocks {action}, "
              f"{chars:>8,} chars removed{err}")
        grand_blocks += blocks
        grand_chars += chars

    print(f"\n  TOTAL: {grand_blocks} footer blocks | {grand_chars:,} chars removed")


if __name__ == '__main__':
    main()
