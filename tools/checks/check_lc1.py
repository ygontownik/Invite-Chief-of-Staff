#!/usr/bin/env python3
"""check_lc1.py — Rule LC1: capture scan must dedupe + filter.

run_learning_capture_scan() in dash-state-hook.py is supposed to filter
aggressively before writing to proposed-learnings.jsonl: dedupe against
existing rules + prior emissions, exclude Otter / transcript paths,
exclude code-comment lines, and require imperative structure.

This check inspects the producer's output as a proxy for producer
discipline:
  - total record count (>500 = producer over-emitting)
  - unique-snippet ratio (low ratio = dedup broken)
  - presence of Otter / transcript path markers in snippets

Status:
  pass — ratio > 0.7 AND total < 200 AND no Otter snippets
  warn — ratio 0.4-0.7 OR 200 ≤ total ≤ 500 OR ≤2 Otter snippets
  fail — ratio < 0.4 OR total > 500 OR >2 Otter snippets
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
PROPOSED = HOME / "dashboards" / "data" / "compiled" / "proposed-learnings.jsonl"

_OTTER_MARKERS = ("otter", "/transcripts/", "transcript_text")


def run() -> dict[str, Any]:
    if not PROPOSED.exists():
        return {
            "name": "LC1: capture scan dedupe + filter",
            "rule_ref": "LC1",
            "status": "pass",
            "summary": "no proposed-learnings.jsonl present (clean state)",
            "details": {"path": str(PROPOSED), "total": 0},
        }

    total = 0
    snippets: list[str] = []
    otter_hits: list[str] = []

    try:
        with PROPOSED.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                snip = (rec.get("snippet") or "").strip()
                if not snip:
                    continue
                snippets.append(snip)
                low = snip.lower()
                if any(m in low for m in _OTTER_MARKERS):
                    otter_hits.append(snip[:120])
    except OSError as exc:
        return {
            "name": "LC1: capture scan dedupe + filter",
            "rule_ref": "LC1",
            "status": "fail",
            "summary": f"could not read proposed-learnings.jsonl: {exc}",
            "details": {"error": str(exc)},
        }

    unique = len({s.lower() for s in snippets})
    ratio = (unique / len(snippets)) if snippets else 1.0

    otter_n = len(otter_hits)

    if total > 500 or ratio < 0.4 or otter_n > 2:
        status = "fail"
    elif total > 200 or ratio < 0.7 or otter_n > 0:
        status = "warn"
    else:
        status = "pass"

    summary = (
        f"LC1: {total} records, {unique} unique (ratio={ratio:.2f}), "
        f"{otter_n} Otter/transcript snippet(s)"
    )

    return {
        "name": "LC1: capture scan dedupe + filter",
        "rule_ref": "LC1",
        "status": status,
        "summary": summary,
        "details": {
            "path": str(PROPOSED),
            "total": total,
            "unique_snippets": unique,
            "unique_ratio": round(ratio, 3),
            "otter_hits": otter_hits[:10],
            "thresholds": {
                "fail_total": 500, "warn_total": 200,
                "fail_ratio": 0.4, "warn_ratio": 0.7,
                "fail_otter": 2,
            },
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
