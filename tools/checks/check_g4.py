#!/usr/bin/env python3
"""
check_g4.py
===========

Enforces dash_corrections.md :: G4 — no orphan deal directories.

Every ~/dashboards/data/deals/<slug>/ MUST contain at minimum:
  - deal.md
  - actions.md
  - LPs.md
  - TERMS.md

Missing files signal a half-created deal. Per the rule, compile logs
to stderr and continues; this check surfaces the same finding outside
of compile so a system_health pass catches it.

Status:
  - "fail" if any deal directory is missing any required file.
  - "warn" if data root missing.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
DEALS_ROOT = HOME / "dashboards" / "data" / "deals"

REQUIRED = ("deal.md", "actions.md", "LPs.md", "TERMS.md")


def run() -> dict[str, Any]:
    if not DEALS_ROOT.exists():
        return {
            "name": "G4: no orphan deal directories",
            "rule_ref": "dash_corrections.md :: G4",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [f"missing: {DEALS_ROOT}"],
        }

    missing: list[str] = []
    deal_count = 0
    for child in sorted(DEALS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue
        deal_count += 1
        for fname in REQUIRED:
            if not (child / fname).exists():
                missing.append(f"deal={child.name!r} missing {fname}")

    if missing:
        return {
            "name": "G4: no orphan deal directories",
            "rule_ref": "dash_corrections.md :: G4",
            "status": "fail",
            "summary": f"{len(missing)} missing required file(s) across {deal_count} deals",
            "details": missing[:30],
        }

    return {
        "name": "G4: no orphan deal directories",
        "rule_ref": "dash_corrections.md :: G4",
        "status": "pass",
        "summary": f"all {deal_count} deal(s) carry required files {REQUIRED}",
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
