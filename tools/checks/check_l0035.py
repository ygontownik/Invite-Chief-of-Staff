#!/usr/bin/env python3
"""check_l0035.py — Rule L0035: deal status doc discipline (≤8KB).

Rule L0035: Deal status docs must stay ≤8KB. /deal-sync produces complete
clean rewrites (not patches) to keep them small.

Check logic:
  - Scan ~/dashboards/data/deals/*/{status.md,deal.md} — preferring
    status.md if both exist (status.md is the canonical name per spec;
    deal.md is the current local-snapshot artifact). Skip underscore-
    prefixed sentinel dirs (_inbox, _towers).
  - Bucket each file: <=8KB ok, 8-12KB warn, >12KB fail.
  - Aggregate: PASS if all ≤8KB; WARN if any 8-12KB; FAIL if any >12KB.

Returns counts and the over-limit file list with size in KB.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
DEALS_DIR = HOME / "dashboards" / "data" / "deals"

WARN_BYTES = 8 * 1024     # 8 KB
FAIL_BYTES = 12 * 1024    # 12 KB

# Per-deal candidate filenames in preference order.
_STATUS_CANDIDATES = ("status.md", "deal.md")


def run() -> dict[str, Any]:
    if not DEALS_DIR.exists():
        return {
            "name": "L0035: deal status doc discipline (≤8KB)",
            "rule_ref": "L0035",
            "status": "warn",
            "summary": f"deals directory missing: {DEALS_DIR}",
            "details": {"deals_dir": str(DEALS_DIR)},
        }

    total_deals = 0
    over_8kb: list[dict[str, Any]] = []
    over_12kb: list[dict[str, Any]] = []
    sizes: list[dict[str, Any]] = []

    for deal_dir in sorted(DEALS_DIR.iterdir()):
        if not deal_dir.is_dir():
            continue
        # Skip sentinel / non-deal dirs that start with underscore
        if deal_dir.name.startswith("_"):
            continue
        status_path: Path | None = None
        for candidate in _STATUS_CANDIDATES:
            cand = deal_dir / candidate
            if cand.exists():
                status_path = cand
                break
        if status_path is None:
            continue
        total_deals += 1
        try:
            size = status_path.stat().st_size
        except OSError:
            continue

        entry = {
            "deal": deal_dir.name,
            "path": str(status_path.relative_to(HOME)),
            "filename": status_path.name,
            "size_bytes": size,
            "size_kb": round(size / 1024, 2),
        }
        sizes.append(entry)
        if size > FAIL_BYTES:
            over_12kb.append(entry)
            over_8kb.append(entry)
        elif size > WARN_BYTES:
            over_8kb.append(entry)

    if over_12kb:
        status = "fail"
        summary = (
            f"L0035: {len(over_12kb)} deal status doc(s) >12KB "
            f"(of {total_deals} total)"
        )
    elif over_8kb:
        status = "warn"
        summary = (
            f"L0035: {len(over_8kb)} deal status doc(s) 8-12KB "
            f"(of {total_deals} total)"
        )
    else:
        status = "pass"
        summary = (
            f"L0035: all {total_deals} deal status doc(s) ≤8KB"
        )

    return {
        "name": "L0035: deal status doc discipline (≤8KB)",
        "rule_ref": "L0035",
        "status": status,
        "summary": summary,
        "details": {
            "total_deals": total_deals,
            "over_8kb": len(over_8kb),
            "over_12kb": len(over_12kb),
            "warn_bytes": WARN_BYTES,
            "fail_bytes": FAIL_BYTES,
            "list": sorted(over_8kb, key=lambda x: -x["size_bytes"])[:20],
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
