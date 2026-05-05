#!/usr/bin/env python3
"""
check_capture_freshness.py
==========================

Enforces dash_corrections.md :: 2026-05-04 captureSummary freshness
assertion + I4 capture-staleness chip.

The capture pipeline writes briefingSynopsis.captureSummary.date each
run. If the date is more than 1 day old, the capture pipeline didn't
run today — that's a silent pipeline failure that degrades the briefing.
Routines catch-up is supposed to run at every wake, so a stale capture
date is now an actionable signal of pipeline failure (not background
condition).

Severity mirrors the server-side captureStaleness shape:
  - fresh: captureSummary.date within 1 day of today → pass
  - warn:  2–3 days stale → warn
  - stale: >3 days stale → fail
  - unknown: no date present → warn

Status:
  - "fail" when stale (>3 days).
  - "warn" when warn or unknown.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"


def run() -> dict[str, Any]:
    if not DASHBOARD_DATA.exists():
        return {
            "name": "captureSummary freshness",
            "rule_ref": "dash_corrections.md :: 2026-05-04 captureSummary freshness",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [f"missing: {DASHBOARD_DATA}"],
        }

    try:
        d = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "captureSummary freshness",
            "rule_ref": "dash_corrections.md :: 2026-05-04 captureSummary freshness",
            "status": "fail",
            "summary": f"unreadable: {exc}",
            "details": [str(exc)],
        }

    bs = d.get("briefingSynopsis") or {}
    cs = bs.get("captureSummary") or {}
    cs_date = (cs.get("date") or "").strip()

    if not cs_date:
        return {
            "name": "captureSummary freshness",
            "rule_ref": "dash_corrections.md :: 2026-05-04 captureSummary freshness",
            "status": "warn",
            "summary": "captureSummary.date missing (severity=unknown)",
            "details": ["No briefingSynopsis.captureSummary.date in dashboard-data.json"],
        }

    try:
        d0 = datetime.fromisoformat(cs_date[:10]).date()
    except Exception:
        return {
            "name": "captureSummary freshness",
            "rule_ref": "dash_corrections.md :: 2026-05-04 captureSummary freshness",
            "status": "warn",
            "summary": f"captureSummary.date unparseable: {cs_date!r}",
            "details": [f"raw value: {cs_date!r}"],
        }

    days_stale = (date.today() - d0).days

    if days_stale <= 1:
        status = "pass"
        severity = "fresh"
    elif days_stale <= 3:
        status = "warn"
        severity = "warn"
    else:
        status = "fail"
        severity = "stale"

    return {
        "name": "captureSummary freshness",
        "rule_ref": "dash_corrections.md :: 2026-05-04 captureSummary freshness",
        "status": status,
        "summary": (
            f"captureSummary.date={cs_date} ({days_stale} days stale, "
            f"severity={severity})"
        ),
        "details": (
            []
            if status == "pass"
            else [
                f"captureSummary.date: {cs_date}",
                f"today: {date.today().isoformat()}",
                f"days_stale: {days_stale}",
                f"severity: {severity}",
                "→ COS capture pipeline likely did not run today; "
                "check /admin/#tab-routines",
            ]
        ),
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
