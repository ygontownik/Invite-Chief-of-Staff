#!/usr/bin/env python3
"""
check_next_milestone.py
========================

Enforces dash_corrections.md :: 2026-05-04 — `next_milestone` must
always reference a future date.

Every active deal in deal-system-data.json's deals[] must have
`next_milestone_due >= today`. A past `next_milestone_due` means the
milestone happened (close it; queue the next) or slipped (roll forward
with explicit reasoning). Leaving a past date renders stale to the
deal-pipeline mini-card.

The rule itself supplies the detection one-liner; this just wraps it.

Status:
  - "fail" when any active (non-Dormant, non-Live) deal has past
    next_milestone_due.
  - "warn" when active deal has empty next_milestone_due.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

HOME = Path.home()
DEAL_SYSTEM = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"

# Stages where a forward-looking milestone is mandatory.
_ACTIVE_STAGES = {"Watch", "Sourcing", "Active Bid", "Diligence", "Advisory", "Memo", "IC"}


def run() -> dict[str, Any]:
    if not DEAL_SYSTEM.exists():
        return {
            "name": "next_milestone: future-date discipline",
            "rule_ref": "dash_corrections.md :: NM1 :: 2026-05-04 next_milestone rule",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [f"missing: {DEAL_SYSTEM}"],
        }

    try:
        ds = json.loads(DEAL_SYSTEM.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "next_milestone: future-date discipline",
            "rule_ref": "dash_corrections.md :: NM1 :: 2026-05-04 next_milestone rule",
            "status": "fail",
            "summary": f"unreadable: {exc}",
            "details": [str(exc)],
        }

    today = date.today().isoformat()
    past: list[str] = []
    empty: list[str] = []
    deals = ds.get("deals") or []
    active = 0

    for d in deals:
        stage = (d.get("stage") or "").strip()
        if stage and stage not in _ACTIVE_STAGES:
            continue
        active += 1
        nmd = (d.get("next_milestone_due") or "").strip()
        if not nmd:
            empty.append(f"deal={d.get('name')!r} stage={stage!r} has empty next_milestone_due")
            continue
        if nmd < today:
            past.append(
                f"deal={d.get('name')!r} stage={stage!r} "
                f"next_milestone_due={nmd!r} (today={today!r}) "
                f"next_milestone={(d.get('next_milestone') or '')[:80]!r}"
            )

    if past:
        return {
            "name": "next_milestone: future-date discipline",
            "rule_ref": "dash_corrections.md :: NM1 :: 2026-05-04 next_milestone rule",
            "status": "fail",
            "summary": (
                f"{len(past)} active deal(s) have past next_milestone_due "
                f"(out of {active})"
            ),
            "details": past + empty[:10],
        }
    if empty:
        return {
            "name": "next_milestone: future-date discipline",
            "rule_ref": "dash_corrections.md :: NM1 :: 2026-05-04 next_milestone rule",
            "status": "warn",
            "summary": f"{len(empty)} active deal(s) with empty next_milestone_due",
            "details": empty,
        }

    return {
        "name": "next_milestone: future-date discipline",
        "rule_ref": "dash_corrections.md :: NM1 :: 2026-05-04 next_milestone rule",
        "status": "pass",
        "summary": f"all {active} active deal(s) carry future next_milestone_due",
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
