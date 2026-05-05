"""check_relative_dates.py — enforce rule AB1 (absolute dates only).

WHAT IT CATCHES
---------------
Per rule AB1 (dash_corrections.md) every reference to a date or week in
extracted dashboard text MUST be in absolute YYYY-MM-DD form. Relative
phrasing ("tomorrow", "next week", "Wed 4/29", "Friday 5/1", "EOD")
reads stale every day after the extraction date even when the underlying
action is still valid, so the user reads the dashboard as broken.

Two-layer defense exists:
  1. Extraction-prompt enrichment — LLM extractors instructed to resolve
     relative dates to YYYY-MM-DD against the source document's date.
  2. cos-dashboard-fetch.py :: _materialize_next_week — post-process
     converts relative phrasings the LLM missed.

This check is the third layer — runtime audit. Scans the served data
files for any surviving relative-date phrasing and reports violations,
giving the user/maintainer a chance to tighten extractor prompts or
add new patterns to the post-process before the bug compounds.

STATUSES
--------
- pass : 0 violations
- warn : 1-10 violations (drift; tighten extractor)
- fail : 11+ violations (extraction or post-process is broken)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"

# Forbidden patterns — anything matching these in extracted text is a
# rule-AB1 violation. Anchored to substantive contexts so we don't flag
# legitimate uses (e.g., "EOD market close" in a memo summary).
_PATTERNS = [
    (re.compile(r'\btomorrow\b', re.I),                  'tomorrow'),
    (re.compile(r'\b(?:next|this)\s+week\b', re.I),       'next/this week'),
    (re.compile(r'\b(?:next|this)\s+'
                r'(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b', re.I),
                                                           'next/this <day>'),
    # Day + M/D shorthand — "Wed 4/29", "Friday 5/1"
    (re.compile(r'\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day|s)?\.?\s+'
                r'\d{1,2}/\d{1,2}(?:/\d{2,4})?\b', re.I),
                                                           '<day> M/D'),
    # Bare M/D after a date verb — "by 5/12", "due 4/30"
    (re.compile(r'\b(?:by|before|due|on|until|through|circa)\s+'
                r'\d{1,2}/\d{1,2}(?!\d)(?:/\d{2,4})?\b', re.I),
                                                           '<verb> M/D'),
    (re.compile(r'\bEOD\b'),                              'EOD'),
    (re.compile(r'\bearly\s+(?:next|this)\s+week\b', re.I),
                                                           'early next/this week'),
    (re.compile(r'\blate\s+(?:next|this)\s+week\b', re.I),
                                                           'late next/this week'),
]

# Fields to scan per item bucket. (bucket_key, [field_names])
_SCANNED_BUCKETS = [
    ('awaitingExternal', ['content', 'what', 'context']),
    ('followUps',        ['what', 'context']),
    ('dealIntel',        ['content', 'context']),
    ('originationInbox', ['content', 'context']),
    ('recentActivity',   ['summary', 'content']),
]


def run() -> dict[str, Any]:
    if not DASHBOARD_DATA.exists():
        return {
            'name': 'AB1: absolute dates only (no relative phrasing)',
            'rule_ref': 'dash_corrections.md :: AB1',
            'status': 'warn',
            'summary': 'dashboard-data.json not present — skipped',
            'details': [],
        }
    try:
        d = json.loads(DASHBOARD_DATA.read_text(encoding='utf-8'))
    except Exception as exc:
        return {
            'name': 'AB1: absolute dates only (no relative phrasing)',
            'rule_ref': 'dash_corrections.md :: AB1',
            'status': 'fail',
            'summary': f'unreadable: {exc}',
            'details': [str(exc)],
        }

    violations: list[str] = []
    total_items = 0
    for bucket, fields in _SCANNED_BUCKETS:
        items = d.get(bucket) or []
        if not isinstance(items, list):
            continue
        for item in items:
            total_items += 1
            if not isinstance(item, dict):
                continue
            for fld in fields:
                v = item.get(fld)
                if not isinstance(v, str) or not v:
                    continue
                for pat, label in _PATTERNS:
                    m = pat.search(v)
                    if not m:
                        continue
                    cp = (item.get('counterparty')
                          or item.get('who')
                          or item.get('parent_id') or '?')
                    snippet = v[max(0, m.start() - 12):m.end() + 25]
                    violations.append(
                        f'[{bucket}.{fld}] cp={cp!r} "{label}": ...{snippet}...'
                    )
                    break  # one violation per field is enough signal
    if not violations:
        return {
            'name': 'AB1: absolute dates only (no relative phrasing)',
            'rule_ref': 'dash_corrections.md :: AB1',
            'status': 'pass',
            'summary': f'0 relative-date violations across {total_items} items',
            'details': [],
        }

    if len(violations) <= 10:
        status = 'warn'
    else:
        status = 'fail'
    return {
        'name': 'AB1: absolute dates only (no relative phrasing)',
        'rule_ref': 'dash_corrections.md :: AB1',
        'status': status,
        'summary': (
            f'{len(violations)} relative-date phrasing violation(s) across '
            f'{total_items} items — extractor or post-process leak'
        ),
        'details': violations[:20] + (
            [f'... and {len(violations) - 20} more'] if len(violations) > 20 else []
        ),
    }
