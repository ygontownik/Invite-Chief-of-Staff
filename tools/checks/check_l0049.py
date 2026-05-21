#!/usr/bin/env python3
"""check_l0049.py — Rule L0049: followUps `who` field must be the counterparty.

The dashboard's followUps array uses `who` to mean "who I'm waiting on /
who needs to act externally", NOT "who owns the action on my side". When
`who` collapses to the principal's first name (e.g. "Yoni"), it means
the writer mistakenly stored the action owner instead of the
counterparty — the dashboard then can't render "awaiting external" cues
correctly.

This check loads the principal name dynamically from firm_context.yaml
(Rule PD1 — multi-tenant safety) and flags any followUp whose `who`
equals the principal's first name (case-insensitive).

Status:
  pass — 0 violations
  warn — 1-5 violations
  fail — >5 violations
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"
PIPELINE_DIR = HOME / "cos-pipeline"


def _load_principal_first() -> str | None:
    """Return the principal's first-name token, lowercased. None if not loadable."""
    try:
        if str(PIPELINE_DIR) not in sys.path:
            sys.path.insert(0, str(PIPELINE_DIR))
        import _firm_context as _fc  # type: ignore
        ctx = _fc.load_firm_context()
    except Exception:
        return None
    principal = (ctx or {}).get("principal") or {}
    name = (principal.get("name") or "").strip()
    if not name:
        return None
    return name.split()[0].lower()


def run() -> dict[str, Any]:
    if not DASHBOARD_DATA.exists():
        return {
            "name": "L0049: followUps `who` semantics",
            "rule_ref": "L0049",
            "status": "warn",
            "summary": f"dashboard-data.json not present: {DASHBOARD_DATA}",
            "details": {"path": str(DASHBOARD_DATA)},
        }

    principal_first = _load_principal_first()
    if not principal_first:
        return {
            "name": "L0049: followUps `who` semantics",
            "rule_ref": "L0049",
            "status": "warn",
            "summary": "could not resolve principal first-name from firm_context",
            "details": {"hint": "ensure ~/cos-pipeline-config-*/firm_context.yaml :: principal.name"},
        }

    try:
        data = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "L0049: followUps `who` semantics",
            "rule_ref": "L0049",
            "status": "fail",
            "summary": f"dashboard-data.json unreadable: {exc}",
            "details": {"error": str(exc)},
        }

    follow_ups = data.get("followUps") or []
    violations: list[dict[str, Any]] = []
    for fu in follow_ups:
        if not isinstance(fu, dict):
            continue
        who = (fu.get("who") or "").strip()
        if not who:
            continue
        if who.lower() == principal_first:
            violations.append({
                "id": fu.get("id"),
                "who": who,
                "what": (fu.get("what") or "")[:120],
                "source": (fu.get("source") or "")[:80],
            })

    n = len(violations)
    if n == 0:
        status = "pass"
    elif n <= 5:
        status = "warn"
    else:
        status = "fail"

    return {
        "name": "L0049: followUps `who` semantics",
        "rule_ref": "L0049",
        "status": status,
        "summary": (
            f"L0049: {n} followUp(s) with who={principal_first.title()!r} "
            f"(should be counterparty), out of {len(follow_ups)} total"
        ),
        "details": {
            "principal_first": principal_first,
            "total_follow_ups": len(follow_ups),
            "violation_count": n,
            "violations": violations[:20],
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
