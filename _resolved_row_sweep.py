#!/usr/bin/env python3
"""
_resolved_row_sweep.py — Close the removal loop for every accumulating collection.

WHAT THIS SWEEPS
────────────────
followUps[] / awaitingExternal[]
    Drop rows marked RESOLVED (✅ RESOLVED, [RESOLVED], DONE — ) after a 2-day
    grace window so they appear in the morning briefing before disappearing.
    Also stamps _staleDays on past-due items so the dashboard can surface them.

originationInbox[] / dealIntel[] / themes[]
    Archive envelope items older than AGE_ARCHIVE_DAYS into *Archive[] arrays
    (originationArchive, dealIntelArchive, themesArchive). Never deleted — just
    moved out of the live inbox so they don't crowd current intel.

routingExceptions[]
    Prune validation failures older than ROUTING_EXCEPTIONS_PRUNE_DAYS. These
    are items that had missing required fields (no counterparty, no parent_id).
    Old ones are noise; recent ones deserve a fix pass.

build-backlog.json items[]
    Drop items where completedAt is set and > BUILD_BACKLOG_GRACE_DAYS old.
    Mark items complete via CLI: --complete BACKLOG_ID

RESOLUTION MARKERS (followUps / awaitingExternal):
    - "✅ RESOLVED"   — human-marked
    - "[RESOLVED]"    — pipeline-marked
    - "DONE — "       — pipeline-marked

Run nightly (wired into SKILL.md STEP 7 after every transcript run), or manually:

    python3 -m routines.process._resolved_row_sweep [--dry-run]
    python3 -m routines.process._resolved_row_sweep --complete BACKLOG_ID
    python3 -m routines.process._resolved_row_sweep --list-backlog

Returns exit 0 + JSON report on stderr.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

DASHBOARD_DATA_PATH    = Path.home() / "dashboards/data/compiled/dashboard-data.json"
BUILD_BACKLOG_PATH     = Path.home() / "dashboards/data/user-state/build-backlog.json"

GRACE_DAYS                     = 2    # resolved followUps/awaitingExternal
STALE_FLAG_DAYS                = 45   # past-due items get _staleDays stamped (not removed)
AGE_ARCHIVE_DAYS               = 90   # originationInbox / dealIntel / themes
ROUTING_EXCEPTIONS_PRUNE_DAYS  = 30   # validation failures older than this are dropped
BUILD_BACKLOG_GRACE_DAYS       = 1    # completedAt items removed after 1 day

RESOLUTION_MARKERS = ("✅ RESOLVED", "[RESOLVED]", "DONE — ", "DONE -- ")


# ── Date helpers ──────────────────────────────────────────────────────────────

def _is_resolved(text: str) -> bool:
    if not text: return False
    u = text.upper()
    return any(m.upper() in u for m in RESOLUTION_MARKERS)


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str: return None
    try:
        return datetime.fromisoformat(str(date_str)[:10])
    except (ValueError, TypeError):
        return None


def _past_grace(date_str: str | None, today: datetime, grace: int = GRACE_DAYS) -> bool:
    d = _parse_date(date_str)
    if d is None: return True          # no date → assume past grace
    return (today - d).days >= grace


def _item_date(item: dict) -> str | None:
    """Best-effort: pull the most useful date from an envelope item."""
    for field in ("addedDate", "added_at", "addedAt", "createdAt", "date"):
        if v := item.get(field):
            return str(v)[:10]
    sr = item.get("source_ref") or {}
    if v := sr.get("date"):
        return str(v)[:10]
    return None


def _item_age_days(item: dict, today: datetime) -> int | None:
    d = _parse_date(_item_date(item))
    return (today - d).days if d else None


def _exception_date(exc: dict) -> str | None:
    """routingExceptions wrap the real item under exc["item"]."""
    for field in ("added_at", "addedDate", "createdAt"):
        if v := exc.get(field): return str(v)[:10]
    inner = exc.get("item") or {}
    return _item_date(inner)


# ── Main sweep ────────────────────────────────────────────────────────────────

def sweep(dash: dict, today: datetime | None = None) -> dict:
    """Mutates `dash` in place. Returns a report dict."""
    today = today or datetime.now()
    report: dict = {"swept_at": today.isoformat(), "grace_days": GRACE_DAYS}

    # ── followUps ──────────────────────────────────────────────────────────────
    fu_before = len(dash.get("followUps", []))
    stale_stamped = 0
    new_fu: list[dict] = []
    for f in dash.get("followUps", []):
        if _is_resolved(f.get("what", "")) and _past_grace(
            f.get("addedDate") or f.get("due"), today
        ):
            continue  # drop
        # Stamp stale flag (not urgent, past-due > STALE_FLAG_DAYS)
        if not f.get("urgent"):
            d = _parse_date(f.get("due"))
            if d:
                days_past = (today - d).days
                if days_past >= STALE_FLAG_DAYS:
                    f["_staleDays"] = days_past
                    stale_stamped += 1
        new_fu.append(f)
    dash["followUps"] = new_fu
    report.update(
        followUps_removed=fu_before - len(new_fu),
        followUps_remaining=len(new_fu),
        followUps_stale_flagged=stale_stamped,
    )

    # ── awaitingExternal ───────────────────────────────────────────────────────
    ae_before = len(dash.get("awaitingExternal", []))
    ae_stale = 0
    new_ae: list[dict] = []
    for a in dash.get("awaitingExternal", []):
        if _is_resolved(a.get("content", "")) and _past_grace(
            a.get("addedDate") or a.get("due"), today
        ):
            continue
        d = _parse_date(a.get("due"))
        if d:
            days_past = (today - d).days
            if days_past >= STALE_FLAG_DAYS:
                a["_staleDays"] = days_past
                ae_stale += 1
        new_ae.append(a)
    dash["awaitingExternal"] = new_ae
    report.update(
        awaitingExternal_removed=ae_before - len(new_ae),
        awaitingExternal_remaining=len(new_ae),
        awaitingExternal_stale_flagged=ae_stale,
    )

    # ── originationInbox → originationArchive ──────────────────────────────────
    orig_before = len(dash.get("originationInbox", []))
    keep_orig, arch_orig = [], []
    for item in dash.get("originationInbox", []):
        age = _item_age_days(item, today)
        if age is not None and age >= AGE_ARCHIVE_DAYS:
            arch_orig.append(item)
        else:
            keep_orig.append(item)
    dash["originationInbox"] = keep_orig
    if arch_orig:
        dash.setdefault("originationArchive", []).extend(arch_orig)
    report.update(
        originationInbox_archived=len(arch_orig),
        originationInbox_remaining=len(keep_orig),
    )

    # ── dealIntel → dealIntelArchive ───────────────────────────────────────────
    di_before = len(dash.get("dealIntel", []))
    keep_di, arch_di = [], []
    for item in dash.get("dealIntel", []):
        age = _item_age_days(item, today)
        if age is not None and age >= AGE_ARCHIVE_DAYS:
            arch_di.append(item)
        else:
            keep_di.append(item)
    dash["dealIntel"] = keep_di
    if arch_di:
        dash.setdefault("dealIntelArchive", []).extend(arch_di)
    report.update(
        dealIntel_archived=len(arch_di),
        dealIntel_remaining=len(keep_di),
    )

    # ── themes → themesArchive ─────────────────────────────────────────────────
    th_before = len(dash.get("themes", []))
    keep_th, arch_th = [], []
    for item in dash.get("themes", []):
        age = _item_age_days(item, today)
        if age is not None and age >= AGE_ARCHIVE_DAYS:
            arch_th.append(item)
        else:
            keep_th.append(item)
    dash["themes"] = keep_th
    if arch_th:
        dash.setdefault("themesArchive", []).extend(arch_th)
    report.update(
        themes_archived=len(arch_th),
        themes_remaining=len(keep_th),
    )

    # ── routingExceptions → prune old ones ────────────────────────────────────
    rex_before = len(dash.get("routingExceptions", []))
    dash["routingExceptions"] = [
        r for r in dash.get("routingExceptions", [])
        if not _past_grace(_exception_date(r), today, ROUTING_EXCEPTIONS_PRUNE_DAYS)
    ]
    report.update(
        routingExceptions_pruned=rex_before - len(dash["routingExceptions"]),
        routingExceptions_remaining=len(dash["routingExceptions"]),
    )

    return report


# ── Build-backlog sweep ───────────────────────────────────────────────────────

def sweep_build_backlog(today: datetime | None = None) -> dict:
    """Remove build-backlog items that have completedAt set and are past grace."""
    today = today or datetime.now()
    try:
        data = json.loads(BUILD_BACKLOG_PATH.read_text())
    except FileNotFoundError:
        return {"build_backlog_removed": 0, "build_backlog_remaining": 0}

    before = len(data.get("items", []))
    data["items"] = [
        item for item in data.get("items", [])
        if not (
            item.get("completedAt") and
            _past_grace(item["completedAt"], today, BUILD_BACKLOG_GRACE_DAYS)
        )
    ]
    removed = before - len(data["items"])
    if removed:
        data["updated_at"] = today.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        BUILD_BACKLOG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    return {"build_backlog_removed": removed, "build_backlog_remaining": len(data["items"])}


def mark_build_item_complete(item_id: str, today: datetime | None = None) -> dict:
    """Set completedAt on a build-backlog item by id. Returns the updated item or error."""
    today = today or datetime.now()
    try:
        data = json.loads(BUILD_BACKLOG_PATH.read_text())
    except FileNotFoundError:
        return {"error": "build-backlog.json not found"}

    matched = None
    for item in data.get("items", []):
        if item.get("id") == item_id:
            item["completedAt"] = today.strftime("%Y-%m-%d")
            matched = item
            break

    if not matched:
        ids = [i.get("id") for i in data.get("items", [])]
        return {"error": f"ID '{item_id}' not found. Available: {ids}"}

    data["updated_at"] = today.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    BUILD_BACKLOG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"marked_complete": matched}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be swept without writing.")
    ap.add_argument("--complete", metavar="BACKLOG_ID",
                    help="Mark a build-backlog item complete by its id.")
    ap.add_argument("--list-backlog", action="store_true",
                    help="Print all build-backlog items and their status.")
    args = ap.parse_args()

    if args.complete:
        result = mark_build_item_complete(args.complete)
        if "error" in result:
            print(f"ERROR: {result['error']}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.list_backlog:
        try:
            data = json.loads(BUILD_BACKLOG_PATH.read_text())
        except FileNotFoundError:
            print("build-backlog.json not found", file=sys.stderr)
            return 1
        for item in data.get("items", []):
            status = f"✅ DONE {item['completedAt']}" if item.get("completedAt") else "⏳ OPEN"
            print(f"[{status}] {item['id']:30s}  {item.get('name', '')}")
        return 0

    dash = json.loads(DASHBOARD_DATA_PATH.read_text())
    report = sweep(dash)
    bl_report = sweep_build_backlog()
    report.update(bl_report)

    any_change = any([
        report.get("followUps_removed", 0),
        report.get("awaitingExternal_removed", 0),
        report.get("originationInbox_archived", 0),
        report.get("dealIntel_archived", 0),
        report.get("themes_archived", 0),
        report.get("routingExceptions_pruned", 0),
    ])

    if not args.dry_run and any_change:
        DASHBOARD_DATA_PATH.write_text(json.dumps(dash, indent=2, ensure_ascii=False))

    sys.stderr.write(json.dumps({**report, "dry_run": args.dry_run}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
