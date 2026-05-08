#!/usr/bin/env python3
"""
check_alias_precision.py
========================

Read every per-deal `log.json` under ~/dashboards/data/deals/<slug>/
and compute, for each deal, the share of entries whose `match` field
equals "token". A high token ratio means upstream extraction is missing
explicit `parent_id` links and the compiler had to fall back to
substring search — fragile and easy to misroute.

Threshold: any deal with token_rate > 60% surfaces as a warning. The
audit shape mirrors ~/dashboards/docs/ALIAS_AUDIT_2026-05-05.md.

Status semantics:
  - "pass" : every deal has token_rate <= 60%
  - "warn" : at least one deal exceeds 60%
  - "fail" : no logs were readable at all (likely a path / permission
             problem, not a precision issue)

Run standalone:
  python3 ~/cos-pipeline/tools/checks/check_alias_precision.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
DEALS_ROOT = HOME / "dashboards" / "data" / "deals"
THRESHOLD = 0.60  # 60% token rate flips a deal to warn


def _score_one(log_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "deal_id": log_path.parent.name,
            "error": f"unreadable log.json: {exc}",
        }
    # log.json shape varies: bare list (most deals) OR dict with "entries" key.
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("entries") or []
    else:
        entries = []
    total = len(entries)
    if total == 0:
        return {
            "deal_id": (data.get("deal_id") if isinstance(data, dict) else None) or log_path.parent.name,
            "total": 0,
            "token": 0,
            "explicit": 0,
            "other": 0,
            "token_rate": 0.0,
        }
    token = sum(1 for e in entries if e.get("match") == "token")
    explicit = sum(1 for e in entries if e.get("match") == "explicit")
    other = total - token - explicit
    return {
        "deal_id": (data.get("deal_id") if isinstance(data, dict) else None) or log_path.parent.name,
        "total": total,
        "token": token,
        "explicit": explicit,
        "other": other,
        "token_rate": round(token / total, 4),
    }


def run() -> dict[str, Any]:
    if not DEALS_ROOT.exists():
        return {
            "name": "alias_precision",
            "status": "fail",
            "summary": f"alias_precision: deals dir not found at ~/{DEALS_ROOT.relative_to(HOME)}",
            "details": {"per_deal": []},
        }

    per_deal: list[dict[str, Any]] = []
    for deal_dir in sorted(DEALS_ROOT.iterdir()):
        if not deal_dir.is_dir():
            continue
        if deal_dir.name.startswith("_"):
            continue  # skip _inbox and similar holding bins
        log_path = deal_dir / "log.json"
        if not log_path.exists():
            continue
        scored = _score_one(log_path)
        if scored is not None:
            per_deal.append(scored)

    if not per_deal:
        return {
            "name": "alias_precision",
            "status": "fail",
            "summary": "alias_precision: zero deal logs readable",
            "details": {"per_deal": []},
        }

    flagged = [
        d for d in per_deal
        if "token_rate" in d and d["token_rate"] > THRESHOLD
    ]
    errored = [d for d in per_deal if "error" in d]

    if flagged:
        status = "warn"
        summary = (
            f"alias_precision: {len(flagged)} deal(s) over "
            f"{int(THRESHOLD * 100)}% token-match rate "
            f"({', '.join(d['deal_id'] for d in flagged)})"
        )
    elif errored:
        status = "warn"
        summary = (
            f"alias_precision: {len(errored)} deal log(s) unreadable; "
            f"others all under {int(THRESHOLD * 100)}%"
        )
    else:
        status = "pass"
        summary = (
            f"alias_precision: all {len(per_deal)} deal log(s) under "
            f"{int(THRESHOLD * 100)}% token-match rate"
        )

    return {
        "name": "alias_precision",
        "status": status,
        "summary": summary,
        "details": {
            "threshold": THRESHOLD,
            "per_deal": per_deal,
            "flagged": [d["deal_id"] for d in flagged],
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
