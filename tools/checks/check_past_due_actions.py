#!/usr/bin/env python3
"""
check_past_due_actions.py
==========================

Enforces dash_corrections.md :: 2026-05-04 past-due deal action sweep.

Every action in deal-system-data.json's deals[].actions[] with status
∈ {open, in-progress} and `due` < today must be classified at the next
/dash audit (Resolved / Superseded / Stage-graduated / Rolled forward
/ Blocked). Leaving a past-due open action is silent rot — it makes
the dashboard look stale and trains the user to ignore the dates.

This check surfaces every past-due open action so a system_health pass
catches the rot before the user sees it on the dashboard.

Status:
  - "fail" when ANY past-due open/in-progress action exists.
  - "warn" when actions have no `due` field at all.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

HOME = Path.home()
DEAL_SYSTEM = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"

_OPEN_STATES = {"open", "in-progress", "in_progress", "active"}


def run() -> dict[str, Any]:
    if not DEAL_SYSTEM.exists():
        return {
            "name": "past-due deal actions",
            "rule_ref": "dash_corrections.md :: 2026-05-04 past-due deal action sweep",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [f"missing: {DEAL_SYSTEM}"],
        }

    try:
        ds = json.loads(DEAL_SYSTEM.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "past-due deal actions",
            "rule_ref": "dash_corrections.md :: 2026-05-04 past-due deal action sweep",
            "status": "fail",
            "summary": f"unreadable: {exc}",
            "details": [str(exc)],
        }

    today = date.today().isoformat()
    past_due: list[str] = []
    no_date: list[str] = []
    total_open = 0

    for deal in (ds.get("deals") or []):
        for a in (deal.get("actions") or []):
            status = (a.get("status") or "").lower()
            if status not in _OPEN_STATES:
                continue
            total_open += 1
            due = (a.get("due") or "").strip()
            if not due:
                no_date.append(
                    f"deal={deal.get('name')!r} action={(a.get('action') or '')[:80]!r} "
                    "(no due date)"
                )
                continue
            if due < today:
                past_due.append(
                    f"deal={deal.get('name')!r} due={due} status={status!r} "
                    f"action={(a.get('action') or '')[:100]!r}"
                )

    if past_due:
        return {
            "name": "past-due deal actions",
            "rule_ref": "dash_corrections.md :: 2026-05-04 past-due deal action sweep",
            "status": "fail",
            "summary": (
                f"{len(past_due)} past-due open action(s) "
                f"(out of {total_open} open)"
            ),
            "details": past_due[:30],
        }
    if no_date:
        return {
            "name": "past-due deal actions",
            "rule_ref": "dash_corrections.md :: 2026-05-04 past-due deal action sweep",
            "status": "warn",
            "summary": f"{len(no_date)} open action(s) with no due date",
            "details": no_date[:20],
        }

    return {
        "name": "past-due deal actions",
        "rule_ref": "dash_corrections.md :: 2026-05-04 past-due deal action sweep",
        "status": "pass",
        "summary": f"all {total_open} open action(s) have future due dates",
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
