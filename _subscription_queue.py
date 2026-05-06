#!/usr/bin/env python3
"""
_subscription_queue.py — drain the subscription rate-limit queue.

WHY THIS EXISTS

`_model_router.call_claude(mode='subscription')` enqueues TIME_INSENSITIVE
calls into ``data-<tenant>/queue.jsonl`` whenever the 5-hour subscription
window is exhausted. This module is the cron-callable daemon that picks
those rows back up after the window resets and re-fires them via
``call_claude`` (which now succeeds). On persistent failure it advances
``queue_until`` by one hour for retry-with-backoff.

USAGE

    # Drain a single tenant's queue (uses $COS_TENANT_SLUG):
    python3 _subscription_queue.py

    # Specific tenant:
    python3 _subscription_queue.py --tenant=<slug>

    # Dry-run (show what would fire; no calls):
    python3 _subscription_queue.py --tenant=<slug> --dry-run

SCHEDULE

A LaunchAgent fires this every 30 minutes. The daemon is a no-op when
``queue.jsonl`` is empty or every row's ``queue_until`` is still in the
future, so the cron cost is negligible.

QUEUE ROW SCHEMA (written by `_model_router._enqueue_subscription_task`)

    {
      "ts":           "<ISO8601 UTC>",
      "task_type":    "<routine name>",
      "model":        "<model id>",
      "package":      "<briefing|capture|research|deals|...>",
      "queue_until":  <unix ts | ISO string | null>,
      "attempts":     <int — 1 on first enqueue>,
      "error_type":   "ProcessError|ClaudeSDKError",
      "error_msg":    "<str>",
      "system":       <str | list of blocks | null>,
      "messages":     [{"role":..., "content":...}, ...]
    }

DAEMON CONTRACT

For each row in queue.jsonl:
  - If queue_until is in the future, leave it.
  - Else, call ``call_claude(task_type, system, messages, mode='subscription')``.
      * Success -> drop the row.
      * Re-raise -> bump ``attempts`` and advance ``queue_until`` by
                    one hour (capped at 24 attempts; rows past that are
                    moved to ``queue.dead.jsonl`` for human review).
The whole file is rewritten atomically (tempfile + rename) so a crash
mid-drain doesn't lose surviving rows.

Stdlib only. Imports ``_model_router`` at runtime; tenants on api mode
who never enqueue still don't pay the import cost.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_PIPELINE_ROOT = Path.home() / "cos-pipeline"
_DEFAULT_TENANT = os.environ.get("COS_TENANT_SLUG", "default")
_BACKOFF_HOURS = 1
_MAX_ATTEMPTS = 24


# ─────────────────────────────────────────────────────────────────────
# Queue file paths.
# ─────────────────────────────────────────────────────────────────────

def _queue_path(tenant: str) -> Path:
    return _PIPELINE_ROOT / f"data-{tenant}" / "queue.jsonl"


def _dead_path(tenant: str) -> Path:
    return _PIPELINE_ROOT / f"data-{tenant}" / "queue.dead.jsonl"


# ─────────────────────────────────────────────────────────────────────
# Queue I/O.
# ─────────────────────────────────────────────────────────────────────

def _load_queue(tenant: str) -> list[dict]:
    path = _queue_path(tenant)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[queue] WARN: skipping malformed row: {e}\n")
    return rows


def _write_queue_atomic(tenant: str, rows: list[dict]) -> None:
    path = _queue_path(tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", delete=False, dir=str(path.parent), suffix=".jsonl",
    ) as tmp:
        for r in rows:
            tmp.write(json.dumps(r, default=str) + "\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _append_dead(tenant: str, row: dict) -> None:
    path = _dead_path(tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


# ─────────────────────────────────────────────────────────────────────
# Time helpers.
# ─────────────────────────────────────────────────────────────────────

def _to_datetime(val: Any) -> Optional[datetime]:
    """Coerce queue_until to a UTC datetime. Returns None for null/'unknown'."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(val, str):
        s = val.strip()
        if not s or s.lower() in ("null", "none", "unknown"):
            return None
        # Try ISO-8601.
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
        # Try unix timestamp embedded in str.
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except ValueError:
            return None
    return None


def _is_due(queue_until: Any, now: datetime) -> bool:
    dt = _to_datetime(queue_until)
    if dt is None:
        # No reset timestamp known — fire on every drain pass; the
        # subscription dispatch will re-queue with backoff if still
        # rate-limited.
        return True
    return dt <= now


def _bump_queue_until(now: datetime) -> str:
    return (now + timedelta(hours=_BACKOFF_HOURS)).isoformat()


# ─────────────────────────────────────────────────────────────────────
# Main drain loop.
# ─────────────────────────────────────────────────────────────────────

def drain(tenant: str, *, dry_run: bool = False) -> dict:
    """Drain the queue for `tenant`. Returns a stats dict."""
    rows = _load_queue(tenant)
    if not rows:
        return {"loaded": 0, "fired": 0, "succeeded": 0,
                "failed": 0, "skipped": 0, "dead": 0}

    now = datetime.now(timezone.utc)
    surviving: list[dict] = []
    stats = {"loaded": len(rows), "fired": 0, "succeeded": 0,
             "failed": 0, "skipped": 0, "dead": 0}

    # Lazy-import to avoid pulling _model_router into processes that
    # only run the queue daemon as a no-op.
    if not dry_run:
        import _model_router as mr   # noqa: F401  (imported for side: ensures install)

    for row in rows:
        if not _is_due(row.get("queue_until"), now):
            stats["skipped"] += 1
            surviving.append(row)
            continue

        task_type = row.get("task_type")
        if not task_type:
            sys.stderr.write(
                "[queue] WARN: row missing task_type; moving to dead.\n"
            )
            stats["dead"] += 1
            if not dry_run:
                _append_dead(tenant, row)
            continue

        if dry_run:
            print(f"[queue] would fire: {task_type} "
                  f"(attempts={row.get('attempts', 1)})")
            stats["fired"] += 1
            surviving.append(row)
            continue

        stats["fired"] += 1
        try:
            mr.call_claude(
                task_type=task_type,
                system=row.get("system"),
                messages=row.get("messages") or [],
                mode="subscription",
                tenant=tenant,
            )
            stats["succeeded"] += 1
            # Row drops out of `surviving` — call landed.
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            attempts = int(row.get("attempts", 1)) + 1
            row["attempts"] = attempts
            row["last_error_type"] = type(e).__name__
            row["last_error_msg"] = str(e)
            row["queue_until"] = _bump_queue_until(now)
            if attempts > _MAX_ATTEMPTS:
                stats["dead"] += 1
                _append_dead(tenant, row)
                # Don't keep in queue.
            else:
                surviving.append(row)

    if not dry_run:
        _write_queue_atomic(tenant, surviving)

    return stats


# ─────────────────────────────────────────────────────────────────────
# CLI entry.
# ─────────────────────────────────────────────────────────────────────

def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 2)[1])
    ap.add_argument("--tenant", default=_DEFAULT_TENANT)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would fire; no calls; queue not rewritten")
    ap.add_argument("--list", action="store_true",
                    help="dump every queued row and exit")
    args = ap.parse_args()

    if args.list:
        for r in _load_queue(args.tenant):
            print(json.dumps(r, default=str))
        return 0

    stats = drain(args.tenant, dry_run=args.dry_run)
    print(f"[queue] tenant={args.tenant} {stats}")
    # Exit 0 even on failed re-fires — queue daemon's job is to keep the
    # queue moving, not to surface call failures (the dispatch ledger does
    # that). Exit non-zero only on infra-level failure (handled below).
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[queue] FATAL: {type(e).__name__}: {e}\n")
        sys.exit(2)
