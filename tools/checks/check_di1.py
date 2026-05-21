#!/usr/bin/env python3
"""check_di1.py — Rule DI1: deal-intel emission.

Rule DI1 (L0007): When discussing any registered TCIP deal in any
session, emit `---DEAL-INTEL---` blocks throughout the session. The
Claude Code Stop hook routes them to the correct deal's log.json
automatically via intel_capture.py.

Check logic:
  - Verify ~/dashboards/data/intel_capture_state.json exists and has been
    updated within 24h (proxy: the hook is firing).
  - Count intel-source log.json entries across all deals in the last
    7 days; if zero, downgrade to warn (hook might emit empties).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()
STATE = HOME / "dashboards" / "data" / "intel_capture_state.json"
DEALS_DIR = HOME / "dashboards" / "data" / "deals"
WARN_LAG_SEC = 24 * 3600


def _latest_scan_ts(state: dict[str, Any]) -> float | None:
    """Walk the nested state file and return the newest last_scan epoch."""
    newest: float | None = None
    # Schema: {tool: {session_path: {captured: [...], last_scan: ISO}}}
    for tool_val in state.values() if isinstance(state, dict) else []:
        if not isinstance(tool_val, dict):
            continue
        for sess_val in tool_val.values():
            if not isinstance(sess_val, dict):
                continue
            ls = sess_val.get("last_scan")
            if not ls:
                continue
            try:
                # Tolerate both with-and-without timezone
                dt = datetime.fromisoformat(str(ls).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
            except Exception:
                continue
            if newest is None or ts > newest:
                newest = ts
    return newest


def _count_recent_intel_entries(days: int = 7) -> tuple[int, int]:
    """Return (intel_entry_count, deals_scanned) across last `days`."""
    if not DEALS_DIR.exists():
        return (0, 0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    intel_count = 0
    deals_scanned = 0
    for deal_dir in DEALS_DIR.iterdir():
        if not deal_dir.is_dir():
            continue
        log_path = deal_dir / "log.json"
        if not log_path.exists():
            continue
        deals_scanned += 1
        try:
            raw = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries = raw.get("entries", []) if isinstance(raw, dict) else []
        for e in entries:
            if not isinstance(e, dict):
                continue
            if e.get("source") != "intel":
                continue
            d = e.get("date") or ""
            try:
                dt = datetime.fromisoformat(str(d)[:10]).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if dt >= cutoff:
                intel_count += 1
    return (intel_count, deals_scanned)


def run() -> dict[str, Any]:
    if not STATE.exists():
        return {
            "name": "DI1: deal-intel emission",
            "rule_ref": "DI1",
            "status": "fail",
            "summary": f"intel_capture_state.json missing — hook never ran ({STATE})",
            "details": {"state_path": str(STATE)},
        }

    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "DI1: deal-intel emission",
            "rule_ref": "DI1",
            "status": "fail",
            "summary": f"intel_capture_state.json unreadable: {exc}",
            "details": {"error": str(exc)},
        }

    last_ts = _latest_scan_ts(state)
    now = time.time()
    age = (now - last_ts) if last_ts else None

    intel_count, deals_scanned = _count_recent_intel_entries(days=7)

    # Status resolution
    if last_ts is None:
        status = "fail"
        summary = "intel_capture_state.json has no last_scan timestamps"
    elif age is not None and age > WARN_LAG_SEC:
        status = "warn"
        hours = age / 3600
        summary = f"last intel scan {hours:.1f}h ago — hook may have stopped firing"
    elif intel_count == 0:
        status = "warn"
        summary = (
            f"intel hook is fresh but 0 intel entries in last 7 days "
            f"across {deals_scanned} deal(s)"
        )
    else:
        status = "pass"
        summary = (
            f"hook fresh ({age/3600:.1f}h ago); {intel_count} intel entries "
            f"across {deals_scanned} deal(s) in last 7 days"
        )

    return {
        "name": "DI1: deal-intel emission",
        "rule_ref": "DI1",
        "status": status,
        "summary": summary,
        "details": {
            "state_path": str(STATE),
            "last_scan_epoch": last_ts,
            "last_scan_iso": (
                datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()
                if last_ts else None
            ),
            "age_seconds": int(age) if age is not None else None,
            "warn_lag_sec": WARN_LAG_SEC,
            "intel_entries_last_7d": intel_count,
            "deals_scanned": deals_scanned,
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
