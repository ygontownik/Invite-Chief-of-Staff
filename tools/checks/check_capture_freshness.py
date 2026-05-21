#!/usr/bin/env python3
"""
check_capture_freshness.py
==========================

Enforces dash_corrections.md :: 2026-05-04 captureSummary freshness
assertion + I4 capture-staleness chip.

WHAT FRESHNESS MEANS
--------------------
The intent is "did the daily ingest pipelines run today?" The original
implementation read `briefingSynopsis.captureSummary.date` from
dashboard-data.json — but that field is a fossil written by a
deprecated legacy task (morning-briefing). The current cos-pipeline
ships two daily writers:

  - cos_capture_pipeline (7:22am)  → email + calendar capture
  - cos_personal_briefing (7:51am) → briefing + market roll-up

Both LaunchAgents log to ~/dashboards/logs/<name>.log with
"--- TIMESTAMP run ---" and "TIMESTAMP exit=N" markers. The most
recent successful run (`exit=0`) is the real freshness signal.

This check now reads the most recent successful run from each pipeline
log and applies the staleness rules below. Falls back to the legacy
captureSummary.date / lastBriefingDate fields when log files are
missing (e.g., on a fresh subscriber install where logs haven't
accumulated yet).

SEVERITY
--------
  fresh   : both pipelines ran successfully today               → pass
  warn    : at least one ran in the last 3 days                 → warn
  stale   : both pipelines failed or haven't run in >3 days     → fail
  unknown : no log files and no fallback dates available        → warn
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"
LOG_DIR = HOME / "dashboards" / "logs"

PIPELINES = {
    "cos-capture-pipeline":  LOG_DIR / "cos-capture-pipeline.log",
    "cos-personal-briefing": LOG_DIR / "cos-personal-briefing.log",
}

# Format from runner shell scripts:
#   --- 2026-05-05T08:42:20-04:00 run ---
#   ... lines of pipeline output ...
#   2026-05-05T08:46:16-04:00 exit=0
_RUN_LINE = re.compile(r"^---\s+(\S+)\s+run\s+---")
_EXIT_LINE = re.compile(r"^(\S+)\s+exit=(\d+)\s*$")


def _last_successful_run(log_path: Path) -> Optional[date]:
    """Return the date of the most recent `exit=0` line in `log_path`,
    or None if no successful run is found."""
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    last_ok: Optional[date] = None
    for line in text.splitlines():
        m = _EXIT_LINE.match(line)
        if not m:
            continue
        if m.group(2) != "0":
            continue
        ts = m.group(1)
        try:
            dt = datetime.fromisoformat(ts[:25])
            d = dt.date()
            if last_ok is None or d > last_ok:
                last_ok = d
        except Exception:
            continue
    return last_ok


def _legacy_dates_from_dashboard_data() -> dict[str, str]:
    """Fallback for installs without log files yet — read the schema
    fields the original check looked at."""
    if not DASHBOARD_DATA.exists():
        return {}
    try:
        d = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    except Exception:
        return {}
    bs = d.get("briefingSynopsis") or {}
    cs = bs.get("captureSummary") or {}
    out = {}
    if cs.get("date"):
        out["captureSummary.date"] = str(cs["date"])[:10]
    if bs.get("lastBriefingDate"):
        out["lastBriefingDate"] = str(bs["lastBriefingDate"])[:10]
    return out


def run() -> dict[str, Any]:
    today = date.today()
    pipeline_runs: dict[str, Optional[date]] = {}
    for name, path in PIPELINES.items():
        pipeline_runs[name] = _last_successful_run(path)

    # Compute oldest "days stale" across the pipelines that exist
    days_stale_per: dict[str, int] = {}
    for name, last in pipeline_runs.items():
        if last is None:
            continue
        days_stale_per[name] = (today - last).days

    # If we have any log signal, drive the check from that
    if days_stale_per:
        worst = max(days_stale_per.values())
        best = min(days_stale_per.values())

        # FRESH: both ran successfully today (or only one pipeline
        # exists and it ran today).
        if worst <= 1:
            status, severity = "pass", "fresh"
        elif best <= 1 and worst <= 3:
            # One pipeline current, the other 2-3 days behind
            status, severity = "warn", "one-pipeline-stale"
        elif worst <= 3:
            status, severity = "warn", "warn"
        else:
            status, severity = "fail", "stale"

        details = [f"today: {today.isoformat()}"]
        for name, last in pipeline_runs.items():
            if last is None:
                details.append(f"{name}: no successful run found in log")
            else:
                details.append(
                    f"{name}: last exit=0 at {last.isoformat()} "
                    f"({(today - last).days} day(s) ago)"
                )
        if status != "pass":
            details.append(
                "→ check ~/dashboards/logs/*.log; if a pipeline failed, "
                "re-run via the launch agent or runner script."
            )

        return {
            "name": "captureSummary freshness",
            "rule_ref": "dash_corrections.md :: CF1 :: 2026-05-04 captureSummary freshness",
            "status": status,
            "summary": (
                f"pipelines: "
                + ", ".join(
                    f"{n.split('-')[-1]}={d}d"
                    for n, d in sorted(days_stale_per.items())
                )
                + f" (severity={severity})"
            ),
            "details": details,
        }

    # No log signal — fall back to dashboard-data.json fields
    legacy = _legacy_dates_from_dashboard_data()
    if not legacy:
        return {
            "name": "captureSummary freshness",
            "rule_ref": "dash_corrections.md :: CF1 :: 2026-05-04 captureSummary freshness",
            "status": "warn",
            "summary": "no pipeline logs and no dashboard freshness fields present",
            "details": [
                f"checked: {[str(p) for p in PIPELINES.values()]}",
                f"checked: {DASHBOARD_DATA}",
            ],
        }

    # Use the freshest legacy date as the signal
    best_date: Optional[date] = None
    best_field = ""
    for k, v in legacy.items():
        try:
            d0 = datetime.fromisoformat(v[:10]).date()
            if best_date is None or d0 > best_date:
                best_date = d0
                best_field = k
        except Exception:
            continue

    if best_date is None:
        return {
            "name": "captureSummary freshness",
            "rule_ref": "dash_corrections.md :: CF1 :: 2026-05-04 captureSummary freshness",
            "status": "warn",
            "summary": "legacy freshness fields present but unparseable",
            "details": [f"raw: {legacy}"],
        }

    days_stale = (today - best_date).days
    if days_stale <= 1:
        status, severity = "pass", "fresh"
    elif days_stale <= 3:
        status, severity = "warn", "warn"
    else:
        status, severity = "fail", "stale"

    return {
        "name": "captureSummary freshness",
        "rule_ref": "dash_corrections.md :: CF1 :: 2026-05-04 captureSummary freshness",
        "status": status,
        "summary": (
            f"{best_field}={best_date.isoformat()} "
            f"({days_stale}d stale, severity={severity}; pipeline logs absent)"
        ),
        "details": [
            f"today: {today.isoformat()}",
            f"freshest legacy field: {best_field} = {best_date.isoformat()}",
            f"days stale: {days_stale}",
            f"severity: {severity}",
            "→ no pipeline log files at ~/dashboards/logs/*.log; "
            "this looks like a fresh install or logs were rotated.",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
