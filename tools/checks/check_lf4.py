#!/usr/bin/env python3
"""
check_lf4.py — Rule LF4 (L0046): 90-day _Unsorted → _Archive sweep.

Runtime inspection. Each applied organizer run appends a log line to
~/dashboards/logs/local-organizer.log containing `archived N`. Presence
of the field proves the sweep was checked even if N=0. We also verify
at least one `_Archive` destination dir exists under a monitored folder.

Status:
  - pass: at least one organizer run in the last 7d logged `archived N`
  - warn: log exists but no runs in 7d (or no archive activity in 30d)
  - fail: log missing entirely (LaunchAgent never fired) OR no
          _Archive destination dir exists under any monitored folder
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

HOME = Path.home()
LOG_PATH = HOME / "dashboards" / "logs" / "local-organizer.log"
MONITORED = [HOME / "Downloads", HOME / "Desktop", HOME / "Documents"]

# Canonical log line:
#   "2026-05-21 · scanned N · ... · archived N"
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
_ARCHIVED_RE = re.compile(r"\barchived\s+(\d+)\b")


def _parse_log_tail(n: int = 50) -> list[tuple[date, int]]:
    if not LOG_PATH.exists():
        return []
    try:
        lines = [ln for ln in LOG_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
    out: list[tuple[date, int]] = []
    for ln in lines[-n:]:
        dm, am = _DATE_RE.match(ln), _ARCHIVED_RE.search(ln)
        if not dm or not am:
            continue
        try:
            d = datetime.strptime(dm.group(1), "%Y-%m-%d").date()
            out.append((d, int(am.group(1))))
        except ValueError:
            continue
    return out


def run() -> dict[str, Any]:
    archive_present = {str(f): (f / "_Archive").is_dir() for f in MONITORED}
    any_dir = any(archive_present.values())

    if not LOG_PATH.exists():
        return {"name": "LF4: 90-day archive sweep", "rule_ref": "LF4",
                "status": "fail",
                "summary": f"organizer log missing — LaunchAgent never fired: {LOG_PATH}",
                "details": {"log_path": str(LOG_PATH),
                            "archive_dirs_present": archive_present}}
    if not any_dir:
        return {"name": "LF4: 90-day archive sweep", "rule_ref": "LF4",
                "status": "fail",
                "summary": "no _Archive destination dir under any monitored folder",
                "details": {"archive_dirs_present": archive_present,
                            "log_path": str(LOG_PATH)}}

    runs = _parse_log_tail(n=50)
    today = date.today()
    runs_7d = [r for r in runs if (today - r[0]) <= timedelta(days=7)]
    runs_30d = [r for r in runs if (today - r[0]) <= timedelta(days=30)]
    archived_30d = sum(n for _, n in runs_30d)
    last_archive = max((d for d, n in runs if n > 0), default=None)

    if runs_7d:
        status = "pass"
        summary = (f"sweep ran {len(runs_7d)}x in last 7d "
                   f"(archived total 30d={archived_30d})")
    elif runs and (today - runs[-1][0]) <= timedelta(days=30):
        status = "warn"
        summary = (f"no runs in 7d (last {runs[-1][0]}, archived 30d={archived_30d})")
    else:
        status = "warn"
        summary = "no organizer runs logged in last 30d — LaunchAgent stalled?"

    return {"name": "LF4: 90-day archive sweep", "rule_ref": "LF4",
            "status": status, "summary": summary,
            "details": {"log_path": str(LOG_PATH),
                        "archive_dirs_present": archive_present,
                        "runs_in_last_7d": len(runs_7d),
                        "runs_in_last_30d": len(runs_30d),
                        "archived_total_last_30d": archived_30d,
                        "last_archive_event": last_archive.isoformat() if last_archive else None,
                        "last_run_date": runs[-1][0].isoformat() if runs else None}}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
