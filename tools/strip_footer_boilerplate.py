#!/usr/bin/env python3
"""
Strip email footer boilerplate from Google Docs (NotebookLM sources).
Uses the Docs API to identify and delete short boilerplate paragraphs.
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, '/Users/ygontownik/credentials')

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_PATH = Path('~/credentials/token.json').expanduser()
creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
drive = build('drive', 'v3', credentials=creds)
docs_svc = build('docs', 'v1', credentials=creds)

# Docs to clean (primary targets)
PRIMARY_DOCS = [
    ("Bank Street Group LLC",   "1LoeiC6Z6xFXnnCelm7j6MgkduUOvrEcgYhQizfy-NJc"),
    ("a16z",                    "1fkH1X6HQw-ruogp54Zq77SjgcD2kvptc-CDcHsxxdiY"),
    ("Suhail Y Tayeb / Dirt to Data", "1AXiVPtbOvylG8VhuHaORd_0RPdGyA3hVB0IK-zHSFN8"),
    ("Global Data Center Hub",  "12x2JwmnXymIyJvgbYiiL5YlVXpBCYduXOrPtOufuz8U"),
]

# Additional docs to check
ADDITIONAL_DOCS = [
    ("Energy Pipeline (Gemini)", "1olCXFTHX0tv3Bqb29x02s7Oa7aryW1SNNFJQQtndvmQ"),
    ("FVR Energy Finance",       "1Jg_-LamIsKVKXBrWlZICZGrQTkOoXBeAT2U2rNldLoA"),
    ("GS Energy",                "1NGKZXv0MgkbBXXQRPQ2Kiq1AgKg88v5bmyPlpz4Pi9c"),
    ("GS Macro Market",          "19wGIr8UoxiRuL2jEFOp-aGLA4sHj_rGP5DeILWDyKCQ"),
    ("RBN Energy Daily Archive", "1N6mqhMJn1IJP-5EwByYccEb0uaoBeUDXKRNT8BUbfW4"),
]

ALL_DOCS = PRIMARY_DOCS + ADDITIONAL_DOCS

# Boilerplate detection patterns (applied to stripped, lowercased paragraph text)
# Each entry: (pattern, max_chars_or_None)
# max_chars: if set, only match if paragraph length <= max_chars
BOILERPLATE_PATTERNS = [
    # Unsubscribe — always short
    (re.compile(r'\bunsubscribe\b', re.I), 120),
    # All rights reserved
    (re.compile(r'all rights reserved', re.I), 150),
    # View in browser
    (re.compile(r'view (in|this) (browser|email|online)', re.I), 120),
    # You are receiving this
    (re.compile(r"you('re| are) receiving this", re.I), 150),
    # Update / manage preferences
    (re.compile(r'(update|manage) (your |email )?preferences', re.I), 120),
    # Privacy policy as standalone short line
    (re.compile(r'^privacy policy[\s\W]*$', re.I), 60),
    # Copyright © as standalone
    (re.compile(r'^copyright\s*©', re.I), 100),
    # Sent to + email
    (re.compile(r'sent to\s+[\w._%+-]+@[\w.-]+', re.I), 150),
    # If you no longer wish
    (re.compile(r'if you no longer wish', re.I), 150),
    # This email was sent
    (re.compile(r'this email was sent', re.I), 150),
    # Forward to a friend / share this email
    (re.compile(r'forward (this |to )?(a )?friend', re.I), 120),
    # To stop receiving
    (re.compile(r'to stop receiving', re.I), 120),
    # Click here to unsubscribe / opt out
    (re.compile(r'(click here|opt.?out)', re.I), 120),
    # Our mailing address
    (re.compile(r'our mailing address', re.I), 150),
    # You have received this
    (re.compile(r'you have received this (email|newsletter|message)', re.I), 150),
    # Manage subscription
    (re.compile(r'manage (your )?(email )?subscription', re.I), 120),
    # Add us to your address book
    (re.compile(r'add (us to your|to your) address book', re.I), 120),
    # © \d{4} — short copyright line
    (re.compile(r'^©\s*\d{4}', re.I), 100),
    # Newsletter footer: "powered by" short line
    (re.compile(r'^powered by\s+\w', re.I), 80),
    # Don't want these emails?
    (re.compile(r"don'?t want (these|this) (emails?|newsletter)", re.I), 120),
    # To unsubscribe from
    (re.compile(r'to unsubscribe from', re.I), 120),
    # Email delivered by / sent via
    (re.compile(r'(email|newsletter) (delivered by|sent via)', re.I), 120),
]

# Patterns that indicate the paragraph is substantive DESPITE containing a footer word
# If any of these match, skip deletion even if a boilerplate pattern hits
SAFE_PATTERNS = [
    re.compile(r'─{3,}|═{3,}'),       # structural separators
    re.compile(r'^\s*(TABLE OF CONTENTS|TOC)', re.I),
]


def get_doc_text_and_paragraphs(doc_id):
    """Fetch document and return (doc, list of (para_index, start_idx, end_idx, text))."""
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    body_content = doc.get('body', {}).get('content', [])

    paragraphs = []
    for element in body_content:
        if 'paragraph' not in element:
            continue
        para = element['paragraph']
        start_idx = element.get('startIndex', 0)
        end_idx = element.get('endIndex', 0)

        # Gather text
        text = ''
        for pe in para.get('elements', []):
            tr = pe.get('textRun')
            if tr:
                text += tr.get('content', '')

        text_stripped = text.strip()
        paragraphs.append((start_idx, end_idx, text_stripped))

    return doc, paragraphs


def is_boilerplate(text):
    """Return True if text matches boilerplate patterns and is short enough."""
    if not text:
        return False

    # Safety check — never delete long paragraphs regardless
    if len(text) > 200:
        return False

    # Check safe patterns first
    for sp in SAFE_PATTERNS:
        if sp.search(text):
            return False

    for pattern, max_chars in BOILERPLATE_PATTERNS:
        if max_chars is not None and len(text) > max_chars:
            continue
        if pattern.search(text):
            return True

    return False


def count_boilerplate(paragraphs):
    """Return list of (start_idx, end_idx) for boilerplate paragraphs."""
    hits = []
    for start_idx, end_idx, text in paragraphs:
        if is_boilerplate(text):
            hits.append((start_idx, end_idx, text))
    return hits


def delete_ranges(doc_id, ranges_to_delete):
    """Delete content ranges in reverse order (to preserve indices)."""
    # Sort by start_idx descending so deletions don't shift subsequent indices
    sorted_ranges = sorted(ranges_to_delete, key=lambda x: x[0], reverse=True)

    requests = []
    for start_idx, end_idx, _ in sorted_ranges:
        if end_idx > start_idx:
            requests.append({
                'deleteContentRange': {
                    'range': {
                        'startIndex': start_idx,
                        'endIndex': end_idx,
                    }
                }
            })

    if not requests:
        return

    # Docs API has a limit of ~500 requests per batchUpdate; chunk if needed
    CHUNK_SIZE = 400
    for i in range(0, len(requests), CHUNK_SIZE):
        chunk = requests[i:i + CHUNK_SIZE]
        docs_svc.documents().batchUpdate(
            documentId=doc_id,
            body={'requests': chunk}
        ).execute()


def get_doc_char_count(doc_id):
    """Return approximate character count of doc body."""
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    body_content = doc.get('body', {}).get('content', [])
    total = 0
    for element in body_content:
        if 'paragraph' in element:
            for pe in element['paragraph'].get('elements', []):
                tr = pe.get('textRun')
                if tr:
                    total += len(tr.get('content', ''))
    return total


def process_doc(name, doc_id, dry_run=False):
    print(f"\n{'='*60}")
    print(f"Doc: {name}")
    print(f"ID:  {doc_id}")

    try:
        doc, paragraphs = get_doc_text_and_paragraphs(doc_id)
    except Exception as e:
        print(f"  ERROR fetching doc: {e}")
        return 0

    print(f"  Total paragraphs: {len(paragraphs)}")

    hits = count_boilerplate(paragraphs)
    print(f"  Boilerplate paragraphs found: {len(hits)}")

    if not hits:
        print("  Nothing to delete.")
        return 0

    # Show sample hits (first 10)
    print("  Sample boilerplate paragraphs:")
    for start_idx, end_idx, text in hits[:10]:
        preview = text[:80].replace('\n', '↵')
        print(f"    [{start_idx}:{end_idx}] {repr(preview)}")
    if len(hits) > 10:
        print(f"    ... and {len(hits) - 10} more")

    if dry_run:
        print("  [DRY RUN] Skipping deletion.")
        return len(hits)

    # Get before char count
    before_chars = sum(len(t) for _, _, t in paragraphs)

    # Delete
    print(f"  Deleting {len(hits)} paragraphs...")
    try:
        delete_ranges(doc_id, hits)
    except Exception as e:
        print(f"  ERROR during deletion: {e}")
        return 0

    # Get after char count (re-fetch)
    try:
        _, paragraphs_after = get_doc_text_and_paragraphs(doc_id)
        after_chars = sum(len(t) for _, _, t in paragraphs_after)
        para_after_count = len(paragraphs_after)
    except Exception as e:
        print(f"  Could not re-fetch for size check: {e}")
        after_chars = None
        para_after_count = None

    print(f"  Done. Paragraphs before: {len(paragraphs)} → after: {para_after_count}")
    if after_chars is not None:
        print(f"  Chars before: {before_chars:,} → after: {after_chars:,} (removed {before_chars - after_chars:,})")

    return len(hits)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Show what would be deleted without deleting')
    args = parser.parse_args()

    if args.dry_run:
        print("*** DRY RUN MODE — no changes will be made ***\n")

    total_removed = 0
    results = []

    for name, doc_id in ALL_DOCS:
        removed = process_doc(name, doc_id, dry_run=args.dry_run)
        total_removed += removed
        results.append((name, removed))

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for name, removed in results:
        status = f"{removed} paragraphs removed" if removed > 0 else "nothing to remove"
        print(f"  {name:<35} {status}")
    print(f"\n  Total boilerplate paragraphs {'found' if args.dry_run else 'removed'}: {total_removed}")


if __name__ == '__main__':
    main()
