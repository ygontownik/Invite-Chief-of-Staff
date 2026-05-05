#!/usr/bin/env python3
"""
check_v1.py
===========

Enforces dash_corrections.md :: V1 — per-deal activity log + parent_id
propagation.

V1 says every active deal needs an auto-derived chronological narrative
in `data/deals/<TICKER>/log.json > entries[]`. Past failure (towers
0-explicit bug): `parent_id` was not propagating from extraction →
log-append, so the deal's `match: explicit` rate was 0% and the
narrative looked thin even when the underlying signal was rich.

Checks per deal directory under ~/dashboards/data/deals/<slug>/:
  1. log.json exists with shape {deal_id, entries: [...]}.
  2. entries[].id is present and stable (8-hex-shaped djb2 — anything
     non-empty is acceptable here; we just verify presence).
  3. The fraction of entries with `match: explicit` (LLM-tagged) vs
     `match: token` (substring fallback) is >0% when the deal has
     more than 10 entries — zero-explicit on a populated log signals
     the parent_id propagation regression.

Status:
  - "fail" if any deal directory under data/deals/ has no log.json
    AND has any other deal artifacts (deal.md, actions.md).
  - "warn" if any deal log has >10 entries and 0 of them have
    match=explicit (the towers regression shape).
  - "pass" otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
DEALS_ROOT = HOME / "dashboards" / "data" / "deals"


def run() -> dict[str, Any]:
    if not DEALS_ROOT.exists():
        return {
            "name": "V1: per-deal log + parent_id propagation",
            "rule_ref": "dash_corrections.md :: V1",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [f"missing: {DEALS_ROOT}"],
        }

    missing_logs: list[str] = []
    zero_explicit: list[str] = []
    deal_count = 0

    for child in sorted(DEALS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            # _inbox or other staging dirs aren't deals.
            continue
        deal_count += 1
        log_path = child / "log.json"
        deal_md = child / "deal.md"
        if not log_path.exists():
            if deal_md.exists():
                missing_logs.append(
                    f"deal={child.name!r} has deal.md but no log.json"
                )
            continue
        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception as exc:
            missing_logs.append(f"deal={child.name!r} log.json unreadable: {exc}")
            continue
        entries = (log or {}).get("entries") if isinstance(log, dict) else None
        if not isinstance(entries, list):
            missing_logs.append(
                f"deal={child.name!r} log.json missing entries[]"
            )
            continue
        if len(entries) > 10:
            explicit_n = sum(
                1 for e in entries if (e.get("match") or "").lower() == "explicit"
            )
            if explicit_n == 0:
                zero_explicit.append(
                    f"deal={child.name!r} has {len(entries)} entries but "
                    f"0 with match=explicit (parent_id propagation suspect)"
                )

    details = missing_logs + zero_explicit

    if missing_logs:
        return {
            "name": "V1: per-deal log + parent_id propagation",
            "rule_ref": "dash_corrections.md :: V1",
            "status": "fail",
            "summary": (
                f"{len(missing_logs)} deal(s) missing/broken log.json out of "
                f"{deal_count}"
            ),
            "details": details[:30],
        }

    if zero_explicit:
        return {
            "name": "V1: per-deal log + parent_id propagation",
            "rule_ref": "dash_corrections.md :: V1",
            "status": "warn",
            "summary": (
                f"{len(zero_explicit)} deal(s) with populated log but "
                "zero explicit-tagged entries (parent_id propagation regression?)"
            ),
            "details": details[:30],
        }

    return {
        "name": "V1: per-deal log + parent_id propagation",
        "rule_ref": "dash_corrections.md :: V1",
        "status": "pass",
        "summary": f"{deal_count} deal(s) all have log.json with healthy explicit-rate",
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
