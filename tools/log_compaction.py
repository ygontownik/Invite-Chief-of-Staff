#!/usr/bin/env python3
"""
log_compaction.py — Archive folded log.json entries older than N days.

For each registered deal:
  1. Read ~/dashboards/data/deals/<deal_id>/log.json
  2. Find entries with date < (today - DEFAULT_DAYS) days
  3. Move them to log.archive.json (append, dedup by id)
  4. Rewrite log.json with the remaining (recent) entries

Preserves the {deal_id, updated_at, entries: [...]} shape.

Run via /deal-sync at end of cycle, or manually:
    python3 log_compaction.py                   # all registered deals
    python3 log_compaction.py --deal <deal_id>  # one deal
    python3 log_compaction.py --days 14         # custom threshold
    python3 log_compaction.py --dry-run         # report only

Multi-tenant safe: data dir resolved via $COS_DATA_DIR or default.
"""

from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Coordination layer
sys.path.insert(0, str(Path(__file__).parent))
from coordination import lock, mark_run  # noqa: E402

HOLDER = "log_compaction.py"
DEFAULT_DAYS = 30

DATA_DIR = Path(os.environ.get(
    "COS_DATA_DIR",
    str(Path.home() / "dashboards/data"),
))
DEALS_DIR = DATA_DIR / "deals"


def parse_date(s: str) -> datetime | None:
    if not s:
        return None
    # Try ISO date first (2026-05-19), then full ISO timestamp.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(s[:len(fmt)], fmt)
        except ValueError:
            continue
        # Always return timezone-aware (UTC) so comparisons work.
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compact_one(deal_id: str, days: int, dry_run: bool) -> dict:
    """Compact one deal's log.json. Returns stats dict."""
    log_path = DEALS_DIR / deal_id / "log.json"
    archive_path = DEALS_DIR / deal_id / "log.archive.json"
    if not log_path.exists():
        return {"deal_id": deal_id, "skipped": "log.json not found"}

    with lock(f"log.json:{deal_id}", HOLDER, ttl_seconds=60, timeout_seconds=30):
        raw = json.loads(log_path.read_text())
        # Handle both shapes: {entries: [...]} OR [...] at root.
        # Normalize to the dict shape on write (matches /deal-sync expectations).
        if isinstance(raw, list):
            entries = raw
            was_list = True
        elif isinstance(raw, dict):
            entries = raw.get("entries", [])
            was_list = False
        else:
            return {"deal_id": deal_id, "skipped": f"unexpected log shape: {type(raw).__name__}"}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        keep, archive = [], []
        for e in entries:
            if not isinstance(e, dict):
                keep.append(e)
                continue
            d = parse_date(e.get("date") or e.get("timestamp"))
            if d and d < cutoff:
                archive.append(e)
            else:
                keep.append(e)

        if not archive:
            return {"deal_id": deal_id, "archived": 0, "kept": len(keep),
                    "log_size": log_path.stat().st_size, "shape": "list" if was_list else "dict"}

        if dry_run:
            return {"deal_id": deal_id, "archived": len(archive), "kept": len(keep),
                    "log_size": log_path.stat().st_size, "dry_run": True,
                    "shape": "list" if was_list else "dict"}

        # Merge into archive (dedup by id)
        existing_archive_entries = []
        if archive_path.exists():
            try:
                arc = json.loads(archive_path.read_text())
                existing_archive_entries = (arc.get("entries", []) if isinstance(arc, dict) else arc) or []
            except json.JSONDecodeError:
                pass
        seen_ids = {e.get("id") for e in existing_archive_entries if isinstance(e, dict) and e.get("id")}
        merged_archive = existing_archive_entries + [e for e in archive if not (isinstance(e, dict) and e.get("id") in seen_ids)]

        archive_doc = {
            "deal_id": deal_id,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entries": merged_archive,
        }
        # Atomic writes
        tmp_arc = archive_path.with_suffix(".tmp")
        tmp_arc.write_text(json.dumps(archive_doc, indent=2))
        tmp_arc.replace(archive_path)

        # Always normalize to the dict shape (deal_id + updated_at + entries).
        new_log_doc = {
            "deal_id": deal_id,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entries": keep,
        }
        tmp_log = log_path.with_suffix(".tmp")
        tmp_log.write_text(json.dumps(new_log_doc, indent=2))
        tmp_log.replace(log_path)

    return {"deal_id": deal_id, "archived": len(archive), "kept": len(keep),
            "log_size": log_path.stat().st_size, "archive_size": archive_path.stat().st_size}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--deal", help="Compact only this deal_id (default: all)")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Archive entries older than this many days (default: {DEFAULT_DAYS})")
    p.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = p.parse_args(argv)

    if not DEALS_DIR.exists():
        sys.exit(f"ERROR: deals dir not found: {DEALS_DIR}")

    if args.deal:
        deals = [args.deal]
    else:
        deals = sorted(d.name for d in DEALS_DIR.iterdir() if d.is_dir() and not d.name.startswith("_"))

    total_archived = 0
    for deal_id in deals:
        stats = compact_one(deal_id, args.days, args.dry_run)
        if "skipped" in stats:
            print(f"  {deal_id:15} SKIP — {stats['skipped']}")
            continue
        marker = "[DRY] " if stats.get("dry_run") else ""
        print(f"  {deal_id:15} {marker}archived={stats['archived']:4d} "
              f"kept={stats['kept']:4d}  log={stats['log_size']:>7} bytes")
        total_archived += stats["archived"]

    if not args.dry_run and total_archived > 0:
        mark_run(HOLDER)
    print(f"\n{'Dry-run total' if args.dry_run else 'Total'} archived: {total_archived} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
