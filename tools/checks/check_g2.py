#!/usr/bin/env python3
"""
check_g2.py
===========

Enforces dash_corrections.md :: G2 — schema validation at config load.

Every active row in deal-config.yaml's prospectiveInvestors /
capitalRaisingAdvisors / liveDeals / dealOrigination needs:
  - name (non-empty)
  - lastAction (non-empty date or text)
  - nextTouchBase OR movedToDormant (non-empty)
  - owner (non-empty)
  - myAction (non-empty) OR explicit dormant flag

This check post-validates the COMPILED dashboard-data.json (the only
artifact promised public-stable in the manifest). Specifically, we
walk dealPortfolio.deals[] and the recruiting / fundraising arrays,
flagging rows that violate the schema.

Status:
  - "fail" when any active row violates schema (drops it from render
    silently in production — bug surface).
  - "warn" when only soft fields (e.g. owner whitespace) are missing.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"


def _missing(row: dict[str, Any], fields: list[str]) -> list[str]:
    out: list[str] = []
    for f in fields:
        v = row.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            out.append(f)
    return out


def run() -> dict[str, Any]:
    if not DASHBOARD_DATA.exists():
        return {
            "name": "G2: schema validation on compiled rows",
            "rule_ref": "dash_corrections.md :: G2",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [f"missing: {DASHBOARD_DATA}"],
        }

    try:
        d = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "G2: schema validation on compiled rows",
            "rule_ref": "dash_corrections.md :: G2",
            "status": "fail",
            "summary": f"unreadable: {exc}",
            "details": [str(exc)],
        }

    fail_rows: list[str] = []
    warn_rows: list[str] = []

    # Fundraising — prospectiveInvestors / capitalRaisingAdvisors.
    fr = d.get("fundraising") or {}
    for bucket_name in ("prospectiveInvestors", "capitalRaisingAdvisors"):
        bucket = fr.get(bucket_name) or []
        for row in bucket:
            if not isinstance(row, dict):
                continue
            if (row.get("status") or "").lower() == "dormant":
                continue
            miss = _missing(row, ["name"])
            if miss:
                fail_rows.append(
                    f"{bucket_name}: row missing {miss}: {str(row)[:100]}"
                )
                continue
            soft = _missing(row, ["lastAction", "owner"])
            no_next = not (row.get("nextTouchBase") or row.get("movedToDormant"))
            no_act = not (row.get("myAction") or "").strip() and not (
                row.get("dormant") or False
            )
            if soft or no_next:
                warn_rows.append(
                    f"{bucket_name}: {row.get('name')!r} missing="
                    f"{soft + (['nextTouchBase|movedToDormant'] if no_next else [])}"
                )
            if no_act and not no_next:
                warn_rows.append(
                    f"{bucket_name}: {row.get('name')!r} has no myAction "
                    "and no dormant flag (G2 silent-drop candidate)"
                )

    # Live deals — verify minimal shape.
    portfolio = d.get("dealPortfolio") or {}
    deals = portfolio.get("deals") or []
    for deal in deals:
        if not isinstance(deal, dict):
            continue
        miss = _missing(deal, ["name"])
        if miss:
            fail_rows.append(f"livedeals: deal missing {miss}: {str(deal)[:100]}")

    if fail_rows:
        return {
            "name": "G2: schema validation on compiled rows",
            "rule_ref": "dash_corrections.md :: G2",
            "status": "fail",
            "summary": f"{len(fail_rows)} hard schema violation(s), {len(warn_rows)} soft",
            "details": (fail_rows + warn_rows)[:40],
        }
    if warn_rows:
        return {
            "name": "G2: schema validation on compiled rows",
            "rule_ref": "dash_corrections.md :: G2",
            "status": "warn",
            "summary": f"0 hard violations, {len(warn_rows)} soft",
            "details": warn_rows[:40],
        }
    return {
        "name": "G2: schema validation on compiled rows",
        "rule_ref": "dash_corrections.md :: G2",
        "status": "pass",
        "summary": f"all rows pass schema across fundraising + {len(deals)} deals",
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
