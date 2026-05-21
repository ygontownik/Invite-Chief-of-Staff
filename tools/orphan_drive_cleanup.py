#!/usr/bin/env python3
"""
orphan_drive_cleanup.py — Trash orphan Drive folders/files left by failed /new-deal runs.

The new-deal script stashes pre-existing Drive IDs in `_orphan_ids_pending_cleanup`
when overwriting a same-deal_id stub entry (see the new-deal writer module).
This tool walks deal-system-data.json, finds those entries, and trashes the
listed IDs via Drive API.

Usage:
    python3 orphan_drive_cleanup.py            # dry-run: list IDs + names, no changes
    python3 orphan_drive_cleanup.py --apply    # trash via Drive API, remove field

Log: ~/dashboards/logs/orphan-cleanup.log
"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEAL_SYSTEM_DATA = REPO / "deal-system-data.json"
LOG_PATH = Path.home() / "dashboards" / "logs" / "orphan-cleanup.log"

ORPHAN_FIELD = "_orphan_ids_pending_cleanup"


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def load_deals() -> dict:
    return json.loads(DEAL_SYSTEM_DATA.read_text())


def save_deals(data: dict) -> None:
    DEAL_SYSTEM_DATA.write_text(json.dumps(data, indent=2))


def collect_orphans(data: dict) -> list[tuple[int, str, dict]]:
    """Returns [(index, deal_id, orphan_dict), ...]."""
    out = []
    for i, d in enumerate(data.get("deals", [])):
        orphans = d.get(ORPHAN_FIELD)
        if orphans:
            out.append((i, d.get("deal_id", "<unknown>"), orphans))
    return out


def get_name(drive, file_id: str) -> str:
    try:
        meta = drive.files().get(
            fileId=file_id, fields="id,name,trashed,mimeType"
        ).execute()
        trashed = " (already trashed)" if meta.get("trashed") else ""
        return f"{meta.get('name', '?')}{trashed}"
    except Exception as e:
        return f"<error: {type(e).__name__}: {str(e)[:80]}>"


def trash(drive, file_id: str) -> bool:
    try:
        drive.files().update(fileId=file_id, body={"trashed": True}).execute()
        return True
    except Exception as e:
        log(f"  ✗ failed to trash {file_id}: {type(e).__name__}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Trash the orphan IDs and remove the field. Default is dry-run.")
    args = ap.parse_args()

    data = load_deals()
    orphans = collect_orphans(data)

    if not orphans:
        print("No deals carry _orphan_ids_pending_cleanup. Nothing to do.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    log(f"=== {mode} — {len(orphans)} deal(s) with orphans ===")

    drive = None
    if args.apply or True:  # always resolve names for visibility
        # Import here so a dry-run with no creds still prints the IDs
        try:
            sys.path.insert(0, str(REPO))
            from tcip_new_deal import get_drive_service
            drive = get_drive_service()
        except Exception as e:
            log(f"  ⚠ could not init Drive service ({e}); name lookup disabled")

    any_changes = False
    for idx, deal_id, orphan_dict in orphans:
        log(f"\n[{deal_id}] {len(orphan_dict)} orphan ID(s):")
        for field, file_id in orphan_dict.items():
            name = get_name(drive, file_id) if drive else "<name lookup skipped>"
            log(f"  - {field}: {file_id}  →  {name}")

        if not args.apply:
            continue

        # Apply: trash each, then drop the field
        successes = []
        for field, file_id in orphan_dict.items():
            if trash(drive, file_id):
                log(f"  ✓ trashed {field}: {file_id}")
                successes.append(field)
            # On failure we keep the field so the next run retries.
        if len(successes) == len(orphan_dict):
            data["deals"][idx].pop(ORPHAN_FIELD, None)
            log(f"  ✓ removed {ORPHAN_FIELD} from {deal_id}")
            any_changes = True
        else:
            log(f"  ⚠ {deal_id}: {len(orphan_dict) - len(successes)} ID(s) failed; "
                f"field retained for retry")

    if args.apply and any_changes:
        save_deals(data)
        log(f"\n✓ deal-system-data.json updated")
    elif not args.apply:
        log(f"\nDry-run complete. Re-run with --apply to trash.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
