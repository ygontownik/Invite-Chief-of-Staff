#!/usr/bin/env python3
"""
check_loose_local_files.py
==========================

Enforces LEARNINGS-LEDGER LF1 — no loose files at the top level of
~/Downloads, ~/Desktop, or ~/Documents after 24 hours. The
local_file_organizer.py LaunchAgent should route everything older than
24h into _Routed/_Junk/_Personal/_Unsorted/_Archive subfolders; this
check surfaces drift if the organizer hasn't run or its config is wrong.

Status:
  - "pass" if every monitored folder has <= LOOSE_OK_THRESHOLD loose files
    older than 24 hours at the top level.
  - "warn" if any folder exceeds the threshold but stays under HARD_LIMIT.
  - "fail" if any folder has > HARD_LIMIT loose-old files, OR if the
    organizer state file is older than 36 hours (signal that the
    LaunchAgent stopped firing).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

HOME = Path.home()
MONITORED = [HOME / "Downloads", HOME / "Desktop", HOME / "Documents"]
ORGANIZER_STATE = HOME / "credentials" / "local_organizer_state.json"
SKIP_PREFIXES = ("_Routed", "_Junk", "_Personal", "_Unsorted", "_Archive", ".")

# Thresholds. Tuned so the check doesn't fire on the first day after the
# organizer is installed (when there's a one-time backlog) but starts
# nagging if drift persists.
LOOSE_OK_THRESHOLD = 20      # ≤20 loose-old at any folder = clean
HARD_LIMIT = 100             # >100 loose-old = fail
STATE_STALE_HOURS = 36       # state file older than this → fail


def _count_loose_old(folder: Path, cutoff_epoch: float) -> int:
    """Count immediate-child files at folder top that are older than cutoff
    by atime AND are not in a skip-prefix folder. Symlinks/dotfiles ignored."""
    if not folder.exists() or not folder.is_dir():
        return 0
    n = 0
    try:
        for child in folder.iterdir():
            if child.is_symlink():
                continue
            if not child.is_file():
                continue
            if child.name.startswith(SKIP_PREFIXES):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            if st.st_atime <= cutoff_epoch:
                n += 1
    except PermissionError:
        return 0
    return n


def run() -> dict[str, Any]:
    cutoff = time.time() - (24 * 3600)
    counts: dict[str, int] = {}
    for folder in MONITORED:
        counts[str(folder)] = _count_loose_old(folder, cutoff)

    # Organizer state freshness
    state_warn = False
    state_fail = False
    state_age_hours: float | None = None
    if ORGANIZER_STATE.exists():
        try:
            age_s = time.time() - ORGANIZER_STATE.stat().st_mtime
            state_age_hours = age_s / 3600.0
            if state_age_hours > STATE_STALE_HOURS:
                state_fail = True
        except OSError:
            state_warn = True
    else:
        # Not yet installed — warn (this is fine in the install gap)
        state_warn = True

    over_warn = [f for f, n in counts.items() if n > LOOSE_OK_THRESHOLD]
    over_hard = [f for f, n in counts.items() if n > HARD_LIMIT]

    if over_hard or state_fail:
        status = "fail"
    elif over_warn or state_warn:
        status = "warn"
    else:
        status = "pass"

    bits = [f"{Path(f).name}:{n}" for f, n in counts.items()]
    summary = "loose_local_files: " + ", ".join(bits)
    if state_age_hours is not None:
        summary += f" · organizer state {state_age_hours:.0f}h old"
    elif state_warn:
        summary += " · organizer state missing (LaunchAgent not yet installed?)"

    return {
        "name": "loose_local_files",
        "rule_ref": "LF1",
        "status": status,
        "summary": summary,
        "details": {
            "counts": counts,
            "loose_ok_threshold": LOOSE_OK_THRESHOLD,
            "hard_limit": HARD_LIMIT,
            "organizer_state_path": str(ORGANIZER_STATE),
            "organizer_state_age_hours": state_age_hours,
            "over_warn": over_warn,
            "over_hard": over_hard,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
